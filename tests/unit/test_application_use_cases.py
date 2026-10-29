"""Unit tests for application use cases.

Tests cover the 4 application use cases:
- submit_task    (yascheduler.application.submit_task)
- allocate_task  (yascheduler.application.allocate_task)
- consume_task   (yascheduler.application.consume_task)
- deallocate_nodes (yascheduler.application.deallocate_nodes)

Event recording tests (TaskCreated, TaskAllocated, TaskCompleted, TaskFailed)
are in test_application_events.py.
"""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for application use cases (submit, allocate, consume, deallocate).
# SCOPE: submit_task validation, allocate_task free/cloud/error paths (session-node pairing, allocate_to, node_id logging), deallocate_nodes disable/skip.
# KEYWORDS: submit_task, allocate_task, deallocate_nodes
# endregion MODULE_CONTRACT

import asyncio
import time
from pathlib import PurePath
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from yascheduler.application.allocate_task import (
    _cleanup_tmp_node_best_effort,
    _persist_node_with_cleanup,
    allocate_task,
)
from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.deallocate_nodes import deallocate_node, deallocate_nodes
from yascheduler.application.submit_task import submit_task
from yascheduler.application.uow import AbstractUnitOfWork
from yascheduler.domain import (
    Engine,
    EngineRepository,
    TaskInputDirectory,
    TaskInputFile,
)
from yascheduler.domain.events import TaskCreated
from yascheduler.domain.exceptions import (
    CloudAllocateError,
    InvalidTaskSnapshotError,
    MissingInputFileError,
    UnsupportedEngineError,
)
from yascheduler.domain.model import (
    NewNode,
    NewTask,
    Node,
    NodeId,
    Task,
    TaskId,
    TaskStatus,
    Todo,
    error_of,
)
from yascheduler.domain.ports import CloudProvisioner
from yascheduler.infra.cloud import ConfigCloudAzure

# =============================================================================
# submit_task  (3 tests)
# =============================================================================


