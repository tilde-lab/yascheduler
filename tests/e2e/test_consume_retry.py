# region MODULE_CONTRACT
# PURPOSE: E2E tests for the V0 consume_task lifecycle: transient deferral, terminal finalisation from a pass with zero transient errors, post-finalisation best-effort remote cleanup, and daemon restart re-download.
# SCOPE: retry-then-success, permanent failure, transient directory preservation, mixed-error non-stickiness, restart re-download, cleanup failure after finalisation, cleanup of an already-absent directory, and tracker/occupancy release.
# KEYWORDS: consume_task, retry, defer, finalise, cleanup_remote, restart, tracker, e2e
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from asyncssh.sftp import SFTPFailure, SFTPNoSuchFile

from yascheduler.application import AllocationTracker, MessageBus
from yascheduler.application.consume_task import consume_task
from yascheduler.domain import (
    DomainEvent,
    EngineRepository,
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
from yascheduler.infra import OutputDownloader
from yascheduler.infra.ssh.repository import SSHMachineRepository

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from yascheduler.application.orchestrator import Orchestrator
    from yascheduler.domain import MachineSession
    from yascheduler.entrypoints import Config
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork


async def _setup_node_and_submit(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
) -> TaskId:
    """Connect, deploy engine, add node row, submit a task. Returns task_id."""
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
        "e2e retry test",
        {"1.input": "hello e2e"},
        "test_shell",
    )
    assert task_id.value > 0
    return task_id


async def _wait_status(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: TaskId,
    target: DomainTaskStatus,
    timeout_s: float = 30.0,
) -> Task | None:
    """Poll until task reaches target status or timeout. Returns the task."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    task = None
    while asyncio.get_running_loop().time() < deadline:
        async with uow_factory() as uow:
            task = await uow.tasks.get(task_id)
        if task and task.status == target:
            return task
        await asyncio.sleep(0.5)
    return task


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


def _terminal_events_for(
    events: Sequence[DomainEvent],
    task_id: TaskId,
) -> list[DomainEvent]:
    return [
        event
        for event in events
        if event.task_id == task_id and isinstance(event, (TaskCompleted, TaskFailed))
    ]


async def _wait_until(
    predicate: Callable[[], bool],
    timeout_s: float = 10.0,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


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


async def _wait_remote_path_absent(
    ssh_container: dict[str, Any],
    remote_path: str,
    timeout_s: float = 10.0,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if not await _remote_path_exists(ssh_container, remote_path):
            return True
        await asyncio.sleep(0.05)
    return not await _remote_path_exists(ssh_container, remote_path)


async def _task_or_fail(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: TaskId,
) -> Task:
    async with uow_factory() as uow:
        task = await uow.tasks.get(task_id)
    assert task is not None
    return task


async def test_consume_retry_then_success(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient-only first pass defers; second pass succeeds and cleanup removes
    the remote tree. Tracker and occupancy slots are released only on finalisation.
    """
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    real_download = orchestrator._output_downloader.download_outputs
    call_count = 0
    remote_dirs: list[str] = []
    first_pass_seen = asyncio.Event()
    second_pass_started = asyncio.Event()
    allow_second_pass = asyncio.Event()

    async def flaky_download_outputs(
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        files: list[str],
        task_id: TaskId | None = None,
    ) -> tuple[
        str,
        str,
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]:
        nonlocal call_count
        if task_id == task_id_from_setup:
            call_count += 1
            remote_dirs.append(remote_dir)
            if call_count == 1:
                first_pass_seen.set()
                return (
                    "",
                    "",
                    [(remote_dir + "/1.input.out", SFTPFailure("transient blip"))],
                    [],
                )
            if call_count == 2:
                second_pass_started.set()
                await allow_second_pass.wait()
        return await real_download(
            session=session,
            remote_dir=remote_dir,
            local_dir=local_dir,
            files=files,
            task_id=task_id,
        )

    task_id_from_setup = task_id
    orchestrator._output_downloader.download_outputs = flaky_download_outputs  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        await asyncio.wait_for(second_pass_started.wait(), timeout=30)

        task = await _task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING
        assert not _terminal_events_for(events, task_id)
        assert remote_dirs, "download_outputs was never called"
        assert await _remote_path_exists(ssh_container, remote_dirs[0]), (
            "transient deferral must preserve the remote task directory"
        )

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await _wait_until(lambda: node_id in orchestrator._occupancy_started)
        assert orchestrator._tracker.add(task_id, node_id)
        assert task_id in orchestrator._tracker

        allow_second_pass.set()
        task = await _wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is None, (
            f"Expected no error on retry-then-success, got: {task.state.error}"
        )
        local_folder = task.local_folder
        assert local_folder, "Task metadata missing local_folder"
        output_file = Path(str(local_folder)) / "1.input.out"
        assert output_file.exists(), f"Output file not found: {output_file}"
        assert output_file.read_text() == "hello e2e"
        assert await _wait_until(lambda: task_id not in orchestrator._tracker), (
            "tracker slot must be released at finalisation"
        )
        assert await _wait_until(
            lambda: node_id not in orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"
        assert await _wait_remote_path_absent(ssh_container, remote_dirs[0]), (
            "successful finalisation must remove the remote task directory"
        )

        terminal_events = _terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskCompleted)
    finally:
        allow_second_pass.set()
        await _stop_orchestrator(orchestrator, orch_task)
        await _cleanup_node(uow_factory, ssh_container)


