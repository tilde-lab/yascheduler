# region MODULE_CONTRACT
# PURPOSE: Shared scaffolding for e2e tests that drive the real daemon against real PostgreSQL and SSH: single-node setup/submission, UoW status polling, domain-event capture, out-of-band remote path probes, daemon/node teardown, and a scripted output-retrieval double.
# SCOPE: setup_node_and_submit, wait_status, wait_until, task_or_fail, remote_path_exists, wait_remote_path_absent, capture_events, terminal_events_for, stop_orchestrator, cleanup_node, and ScriptedDownloadOutputs (per-call transient/permanent outcomes, passthrough to the real downloader, per-pass hooks, remote_dir recording).
# INVARIANTS: Polling helpers return their last observation and never fail by themselves; each caller keeps its own assertions. Remote probes open their own SSH sessions so cleanup is observed independently of the daemon. ScriptedDownloadOutputs delegates to the real implementation once its script is exhausted.
# KEYWORDS: e2e helpers, node setup, status polling, event capture, remote path, orchestrator teardown, download_outputs script
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import TYPE_CHECKING, Any

from asyncssh.sftp import SFTPNoSuchFile

from yascheduler.application import MessageBus
from yascheduler.domain import TaskCompleted, TaskFailed
from yascheduler.domain.model import NewNode, Node, NodeId
from yascheduler.entrypoints.di import make_cli_deps
from yascheduler.infra.ssh.repository import SSHMachineRepository

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    import pytest

    from yascheduler.application.orchestrator import Orchestrator
    from yascheduler.domain import DomainEvent, MachineSession
    from yascheduler.domain.model import Task, TaskId
    from yascheduler.domain.model import TaskStatus as DomainTaskStatus
    from yascheduler.entrypoints import Config
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

    # One pass's (transient, permanent) download errors.
    DownloadResult = tuple[
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]

__all__ = [
    "ScriptedDownloadOutputs",
    "capture_events",
    "cleanup_node",
    "remote_path_exists",
    "setup_node_and_submit",
    "stop_orchestrator",
    "task_or_fail",
    "terminal_events_for",
    "wait_remote_path_absent",
    "wait_status",
    "wait_until",
]


# region FUNC_setup_node_and_submit
# PURPOSE: Insert an SSH node row, deploy every configured engine, and submit one task so a test exercises the real allocation/upload/spawn/consume path.
async def setup_node_and_submit(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    *,
    engine: str,
    inputs: dict[str, object],
    label: str,
) -> TaskId:
    """Connect, deploy engines, add a node row, submit a task. Returns task_id."""
    async with uow_factory() as uow:
        db_node = await uow.nodes.insert(
            NewNode(
                hostname=ssh_container["host"],
                username=ssh_container["username"],
                port=ssh_container["port"],
                enabled=True,
                ncpus=None,
            ),
        )
        await uow.commit()

    repository = SSHMachineRepository()
    session = await repository.connect(
        node=db_node,
        client_keys=[ssh_container["key_path"]],
        data_dir=e2e_config.remote.data_dir,
        engines_dir=e2e_config.remote.engines_dir,
        tasks_dir=e2e_config.remote.tasks_dir,
    )
    await session.setup_node(e2e_config.engines)
    await repository.disconnect(db_node.node_id)

    deps = make_cli_deps(e2e_config)
    task_id = await deps.submit(label, inputs, engine)
    assert task_id.value > 0
    return task_id


# endregion FUNC_setup_node_and_submit