class TestSubmitTask:
    """submit_task — validates engine and typed snapshot coverage, then persists."""

    async def test_submit_task_unknown_engine(
        self,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        """Engine not in repository -> UnsupportedEngineError before any write."""
        mock_engine_repo.__contains__.return_value = False

        with pytest.raises(UnsupportedEngineError) as exc_info:
            await submit_task(
                label="test",
                metadata={"inp": "data"},
                engine_name="nonexistent_engine",
                engines=mock_engine_repo,
                uow_factory=mock_uow_factory,
            )

        assert "nonexistent_engine" in str(exc_info.value)
        mock_uow_factory.assert_not_called()

    async def test_submit_task_empty_snapshot_satisfies_engine_without_declarations(
        self,
        mock_uow_factory: MagicMock,
    ) -> None:
        """An engine without input declarations accepts an empty snapshot."""
        engine = Engine(name="no_inputs", spawn="run")
        engines = EngineRepository(data={"no_inputs": engine})
        uow = mock_uow_factory.return_value
        uow.tasks.insert = AsyncMock(
            return_value=Task(
                task_id=TaskId(42),
                engine="no_inputs",
                state=Todo(),
            )
        )

        task_id = await submit_task(
            label="empty-ok",
            metadata={"unrelated": "kept"},
            engine_name="no_inputs",
            engines=engines,
            uow_factory=mock_uow_factory,
            input_snapshot=(),
        )

        assert task_id == TaskId(42)
        inserted_arg: NewTask = uow.tasks.insert.call_args[0][0]
        assert inserted_arg.input_entries == ()
        assert inserted_arg.extra == {"unrelated": "kept"}

    async def test_submit_task_empty_snapshot_cannot_use_legacy_metadata_key(
        self,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        """Typed snapshot coverage ignores legacy metadata keys entirely."""
        with pytest.raises(MissingInputFileError) as exc_info:
            await submit_task(
                label="empty-missing",
                metadata={"inp": "legacy-data"},
                engine_name="test_engine",
                engines=mock_engine_repo,
                uow_factory=mock_uow_factory,
                input_snapshot=(),
            )

        assert exc_info.value.filename == "inp"
        assert exc_info.value.engine_name == "test_engine"
        mock_uow_factory.assert_not_called()

    async def test_submit_task_success_returns_task_id(
        self,
        engine: Engine,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        """Happy path: validates snapshot, inserts, saves, commits, returns id."""
        uow = mock_uow_factory.return_value

        def _insert_side_effect(new_task: NewTask) -> Task:
            from datetime import datetime

            task = Task(
                task_id=TaskId(42),
                label=new_task.label,
                engine=new_task.engine,
                state=Todo(),
                webhook_url=new_task.webhook_url,
                webhook_custom_params=new_task.webhook_custom_params,
                extra=new_task.extra,
                created_at=datetime(2025, 1, 1),
                updated_at=datetime(2025, 1, 1),
            )
            # insert now calls materialize_task, so the returned Task has TaskCreated in events
            from yascheduler.domain.model import materialize_task

            return materialize_task(task)

        uow.tasks.insert = AsyncMock(side_effect=_insert_side_effect)
        snapshot = (TaskInputFile(path="inp", content=b"content"),)

        task_id = await submit_task(
            label="my_job",
            metadata={"inp": "content"},
            engine_name="test_engine",
            engines=mock_engine_repo,
            uow_factory=mock_uow_factory,
            input_snapshot=snapshot,
        )

        assert task_id == TaskId(42)

        # insert was called with a TO_DO task
        uow.tasks.insert.assert_called_once()
        inserted_arg: NewTask = uow.tasks.insert.call_args[0][0]
        assert inserted_arg.label == "my_job"
        assert inserted_arg.extra == {"inp": "content"}
        assert inserted_arg.engine == "test_engine"
        assert inserted_arg.input_entries == snapshot

        # save was called with the task that has TaskCreated in events
        uow.tasks.save.assert_called_once()
        saved_arg: Task = uow.tasks.save.call_args[0][0]
        assert saved_arg.task_id == TaskId(42)
        assert (
            saved_arg.state.remote_folder is None
        )  # remote_folder not set at submit time
        assert len(saved_arg.events) == 1
        assert isinstance(saved_arg.events[0], TaskCreated)

        uow.commit.assert_called_once()

    async def test_submit_task_non_empty_snapshot_is_normalized_for_new_task(
        self,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        """The persisted snapshot is materialized and sorted by path."""
        snapshot = (
            TaskInputFile(path="basis/Si.orb", content=b"orb"),
            TaskInputFile(path="inp", content=b"x"),
        )
        uow = mock_uow_factory.return_value
        uow.tasks.insert = AsyncMock(
            return_value=Task(
                task_id=TaskId(42),
                engine="test_engine",
                state=Todo(),
            )
        )

        await submit_task(
            label="normalized-snapshot",
            metadata={"unrelated": "kept"},
            engine_name="test_engine",
            engines=mock_engine_repo,
            uow_factory=mock_uow_factory,
            input_snapshot=snapshot,
        )

        inserted_arg: NewTask = uow.tasks.insert.call_args[0][0]
        assert inserted_arg.input_entries == (
            TaskInputDirectory(path="basis"),
            TaskInputFile(path="basis/Si.orb", content=b"orb"),
            TaskInputFile(path="inp", content=b"x"),
        )
        assert inserted_arg.input_entries is not snapshot

    async def test_submit_task_snapshot_strips_typed_metadata_from_extra(
        self,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        """Typed metadata fields never leak into extra."""
        uow = mock_uow_factory.return_value
        uow.tasks.insert = AsyncMock(
            return_value=Task(
                task_id=TaskId(42),
                engine="test_engine",
                state=Todo(),
            )
        )
        metadata = {
            "engine": "wrong-engine",
            "remote_folder": "remote",
            "local_folder": "local",
            "webhook_url": "https://example.invalid/hook",
            "webhook_custom_params": {"token": "redacted"},
            "error": "previous-error",
            "unrelated": 42,
        }

        await submit_task(
            label="typed-explicit",
            metadata=metadata,
            engine_name="test_engine",
            engines=mock_engine_repo,
            uow_factory=mock_uow_factory,
            input_snapshot=(TaskInputFile(path="inp", content=b"x"),),
        )

        inserted_arg: NewTask = uow.tasks.insert.call_args[0][0]
        assert inserted_arg.extra == {"unrelated": 42}
        assert inserted_arg.local_folder == "local"
        assert inserted_arg.webhook_url == "https://example.invalid/hook"
        assert inserted_arg.webhook_custom_params == {"token": "redacted"}

    async def test_submit_task_cleaned_boundary_metadata_strips_typed_fields(
        self,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        """CLIDeps-cleaned metadata still has typed fields stripped from extra."""
        uow = mock_uow_factory.return_value
        uow.tasks.insert = AsyncMock(
            return_value=Task(
                task_id=TaskId(42),
                engine="test_engine",
                state=Todo(),
            )
        )
        metadata = {
            "engine": "wrong-engine",
            "remote_folder": "remote",
            "local_folder": "local",
            "webhook_url": "https://example.invalid/hook",
            "webhook_custom_params": {"token": "redacted"},
            "error": "previous-error",
            "unrelated": 42,
        }

        await submit_task(
            label="typed-cleaned",
            metadata=metadata,
            engine_name="test_engine",
            engines=mock_engine_repo,
            uow_factory=mock_uow_factory,
            input_snapshot=(TaskInputFile(path="inp", content=b"legacy-body"),),
        )

        inserted_arg: NewTask = uow.tasks.insert.call_args[0][0]
        assert inserted_arg.extra == {"unrelated": 42}

    async def test_submit_task_glob_declaration_satisfied_by_matching_entry(
        self,
        mock_uow_factory: MagicMock,
    ) -> None:
        """A glob declaration is satisfied by a matching file and materialized parent."""
        engine = Engine(
            name="glob_engine",
            spawn="run",
            input_files=("basis/**/*.orb",),
        )
        engines = EngineRepository(data={"glob_engine": engine})
        uow = mock_uow_factory.return_value
        uow.tasks.insert = AsyncMock(
            return_value=Task(
                task_id=TaskId(42),
                engine="glob_engine",
                state=Todo(),
            )
        )

        await submit_task(
            label="glob-ok",
            metadata={"unrelated": "kept"},
            engine_name="glob_engine",
            engines=engines,
            uow_factory=mock_uow_factory,
            input_snapshot=(TaskInputFile(path="basis/a.orb", content=b"x"),),
        )

        inserted_arg: NewTask = uow.tasks.insert.call_args[0][0]
        assert inserted_arg.input_entries == (
            TaskInputDirectory(path="basis"),
            TaskInputFile(path="basis/a.orb", content=b"x"),
        )

    async def test_submit_task_glob_declaration_not_satisfied_by_directory_only(
        self,
        mock_uow_factory: MagicMock,
    ) -> None:
        """A glob declaration needs a matching entry, not just an empty parent."""
        engine = Engine(
            name="glob_engine",
            spawn="run",
            input_files=("basis/**/*.orb",),
        )
        engines = EngineRepository(data={"glob_engine": engine})

        with pytest.raises(MissingInputFileError) as exc_info:
            await submit_task(
                label="glob-missing",
                metadata={},
                engine_name="glob_engine",
                engines=engines,
                uow_factory=mock_uow_factory,
                input_snapshot=(TaskInputDirectory(path="basis"),),
            )

        assert exc_info.value.filename == "basis/**/*.orb"
        mock_uow_factory.assert_not_called()

    async def test_submit_task_literal_directory_satisfied_by_directory_entry(
        self,
        mock_uow_factory: MagicMock,
    ) -> None:
        """A literal directory declaration matches an explicit directory entry."""
        engine = Engine(name="dir_engine", spawn="run", input_files=("basis",))
        engines = EngineRepository(data={"dir_engine": engine})
        uow = mock_uow_factory.return_value
        uow.tasks.insert = AsyncMock(
            return_value=Task(
                task_id=TaskId(42),
                engine="dir_engine",
                state=Todo(),
            )
        )

        await submit_task(
            label="dir-ok",
            metadata={},
            engine_name="dir_engine",
            engines=engines,
            uow_factory=mock_uow_factory,
            input_snapshot=(TaskInputDirectory(path="basis"),),
        )

        inserted_arg: NewTask = uow.tasks.insert.call_args[0][0]
        assert inserted_arg.input_entries == (TaskInputDirectory(path="basis"),)

    async def test_submit_task_invalid_snapshot_fails_before_write(
        self,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        """Duplicate snapshot paths raise before any UoW is opened."""
        duplicate_snapshot = [
            TaskInputFile(path="inp", content=b"first"),
            TaskInputFile(path="inp", content=b"second"),
        ]

        with pytest.raises(InvalidTaskSnapshotError) as exc_info:
            await submit_task(
                label="invalid",
                metadata={},
                engine_name="test_engine",
                engines=mock_engine_repo,
                uow_factory=mock_uow_factory,
                input_snapshot=duplicate_snapshot,  # type: ignore[arg-type]
            )

        assert exc_info.value.path == "inp"
        mock_uow_factory.assert_not_called()

    async def test_submit_task_copies_snapshot_list_to_immutable_tuple(
        self,
        mock_engine_repo: MagicMock,
        mock_uow_factory: MagicMock,
    ) -> None:
        """Mutating the caller's list after submission cannot alter NewTask inputs."""
        source = [TaskInputFile(path="inp", content=b"x")]
        uow = mock_uow_factory.return_value
        uow.tasks.insert = AsyncMock(
            return_value=Task(
                task_id=TaskId(42),
                engine="test_engine",
                state=Todo(),
            )
        )

        await submit_task(
            label="copied",
            metadata={},
            engine_name="test_engine",
            engines=mock_engine_repo,
            uow_factory=mock_uow_factory,
            input_snapshot=source,  # type: ignore[arg-type]
        )

        inserted_arg: NewTask = uow.tasks.insert.call_args[0][0]
        assert isinstance(inserted_arg.input_entries, tuple)
        assert inserted_arg.input_entries == (TaskInputFile(path="inp", content=b"x"),)
        source.append(TaskInputFile(path="other", content=b"y"))
        assert inserted_arg.input_entries == (TaskInputFile(path="inp", content=b"x"),)


# =============================================================================
# allocate_task  (6 tests)
# =============================================================================


class TestAllocateTask:
    """allocate_task — match a TO_DO task to free machine or request cloud."""

    @pytest.fixture
    def todo_task(self) -> Task:
        from datetime import datetime

        return Task(
            task_id=TaskId(1),
            label="test",
            engine="test_engine",
            state=Todo(),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

    async def test_allocate_task_unsupported_engine(self, todo_task: Task) -> None:
        """Engine name not in repo -> reject via UoW, return False."""
        engines = MagicMock(spec=EngineRepository)
        engines.get.return_value = None  # engine lookup fails

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.get_todo = AsyncMock(return_value=todo_task)
        uow.tasks.save = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        uow.collect_events = AsyncMock(return_value=[])
        uow.publish_events = AsyncMock()

        tracker = MagicMock(spec=AllocationTracker)
        allocation_lock = asyncio.Lock()

        result = await allocate_task(
            task_id=todo_task.task_id,
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

        assert result is False
        uow.tasks.save.assert_called_once()
        saved_task: Task = uow.tasks.save.call_args[0][0]
        assert saved_task.status == TaskStatus.DONE
        assert error_of(saved_task) == "unsupported engine"

    async def test_allocate_task_finds_free_machine(
        self,
        todo_task: Task,
        engine: Engine,
    ) -> None:
        """[11.6a] Free machine -> allocates, returns True, discards tracker slot. select_provider NOT called."""
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

        result = await allocate_task(
            task_id=todo_task.task_id,
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

        assert result is True
        repository.list_free.assert_called_once_with(platforms=["linux"])
        start_on_machine.assert_called_once()
        _call_session, _call_engine, _call_task = start_on_machine.call_args[0]
        assert _call_session is free_session
        assert _call_engine is engine
        assert _call_task.state.allocated_node_id == NodeId(1)
        assert not hasattr(_call_task, "allocated_ip")
        occupancy_checker.start_occupancy_check.assert_called_once_with(
            free_session,
            engine,
        )
        uow.tasks.save.assert_called_once()
        saved_task: Task = uow.tasks.save.call_args[0][0]
        assert saved_task.state.allocated_node_id == NodeId(1)
        assert not hasattr(saved_task, "allocated_ip")
        assert saved_task.status == TaskStatus.RUNNING
        uow.commit.assert_called_once()
        # tracker.discard called instead of clouds.mark_task_done
        tracker.discard.assert_called_once_with(todo_task.task_id)
        # cloud path NOT reached
        clouds.select_provider.assert_not_called()

    async def test_allocate_task_cloud_fallback_happy_path(
        self,
        todo_task: Task,
        engine: Engine,
    ) -> None:
        """[11.6b] No free machine -> select_provider, insert tmp-node, allocate, final persist."""
        engines = MagicMock(spec=EngineRepository)
        engines.get.return_value = engine

        repository = MagicMock()
        repository.list_free = MagicMock(return_value=[])  # No free machines
        occupancy_checker = MagicMock()

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.get_todo = AsyncMock(return_value=todo_task)
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.nodes = AsyncMock()
        uow.nodes.list_all = AsyncMock(return_value=[])
        tmp_node = Node(
            node_id=NodeId(2),
            hostname="",
            ncpus=None,
            enabled=False,
            cloud="aws",
        )
        uow.nodes.insert = AsyncMock(return_value=tmp_node)
        uow.nodes.update = AsyncMock()
        uow.nodes.remove = AsyncMock()
        uow.collect_events = AsyncMock(return_value=[])
        uow.publish_events = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        tracker = MagicMock(spec=AllocationTracker)
        tracker.add.return_value = True  # Not already tracked

        allocation_lock = asyncio.Lock()

        clouds = MagicMock(spec=CloudProvisioner)
        clouds.select_provider.return_value = "aws"
        cloud_node = Node(
            node_id=NodeId(1),
            hostname="[IP]",
            ncpus=4,
            cloud="aws",
            enabled=True,
            username="root",
            port=22,
        )
        clouds.allocate = AsyncMock(return_value=cloud_node)

        start_on_machine = AsyncMock()

        result = await allocate_task(
            task_id=todo_task.task_id,
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

        assert result is False  # Cloud path returns False

        # tracker.add called
        tracker.add.assert_called_once_with(todo_task.task_id)
        # tracker.set_node patches the task-to-node link after tmp-node insert
        tracker.set_node.assert_called_once_with(todo_task.task_id, NodeId(2))

        # select_provider called with platforms and current counts
        clouds.select_provider.assert_called_once_with(["linux"], {})

        # tmp node inserted via insert(NewNode(cloud=..., enabled=False)) before cloud allocate
        uow.nodes.insert.assert_any_call(NewNode(cloud="aws", enabled=False))

        # cloud allocate called with provider name + tmp Node
        clouds.allocate.assert_called_once_with("aws", tmp_node)

        # final persist: update(node) — no insert, no remove on success
        uow.nodes.update.assert_called_once_with(cloud_node)
        uow.nodes.remove.assert_not_called()
        uow.nodes.get.assert_not_called()

        # commit called at least once (tmp insert + final persist are separate UoWs)
        assert uow.commit.call_count >= 2

        # tracker.discard NOT called on happy cloud path (deferred to consume)
        tracker.discard.assert_not_called()

    async def test_allocate_task_cloud_fallback_failure_cleanup(
        self,
        todo_task: Task,
        engine: Engine,
    ) -> None:
        """[11.6c] Cloud allocate fails -> tmp-node removed by node_id, tracker discarded, error re-raised."""
        engines = MagicMock(spec=EngineRepository)
        engines.get.return_value = engine

        repository = MagicMock()
        repository.list_free = MagicMock(return_value=[])
        occupancy_checker = MagicMock()

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.get_todo = AsyncMock(return_value=todo_task)
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.nodes = AsyncMock()
        uow.nodes.list_all = AsyncMock(return_value=[])
        tmp_node = Node(
            node_id=NodeId(2),
            hostname="",
            ncpus=None,
            enabled=False,
            cloud="aws",
        )
        uow.nodes.insert = AsyncMock(return_value=tmp_node)
        uow.nodes.remove = AsyncMock()
        uow.collect_events = AsyncMock(return_value=[])
        uow.publish_events = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        tracker = MagicMock(spec=AllocationTracker)
        tracker.add.return_value = True

        allocation_lock = asyncio.Lock()

        clouds = MagicMock(spec=CloudProvisioner)
        clouds.select_provider.return_value = "aws"
        clouds.allocate = AsyncMock(side_effect=CloudAllocateError("VM create failed"))

        start_on_machine = AsyncMock()

        with pytest.raises(CloudAllocateError):
            await allocate_task(
                task_id=todo_task.task_id,
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

        # tmp-node inserted via insert(NewNode(cloud=..., enabled=False))
        uow.nodes.insert.assert_any_call(NewNode(cloud="aws", enabled=False))

        # tmp-node removed by node_id directly in cleanup UoW (no get lookup)
        uow.nodes.remove.assert_any_call(NodeId(2))
        uow.nodes.get.assert_not_called()

        # commit called for tmp-insert and cleanup
        assert uow.commit.call_count >= 2

        # tracker.discard called after cleanup
        tracker.discard.assert_called_once_with(todo_task.task_id)

        # cloud node was NOT added (failed before final persist)
        uow.nodes.insert.assert_any_call(NewNode(cloud="aws", enabled=False))

    async def test_allocate_task_dedup_returns_false(
        self,
        todo_task: Task,
        engine: Engine,
    ) -> None:
        """[11.6d] tracker.add returns False -> returns early, no cloud ops."""
        engines = MagicMock(spec=EngineRepository)
        engines.get.return_value = engine

        repository = MagicMock()
        repository.list_free = MagicMock(return_value=[])
        occupancy_checker = MagicMock()

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.get_todo = AsyncMock(return_value=todo_task)
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.collect_events = AsyncMock(return_value=[])
        uow.publish_events = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        tracker = MagicMock(spec=AllocationTracker)
        tracker.add.return_value = False  # Already in-flight

        allocation_lock = asyncio.Lock()
        clouds = MagicMock(spec=CloudProvisioner)
        start_on_machine = AsyncMock()

        result = await allocate_task(
            task_id=todo_task.task_id,
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

        assert result is False

        # select_provider NOT called
        clouds.select_provider.assert_not_called()
        # allocate NOT called
        clouds.allocate.assert_not_called()
        # no DB writes
        uow.tasks.save.assert_not_called()
        uow.commit.assert_not_called()
        # tracker.discard NOT called (no slot was taken)
        tracker.discard.assert_not_called()

    async def test_allocate_task_throttle_returns_none(
        self,
        todo_task: Task,
        engine: Engine,
    ) -> None:
        """[11.6e] select_provider returns None -> tracker.discard, returns False, no tmp-node."""
        engines = MagicMock(spec=EngineRepository)
        engines.get.return_value = engine

        repository = MagicMock()
        repository.list_free = MagicMock(return_value=[])
        occupancy_checker = MagicMock()

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.get_todo = AsyncMock(return_value=todo_task)
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.nodes = AsyncMock()
        uow.nodes.list_all = AsyncMock(return_value=[])
        uow.collect_events = AsyncMock(return_value=[])
        uow.publish_events = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        tracker = MagicMock(spec=AllocationTracker)
        tracker.add.return_value = True

        allocation_lock = asyncio.Lock()
        clouds = MagicMock(spec=CloudProvisioner)
        clouds.select_provider.return_value = None  # Throttle/no capacity
        start_on_machine = AsyncMock()

        result = await allocate_task(
            task_id=todo_task.task_id,
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

        assert result is False

        # select_provider was called
        clouds.select_provider.assert_called_once()
        # allocate NOT called
        clouds.allocate.assert_not_called()
        # NO tmp-node insertion
        uow.nodes.insert.assert_not_called()
        # tracker.discard called (releases the slot since no provider)
        tracker.discard.assert_called_once_with(todo_task.task_id)


# =============================================================================
# tmp-node cleanup by NodeId (remove-tmp-node-fake-ip)
# =============================================================================


class TestTmpCleanupByNodeId:
    """tmp-cleanup paths call uow.nodes.remove(tmp_node_id) directly (no get lookup)."""

    async def test_cleanup_best_effort_removes_by_node_id_directly(self) -> None:
        """[9.1] _cleanup_tmp_node_best_effort: remove(tmp_node_id) directly, no get lookup."""
        uow = AsyncMock()
        uow.nodes.remove = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        await _cleanup_tmp_node_best_effort(uow_factory, NodeId(7), TaskId(1), "ctx")

        uow.nodes.remove.assert_awaited_once_with(NodeId(7))
        uow.commit.assert_awaited_once()
        # No get lookup — the NodeId is in hand from insert's RETURNING node_id.
        uow.nodes.get.assert_not_called()

    async def test_cleanup_best_effort_idempotent_on_0_row_remove(self) -> None:
        """[9.2] _cleanup_tmp_node_best_effort: a 0-row remove is a no-op (no error, commit still runs)."""
        uow = AsyncMock()
        # remove affects 0 rows — but the repo does NOT raise on 0 rows
        # (DELETE WHERE node_id = :node_id is a no-op); just completes.
        uow.nodes.remove = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        # Must not raise — idempotent cleanup.
        await _cleanup_tmp_node_best_effort(uow_factory, NodeId(999), TaskId(1), "ctx")

        uow.nodes.remove.assert_awaited_once_with(NodeId(999))
        uow.commit.assert_awaited_once()
        uow.nodes.get.assert_not_called()

    async def test_persist_with_cleanup_success_removes_tmp_by_node_id(self) -> None:
        """[9.3] _persist_node_with_cleanup success: update(node) + commit (no get lookup, no remove)."""
        cloud_node = Node(
            node_id=NodeId(7),
            hostname="10.0.0.100",
            ncpus=4,
            cloud="aws",
            enabled=True,
            username="root",
            port=22,
        )
        uow = AsyncMock()
        uow.nodes.update = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        clouds = MagicMock(spec=CloudProvisioner)

        await _persist_node_with_cleanup(
            node=cloud_node,
            clouds=clouds,
            uow_factory=uow_factory,
            tmp_node_id=NodeId(7),
            task_id=TaskId(1),
        )

        uow.nodes.update.assert_awaited_once_with(cloud_node)
        uow.commit.assert_awaited_once()
        uow.nodes.get.assert_not_called()
        uow.nodes.remove.assert_not_called()
        clouds.deallocate.assert_not_called()  # success path, no cleanup


# =============================================================================
# deallocate_nodes  (4 tests)
# =============================================================================


class TestDeallocateNodes:
    """deallocate_nodes — disable idle cloud nodes, return Node objects for VM deletion."""

    async def test_deallocate_nodes_disables_idle_cloud_nodes(self) -> None:
        """Enabled cloud node idle beyond tolerance -> disable_node called, Node returned."""
        # An enabled Azure node
        az_node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, cloud="az")

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.nodes = AsyncMock()
        uow.nodes.list_enabled = AsyncMock(return_value=[az_node])
        uow.nodes.list_disabled = AsyncMock(return_value=[az_node])
        uow.nodes.disable = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        config_clouds = [
            MagicMock(spec=ConfigCloudAzure, prefix="az", idle_tolerance=300),
        ]

        # free_since (monotonic) is well beyond tolerance so the node qualifies
        idle_machines = {NodeId(1): time.monotonic() - 3600}

        result = await deallocate_nodes(
            uow_factory=uow_factory,
            config_clouds=config_clouds,
            idle_machines=idle_machines,
        )

        # First phase: node should be disabled (by node_id)
        uow.nodes.disable.assert_called_once_with(NodeId(1))
        uow.commit.assert_called()

        # Second phase: disabled Node qualifies (has cloud, valid ip) -> returned
        assert isinstance(result, list)
        assert any(
            isinstance(n, Node) and n.node_id == NodeId(1) and n.hostname == "10.0.0.1"
            for n in result
        )

    async def test_deallocate_nodes_skips_disabling_static_but_returns_for_reap(
        self,
    ) -> None:
        """Static node (cloud=None) is NOT disabled by idle-tolerance (cloud-only phase), but IS returned for row removal — the drain teardown is not cloud-specific.

        Pins the reviewer fix: a static node disabled by --remove-soft must be
        reaped by the collect phase once its task finishes, otherwise it sits
        disabled in the DB indefinitely.
        """
        local_node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, cloud=None)

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.nodes = AsyncMock()
        uow.nodes.list_enabled = AsyncMock(return_value=[local_node])
        uow.nodes.list_disabled = AsyncMock(return_value=[local_node])
        uow.nodes.disable = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        config_clouds = [
            MagicMock(spec=ConfigCloudAzure, prefix="az", idle_tolerance=300),
        ]

        idle_machines = {NodeId(1): time.monotonic() - 3600}

        result = await deallocate_nodes(
            uow_factory=uow_factory,
            config_clouds=config_clouds,
            idle_machines=idle_machines,
        )

        # Disable phase is cloud-only: node.cloud=None != "az" -> NOT disabled.
        uow.nodes.disable.assert_not_called()
        # Collect phase returns it for teardown (row removal) regardless of cloud.
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].node_id == NodeId(1)
        assert result[0].cloud is None

    async def test_deallocate_nodes_returns_node_objects(self) -> None:
        """Return type is list[Node]; each element carries node_id, ip, cloud (proves D1)."""
        cloud_node = Node(node_id=NodeId(2), hostname="10.0.0.2", ncpus=2, cloud="aws")

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.nodes = AsyncMock()
        uow.nodes.list_enabled = AsyncMock(return_value=[cloud_node])
        uow.nodes.list_disabled = AsyncMock(return_value=[cloud_node])
        uow.nodes.disable = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        config_clouds = [
            MagicMock(spec=ConfigCloudAzure, prefix="aws", idle_tolerance=300),
        ]
        idle_machines = {NodeId(2): time.monotonic() - 3600}

        result = await deallocate_nodes(
            uow_factory=uow_factory,
            config_clouds=config_clouds,
            idle_machines=idle_machines,
        )

        assert isinstance(result, list)
        assert len(result) == 1
        only = result[0]
        assert isinstance(only, Node)
        assert only.node_id == NodeId(2)
        assert only.hostname == "10.0.0.2"
        assert only.cloud == "aws"

    async def test_deallocate_nodes_no_dot_filter(self) -> None:
        """Phase 2 filter is `node.hostname not in busy_ips and node.cloud` — no `.` guard (proves D4).

        A disabled cloud node with a valid ipv4 ip (dots present) is returned; the
        old `and "." in node.hostname` guard would also have passed it, but this test
        pins the filter shape so a future regression reintroducing the guard fails.
        """
        cloud_node = Node(node_id=NodeId(3), hostname="10.0.0.3", ncpus=2, cloud="aws")

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.nodes = AsyncMock()
        uow.nodes.list_enabled = AsyncMock(return_value=[])
        uow.nodes.list_disabled = AsyncMock(return_value=[cloud_node])
        uow.nodes.disable = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        config_clouds = [
            MagicMock(spec=ConfigCloudAzure, prefix="aws", idle_tolerance=300),
        ]
        idle_machines: dict[NodeId, float] = {}

        result = await deallocate_nodes(
            uow_factory=uow_factory,
            config_clouds=config_clouds,
            idle_machines=idle_machines,
        )

        assert isinstance(result, list)
        assert any(
            isinstance(n, Node) and n.node_id == NodeId(3) and n.hostname == "10.0.0.3"
            for n in result
        )


# =============================================================================
# deallocate_node bracketing (3 tests)
# =============================================================================


class TestDeallocateNodeBracketing:
    """deallocate_node — disable+remove bracketing around cloud delete."""

    async def test_bracketing_order_disable_cloud_delete_remove(self) -> None:
        """[12.4] disable+commit -> cloud deallocate -> remove+commit ordering."""
        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, cloud="aws")
        gateway = MagicMock()
        gateway.get_session.return_value = None  # not connected -> BUSY re-check no-op
        gateway.contains.return_value = False  # Skip disconnect

        calls: list[str] = []

        uow = AsyncMock()
        uow.nodes.disable = AsyncMock(
            side_effect=lambda _node_id: calls.append("disable"),
        )
        uow.nodes.remove = AsyncMock(
            side_effect=lambda _node_id: calls.append("remove"),
        )
        uow.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        clouds = MagicMock(spec=CloudProvisioner)
        clouds.deallocate = AsyncMock(
            side_effect=lambda _node: calls.append("deallocate"),
        )

        await deallocate_node(
            node=node,
            repository=gateway,
            clouds=clouds,
            uow_factory=uow_factory,
        )

        assert calls == ["disable", "commit", "deallocate", "remove", "commit"]

    async def test_static_node_disconnects_and_removes_without_cloud_delete(
        self,
    ) -> None:
        """Static node (cloud=None) reaped: SSH disconnect + row remove, but NO disable and NO cloud VM deletion.

        The reviewer fix: a static node disabled by --remove-soft must have
        its DB row removed (and SSH session disconnected) once its task
        finishes — not sit disabled indefinitely. Cloud-only steps are skipped.
        """
        node = Node(node_id=NodeId(2), hostname="10.0.0.2", ncpus=4, cloud=None)
        gateway = MagicMock()
        gateway.get_session.return_value = None  # not connected -> BUSY re-check no-op
        gateway.contains.return_value = True  # connected -> disconnect runs
        gateway.disconnect = AsyncMock(
            side_effect=lambda _nid: calls.append("disconnect")
        )

        calls: list[str] = []

        uow = AsyncMock()
        uow.nodes.disable = AsyncMock(
            side_effect=lambda _node_id: calls.append("disable"),
        )
        uow.nodes.remove = AsyncMock(
            side_effect=lambda _node_id: calls.append("remove"),
        )
        uow.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        clouds = MagicMock(spec=CloudProvisioner)
        clouds.deallocate = AsyncMock(
            side_effect=lambda _node: calls.append("deallocate"),
        )

        await deallocate_node(
            node=node,
            repository=gateway,
            clouds=clouds,
            uow_factory=uow_factory,
        )

        # SSH disconnected, row removed and committed.
        gateway.disconnect.assert_awaited_once_with(NodeId(2))
        uow.nodes.remove.assert_awaited_once_with(NodeId(2))
        assert calls == ["disconnect", "remove", "commit"]
        # Cloud-only steps NOT taken for a static node.
        uow.nodes.disable.assert_not_called()
        clouds.deallocate.assert_not_called()

    async def test_failure_in_cloud_delete_leaves_node_disabled(self) -> None:
        """[12.4] clouds.deallocate raises -> node stays disabled, remove NOT called."""
        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, cloud="aws")
        gateway = MagicMock()
        gateway.get_session.return_value = None  # not connected -> BUSY re-check no-op
        gateway.contains.return_value = False

        calls: list[str] = []

        uow = AsyncMock()
        uow.nodes.disable = AsyncMock(
            side_effect=lambda _node_id: calls.append("disable"),
        )
        uow.nodes.remove = AsyncMock(
            side_effect=lambda _node_id: calls.append("remove"),
        )
        uow.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        clouds = MagicMock(spec=CloudProvisioner)
        clouds.deallocate = AsyncMock(
            side_effect=CloudAllocateError("VM deletion failed"),
        )

        with pytest.raises(CloudAllocateError):
            await deallocate_node(
                node=node,
                repository=gateway,
                clouds=clouds,
                uow_factory=uow_factory,
            )

        # disable was called and committed
        assert "disable" in calls
        assert "commit" in calls
        # remove was NOT called (exception before it)
        assert "remove" not in calls
        assert calls == ["disable", "commit"]

    async def test_remove_failure_after_cloud_delete_is_logged_not_raised(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """[review-hardening] remove UoW fails after clouds.deallocate succeeded -> exception swallowed, REMOVE_FAILED logged; cloud VM is gone so worker stays alive for reconciliation."""
        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, cloud="aws")
        gateway = MagicMock()
        gateway.get_session.return_value = None  # not connected -> BUSY re-check no-op
        gateway.contains.return_value = False

        uow = AsyncMock()
        uow.nodes.disable = AsyncMock()
        uow.nodes.remove = AsyncMock()
        # First commit (disable) succeeds; second commit (remove) fails.
        uow.commit = AsyncMock(side_effect=[None, RuntimeError("remove db lost")])
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        clouds = MagicMock(spec=CloudProvisioner)
        clouds.deallocate = AsyncMock()

        with caplog.at_level(
            "ERROR",
            logger="yascheduler.application.deallocate_nodes",
        ):
            # Must not raise — the cloud VM is already gone.
            await deallocate_node(
                node=node,
                repository=gateway,
                clouds=clouds,
                uow_factory=uow_factory,
            )

        # Cloud delete happened; disable happened; remove attempted.
        clouds.deallocate.assert_awaited_once_with(node)
        uow.nodes.disable.assert_awaited_once_with(NodeId(1))
        uow.nodes.remove.assert_awaited_once_with(NodeId(1))
        # Reconciliation narrative logged (plain narrative, no grace marker).
        assert any(
            "node remove failed" in r.message and "10.0.0.1" in r.message
            for r in caplog.records
        )

    async def test_busy_machine_at_entry_skips_teardown(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """[C1] in-flight allocator occupied the slot after deallocate_nodes snapshotted busy_node_ids -> teardown skipped entirely so the live task is not lost; node stays disabled+connected for the consume loop, reaped by a later cycle."""
        from yascheduler.domain.model import MachineState

        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, cloud="aws")

        busy_session = MagicMock()
        busy_session.machine.state = MachineState.BUSY

        gateway = MagicMock()
        gateway.get_session.return_value = busy_session

        uow = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        clouds = MagicMock(spec=CloudProvisioner)
        clouds.deallocate = AsyncMock()

        with caplog.at_level(
            "DEBUG",
            logger="yascheduler.application.deallocate_nodes",
        ):
            await deallocate_node(
                node=node,
                repository=gateway,
                clouds=clouds,
                uow_factory=uow_factory,
            )

        # Nothing destructive ran: no SSH disconnect, no DB disable, no VM delete, no remove.
        gateway.disconnect.assert_not_called()
        uow.nodes.disable.assert_not_called()
        clouds.deallocate.assert_not_called()
        uow.nodes.remove.assert_not_called()
        rec = next(r for r in caplog.records if r.getMessage() == "SKIP_BUSY")
        assert getattr(rec, "node_id", None) == NodeId(1)
        assert getattr(rec, "hostname", None) == "10.0.0.1"
