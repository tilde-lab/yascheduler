# region MODULE_CONTRACT
# PURPOSE: E2E tests for the V0 consume_task lifecycle: transient deferral, terminal finalisation from a pass with zero transient errors, post-finalisation best-effort remote cleanup, and daemon restart re-download.
# SCOPE: retry-then-success, permanent failure, transient directory preservation, mixed-error non-stickiness, restart re-download, cleanup failure after finalisation, cleanup of an already-absent directory, and tracker/occupancy release.
# DEPENDENCIES: USES API: tests.e2e.helpers (node setup, polling, event capture, remote probes, daemon teardown, scripted downloads).
# KEYWORDS: consume_task, retry, defer, finalise, cleanup_remote, restart, tracker, e2e
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from asyncssh.sftp import SFTPFailure

from tests.e2e.helpers import (
    ScriptedDownloadOutputs,
    capture_events,
    cleanup_node,
    remote_path_exists,
    setup_node_and_submit,
    stop_orchestrator,
    task_or_fail,
    terminal_events_for,
    wait_remote_path_absent,
    wait_status,
    wait_until,
)
from yascheduler.application import AllocationTracker
from yascheduler.application.consume_task import consume_task
from yascheduler.domain import (
    EngineRepository,
    TaskCompleted,
    TaskFailed,
    allocated_node_id_of,
)
from yascheduler.domain.model import TaskId
from yascheduler.domain.model import TaskStatus as DomainTaskStatus
from yascheduler.entrypoints.di import make_daemon
from yascheduler.infra import OutputDownloader
from yascheduler.infra.ssh.repository import SSHMachineRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.application.orchestrator import Orchestrator
    from yascheduler.domain import MachineSession
    from yascheduler.entrypoints import Config
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork


