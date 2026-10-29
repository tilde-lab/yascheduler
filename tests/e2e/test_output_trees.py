# region MODULE_CONTRACT
# PURPOSE: Prove through the real PostgreSQL + SSH daemon that recursive output retrieval preserves a tree-producing engine's task-relative layout in the local result folder, cleans the remote tree only after the terminal transition, and keeps the defer/finalise lifecycle for tree selections.
# SCOPE: Tree engine e2e — a declared directory plus an overlapping glob and an explicit root file in one clean pass; a mixed transient/permanent pass that defers; a later clean pass that finalises; cleanup-ordering evidence captured inside cleanup_remote; exact local layout/bytes; and per-entry resolution logs.
# INVARIANTS: Expected bytes and relative paths are independent literals in this module. Observations recorded inside daemon hooks are asserted outside them, because consume_task swallows cleanup exceptions and could otherwise hide a failed assertion. No ABACUS binary, parser change, or cloud account is involved.
# KEYWORDS: e2e, output tree, recursive retrieval, selections, glob overlap, cleanup ordering, defer, finalise
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from asyncssh.sftp import SFTPFailure, SFTPNoSuchFile

from tests.log_assertions import extra_fields
from yascheduler.application import MessageBus
from yascheduler.domain import (
    DomainEvent,
    TaskCompleted,
    TaskFailed,
    allocated_node_id_of,
)
from yascheduler.domain.model import (
    NewNode,
    Node,
    NodeId,
    Task,
    TaskId,
)
from yascheduler.domain.model import TaskStatus as DomainTaskStatus
from yascheduler.entrypoints.di import make_cli_deps, make_daemon
from yascheduler.infra.ssh.repository import SSHMachineRepository
from yascheduler.infra.ssh.session import SSHMachineSession

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from yascheduler.application.orchestrator import Orchestrator
    from yascheduler.domain import MachineSession
    from yascheduler.entrypoints import Config
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

__all__ = [
    "test_output_tree_lands_nested_layout_and_cleans_after_finalisation",
    "test_output_tree_mixed_errors_defer_then_clean_pass_finalises",
]

_ENGINE = "test_tree"
_INPUT_NAME = "1.input"
_INPUT_TEXT = "hello tree e2e"

# Independent expected outputs. The engine fixture writes these exact bytes;
# every assertion compares against these literals, never against the downloader.
_NESTED_RESULT = b"nested-result-tree\n"
_ROOT_RESULT = b"root-result-tree\n"
_RESULT_TREE: dict[str, bytes | None] = {
    "OUT.aiida": None,
    "OUT.aiida/nested": None,
    "OUT.aiida/nested/result.dat": _NESTED_RESULT,
    "root.dat": _ROOT_RESULT,
}
_MISSING_SELECTION = "missing.out"


