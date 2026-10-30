"""Yascheduler client."""
# region MODULE_CONTRACT
# PURPOSE: Provide the public Python/CLI client (Yascheduler) for submitting and querying tasks, bridging sync callers to the async use-case layer via to_sync adapter.
# SCOPE: Yascheduler sync/async client class, to_sync adapter, _task_to_dict projection.
# KEYWORDS: client, facade, task, submit, query, sync, async
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

from yascheduler.application import legacy_input_value, query_tasks
from yascheduler.domain import (
    AnyTask,
    Node,
    NodeId,
    TaskId,
    TaskInputEntry,
    TaskInputFile,
    TaskStatus,
    allocated_node_id_of,
    error_of,
    remote_folder_of,
)
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.shared import ParamSpec

from .di import CLIDeps, make_cli_deps
from .paths import CONFIG_FILE

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, Sequence
    from pathlib import PurePath

    from yascheduler.domain import TaskInputSnapshot
    from yascheduler.entrypoints.config import Config
__all__ = ["Yascheduler"]

ReturnT_co = TypeVar("ReturnT_co", covariant=True)
ParamT = ParamSpec("ParamT")


# region FUNC_to_sync
# PURPOSE: Wrap an async function so it can be called synchronously, detecting a running event loop and offloading to a worker thread when necessary.
# RATIONALE:
#   Q: Why does to_sync spin up a worker thread when an event loop is already running instead of using asyncio.run_until_complete?
#   A: asyncio.run_until_complete cannot be called on a running loop; the only ways to block on a coroutine from inside a running loop are (1) await it (impossible — the caller is sync) or (2) move the coroutine to a different loop in a different thread and block on its result. The worker-thread path is option (2).
def to_sync(
    func: Callable[ParamT, Coroutine[Any, Any, ReturnT_co]],
) -> Callable[ParamT, ReturnT_co]:
    """Wrap async function and run it sync in thread."""

    @wraps(func)
    def outer(*args: ParamT.args, **kwargs: ParamT.kwargs) -> ReturnT_co:
        """Execute the async method synchronously in sync and async runtime."""
        coro = func(*args, **kwargs)
        try:
            asyncio.get_running_loop()  # Triggers RuntimeError if no running event loop

            # Create a separate thread so we can block before returning
            with ThreadPoolExecutor(1) as pool:
                return pool.submit(lambda: asyncio.run(coro)).result()
        except RuntimeError:
            return asyncio.run(coro)

    return outer


# endregion FUNC_to_sync


# region FUNC__input_entry_to_public
# PURPOSE: Project one task input entry into its documented JSON-safe public form — files as padded base64 without line breaks, directories content-free — so consumers can rebuild exact input bytes from query results.
# INVARIANTS: Standard base64 with padding (base64.b64encode, no newlines); entry order is the caller's (path-sorted snapshot).
def _input_entry_to_public(entry: TaskInputEntry) -> dict[str, Any]:
    """Return the public query representation of one input entry."""
    if isinstance(entry, TaskInputFile):
        return {
            "path": entry.path,
            "kind": entry.kind.value,
            "content_base64": base64.b64encode(entry.content).decode("ascii"),
        }
    return {"path": entry.path, "kind": entry.kind.value}


# endregion FUNC__input_entry_to_public