async def test_consume_retry_then_success(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient-only first pass defers; second pass succeeds and cleanup removes
    the remote tree. Tracker and occupancy slots are released only on finalisation.
    """
    task_id = await setup_node_and_submit(
        e2e_config,
        uow_factory,
        ssh_container,
        engine="test_shell",
        inputs={"1.input": "hello e2e"},
        label="e2e retry test",
    )
    events = capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    first_pass_seen = asyncio.Event()
    second_pass_started = asyncio.Event()
    allow_second_pass = asyncio.Event()

    async def gate_second_pass() -> None:
        second_pass_started.set()
        await allow_second_pass.wait()

    download = ScriptedDownloadOutputs(
        orchestrator._output_downloader.download_outputs,
        script=[([("1.input.out", SFTPFailure("transient blip"))], [])],
        hooks={1: first_pass_seen.set, 2: gate_second_pass},
    )
    orchestrator._output_downloader.download_outputs = download  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        await asyncio.wait_for(second_pass_started.wait(), timeout=30)

        task = await task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING
        assert not terminal_events_for(events, task_id)
        assert download.remote_dirs, "download_outputs was never called"
        assert await remote_path_exists(ssh_container, download.remote_dirs[0]), (
            "transient deferral must preserve the remote task directory"
        )

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await wait_until(lambda: node_id in orchestrator._occupancy_started)
        assert orchestrator._tracker.add(task_id, node_id)
        assert task_id in orchestrator._tracker

        allow_second_pass.set()
        task = await wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

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
        assert await wait_until(lambda: task_id not in orchestrator._tracker), (
            "tracker slot must be released at finalisation"
        )
        assert await wait_until(
            lambda: node_id not in orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"
        assert await wait_remote_path_absent(ssh_container, download.remote_dirs[0]), (
            "successful finalisation must remove the remote task directory"
        )

        terminal_events = terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskCompleted)
    finally:
        allow_second_pass.set()
        await stop_orchestrator(orchestrator, orch_task)
        await cleanup_node(uow_factory, ssh_container)


async def test_consume_permanent_marks_done_with_error(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing output selection is permanent: DONE with a Download error naming
    the raw selection, and V0 still performs the post-finalisation remote cleanup.
    """
    task_id = await setup_node_and_submit(
        e2e_config,
        uow_factory,
        ssh_container,
        engine="test_shell",
        inputs={"1.input": "hello e2e"},
        label="e2e retry test",
    )
    events = capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    missing_selection = "missing.out"

    download = ScriptedDownloadOutputs(
        orchestrator._output_downloader.download_outputs,
        extra_selections=[missing_selection],
    )
    orchestrator._output_downloader.download_outputs = download  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        task = await wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert download.remote_dirs, "download_outputs was never called"
        assert task.state.error is not None, (
            "Expected error on permanent download failure"
        )
        assert "Download error" in task.state.error
        assert missing_selection in task.state.error
        assert "selection matched no entries" in task.state.error
        assert download.remote_dirs[-1] + "/" + missing_selection not in (
            task.state.error
        ), (
            "the permanent error must name the raw selection, not a "
            "pre-joined remote path"
        )

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await wait_until(
            lambda: node_id not in orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"
        assert await wait_remote_path_absent(ssh_container, download.remote_dirs[-1]), (
            "permanent retrieval failure must still remove the remote directory"
        )

        terminal_events = terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskFailed)
        assert terminal_events[0].reason == task.state.error
    finally:
        await stop_orchestrator(orchestrator, orch_task)
        await cleanup_node(uow_factory, ssh_container)


async def test_consume_transient_preserves_remote_dir_regression(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: transient download errors keep the task RUNNING, preserve the
    remote tree, and never call cleanup_remote.
    """
    task_id = await setup_node_and_submit(
        e2e_config,
        uow_factory,
        ssh_container,
        engine="test_shell",
        inputs={"1.input": "hello e2e"},
        label="e2e retry test",
    )
    events = capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    cleanup_calls: list[str] = []
    first_pass_seen = asyncio.Event()

    async def recording_cleanup_remote(
        session: MachineSession,
        remote_dir: str,
        task_id: TaskId | None = None,
    ) -> None:
        cleanup_calls.append(remote_dir)

    download = ScriptedDownloadOutputs(
        orchestrator._output_downloader.download_outputs,
        script=[([("1.input.out", SFTPFailure("transient"))], [])],
        hooks={1: first_pass_seen.set},
        repeat_last=True,
    )
    orchestrator._output_downloader.download_outputs = download  # type: ignore[method-assign]
    orchestrator._output_downloader.cleanup_remote = recording_cleanup_remote  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        assert await wait_until(lambda: task_id not in orchestrator._consuming)

        task = await task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING, (
            f"Expected RUNNING (deferred), got {task.status}"
        )
        assert not terminal_events_for(events, task_id)
        assert cleanup_calls == [], "transient deferral must not clean the remote dir"
        assert download.remote_dirs, "download_outputs was never called"
        assert await remote_path_exists(ssh_container, download.remote_dirs[-1]), (
            "transient deferral must preserve the remote task directory"
        )
    finally:
        await stop_orchestrator(orchestrator, orch_task)
        await cleanup_node(uow_factory, ssh_container)


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
    task_id = await setup_node_and_submit(
        e2e_config,
        uow_factory,
        ssh_container,
        engine="test_shell",
        inputs={"1.input": "hello e2e"},
        label="e2e retry test",
    )
    events = capture_events(monkeypatch)

    orchestrator = await make_daemon(e2e_config)
    missing_selection = "still-missing.out"
    stale_selection = "stale-only.out"
    first_pass_seen = asyncio.Event()
    second_pass_started = asyncio.Event()
    allow_second_pass = asyncio.Event()

    async def gate_second_pass() -> None:
        second_pass_started.set()
        await allow_second_pass.wait()

    download = ScriptedDownloadOutputs(
        orchestrator._output_downloader.download_outputs,
        script=[
            (
                [("1.input.out", SFTPFailure("transient blip"))],
                [
                    (missing_selection, FileNotFoundError("No such file")),
                    (stale_selection, OSError("old permanent failure")),
                ],
            ),
        ],
        hooks={1: first_pass_seen.set, 2: gate_second_pass},
        extra_selections=[missing_selection],
    )
    orchestrator._output_downloader.download_outputs = download  # type: ignore[method-assign]

    orch_task = asyncio.create_task(orchestrator.start())

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        await asyncio.wait_for(second_pass_started.wait(), timeout=30)

        task = await task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING
        assert not terminal_events_for(events, task_id)
        assert download.remote_dirs, "download_outputs was never called"
        assert await remote_path_exists(ssh_container, download.remote_dirs[0]), (
            "mixed transient+permanent errors must preserve the remote directory"
        )

        allow_second_pass.set()
        task = await wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is not None
        assert "Download error" in task.state.error
        assert missing_selection in task.state.error
        assert "selection matched no entries" in task.state.error
        assert download.remote_dirs[0] + "/" + missing_selection not in (
            task.state.error
        ), "the final error must name the raw selection, not a pre-joined remote path"
        assert stale_selection not in task.state.error, (
            "the failed pass must report only its own permanent errors"
        )
        assert "transient blip" not in task.state.error
        local_folder = task.local_folder
        assert local_folder
        assert (Path(str(local_folder)) / "1.input.out").read_text() == "hello e2e"

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await wait_until(
            lambda: node_id not in orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"
        assert await wait_remote_path_absent(ssh_container, download.remote_dirs[0])

        terminal_events = terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskFailed)
        assert terminal_events[0].reason == task.state.error
    finally:
        allow_second_pass.set()
        await stop_orchestrator(orchestrator, orch_task)
        await cleanup_node(uow_factory, ssh_container)


async def test_consume_restart_after_defer_redownloads_and_succeeds(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new daemon after a deferred pass re-downloads from scratch and completes
    the still-RUNNING task; no restart-resume state is required.
    """
    task_id = await setup_node_and_submit(
        e2e_config,
        uow_factory,
        ssh_container,
        engine="test_shell",
        inputs={"1.input": "hello e2e"},
        label="e2e retry test",
    )
    events = capture_events(monkeypatch)

    first_orchestrator = await make_daemon(e2e_config)
    first_pass_seen = asyncio.Event()
    first_download = ScriptedDownloadOutputs(
        first_orchestrator._output_downloader.download_outputs,
        script=[([("1.input.out", SFTPFailure("daemon restart blip"))], [])],
        hooks={1: first_pass_seen.set},
        repeat_last=True,
    )
    first_orchestrator._output_downloader.download_outputs = first_download  # type: ignore[method-assign]
    first_orch_task = asyncio.create_task(first_orchestrator.start())
    first_stopped = False
    second_orchestrator: Orchestrator | None = None
    second_orch_task: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(first_pass_seen.wait(), timeout=30)
        assert await wait_until(lambda: task_id not in first_orchestrator._consuming)
        task = await task_or_fail(uow_factory, task_id)
        assert task.status == DomainTaskStatus.RUNNING
        assert not terminal_events_for(events, task_id)
        assert first_download.remote_dirs
        assert await remote_path_exists(ssh_container, first_download.remote_dirs[0])

        await stop_orchestrator(first_orchestrator, first_orch_task)
        first_stopped = True

        second_orchestrator = await make_daemon(e2e_config)
        second_download = ScriptedDownloadOutputs(
            second_orchestrator._output_downloader.download_outputs,
        )
        second_orchestrator._output_downloader.download_outputs = second_download  # type: ignore[method-assign]
        second_orch_task = asyncio.create_task(second_orchestrator.start())

        task = await wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE after daemon restart"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is None
        assert second_download.calls >= 1, (
            "restarted daemon must re-download outputs from scratch"
        )
        assert second_download.remote_dirs
        local_folder = task.local_folder
        assert local_folder
        assert (Path(str(local_folder)) / "1.input.out").read_text() == "hello e2e"

        node_id = allocated_node_id_of(task)
        assert node_id is not None
        assert await wait_until(
            lambda: node_id not in second_orchestrator._occupancy_started
        ), "occupancy slot must be released at finalisation"
        assert await wait_remote_path_absent(
            ssh_container, second_download.remote_dirs[0]
        )

        terminal_events = terminal_events_for(events, task_id)
        assert len(terminal_events) == 1
        assert isinstance(terminal_events[0], TaskCompleted)
    finally:
        if not first_stopped:
            await stop_orchestrator(first_orchestrator, first_orch_task)
        if second_orchestrator is not None and second_orch_task is not None:
            await stop_orchestrator(second_orchestrator, second_orch_task)
        await cleanup_node(uow_factory, ssh_container)


async def test_consume_cleanup_failure_after_success_keeps_done_and_remote_dir(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising cleanup_remote after a successful download cannot change the
    already-committed DONE outcome; the remote directory remains for teardown.
    """
    task_id = await setup_node_and_submit(
        e2e_config,
        uow_factory,
        ssh_container,
        engine="test_shell",
        inputs={"1.input": "hello e2e"},
        label="e2e retry test",
    )
    events = capture_events(monkeypatch)

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
        task = await wait_status(uow_factory, task_id, DomainTaskStatus.DONE)

        assert task is not None, "Task did not reach DONE"
        assert task.status == DomainTaskStatus.DONE
        assert task.state.error is None
        local_folder = task.local_folder
        assert local_folder
        assert (Path(str(local_folder)) / "1.input.out").read_text() == "hello e2e"
        assert remote_dirs, "cleanup_remote was never called"
        assert await wait_until(lambda: task_id not in orchestrator._consuming)
        assert await remote_path_exists(ssh_container, remote_dirs[0]), (
            "cleanup failure must leave the remote directory for node teardown"
        )
        assert await wait_until(lambda: len(terminal_events_for(events, task_id)) == 1)
        terminal_events = terminal_events_for(events, task_id)
        assert isinstance(terminal_events[0], TaskCompleted)
    finally:
        await stop_orchestrator(orchestrator, orch_task)
        await cleanup_node(uow_factory, ssh_container)


async def test_consume_missing_engine_with_absent_remote_dir_finalises(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_container: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The missing-engine path attempts cleanup once on an already-absent remote
    directory and finalises without hanging.
    """
    task_id = await setup_node_and_submit(
        e2e_config,
        uow_factory,
        ssh_container,
        engine="test_shell",
        inputs={"1.input": "hello e2e"},
        label="e2e retry test",
    )
    events = capture_events(monkeypatch)

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

    assert not await remote_path_exists(ssh_container, remote_dir)

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
    task = await task_or_fail(uow_factory, task_id)
    expected_error = "engine 'test_shell' no longer in config"
    assert task.status == DomainTaskStatus.DONE
    assert task.state.error == expected_error
    assert cleanup_calls == [remote_dir]
    assert task_id not in tracker
    assert not await remote_path_exists(ssh_container, remote_dir)
    terminal_events = terminal_events_for(events, task_id)
    assert len(terminal_events) == 1
    assert isinstance(terminal_events[0], TaskFailed)
    assert terminal_events[0].reason == expected_error

    await cleanup_node(uow_factory, ssh_container)