async def test_consume_permanent_marks_done_with_error(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing output file is permanent: DONE with a Download error naming the
    path, and V0 still performs the post-finalisation remote cleanup.
    """
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    real_download = orchestrator._output_downloader.download_outputs
    remote_dirs: list[str] = []
    missing_paths: list[str] = []

    async def missing_output_download(
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        files: list[str],
        task_id: TaskId | None = None,
    ) -> tuple[
        str,
        str,
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]:
        remote_dirs.append(remote_dir)
        missing_path = remote_dir + "/missing.out"
        missing_paths.append(missing_path)
        return await real_download(
            session=session,
            remote_dir=remote_dir,
            local_dir=local_dir,
            files=[*files, missing_path],
            task_id=task_id,
        )

    orchestrator._output_downloader.download_outputs = missing_output_download  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        task = await _wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert missing_paths, "download_outputs was never called"
        missing_path = missing_paths[-1]
        assert task.state.error is not None, (
            "Expected error on permanent download failure"
        )
        assert "Download error" in task.state.error
        assert missing_path in task.state.error

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await _wait_until(
            lambda: node_id not in orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"
        assert await _wait_remote_path_absent(ssh_container, remote_dirs[-1]), (
            "permanent retrieval failure must still remove the remote directory"
        )

        terminal_events = _terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskFailed)
        assert terminal_events[0].reason == task.state.error
    finally:
        await _stop_orchestrator(orchestrator, orch_task)
        await _cleanup_node(uow_factory, ssh_container)


async def test_consume_transient_preserves_remote_dir_regression(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: transient download errors keep the task RUNNING, preserve the
    remote tree, and never call cleanup_remote.
    """
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    remote_dirs: list[str] = []
    cleanup_calls: list[str] = []
    first_pass_seen = asyncio.Event()

    async def transient_only_download(
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        files: list[str],
        task_id: TaskId | None = None,
    ) -> tuple[
        str,
        str,
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]:
        remote_dirs.append(remote_dir)
        first_pass_seen.set()
        return (
            "",
            "",
            [(remote_dir + "/1.input.out", SFTPFailure("transient"))],
            [],
        )

    async def recording_cleanup_remote(
        session: MachineSession,
        remote_dir: str,
        task_id: TaskId | None = None,
    ) -> None:
        cleanup_calls.append(remote_dir)

    orchestrator._output_downloader.download_outputs = transient_only_download  # type: ignore[method-assign]
    orchestrator._output_downloader.cleanup_remote = recording_cleanup_remote  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        assert await _wait_until(lambda: task_id not in orchestrator._consuming)

        task = await _task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING, (
            f"Expected RUNNING (deferred), got {task.status}"
        )
        assert not _terminal_events_for(events, task_id)
        assert cleanup_calls == [], "transient deferral must not clean the remote dir"
        assert remote_dirs, "download_outputs was never called"
        assert await _remote_path_exists(ssh_container, remote_dirs[-1]), (
            "transient deferral must preserve the remote task directory"
        )
    finally:
        await _stop_orchestrator(orchestrator, orch_task)
        await _cleanup_node(uow_factory, ssh_container)


async def test_consume_mixed_errors_defer_then_finalise_on_last_pass_errors(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mixed permanent+transient pass defers. The next pass re-derives errors:
    the transient file recovers, the still-missing file fails the task, and the
    first pass's stale permanent error is not carried forward.
    """
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    real_download = orchestrator._output_downloader.download_outputs
    call_count = 0
    remote_dirs: list[str] = []
    missing_paths: list[str] = []
    stale_paths: list[str] = []
    first_pass_seen = asyncio.Event()
    second_pass_started = asyncio.Event()
    allow_second_pass = asyncio.Event()

    async def mixed_download_outputs(
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        files: list[str],
        task_id: TaskId | None = None,
    ) -> tuple[
        str,
        str,
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]:
        nonlocal call_count
        if task_id == task_id_from_setup:
            call_count += 1
            remote_dirs.append(remote_dir)
            missing_path = remote_dir + "/still-missing.out"
            missing_paths.append(missing_path)
            if call_count == 1:
                stale_path = remote_dir + "/stale-only.out"
                stale_paths.append(stale_path)
                first_pass_seen.set()
                return (
                    "",
                    "",
                    [(remote_dir + "/1.input.out", SFTPFailure("transient blip"))],
                    [
                        (missing_path, OSError("No such file")),
                        (stale_path, OSError("old permanent failure")),
                    ],
                )
            second_pass_started.set()
            await allow_second_pass.wait()
            return await real_download(
                session=session,
                remote_dir=remote_dir,
                local_dir=local_dir,
                files=[*files, missing_path],
                task_id=task_id,
            )
        return await real_download(
            session=session,
            remote_dir=remote_dir,
            local_dir=local_dir,
            files=files,
            task_id=task_id,
        )

    task_id_from_setup = task_id
    orchestrator._output_downloader.download_outputs = mixed_download_outputs  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        await asyncio.wait_for(second_pass_started.wait(), timeout=30)

        task = await _task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING
        assert not _terminal_events_for(events, task_id)
        assert remote_dirs, "download_outputs was never called"
        assert await _remote_path_exists(ssh_container, remote_dirs[0]), (
            "mixed transient+permanent errors must preserve the remote directory"
        )

        allow_second_pass.set()
        task = await _wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is not None
        assert "Download error" in task.state.error
        assert missing_paths[-1] in task.state.error
        assert stale_paths[0] not in task.state.error, (
            "the failed pass must report only its own permanent errors"
        )
        assert "transient blip" not in task.state.error
        local_folder = task.local_folder
        assert local_folder
        assert (Path(str(local_folder)) / "1.input.out").read_text() == "hello e2e"

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await _wait_until(
            lambda: node_id not in orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"
        assert await _wait_remote_path_absent(ssh_container, remote_dirs[0])

        terminal_events = _terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskFailed)
        assert terminal_events[0].reason == task.state.error
    finally:
        allow_second_pass.set()
        await _stop_orchestrator(orchestrator, orch_task)
        await _cleanup_node(uow_factory, ssh_container)


async def test_consume_restart_after_defer_redownloads_and_succeeds(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new daemon after a deferred pass re-downloads from scratch and completes
    the still-RUNNING task; no restart-resume state is required.
    """
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)

    first_orchestrator = await make_daemon(e2e_config)
    first_pass_seen = asyncio.Event()
    first_remote_dirs: list[str] = []

    async def transient_only_download(
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        files: list[str],
        task_id: TaskId | None = None,
    ) -> tuple[
        str,
        str,
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]:
        first_remote_dirs.append(remote_dir)
        first_pass_seen.set()
        return (
            "",
            "",
            [(remote_dir + "/1.input.out", SFTPFailure("daemon restart blip"))],
            [],
        )

    first_orchestrator._output_downloader.download_outputs = transient_only_download  # type: ignore[method-assign]
    first_orch_task = asyncio.create_task(first_orchestrator.start())
    first_stopped = False
    second_orchestrator: Orchestrator | None = None
    second_orch_task: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        assert await _wait_until(lambda: task_id not in first_orchestrator._consuming)
        task = await _task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING
        assert not _terminal_events_for(events, task_id)
        assert first_remote_dirs
        assert await _remote_path_exists(ssh_container, first_remote_dirs[0])

        await _stop_orchestrator(first_orchestrator, first_orch_task)
        first_stopped = True

        second_orchestrator = await make_daemon(e2e_config)
        second_real_download = second_orchestrator._output_downloader.download_outputs
        second_download_calls = 0
        second_remote_dirs: list[str] = []

        async def counting_download_outputs(
            session: MachineSession,
            remote_dir: str,
            local_dir: Path,
            files: list[str],
            task_id: TaskId | None = None,
        ) -> tuple[
            str,
            str,
            list[tuple[str | None, Exception]],
            list[tuple[str | None, Exception]],
        ]:
            nonlocal second_download_calls
            second_download_calls += 1
            second_remote_dirs.append(remote_dir)
            return await second_real_download(
                session=session,
                remote_dir=remote_dir,
                local_dir=local_dir,
                files=files,
                task_id=task_id,
            )

        second_downloader = second_orchestrator._output_downloader
        second_downloader.download_outputs = counting_download_outputs  # type: ignore[method-assign]
        second_orch_task = asyncio.create_task(second_orchestrator.start())

        task = await _wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE after daemon restart"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is None
        assert second_download_calls >= 1, (
            "restarted daemon must re-download outputs from scratch"
        )
        assert second_remote_dirs
        local_folder = task.local_folder
        assert local_folder
        assert (Path(str(local_folder)) / "1.input.out").read_text() == "hello e2e"

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await _wait_until(
            lambda: node_id not in second_orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"
        assert await _wait_remote_path_absent(ssh_container, second_remote_dirs[0])

        terminal_events = _terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskCompleted)
    finally:
        if not first_stopped:
            await _stop_orchestrator(first_orchestrator, first_orch_task)
        if second_orchestrator is not None and second_orch_task is not None:
            await _stop_orchestrator(second_orchestrator, second_orch_task)
        await _cleanup_node(uow_factory, ssh_container)


async def test_consume_cleanup_failure_after_success_keeps_done_and_remote_dir(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising cleanup_remote after a successful download cannot change the
    already-committed DONE outcome; the remote directory remains for teardown.
    """
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    remote_dirs: list[str] = []

    async def raising_cleanup_remote(
        session: MachineSession,
        remote_dir: str,
        task_id: TaskId | None = None,
    ) -> None:
        remote_dirs.append(remote_dir)
        raise RuntimeError("cleanup blip")

    orchestrator._output_downloader.cleanup_remote = raising_cleanup_remote  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        task = await _wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is None
        local_folder = task.local_folder
        assert local_folder
        assert (Path(str(local_folder)) / "1.input.out").read_text() == "hello e2e"
        assert remote_dirs, "cleanup_remote was never called"
        assert await _wait_until(lambda: task_id not in orchestrator._consuming)
        assert await _remote_path_exists(ssh_container, remote_dirs[0]), (
            "cleanup failure must leave the remote directory for node teardown"
        )
        assert await _wait_until(
            lambda: len(_terminal_events_for(events, task_id)) == 1
        )
        terminal_events = _terminal_events_for(events, task_id)
        assert isinstance(terminal_events[0], TaskCompleted)
    finally:
        await _stop_orchestrator(orchestrator, orch_task)
        await _cleanup_node(uow_factory, ssh_container)


async def test_consume_missing_engine_with_absent_remote_dir_finalises(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The missing-engine path attempts cleanup once on an already-absent remote
    directory and finalises without hanging.
    """
    task_id = await _setup_node_and_submit(e2e_config, uow_factory, ssh_container)
    events = _capture_events(monkeypatch)

    async with uow_factory() as uow:
        nodes = await uow.nodes.list_all()
        todo = await uow.tasks.get_todo(task_id)
    assert todo is not None
    node = next(n for n in nodes if n.hostname == ssh_container["host"])
    remote_dir = str(e2e_config.remote.tasks_dir / f"absent_{task_id.value}")
    running = todo.run(node.node_id, remote_dir)
    async with uow_factory() as uow:
        await uow.tasks.save(running)
        await uow.commit()

    assert not await _remote_path_exists(ssh_container, remote_dir)

    downloader = OutputDownloader()
    cleanup_calls: list[str] = []
    real_cleanup = downloader.cleanup_remote

    async def counting_cleanup(
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

    downloader.cleanup_remote = counting_cleanup  # type: ignore[method-assign]
    tracker = AllocationTracker()
    assert tracker.add(task_id)

    repository = SSHMachineRepository()
    session = await repository.connect(
        node=node,
        client_keys=[ssh_container["key_path"]],
    )
    try:
        finalised = await asyncio.wait_for(
            consume_task(
                task_id=task_id,
                session=session,
                output_downloader=downloader,
                engines=EngineRepository(),
                uow_factory=uow_factory,
                local_tasks_dir=e2e_config.local.tasks_dir,
                tracker=tracker,
            ),
            timeout=30,
        )
    finally:
        await repository.disconnect(session.machine.node_id)

    assert finalised is True
    task = await _task_or_fail(uow_factory, task_id)
    expected_error = "engine 'test_shell' no longer in config"
    assert task.status == DomainTaskStatus.DONE
    assert task.state.error == expected_error
    assert cleanup_calls == [remote_dir]
    assert task_id not in tracker
    assert not await _remote_path_exists(ssh_container, remote_dir)
    terminal_events = _terminal_events_for(events, task_id)
    assert len(terminal_events) == 1
    assert isinstance(terminal_events[0], TaskFailed)
    assert terminal_events[0].reason == expected_error

    await _cleanup_node(uow_factory, ssh_container)


async def _stop_orchestrator(
    orchestrator: Orchestrator,
    orch_task: asyncio.Task[None],
) -> None:
    await orchestrator.stop()
    try:
        await asyncio.wait_for(orch_task, timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        orch_task.cancel()


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
            for t in running:
                if t.state.allocated_node_id == node_id:
                    abandoned = t.abandon(error="test cleanup")
                    await uow.tasks.save(abandoned)
            await uow.nodes.remove(node_id)
        await uow.commit()