# region FUNC__task_to_dict
# PURPOSE: Project a Task plus its optional allocated Node and its input snapshot into a flat JSON-serializable mapping so the public client API returns plain dicts to sync callers that cannot see domain value objects like TaskId / NodeId; inputs project top-level as `input_entries` while metadata keeps legacy filename/value file keys for consumer compatibility.
# INVARIANTS: Exactly the top-level keys task_id/label/status/metadata/input_entries/node; the snapshot comes from the query's explicit batched read, never from the task record; entries ordered by task-relative path; file entries stay visible in metadata under their recorded legacy encoding (UTF-8 text, except `fort.9`, which stays base64 as submitted), derived from the immutable snapshot — never a second stored copy; pre-migration `extra` copies of input keys (the normalizer and the forward migration strip them) keep precedence over derived values while they exist; directories never appear in metadata; typed fields are never overwritten by input paths.
def _task_to_dict(
    t: AnyTask,
    nodes_by_id: dict[NodeId, Node],
    snapshot: TaskInputSnapshot = (),
) -> Mapping[str, Any]:
    allocated_node_id = allocated_node_id_of(t)
    node = nodes_by_id.get(allocated_node_id) if allocated_node_id else None
    metadata: dict[str, Any] = {"engine": t.engine}
    remote_folder = remote_folder_of(t)
    if remote_folder is not None:
        metadata["remote_folder"] = remote_folder
    local_folder = t.local_folder
    if local_folder is not None:
        metadata["local_folder"] = local_folder
    if t.webhook_url is not None:
        metadata["webhook_url"] = t.webhook_url
    if t.webhook_custom_params:
        metadata["webhook_custom_params"] = t.webhook_custom_params
    error = error_of(t)
    if error is not None:
        metadata["error"] = error
    for k, v in t.extra.items():
        if k not in metadata:
            metadata[k] = v
    # region BLOCK_derive_legacy_input_metadata
    # The snapshot's files are the task's inputs by contract; their legacy
    # metadata view is derived from the immutable entries, so queries keep the
    # legacy shape without a second stored copy. Keys already present
    # (typed fields or a transitional extra copy) keep their value.
    for entry in snapshot:
        if not isinstance(entry, TaskInputFile):
            continue
        if entry.path in metadata:
            continue
        metadata[entry.path] = legacy_input_value(entry.path, entry.content)
    # endregion BLOCK_derive_legacy_input_metadata
    return {
        "task_id": t.task_id.value,
        "label": t.label,
        "status": t.status,
        "metadata": metadata,
        "input_entries": [
            _input_entry_to_public(entry)
            for entry in sorted(snapshot, key=lambda entry: entry.path)
        ],
        "node": (
            {
                "hostname": node.hostname,
                "port": node.port,
                "username": node.username,
                "cloud": node.cloud,
                "jump_host": node.jump_host,
                "jump_port": node.jump_port,
                "jump_username": node.jump_username,
                "external_id": node.external_id,
                "status": node.status.name,
                "created_at": node.created_at.isoformat(),
                "updated_at": node.updated_at.isoformat(),
            }
            if node is not None
            else None
        ),
    }


# endregion FUNC__task_to_dict