# region FUNC__setup_node_and_submit
# PURPOSE: Insert an SSH node, deploy every configured engine, and submit one task for the tree engine so the test exercises the real allocation/upload/spawn/consume path.
async def _setup_node_and_submit(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
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
    task_id = await deps.submit(
        "e2e output tree test",
        {_INPUT_NAME: _INPUT_TEXT},
        _ENGINE,
    )
    assert task_id.value > 0
    return task_id


# endregion FUNC__setup_node_and_submit


# region FUNC__wait_status
# PURPOSE: Poll a fresh UoW until the task reaches the target status so the test never inspects a stale snapshot.
async def _wait_status(
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


# endregion FUNC__wait_status


# region FUNC__capture_events
# PURPOSE: Record domain events dispatched by every UoW commit in this test so cleanup ordering can be checked against the terminal transition.
def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[DomainEvent]:
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


# endregion FUNC__capture_events


# region FUNC__terminal_events_for
# PURPOSE: Select the task's terminal events so tests can count them exactly.
def _terminal_events_for(
    events: Sequence[DomainEvent],
    task_id: TaskId,
) -> list[DomainEvent]:
    return [
        event
        for event in events
        if event.task_id == task_id and isinstance(event, (TaskCompleted, TaskFailed))
    ]


# endregion FUNC__terminal_events_for


# region FUNC__wait_until
# PURPOSE: Await a same-event-loop predicate without sleeping on wall-clock assumptions.
async def _wait_until(
    predicate: Callable[[], bool],
    timeout_s: float = 15.0,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


# endregion FUNC__wait_until


# region FUNC__remote_path_exists
# PURPOSE: Read one remote path through a fresh SSH session so tests can assert the task directory's lifecycle independently of the daemon's sessions.
async def _remote_path_exists(
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


# endregion FUNC__remote_path_exists


# region FUNC__wait_remote_path_absent
# PURPOSE: Poll until the remote task directory is gone so cleanup is observed as an asynchronous consequence of finalisation rather than a race.
async def _wait_remote_path_absent(
    ssh_container: dict[str, Any],
    remote_path: str,
    timeout_s: float = 15.0,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if not await _remote_path_exists(ssh_container, remote_path):
            return True
        await asyncio.sleep(0.05)
    return not await _remote_path_exists(ssh_container, remote_path)


# endregion FUNC__wait_remote_path_absent


# region FUNC__task_or_fail
# PURPOSE: Load the task or fail the test instead of dereferencing a missing row.
async def _task_or_fail(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: TaskId,
) -> Task:
    async with uow_factory() as uow:
        task = await uow.tasks.get(task_id)
    assert task is not None
    return task


# endregion FUNC__task_or_fail


# region FUNC__local_tree
# PURPOSE: Project the whole local result folder into path -> bytes-for-file/None-for-directory so flattening, an extra enclosing directory, extra files, wrong kinds, and wrong bytes all break one comparison.
def _local_tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): (
            _read_bytes_if_file(path) if path.is_file() else None
        )
        for path in root.rglob("*")
    }


# endregion FUNC__local_tree


# region FUNC__read_bytes_if_file
# PURPOSE: Read a file's bytes, returning None when it is missing so cleanup-hook evidence never raises inside the daemon.
def _read_bytes_if_file(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


# endregion FUNC__read_bytes_if_file


# region FUNC__download_entries
# PURPOSE: Extract this task's OUTPUT_ENTRY trace fields so per-path transfer outcomes are assertable without parsing prose.
def _download_entries(
    records: Sequence[logging.LogRecord],
    task_id: TaskId,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for record in records:
        if record.getMessage() != "OUTPUT_ENTRY":
            continue
        fields = extra_fields(record)
        if getattr(fields.get("task_id"), "value", None) == task_id.value:
            entries.append(fields)
    return entries


# endregion FUNC__download_entries


# region FUNC_test_output_tree_lands_nested_layout_and_cleans_after_finalisation
# PURPOSE: Run a real daemon against a tree-producing engine and prove the declared directory plus overlapping glob plus explicit root file land at their task-relative paths, the remote directory exists before finalisation, and cleanup runs only after the terminal event with the local files already present.
async def test_output_tree_lands_nested_layout_and_cleans_after_finalisation(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    log_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)
    orchestrator = await make_daemon(e2e_config)
    task_id_from_setup = task_id

    spawn_cwds: list[str] = []
    spawn_started = asyncio.Event()
    real_run_bg = SSHMachineSession.run_bg

    async def observed_run_bg(
        self: SSHMachineSession,
        cmd: str,
        *,
        cwd: str | None = None,
    ) -> None:
        if cwd is not None and not spawn_cwds:
            spawn_cwds.append(cwd)
            spawn_started.set()
        await real_run_bg(self, cmd, cwd=cwd)

    monkeypatch.setattr(SSHMachineSession, "run_bg", observed_run_bg)

    cleanup_calls: list[str] = []
    cleanup_observations: list[dict[str, Any]] = []
    real_cleanup = orchestrator._output_downloader.cleanup_remote

    async def recording_cleanup(
        session: MachineSession,
        remote_dir: str,
        task_id: TaskId | None = None,
    ) -> None:
        # Evidence only: assertions happen outside because consume_task logs
        # and swallows cleanup exceptions.
        cleanup_calls.append(remote_dir)
        async with uow_factory() as uow:
            finalised_task = await uow.tasks.get(task_id_from_setup)
        local_folder = (
            Path(str(finalised_task.local_folder))
            if finalised_task is not None and finalised_task.local_folder
            else None
        )
        cleanup_observations.append(
            {
                "remote_dir": remote_dir,
                "terminal_events": len(
                    _terminal_events_for(events, task_id_from_setup)
                ),
                "status": finalised_task.status if finalised_task else None,
                "nested": _read_bytes_if_file(
                    local_folder / "OUT.aiida/nested/result.dat"
                )
                if local_folder
                else None,
                "root": _read_bytes_if_file(local_folder / "root.dat")
                if local_folder
                else None,
                "remote_present": await _remote_path_exists(ssh_container, remote_dir),
            },
        )
        await real_cleanup(
            session=session,
            remote_dir=remote_dir,
            task_id=task_id,
        )

    orchestrator._output_downloader.cleanup_remote = recording_cleanup  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        await asyncio.wait_for(spawn_started.wait(), timeout=30)
        assert len(spawn_cwds) == 1, "the tree engine must spawn exactly once"
        # The task directory exists while the task is still pending/RUNNING;
        # cleanup has not had a chance to remove it.
        pre_terminal = await _task_or_fail(uow_factory, task_id)
        assert pre_terminal.status in (
            DomainTaskStatus.TO_DO,
            DomainTaskStatus.RUNNING,
        )
        assert await _remote_path_exists(ssh_container, spawn_cwds[0]), (
            "the remote task directory must exist before retrieval/finalisation"
        )

        task = await _wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is None
        assert await _wait_until(lambda: len(cleanup_observations) == 1), (
            "cleanup_remote must run exactly once after finalisation"
        )
        assert len(cleanup_calls) == 1
        assert cleanup_observations == [
            {
                "remote_dir": cleanup_calls[0],
                "terminal_events": 1,
                "status": DomainTaskStatus.DONE,
                "nested": _NESTED_RESULT,
                "root": _ROOT_RESULT,
                "remote_present": True,
            },
        ], (
            "cleanup must observe exactly one terminal event and the already "
            f"downloaded local tree; got {cleanup_observations}"
        )
        assert await _wait_remote_path_absent(ssh_container, cleanup_calls[0]), (
            "finalisation must remove the remote task directory"
        )

        local_folder = task.local_folder
        assert local_folder, "Task metadata missing local_folder"
        result_root = Path(str(local_folder))
        assert _local_tree(result_root) == _RESULT_TREE
        assert not (result_root / "result.dat").exists(), (
            "nested outputs must not be flattened into the result root"
        )
        assert not (result_root / "OUT.aiida" / "OUT.aiida").exists(), (
            "retrieval must not add an extra enclosing directory"
        )

        entries = _download_entries(log_records, task_id)
        downloaded_nested = [
            entry
            for entry in entries
            if entry["path"] == "OUT.aiida/nested/result.dat"
            and entry["outcome"] == "downloaded"
        ]
        assert len(downloaded_nested) == 1, (
            f"overlapping selections must transfer the nested file once, got {entries}"
        )
        downloaded_root = [
            entry
            for entry in entries
            if entry["path"] == "root.dat" and entry["outcome"] == "downloaded"
        ]
        assert len(downloaded_root) == 1, (
            f"the explicit root output must transfer exactly once, got {entries}"
        )

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await _wait_until(lambda: task_id not in orchestrator._tracker)
        assert await _wait_until(
            lambda: node_id not in orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"

        terminal_events = _terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskCompleted)
    finally:
        await _stop_orchestrator(orchestrator, orch_task)
        await _cleanup_node(uow_factory, ssh_container)


# endregion FUNC_test_output_tree_lands_nested_layout_and_cleans_after_finalisation


# region FUNC_test_output_tree_mixed_errors_defer_then_clean_pass_finalises
# PURPOSE: Force one pass with a real permanent selection failure plus a transient failure: the task defers, the remote tree is preserved, and a later clean pass finalises with the nested tree present and only its own (empty) error lists.
async def test_output_tree_mixed_errors_defer_then_clean_pass_finalises(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)
    orchestrator = await make_daemon(e2e_config)
    task_id_from_setup = task_id

    real_download = orchestrator._output_downloader.download_outputs
    real_cleanup = orchestrator._output_downloader.cleanup_remote
    call_count = 0
    remote_dirs: list[str] = []
    first_permanent: list[tuple[str | None, str, str]] = []
    first_pass_seen = asyncio.Event()
    second_pass_started = asyncio.Event()
    allow_second_pass = asyncio.Event()
    cleanup_calls: list[str] = []

    async def mixed_download_outputs(
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        selections: Sequence[str],
        task_id: TaskId | None = None,
    ) -> tuple[
        str,
        str,
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]:
        nonlocal call_count
        if task_id != task_id_from_setup:
            return await real_download(
                session=session,
                remote_dir=remote_dir,
                local_dir=local_dir,
                selections=selections,
                task_id=task_id,
            )
        call_count += 1
        remote_dirs.append(remote_dir)
        if call_count == 1:
            (
                local_folder,
                remote_folder,
                _transient,
                permanent,
            ) = await real_download(
                session=session,
                remote_dir=remote_dir,
                local_dir=local_dir,
                selections=[*selections, _MISSING_SELECTION],
                task_id=task_id,
            )
            first_permanent.extend(
                (path, type(err).__name__, str(err)) for path, err in permanent
            )
            first_pass_seen.set()
            # Mixed pass: the real unresolved selection is permanent, and the
            # injected transient failure keeps the task RUNNING.
            return (
                local_folder,
                remote_folder,
                [("OUT.aiida/nested/result.dat", SFTPFailure("transient blip"))],
                permanent,
            )
        second_pass_started.set()
        await allow_second_pass.wait()
        return await real_download(
            session=session,
            remote_dir=remote_dir,
            local_dir=local_dir,
            selections=selections,
            task_id=task_id,
        )

    async def recording_cleanup(
        session: MachineSession,
        remote_dir: str,
        task_id: TaskId | None = None,
    ) -> None:
        cleanup_calls.append(remote_dir)
        await real_cleanup(
            session=session,
            remote_dir=remote_dir,
            task_id=task_id,
        )

    orchestrator._output_downloader.download_outputs = mixed_download_outputs  # type: ignore[method-assign]
    orchestrator._output_downloader.cleanup_remote = recording_cleanup  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        await asyncio.wait_for(second_pass_started.wait(), timeout=30)

        task = await _task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING, (
            f"mixed transient+permanent pass must defer, got {task.status}"
        )
        assert not _terminal_events_for(events, task_id)
        assert cleanup_calls == [], "a deferred pass must not clean the remote dir"
        assert remote_dirs, "download_outputs was never called"
        assert await _remote_path_exists(ssh_container, remote_dirs[0]), (
            "mixed transient+permanent errors must preserve the remote directory"
        )
        assert [entry[0] for entry in first_permanent] == [_MISSING_SELECTION], (
            f"the permanent error path must be the raw selection, got {first_permanent}"
        )
        assert "selection matched no entries" in first_permanent[0][2]
        assert remote_dirs[0] not in (first_permanent[0][0] or ""), (
            "the error path must not be pre-joined with the remote task root"
        )

        deferred_store = e2e_config.local.tasks_dir / Path(remote_dirs[-1]).name
        assert (
            deferred_store / "OUT.aiida/nested/result.dat"
        ).read_bytes() == _NESTED_RESULT, (
            "the real retrieval in the deferred pass must already preserve "
            "the nested layout"
        )

        allow_second_pass.set()
        task = await _wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is None, (
            "the clean pass must finalise on its own error lists, "
            f"got: {task.state.error}"
        )
        assert call_count == 2, f"expected exactly two passes, got {call_count}"

        local_folder = task.local_folder
        assert local_folder, "Task metadata missing local_folder"
        assert _local_tree(Path(str(local_folder))) == _RESULT_TREE
        assert await _wait_until(lambda: len(cleanup_calls) == 1), (
            "cleanup_remote must run exactly once after finalisation"
        )
        assert await _wait_remote_path_absent(ssh_container, cleanup_calls[0]), (
            "finalisation must remove the remote task directory"
        )

        terminal_events = _terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskCompleted)
    finally:
        allow_second_pass.set()
        await _stop_orchestrator(orchestrator, orch_task)
        await _cleanup_node(uow_factory, ssh_container)


# endregion FUNC_test_output_tree_mixed_errors_defer_then_clean_pass_finalises


# region FUNC__stop_orchestrator
# PURPOSE: Stop the real daemon and collect its task without leaking a hanging task on failure.
async def _stop_orchestrator(
    orchestrator: Orchestrator,
    orch_task: asyncio.Task[None],
) -> None:
    await orchestrator.stop()
    try:
        await asyncio.wait_for(orch_task, timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        orch_task.cancel()


# endregion FUNC__stop_orchestrator


# region FUNC__cleanup_node
# PURPOSE: Remove the test node row and abandon any RUNNING task bound to it so a failed test cannot poison the next one.
async def _cleanup_node(
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


# endregion FUNC__cleanup_node