# region FUNC_wait_status
# PURPOSE: Poll a fresh UoW until the task reaches the target status so the test never inspects a stale snapshot.
async def wait_status(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: TaskId,
    target: DomainTaskStatus,
    timeout_s: float = 45.0,
) -> Task | None:
    """Poll until task reaches target status or timeout. Returns the task."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    task = None
    while asyncio.get_running_loop().time() < deadline:
        async with uow_factory() as uow:
            task = await uow.tasks.get(task_id)
        if task and task.status == target:
            return task
        await asyncio.sleep(0.25)
    return task


# endregion FUNC_wait_status


# region FUNC_wait_until
# PURPOSE: Await a same-event-loop predicate without sleeping on wall-clock assumptions.
async def wait_until(
    predicate: Callable[[], bool],
    timeout_s: float = 15.0,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


# endregion FUNC_wait_until


# region FUNC_task_or_fail
# PURPOSE: Load the task or fail the test instead of dereferencing a missing row.
async def task_or_fail(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: TaskId,
) -> Task:
    async with uow_factory() as uow:
        task = await uow.tasks.get(task_id)
    assert task is not None
    return task


# endregion FUNC_task_or_fail


# region FUNC_remote_path_exists
# PURPOSE: Read one remote path through a fresh SSH session so tests can assert the task directory's lifecycle independently of the daemon's sessions.
async def remote_path_exists(
    ssh_container: dict[str, Any],
    remote_path: str,
) -> bool:
    repository = SSHMachineRepository()
    node = Node(
        node_id=NodeId(9999),
        hostname=ssh_container["host"],
        ncpus=None,
        enabled=True,
        cloud=None,
        username=ssh_container["username"],
        port=ssh_container["port"],
    )
    session = await repository.connect(
        node=node,
        client_keys=[ssh_container["key_path"]],
    )
    try:
        async with session.open_sftp() as sftp:
            try:
                await sftp.stat(str(session.path(remote_path)))
            except SFTPNoSuchFile:
                return False
            return True
    finally:
        await repository.disconnect(session.machine.node_id)


# endregion FUNC_remote_path_exists


# region FUNC_wait_remote_path_absent
# PURPOSE: Poll until the remote task directory is gone so cleanup is observed as an asynchronous consequence of finalisation rather than a race.
async def wait_remote_path_absent(
    ssh_container: dict[str, Any],
    remote_path: str,
    timeout_s: float = 15.0,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if not await remote_path_exists(ssh_container, remote_path):
            return True
        await asyncio.sleep(0.05)
    return not await remote_path_exists(ssh_container, remote_path)


# endregion FUNC_wait_remote_path_absent


# region FUNC_capture_events
# PURPOSE: Record domain events dispatched by every UoW commit in this test so terminal transitions and cleanup ordering can be checked.
def capture_events(monkeypatch: pytest.MonkeyPatch) -> list[DomainEvent]:
    """Record domain events dispatched by every UoW commit in this test."""
    events: list[DomainEvent] = []
    real_dispatch = MessageBus.dispatch

    async def capture_dispatch(
        bus: MessageBus,
        domain_events: Sequence[DomainEvent],
    ) -> None:
        events.extend(domain_events)
        await real_dispatch(bus, domain_events)

    monkeypatch.setattr(MessageBus, "dispatch", capture_dispatch)
    return events


# endregion FUNC_capture_events


# region FUNC_terminal_events_for
# PURPOSE: Select the task's terminal events so tests can count them exactly.
def terminal_events_for(
    events: Sequence[DomainEvent],
    task_id: TaskId,
) -> list[DomainEvent]:
    return [
        event
        for event in events
        if event.task_id == task_id and isinstance(event, (TaskCompleted, TaskFailed))
    ]


# endregion FUNC_terminal_events_for


# region FUNC_stop_orchestrator
# PURPOSE: Stop the real daemon and collect its task without leaking a hanging or cancelled task on failure.
async def stop_orchestrator(
    orchestrator: Orchestrator,
    orch_task: asyncio.Task[None],
) -> None:
    await orchestrator.stop()
    try:
        await asyncio.wait_for(orch_task, timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        orch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await orch_task


# endregion FUNC_stop_orchestrator


# region FUNC_cleanup_node
# PURPOSE: Remove the test node row and abandon any RUNNING task bound to it so a failed test cannot poison the next one.
async def cleanup_node(
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
) -> None:
    async with uow_factory() as uow:
        all_nodes = await uow.nodes.list_all()
        matching = [n for n in all_nodes if n.hostname == ssh_container["host"]]
        if matching:
            node_id = matching[0].node_id
            # Abandon any RUNNING tasks on this node before removing it.
            # task_status_field_invariants CHECK forbids RUNNING with NULL
            # allocated_node_id, so the FK ON DELETE SET NULL would violate it.
            running = await uow.tasks.list_running()
            for running_task in running:
                if running_task.state.allocated_node_id == node_id:
                    abandoned = running_task.abandon(error="test cleanup")
                    await uow.tasks.save(abandoned)
            await uow.nodes.remove(node_id)
        await uow.commit()


# endregion FUNC_cleanup_node


# region CLASS_ScriptedDownloadOutputs
# PURPOSE: Replace ``Downloader.download_outputs`` with a per-call script so a test can force transient/permanent outcomes, gate a pass, and still let unscripted passes reach the real implementation.
class ScriptedDownloadOutputs:
    """Scripted stand-in for ``Downloader.download_outputs``.

    The 1-based call ``n`` runs ``hooks[n]`` (sync or async) and then returns
    ``script[n - 1]``. Once the script is exhausted, calls are delegated to
    ``real`` with ``extra_selections`` appended, unless ``repeat_last`` keeps
    replaying the final outcome. Every call's ``remote_dir`` is appended to
    ``remote_dirs`` and counted in ``calls``.
    """

    def __init__(
        self,
        real: Callable[..., Awaitable[DownloadResult]],
        *,
        script: Sequence[DownloadResult] = (),
        hooks: Mapping[int, Callable[[], Any]] | None = None,
        extra_selections: Sequence[str] = (),
        repeat_last: bool = False,
    ) -> None:
        self.calls = 0
        self.remote_dirs: list[str] = []
        self._real = real
        self._script = script
        self._hooks = hooks or {}
        self._extra_selections = extra_selections
        self._repeat_last = repeat_last

    async def __call__(
        self,
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        selections: Sequence[str],
        task_id: TaskId | None = None,
    ) -> DownloadResult:
        self.calls += 1
        self.remote_dirs.append(remote_dir)
        hook = self._hooks.get(self.calls)
        if hook is not None:
            pending = hook()
            if inspect.isawaitable(pending):
                await pending
        if self.calls <= len(self._script):
            return self._script[self.calls - 1]
        if self._repeat_last and self._script:
            return self._script[-1]
        return await self._real(
            session=session,
            remote_dir=remote_dir,
            local_dir=local_dir,
            selections=[*selections, *self._extra_selections],
            task_id=task_id,
        )


# endregion CLASS_ScriptedDownloadOutputs