# region CLASS_Yascheduler
# PURPOSE: Give external consumers a stable sync+async Python client that submits and queries tasks while preserving a public int-typed contract over a TaskId-typed domain.
class Yascheduler:
    """Yascheduler client."""

    STATUS_TO_DO = TaskStatus.TO_DO.value
    STATUS_RUNNING = TaskStatus.RUNNING.value
    STATUS_DONE = TaskStatus.DONE.value

    config: Config

    # region METHOD___init__
    # PURPOSE: Bind the facade to a parsed Config and (optionally) a deps factory seam so the rest of the facade can lazily obtain a fresh CLIDeps per query without re-parsing config and without a test-hostile module-level singleton.
    def __init__(
        self,
        config_path: PurePath | str = CONFIG_FILE,
        *,
        deps_factory: Callable[[Config], CLIDeps] | None = None,
        **_: dict[str, Any],
    ) -> None:
        """Initialize the Yascheduler client with config path and optional DI factory."""
        self.config = parse_config(config_path)
        self._deps_factory = deps_factory or make_cli_deps

    # endregion METHOD___init__

    # region METHOD_queue_submit_task_async
    # PURPOSE: Offload each task submission through a fresh DI container so the caller does not need to open a Unit of Work or import the use case; forward optional output selections and unwrap the TaskId result at the facade boundary so the public contract stays int.
    async def queue_submit_task_async(
        self,
        label: str,
        metadata: Mapping[str, Any],
        engine_name: str,
        *,
        _webhook_onsubmit: bool = False,
        input_entries: Sequence[TaskInputEntry] | None = None,
        output_selections: list[str] | None = None,
    ) -> int:
        """Submit new task, optionally with explicit inputs and output selections."""
        deps = self._deps_factory(self.config)
        # deps.submit (-> TaskId) → extract .value so the public contract stays int.
        return (
            await deps.submit(
                label,
                dict(metadata),
                engine_name,
                input_entries=input_entries,
                output_selections=output_selections,
            )
        ).value

    # endregion METHOD_queue_submit_task_async

    # region METHOD_queue_submit_task
    # PURPOSE: Expose the async submit path to sync callers (AiiDA plugin, REPL) by bridging through to_sync so the facade stays sync on its public surface while the use-case layer stays async.
    def queue_submit_task(
        self,
        label: str,
        metadata: Mapping[str, Any],
        engine_name: str,
        *,
        webhook_onsubmit: bool = False,
        input_entries: Sequence[TaskInputEntry] | None = None,
        output_selections: list[str] | None = None,
    ) -> int:
        """Submit new task, optionally with explicit inputs and output selections."""
        fn = to_sync(self.queue_submit_task_async)
        return fn(
            label,
            metadata,
            engine_name,
            _webhook_onsubmit=webhook_onsubmit,
            input_entries=input_entries,
            output_selections=output_selections,
        )

    # endregion METHOD_queue_submit_task

    # region METHOD_queue_get_tasks_async
    # PURPOSE: Resolve a list of int job ids and int status values into typed TaskId / TaskStatus sequences, run them through the deps-seam query use case, and project each result via _task_to_dict so the public surface returns plain mappings and the marshalling boundary stays on the facade.
    # REQUIRES: status values are valid TaskStatus integer values — TaskStatus(x) is invoked per element and raises ValueError on unknown values.
    async def queue_get_tasks_async(
        self,
        jobs: Sequence[int] | None = None,
        status: Sequence[int] | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Get tasks by ids or statuses."""
        # raise ValueError if unknown task status
        statuses: list[TaskStatus] | None = (
            [TaskStatus(x) for x in status] if status else None
        )
        # The facade is the sole int/TaskId boundary: wrap job ints → TaskId
        # before crossing into the use case (public jobs: list[int] preserved).
        job_ids: list[TaskId] | None = [TaskId(i) for i in jobs] if jobs else None
        deps = self._deps_factory(self.config)
        tasks, nodes_by_id, snapshots = await query_tasks(
            job_ids, statuses, deps.uow_factory
        )
        return [
            _task_to_dict(t, nodes_by_id, snapshots.get(t.task_id, ())) for t in tasks
        ]

    # endregion METHOD_queue_get_tasks_async

    # region METHOD_queue_get_tasks
    # PURPOSE: Bridge the async query path to sync callers via to_sync so the public surface stays sync.
    def queue_get_tasks(
        self,
        jobs: Sequence[int] | None = None,
        status: Sequence[int] | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Get tasks by ids or statuses."""
        return to_sync(self.queue_get_tasks_async)(jobs, status)

    # endregion METHOD_queue_get_tasks

    # region METHOD_queue_get_task_async
    # PURPOSE: Hand callers a one-task view (or None) on top of the list query so they do not have to unwrap [0] or handle empty lists themselves.
    async def queue_get_task_async(self, task_id: int) -> Mapping[str, Any] | None:
        """Get task by id."""
        for task_dict in await self.queue_get_tasks_async(jobs=[task_id]):
            return task_dict
        return None

    # endregion METHOD_queue_get_task_async

    # region METHOD_queue_get_task
    # PURPOSE: Bridge the single-task async path to sync callers via to_sync.
    def queue_get_task(self, task_id: int) -> Mapping[str, Any] | None:
        """Get task by id."""
        for task_dict in self.queue_get_tasks(jobs=[task_id]):
            return task_dict
        return None

    # endregion METHOD_queue_get_task


# endregion CLASS_Yascheduler
