"""Unit tests for domain event recording from application use cases.

Tests cover:
- submit_task records TaskCreated event
- allocate_task records TaskFailed or TaskAllocated event
- consume_task records TaskCompleted or TaskFailed event
"""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for domain event recording from application use cases.
# SCOPE: submit_task records TaskCreated, allocate_task records TaskFailed/TaskAllocated, consume_task records TaskCompleted/TaskFailed.
# KEYWORDS: TaskCreated, TaskFailed, TaskAllocated, TaskCompleted, event recording
# endregion MODULE_CONTRACT

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from yascheduler.application.allocate_task import allocate_task
from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.consume_task import consume_task
from yascheduler.application.submit_task import submit_task
from yascheduler.application.uow import AbstractUnitOfWork
from yascheduler.domain import (
    Engine,
    EngineRepository,
    TaskInputFile,
)
from yascheduler.domain.events import (
    TaskAllocated,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
)
from yascheduler.domain.model import (
    NewTask,
    Node,
    NodeId,
    Task,
    TaskId,
    Todo,
    materialize_task,
)
from yascheduler.domain.ports import CloudProvisioner

# =============================================================================
# Event recording characterization tests (D4)
# =============================================================================


class TestSubmitTaskEvents:
    """Verify submit_task records TaskCreated event."""

    async def test_submit_task_records_task_created_event(
        self,
        engine: Engine,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        uow = mock_uow_factory.return_value

        def _insert_side_effect(new_task: NewTask) -> Task:
            task = Task(
                task_id=TaskId(55),
                label=new_task.label,
                engine=new_task.engine,
                state=Todo(),
                webhook_url=new_task.webhook_url,
                webhook_custom_params=new_task.webhook_custom_params,
                extra=new_task.extra,
                created_at=datetime(2025, 1, 1),
                updated_at=datetime(2025, 1, 1),
            )
            return materialize_task(task)

        uow.tasks.insert = AsyncMock(side_effect=_insert_side_effect)

        await submit_task(
            label="evt_test",
            metadata={"inp": "data"},
            engine_name="test_engine",
            engines=mock_engine_repo,
            uow_factory=mock_uow_factory,
            input_snapshot=(TaskInputFile(path="inp", content=b"data"),),
        )

        saved_arg: Task = uow.tasks.save.call_args[0][0]
        assert len(saved_arg.events) == 1
        event = saved_arg.events[0]
        assert isinstance(event, TaskCreated)
        assert event.task_id == TaskId(55)
        assert event.engine_name == "test_engine"


class TestAllocateTaskEvents:
    """Verify allocate_task records TaskFailed or TaskAllocated events."""

    async def test_validate_engine_records_task_failed_event(self) -> None:
        """Unsupported engine records TaskFailed event."""
        engines = MagicMock(spec=EngineRepository)
        engines.get.return_value = None

        todo_task = Task(
            task_id=TaskId(1),
            label="t",
            engine="bad",
            state=Todo(),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )
        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.get_todo = AsyncMock(return_value=todo_task)
        uow.tasks.save = AsyncMock()
        uow.commit = AsyncMock()
        uow.collect_events = AsyncMock(return_value=[])
        uow.publish_events = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        tracker = MagicMock(spec=AllocationTracker)
        allocation_lock = asyncio.Lock()

        await allocate_task(
            task_id=TaskId(1),
            engines=engines,
            uow_factory=uow_factory,
            repository=MagicMock(),
            occupancy_checker=MagicMock(),
            clouds=MagicMock(),
            start_task_on_machine=AsyncMock(),
            tracker=tracker,
            allocation_lock=allocation_lock,
            remote_tasks_dir=PurePath("/remote/tasks"),
        )

        saved_task: Task = uow.tasks.save.call_args[0][0]
        assert len(saved_task.events) == 1
        event = saved_task.events[0]
        assert isinstance(event, TaskFailed)
        assert event.reason == "unsupported engine"

    async def test_allocate_free_machine_records_task_allocated_event(
        self,
        engine: Engine,
    ) -> None:
        """Successful allocation records TaskAllocated event."""
        import time

        from yascheduler.domain.model import ConnectedMachine, MachineState

        engines = MagicMock(spec=EngineRepository)
        engines.get.return_value = engine

        free_machine = MagicMock(spec=ConnectedMachine)
        free_machine.node_id = NodeId(1)
        free_machine.state = MachineState.FREE
        free_machine.free_since = time.monotonic()
        free_session = SimpleNamespace(machine=free_machine, hostname="10.0.0.1")

        repository = MagicMock()
        repository.list_free = MagicMock(return_value=[free_session])
        occupancy_checker = MagicMock()
        occupancy_checker.start_occupancy_check = MagicMock()

        from datetime import datetime

        todo_task = Task(
            task_id=TaskId(1),
            label="t",
            engine="test_engine",
            state=Todo(),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )
        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.get_todo = AsyncMock(return_value=todo_task)
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.tasks.save = AsyncMock()
        uow.nodes = AsyncMock()
        # Fix A: _find_free_machines intersects list_free with DB-enabled IPs.
        uow.nodes.list_enabled = AsyncMock(
            return_value=[
                Node(node_id=NodeId(1), hostname="[IP]", ncpus=4, enabled=True),
            ],
        )
        uow.commit = AsyncMock()
        uow.collect_events = AsyncMock(return_value=[])
        uow.publish_events = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        tracker = MagicMock(spec=AllocationTracker)
        allocation_lock = asyncio.Lock()
        clouds = MagicMock(spec=CloudProvisioner)
        start_on_machine = AsyncMock(return_value=True)

        await allocate_task(
            task_id=TaskId(1),
            engines=engines,
            uow_factory=uow_factory,
            repository=repository,
            occupancy_checker=occupancy_checker,
            clouds=clouds,
            start_task_on_machine=start_on_machine,
            tracker=tracker,
            allocation_lock=allocation_lock,
            remote_tasks_dir=PurePath("/remote/tasks"),
        )

        # save was called — check the last save has a TaskAllocated event
        save_calls = uow.tasks.save.call_args_list
        last_save_task: Task = save_calls[-1][0][0]
        allocated_events = [
            e for e in last_save_task.events if isinstance(e, TaskAllocated)
        ]
        assert len(allocated_events) == 1
        assert allocated_events[0].node_id == NodeId(1)
        assert allocated_events[0].engine_name == "test_engine"
        # tracker.discard called (machine path discards immediately)
        tracker.discard.assert_called_once_with(TaskId(1))


class TestConsumeTaskEvents:
    """Verify consume_task records TaskCompleted or TaskFailed events."""

    @pytest.fixture
    def mock_output_downloader(self) -> MagicMock:
        output_downloader = MagicMock()
        output_downloader.download_outputs = AsyncMock(return_value=("", "", [], []))
        output_downloader.cleanup_remote = AsyncMock(return_value=None)
        return output_downloader

    async def _run_consume(
        self,
        session: Any,
        output_downloader: MagicMock,
        task: Task,
        uow_factory: Callable[[], AbstractUnitOfWork],
        engines: EngineRepository,
        local_tasks_dir: Path,
        tracker: MagicMock,
    ) -> None:
        await consume_task(
            task_id=task.task_id,
            session=session,  # type: ignore[arg-type]
            output_downloader=output_downloader,
            engines=engines,
            uow_factory=uow_factory,
            local_tasks_dir=local_tasks_dir,
            tracker=tracker,
        )

    async def test_consume_success_records_task_completed_event(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
        tmp_path: Path,
    ) -> None:
        uow = mock_uow_factory.return_value
        uow.tasks.get_running = AsyncMock(return_value=running_task)
        tracker = MagicMock(spec=AllocationTracker)

        await self._run_consume(
            session=SimpleNamespace(),  # opaque to consume_task; only forwarded to operations
            output_downloader=mock_output_downloader,
            task=running_task,
            uow_factory=mock_uow_factory,
            engines=mock_engine_repo,
            local_tasks_dir=tmp_path,
            tracker=tracker,
        )

        saved_task: Task = uow.tasks.save.call_args.args[0]
        assert len(saved_task.events) == 1
        event = saved_task.events[0]
        assert isinstance(event, TaskCompleted)
        assert event.task_id == TaskId(1)
        assert event.local_folder == str(tmp_path / "20250101_120000_42")
        assert saved_task.local_folder == str(tmp_path / "20250101_120000_42")
        mock_output_downloader.cleanup_remote.assert_awaited_once()
        tracker.discard.assert_called_once_with(TaskId(1))

    async def test_consume_failure_records_task_failed_event_with_formatted_reason(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_output_downloader.download_outputs = AsyncMock(
            return_value=(
                "",
                "",
                [],
                [("/remote/file", OSError("Connection refused"))],
            ),
        )
        uow = mock_uow_factory.return_value
        uow.tasks.get_running = AsyncMock(return_value=running_task)
        tracker = MagicMock(spec=AllocationTracker)

        await self._run_consume(
            session=SimpleNamespace(),  # opaque to consume_task; only forwarded to operations
            output_downloader=mock_output_downloader,
            task=running_task,
            uow_factory=mock_uow_factory,
            engines=mock_engine_repo,
            local_tasks_dir=tmp_path,
            tracker=tracker,
        )

        saved_task: Task = uow.tasks.save.call_args.args[0]
        assert len(saved_task.events) == 1
        event = saved_task.events[0]
        assert isinstance(event, TaskFailed)
        assert event.task_id == TaskId(1)
        assert event.reason == "Download error: /remote/file: Connection refused"
        assert saved_task.state.error == event.reason
        mock_output_downloader.cleanup_remote.assert_awaited_once()
        tracker.discard.assert_called_once_with(TaskId(1))
