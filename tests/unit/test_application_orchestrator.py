"""Unit tests for Orchestrator lifecycle management.

Tests cover:
- Constructor creates 4 UniqueQueues with correct names
- Constructor stores allocation_tracker, active_clouds, allocation_lock
- Start lifecycle creates all 5 background tasks
- Stop lifecycle cancels tasks, calls cleanup on clouds/remote_machines/http
- Cancellation propagation to producer-consumer loops
- Concurrency limits (allocate_limit, deallocate_limit) passed through to _create_producer_consumers
- _clouds_get_capacity inline UoW arithmetic over active_clouds
- _deallocator_consumer calls deallocate_node with uow_factory
- TaskAbandoned event recorded when machine is gone
"""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for Orchestrator lifecycle management after v2.0.0 extraction.
# SCOPE: Constructor queue initialization, start/stop lifecycle, cancellation propagation, concurrency limits, _clouds_get_capacity inline UoW arithmetic, constructor stores allocation_tracker/active_clouds/allocation_lock, _deallocator_consumer passes uow_factory to deallocate_node, _allocator_consumer swallows exceptions, _task_consumer_consumer conditional _occupancy_started discard on consume_task bool, in-flight _consuming guard.
# KEYWORDS: Orchestrator, lifecycle, concurrency limits, cancellation
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.orchestrator import Orchestrator
from yascheduler.application.queue import UMessage, UniqueQueue
from yascheduler.domain import Engine, EngineRepository, LocalSettings, RemoteDefaults
from yascheduler.domain.events import TaskAbandoned
from yascheduler.domain.model import (
    AnyTask,
    Done,
    NodeId,
    Running,
    Task,
    TaskId,
    TaskStatus,
    Todo,
    TodoTask,
    error_of,
)
from yascheduler.entrypoints import Config
from yascheduler.infra.persistence import PostgresDbConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from yascheduler.application.uow import AbstractUnitOfWork

# =============================================================================
# Helpers
# =============================================================================


def _make_task(**overrides: Any) -> AnyTask:
    """Build a Task with default typed fields; overrides win.

    Translates legacy ``status``/``allocated_node_id``/``remote_folder``/``error``
    overrides into a ``state`` value object so callers can keep using the old
    kwargs.
    """
    state_overrides = {
        k: overrides.pop(k)
        for k in ("status", "allocated_node_id", "remote_folder", "error")
        if k in overrides
    }
    local_folder = overrides.pop("local_folder", None)
    # local_folder is a status-independent Task field (D6); pass it through to
    # the Task constructor via overrides, not into the Done state object.
    if local_folder is not None:
        overrides["local_folder"] = local_folder
    if state_overrides:
        status = state_overrides.get("status", TaskStatus.TO_DO)
        if status is TaskStatus.RUNNING:
            overrides["state"] = Running(
                allocated_node_id=state_overrides.get("allocated_node_id") or NodeId(1),
                remote_folder=state_overrides.get("remote_folder") or "/r",
            )
        elif status is TaskStatus.DONE:
            overrides["state"] = Done(
                error=state_overrides.get("error"),
                allocated_node_id=state_overrides.get("allocated_node_id"),
                remote_folder=state_overrides.get("remote_folder"),
            )
        else:
            overrides["state"] = Todo(
                remote_folder=state_overrides.get("remote_folder")
            )
    base: dict[str, Any] = {
        "task_id": TaskId(1),
        "engine": "test_engine",
        "created_at": datetime(2025, 1, 1),
        "updated_at": datetime(2025, 1, 1),
        "label": "test",
        "webhook_url": None,
        "webhook_custom_params": {},
        "extra": {},
        "state": Todo(),
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type,type-var]


def create_mock_config(
    allocate_limit: int = 3,
    consume_limit: int = 2,
    conn_machine_limit: int = 1,
    deallocate_limit: int = 1,
) -> MagicMock:
    """Create a mocked Config with local/remote sub-configs."""
    local = MagicMock(spec=LocalSettings)
    local.conn_machine_pending = 10
    local.allocate_pending = 5
    local.consume_pending = 3
    local.deallocate_pending = 2
    local.webhook_reqs_limit = 5
    local.conn_machine_limit = conn_machine_limit
    local.allocate_limit = allocate_limit
    local.consume_limit = consume_limit
    local.deallocate_limit = deallocate_limit

    remote = MagicMock(spec=RemoteDefaults)
    remote.tasks_dir = PurePosixPath("/tmp/tasks")
    remote.data_dir = PurePosixPath("/tmp/data")
    remote.engines_dir = PurePosixPath("/tmp/engines")
    remote.username = "root"

    config = MagicMock(spec=Config)
    config.local = local
    config.remote = remote
    config.clouds = []
    config.db = MagicMock(spec=PostgresDbConfig)

    return config


def make_orchestrator(
    allocate_limit: int = 3,
    consume_limit: int = 2,
    conn_machine_limit: int = 1,
    deallocate_limit: int = 1,
    sleep_interval: int = 0,
    allocation_tracker: AllocationTracker | None = None,
    active_clouds: list | None = None,
    allocation_lock: asyncio.Lock | None = None,
) -> Orchestrator:
    """Create an Orchestrator with all dependencies mocked.

    Uses a real Engine with sleep_interval=0 so asleep_until calls
    return immediately, keeping tests fast.
    """
    config = create_mock_config(
        allocate_limit=allocate_limit,
        consume_limit=consume_limit,
        conn_machine_limit=conn_machine_limit,
        deallocate_limit=deallocate_limit,
    )

    mock_uow = AsyncMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

    def uow_factory() -> AbstractUnitOfWork:
        return mock_uow

    clouds = AsyncMock()
    repository = MagicMock()
    repository.__len__ = MagicMock(return_value=1)
    repository.disconnect_all = AsyncMock()
    task_deployer = MagicMock()
    output_downloader = MagicMock()
    occupancy_checker = MagicMock()

    # EngineRepository.values() must yield at least one Engine so
    # Orchestrator.__init__ can compute min(sleep_interval).
    engine = MagicMock(spec=Engine, sleep_interval=sleep_interval)
    engine.name = "test_engine"
    engines = MagicMock(spec=EngineRepository)
    engines.values.return_value = [engine]

    if allocation_tracker is None:
        allocation_tracker = AllocationTracker()
    if active_clouds is None:
        active_clouds = []
    if allocation_lock is None:
        allocation_lock = asyncio.Lock()

    return Orchestrator(
        local_settings=config.local,
        remote_defaults=config.remote,
        uow_factory=uow_factory,
        clouds=clouds,
        repository=repository,
        task_deployer=task_deployer,
        output_downloader=output_downloader,
        occupancy_checker=occupancy_checker,
        engines=engines,
        config_clouds=[],
        local_tasks_dir=Path("/tmp"),
        allocation_tracker=allocation_tracker,
        active_clouds=active_clouds,
        allocation_lock=allocation_lock,
        list_private_keys_fn=lambda _keys_dir: [],
    )


# =============================================================================
# Tests
# =============================================================================


class TestOrchestratorLifecycle:
    """Orchestrator lifecycle: constructor, start, stop, cancellation, limits."""

    # ------------------------------------------------------------------ #
    # Test 1: constructor creates 4 queues
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_orchestrator_constructor_initializes_queues(self) -> None:
        """Orchestrator __init__ creates 4 UniqueQueues with correct names."""
        orch = make_orchestrator()
        assert orch._conn_machine_q.name == "conn_machine"
        assert orch._allocate_q.name == "allocate"
        assert orch._consume_q.name == "consume"
        assert orch._deallocate_q.name == "deallocate"

    # ------------------------------------------------------------------ #
    # Test 2: start creates all background tasks
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_start_creates_background_tasks(self) -> None:
        """start() creates background tasks for stats, all loops, AND their workers.

        Since fix-orchestrator-producer-silent-death, _create_producer_consumers
        registers worker tasks in self._bg_jobs (in addition to the parent
        coroutine that start() adds). With default limits (conn_machine=1,
        allocate=3, consume=2, deallocate=1) the total is 5 parents + 7 workers = 12.
        """
        orch = make_orchestrator()

        # Pre-set cancellation so loops exit immediately instead of
        # blocking on asleep_until or waiting for queue items.
        orch._cancellation_event.set()
        # wait_some_machines needs at least one connected machine to
        # exit instantly instead of waiting up to 30 s.
        orch._repository.__len__ = MagicMock(return_value=1)  # type: ignore[method-assign]

        with patch(
            "yascheduler.application.orchestrator.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await orch.start()

        # 5 parent coroutines (stats + 4 producer-consumer loops) + 7 workers
        # (conn_machine_limit=1 + allocate_limit=3 + consume_limit=2 + deallocate_limit=1).
        assert len(orch._bg_jobs) == 12

    # ------------------------------------------------------------------ #
    # Test 3: stop cancels tasks and calls cleanup
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks_and_cleans_up(self) -> None:
        """stop() sets cancellation_event, cancels tasks, cleans up resources."""
        orch = make_orchestrator()

        # Create real asyncio Tasks that never complete on their own.
        async def _never_end() -> None:
            await asyncio.Event().wait()

        task1 = asyncio.create_task(_never_end())
        task2 = asyncio.create_task(_never_end())
        orch._bg_jobs = {task1, task2}

        await orch.stop()

        # Cancellation event signalled
        assert orch._cancellation_event.is_set()

        # All background tasks cancelled
        assert task1.cancelled()
        assert task2.cancelled()

        # Cleanup methods called
        orch._clouds.stop.assert_called_once()  # type: ignore[attr-defined]
        orch._repository.disconnect_all.assert_called_once()  # type: ignore[attr-defined]
        # _http lifecycle owned by start()'s finally block, not stop()

    # ------------------------------------------------------------------ #
    # Test 4: cancellation propagates to the producer-consumer loop
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_cancellation_propagates_to_producer_consumer_loop(self) -> None:
        """cancellation_event set before loop start causes immediate exit.

        The producer-consumer loop checks ``self._cancellation_event.is_set()``
        at the top of its while-condition.  When pre-set, the loop exits
        without processing any items.
        """
        orch = make_orchestrator(sleep_interval=0)

        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        async def empty_producer() -> AsyncGenerator[None, None]:
            """Yields nothing — a no-op async generator."""
            return
            yield  # pragma: no cover  # type: ignore[unreachable]

        consumer = AsyncMock()

        # Pre-set cancellation so the while-condition fails immediately
        orch._cancellation_event.set()

        await orch._create_producer_consumers(
            q,
            empty_producer,
            consumer,
            workers_num=2,
        )

        # No items were ever produced since the loop never entered its body
        consumer.assert_not_called()
        assert q.empty()

    # ------------------------------------------------------------------ #
    # Test 5: concurrency limits (allocate_limit) respected
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_concurrency_limits_allocate_limit(self) -> None:
        """allocate_limit config value is passed as workers_num to _create_producer_consumers."""
        orch = make_orchestrator(allocate_limit=3)

        orch._cancellation_event.set()
        orch._repository.__len__ = MagicMock(return_value=1)  # type: ignore[method-assign]

        with (
            patch.object(
                orch,
                "_create_producer_consumers",
                wraps=orch._create_producer_consumers,
            ) as mock_pc,
            patch(
                "yascheduler.application.orchestrator.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await orch.start()

        # _create_producer_consumers is called 4 times (conn_machine / allocate / consume / deallocate)
        assert mock_pc.call_count == 4

        # Second call is for the allocate queue
        allocate_call = mock_pc.call_args_list[1]
        assert allocate_call.kwargs["queue"] is orch._allocate_q
        assert allocate_call.kwargs["workers_num"] == 3

    # ------------------------------------------------------------------ #
    # Test 6: concurrency limits (deallocate_limit) respected
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_concurrency_limits_deallocate_limit(self) -> None:
        """deallocate_limit config value is passed as workers_num to _create_producer_consumers."""
        orch = make_orchestrator(deallocate_limit=5)

        orch._cancellation_event.set()
        orch._repository.__len__ = MagicMock(return_value=1)  # type: ignore[method-assign]

        with (
            patch.object(
                orch,
                "_create_producer_consumers",
                wraps=orch._create_producer_consumers,
            ) as mock_pc,
            patch(
                "yascheduler.application.orchestrator.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await orch.start()

        # Fourth call is for the deallocate queue
        deallocate_call = mock_pc.call_args_list[3]
        assert deallocate_call.kwargs["queue"] is orch._deallocate_q
        assert deallocate_call.kwargs["workers_num"] == 5

    # ------------------------------------------------------------------ #
    # Test 7: concurrency limits (conn_machine_limit) respected
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_concurrency_limits_conn_machine_limit(self) -> None:
        """conn_machine_limit config value is passed as workers_num to _create_producer_consumers."""
        orch = make_orchestrator(conn_machine_limit=4)

        orch._cancellation_event.set()
        orch._repository.__len__ = MagicMock(return_value=1)  # type: ignore[method-assign]

        with (
            patch.object(
                orch,
                "_create_producer_consumers",
                wraps=orch._create_producer_consumers,
            ) as mock_pc,
            patch(
                "yascheduler.application.orchestrator.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await orch.start()

        # First call is for the conn_machine queue
        conn_machine_call = mock_pc.call_args_list[0]
        assert conn_machine_call.kwargs["queue"] is orch._conn_machine_q
        assert conn_machine_call.kwargs["workers_num"] == 4

    # ------------------------------------------------------------------ #
    # Test 8: concurrency limits (consume_limit) respected
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_concurrency_limits_consume_limit(self) -> None:
        """consume_limit config value is passed as workers_num to _create_producer_consumers."""
        orch = make_orchestrator(consume_limit=7)

        orch._cancellation_event.set()
        orch._repository.__len__ = MagicMock(return_value=1)  # type: ignore[method-assign]

        with (
            patch.object(
                orch,
                "_create_producer_consumers",
                wraps=orch._create_producer_consumers,
            ) as mock_pc,
            patch(
                "yascheduler.application.orchestrator.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await orch.start()

        # Third call is for the consume queue
        consume_call = mock_pc.call_args_list[2]
        assert consume_call.kwargs["queue"] is orch._consume_q
        assert consume_call.kwargs["workers_num"] == 7


class TestOrchestratorTaskAbandoned:
    """Test that TaskAbandoned event is recorded when machine is gone."""

    async def test_machine_gone_records_task_abandoned_event(self) -> None:
        """When a machine is gone for > broken_tasks_passes cycles, TaskAbandoned is recorded and tracker slot released."""
        from yascheduler.application.queue import UMessage

        tracker = MagicMock(spec=AllocationTracker)
        orch = make_orchestrator(allocation_tracker=tracker)

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.save = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        orch._uow_factory = uow_factory  # type: ignore[method-assign]

        # Gateway returns None for machine state (machine gone)
        orch._repository.get_session = MagicMock(return_value=None)  # type: ignore[method-assign]

        task = _make_task(
            task_id=TaskId(42),
            webhook_url="https://hook.example.com",
            webhook_custom_params={"k": "v"},
            status=TaskStatus.RUNNING,
            allocated_node_id=NodeId(42),
        )
        msg = UMessage(TaskId(42), task)
        machine_not_found: Counter[str] = Counter()

        # First call: counter is 1, not enough yet
        await orch._task_consumer_consumer(
            cast("UMessage[TaskId, Task[Running]]", msg), machine_not_found
        )
        assert machine_not_found[TaskId(42)] == 1  # type: ignore[index]
        uow.tasks.save.assert_not_called()
        tracker.discard.assert_not_called()

        # Call enough times to exceed broken_tasks_passes (20)
        machine_not_found[TaskId(42)] = 21  # type: ignore[index]
        await orch._task_consumer_consumer(
            cast("UMessage[TaskId, Task[Running]]", msg), machine_not_found
        )

        # save should have been called with a task that has TaskAbandoned event
        uow.tasks.save.assert_called_once()
        saved_task: Task = uow.tasks.save.call_args[0][0]
        assert saved_task.status == TaskStatus.DONE
        assert error_of(saved_task) == "node is gone"
        assert len(saved_task.events) == 1
        event = saved_task.events[0]
        assert isinstance(event, TaskAbandoned)
        assert event.task_id == TaskId(42)
        assert event.node_id == NodeId(42)
        assert event.webhook_url == "https://hook.example.com"
        assert event.webhook_custom_params == {"k": "v"}
        uow.commit.assert_called_once()
        # Tracker slot released on abandon so the int doesn't leak forever.
        tracker.discard.assert_called_once_with(TaskId(42))


class TestCloudsGetCapacity:
    """_clouds_get_capacity computes capacity over active_clouds and uow.nodes.list_all()."""

    @pytest.mark.asyncio
    async def test_empty_active_clouds_returns_zero(self) -> None:
        """active_clouds=[], nodes=[] → max_nodes=0, current=0 → 0."""
        orch = make_orchestrator(active_clouds=[])
        mock_uow = AsyncMock()
        mock_uow.nodes.list_all = AsyncMock(return_value=[])
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        orch._uow_factory = lambda: mock_uow  # type: ignore[method-assign]

        cap = await orch._clouds_get_capacity()
        assert cap == 0

    @pytest.mark.parametrize(
        ("max_nodes", "clouds", "expected"),
        [
            pytest.param(10, ("aws", "aws", "gcp"), 8, id="matching-nodes-subtracted"),
            pytest.param(5, ("aws",) * 10, 0, id="clamped-to-zero"),
            pytest.param(8, ("gcp", "azure", "aws"), 7, id="unrelated-clouds-excluded"),
        ],
    )
    @pytest.mark.asyncio
    async def test_capacity_counts_only_active_cloud_nodes(
        self,
        max_nodes: int,
        clouds: tuple[str, ...],
        expected: int,
    ) -> None:
        """Capacity is max_nodes minus nodes whose cloud is an active cloud prefix."""
        cfg_aws = MagicMock()
        cfg_aws.max_nodes = max_nodes
        cfg_aws.prefix = "aws"
        orch = make_orchestrator(active_clouds=[cfg_aws])

        from yascheduler.domain.model import Node, NodeId

        nodes = [
            Node(node_id=NodeId(i + 1), hostname=str(i + 1), ncpus=2, cloud=cloud)
            for i, cloud in enumerate(clouds)
        ]
        mock_uow = AsyncMock()
        mock_uow.nodes.list_all = AsyncMock(return_value=nodes)
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        orch._uow_factory = lambda: mock_uow  # type: ignore[method-assign]

        cap = await orch._clouds_get_capacity()
        assert cap == expected


class TestOrchestratorConstructor:
    """Orchestrator constructor stores allocation_tracker, active_clouds, allocation_lock."""

    @pytest.mark.asyncio
    async def test_stores_allocation_tracker(self) -> None:
        """Constructor stores allocation_tracker as self._tracker."""
        tracker = AllocationTracker()
        orch = make_orchestrator(allocation_tracker=tracker)
        assert orch._tracker is tracker

    @pytest.mark.asyncio
    async def test_stores_active_clouds(self) -> None:
        """Constructor stores active_clouds as self._active_clouds."""
        my_list: list = []
        orch = make_orchestrator(active_clouds=my_list)
        assert orch._active_clouds is my_list

    @pytest.mark.asyncio
    async def test_stores_allocation_lock(self) -> None:
        """Constructor stores allocation_lock as self._allocation_lock."""
        my_lock = asyncio.Lock()
        orch = make_orchestrator(allocation_lock=my_lock)
        assert orch._allocation_lock is my_lock

    @pytest.mark.asyncio
    async def test_no_adapters_or_configs_stored(self) -> None:
        """Orchestrator does NOT have _adapters or _configs attributes."""
        orch = make_orchestrator()
        assert not hasattr(orch, "_adapters")
        assert not hasattr(orch, "_configs")


class TestDeallocatorConsumer:
    """_deallocator_consumer takes Node from msg.payload and calls deallocate_node (no UoW lookup)."""

    @pytest.mark.asyncio
    async def test_calls_deallocate_node_with_uow_factory(self) -> None:
        """deallocate_node called with the Node from msg.payload directly (no uow.nodes.get)."""
        from yascheduler.application.queue import UMessage
        from yascheduler.domain.model import Node, NodeId

        orch = make_orchestrator()

        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, cloud="aws")
        msg = UMessage(NodeId(1), node)

        with patch(
            "yascheduler.application.orchestrator.deallocate_node",
            new=AsyncMock(),
        ) as mock_dealloc:
            await orch._deallocator_consumer(msg)

        mock_dealloc.assert_called_once_with(
            node,
            orch._repository,
            orch._clouds,
            orch._uow_factory,
        )

    @pytest.mark.asyncio
    async def test_consumer_does_not_duplicate_ssh_teardown(self) -> None:
        """Consumer does NOT call repository.contains/disconnect directly (owned by deallocate_node)."""
        from yascheduler.application.queue import UMessage
        from yascheduler.domain.model import Node, NodeId

        orch = make_orchestrator()
        orch._repository.contains = MagicMock(return_value=True)  # type: ignore[method-assign]
        orch._repository.disconnect = AsyncMock()  # type: ignore[method-assign]

        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, cloud="aws")
        msg = UMessage(NodeId(1), node)

        with patch(
            "yascheduler.application.orchestrator.deallocate_node",
            new=AsyncMock(),
        ):
            await orch._deallocator_consumer(msg)

        orch._repository.contains.assert_not_called()
        orch._repository.disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_consumer_logs_node_id_and_ip_on_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """deallocate_node raises -> error log includes both node_id=%s and hostname=%s."""
        from yascheduler.application.queue import UMessage
        from yascheduler.domain.model import Node, NodeId

        caplog.set_level(logging.ERROR)

        orch = make_orchestrator()

        node = Node(node_id=NodeId(7), hostname="10.0.0.7", ncpus=4, cloud="aws")
        msg = UMessage(NodeId(7), node)

        with patch(
            "yascheduler.application.orchestrator.deallocate_node",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            # Must not raise — consumer swallows to keep worker alive.
            await orch._deallocator_consumer(msg)

        assert len(caplog.records) >= 1
        record = caplog.records[-1]
        assert "node_id=7" in record.getMessage()
        assert "hostname=10.0.0.7" in record.getMessage()


class TestAllocatorConsumer:
    """_allocator_consumer swallows allocate_task exceptions (worker survival)."""

    @pytest.mark.asyncio
    async def test_swallows_cloud_allocate_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """CloudAllocateError from allocate_task is caught + logged; worker survives."""
        from yascheduler.application.queue import UMessage
        from yascheduler.domain.exceptions import CloudAllocateError

        caplog.set_level(logging.ERROR)
        orch = make_orchestrator()

        task_payload = cast("TodoTask", _make_task(engine="e", label="t"))
        msg = UMessage(TaskId(1), task_payload)

        with patch(
            "yascheduler.application.orchestrator.allocate_task",
            new=AsyncMock(side_effect=CloudAllocateError("VM create failed")),
        ) as mock_alloc:
            # Must not raise — _allocator_consumer swallows to keep worker alive.
            await orch._allocator_consumer(msg)

        mock_alloc.assert_called_once()
        assert any("Allocator error" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_swallows_any_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Any Exception from allocate_task is caught; worker survives."""
        from yascheduler.application.queue import UMessage

        caplog.set_level(logging.ERROR)
        orch = make_orchestrator()

        task_payload = cast(
            "TodoTask", _make_task(task_id=TaskId(42), engine="e", label="t")
        )
        msg = UMessage(TaskId(42), task_payload)

        with patch(
            "yascheduler.application.orchestrator.allocate_task",
            new=AsyncMock(side_effect=RuntimeError("unexpected")),
        ) as mock_alloc:
            await orch._allocator_consumer(msg)

        mock_alloc.assert_called_once()
        assert any("Allocator error" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_propagates_allocate_task_args(self) -> None:
        """_allocator_consumer passes all expected kwargs to allocate_task."""
        from yascheduler.application.queue import UMessage

        orch = make_orchestrator()

        task_payload = cast(
            "TodoTask", _make_task(task_id=TaskId(7), engine="e", label="t")
        )
        msg = UMessage(TaskId(7), task_payload)

        with patch(
            "yascheduler.application.orchestrator.allocate_task",
            new=AsyncMock(return_value=False),
        ) as mock_alloc:
            await orch._allocator_consumer(msg)

        mock_alloc.assert_called_once_with(
            task_id=TaskId(7),
            engines=orch._engines,
            uow_factory=orch._uow_factory,
            repository=orch._repository,
            occupancy_checker=orch._occupancy_checker,
            clouds=orch._clouds,
            start_task_on_machine=orch._start_task_on_machine,
            tracker=orch._tracker,
            allocation_lock=orch._allocation_lock,
            remote_tasks_dir=orch._remote_defaults.tasks_dir,
        )


class TestConsumeConditionalDiscard:
    """_task_consumer_consumer discards _occupancy_started only when consume_task returns True."""

    @pytest.mark.parametrize(
        ("consume_result", "in_occupancy"),
        [
            pytest.param(True, False, id="finalised-discards-ip"),
            pytest.param(False, True, id="deferred-keeps-ip"),
        ],
    )
    @pytest.mark.asyncio
    async def test_discards_occupancy_only_on_finalise(
        self,
        consume_result: bool,
        in_occupancy: bool,
    ) -> None:
        """consume_task True discards the occupancy slot; False keeps it for the next cycle."""
        from yascheduler.application.queue import UMessage
        from yascheduler.domain.model import ConnectedMachine, MachineState

        orch = make_orchestrator()
        machine = ConnectedMachine(
            node_id=NodeId(1),
            platforms=("linux",),
            state=MachineState.FREE,
            free_since=0.0,
        )
        session_stub = SimpleNamespace(machine=machine, ip="10.0.0.1")
        orch._repository.get_session = MagicMock(return_value=session_stub)  # type: ignore[method-assign]
        orch._occupancy_started.add(NodeId(1))

        task = _make_task(
            task_id=TaskId(5),
            engine="e",
            label="t",
            status=TaskStatus.RUNNING,
            allocated_node_id=NodeId(1),
        )
        msg = UMessage(TaskId(5), task)

        with patch(
            "yascheduler.application.orchestrator.consume_task",
            new=AsyncMock(return_value=consume_result),
        ):
            await orch._task_consumer_consumer(
                cast("UMessage[TaskId, Task[Running]]", msg), Counter()
            )

        assert (NodeId(1) in orch._occupancy_started) is in_occupancy


class TestConsumeInFlightGuard:
    """In-flight _consuming guard prevents concurrent consume and is released after."""

    @pytest.mark.asyncio
    async def test_producer_skips_in_flight_task(self) -> None:
        """A task id in _consuming is skipped by the producer."""

        orch = make_orchestrator()

        task_a = _make_task(engine="e", label="a", status=TaskStatus.RUNNING)
        task_b = _make_task(
            task_id=TaskId(2),
            engine="e",
            label="b",
            status=TaskStatus.RUNNING,
        )

        mock_uow = AsyncMock()
        mock_uow.tasks.list_running = AsyncMock(return_value=[task_a, task_b])
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        orch._uow_factory = lambda: mock_uow  # type: ignore[method-assign]

        # Mark task_a as in-flight
        orch._consuming.add(TaskId(1))

        yielded: list[UMessage[TaskId, Task]] = [
            msg async for msg in orch._task_consumer_producer()
        ]

        # Only task_b (id=2) is yielded; task_a (id=1) is skipped
        assert len(yielded) == 1
        assert yielded[0].id == TaskId(2)

    @pytest.mark.asyncio
    async def test_guard_released_after_consume_true(self) -> None:
        """consume_task returns True -> task_id removed from _consuming in finally."""
        from yascheduler.application.queue import UMessage
        from yascheduler.domain.model import ConnectedMachine, MachineState

        orch = make_orchestrator()
        machine = ConnectedMachine(
            node_id=NodeId(1),
            platforms=("linux",),
            state=MachineState.FREE,
            free_since=0.0,
        )
        session_stub = SimpleNamespace(machine=machine, ip="10.0.0.1")
        orch._repository.get_session = MagicMock(return_value=session_stub)  # type: ignore[method-assign]
        orch._occupancy_started.add(NodeId(1))

        task = _make_task(
            task_id=TaskId(5),
            engine="e",
            label="t",
            status=TaskStatus.RUNNING,
            allocated_node_id=NodeId(1),
        )
        msg = UMessage(TaskId(5), task)

        with patch(
            "yascheduler.application.orchestrator.consume_task",
            new=AsyncMock(return_value=True),
        ):
            await orch._task_consumer_consumer(
                cast("UMessage[TaskId, Task[Running]]", msg), Counter()
            )

        assert TaskId(5) not in orch._consuming

    @pytest.mark.asyncio
    async def test_guard_released_after_consume_false(self) -> None:
        """consume_task returns False -> task_id still removed from _consuming in finally."""
        from yascheduler.application.queue import UMessage
        from yascheduler.domain.model import ConnectedMachine, MachineState

        orch = make_orchestrator()
        machine = ConnectedMachine(
            node_id=NodeId(1),
            platforms=("linux",),
            state=MachineState.FREE,
            free_since=0.0,
        )
        session_stub = SimpleNamespace(machine=machine, ip="10.0.0.1")
        orch._repository.get_session = MagicMock(return_value=session_stub)  # type: ignore[method-assign]
        orch._occupancy_started.add(NodeId(1))

        task = _make_task(
            task_id=TaskId(5),
            engine="e",
            label="t",
            status=TaskStatus.RUNNING,
            allocated_node_id=NodeId(1),
        )
        msg = UMessage(TaskId(5), task)

        with patch(
            "yascheduler.application.orchestrator.consume_task",
            new=AsyncMock(return_value=False),
        ):
            await orch._task_consumer_consumer(
                cast("UMessage[TaskId, Task[Running]]", msg), Counter()
            )

        # Deferred keeps the ip in _occupancy_started but releases the in-flight guard
        assert TaskId(5) not in orch._consuming
        assert NodeId(1) in orch._occupancy_started

    @pytest.mark.asyncio
    async def test_guard_released_on_consume_exception(self) -> None:
        """consume_task raises -> finally still removes task_id from _consuming."""
        from yascheduler.application.queue import UMessage
        from yascheduler.domain.model import ConnectedMachine, MachineState

        orch = make_orchestrator()
        machine = ConnectedMachine(
            node_id=NodeId(1),
            platforms=("linux",),
            state=MachineState.FREE,
            free_since=0.0,
        )
        session_stub = SimpleNamespace(machine=machine, ip="10.0.0.1")
        orch._repository.get_session = MagicMock(return_value=session_stub)  # type: ignore[method-assign]
        orch._occupancy_started.add(NodeId(1))

        task = _make_task(
            task_id=TaskId(5),
            engine="e",
            label="t",
            status=TaskStatus.RUNNING,
            allocated_node_id=NodeId(1),
        )
        msg = UMessage(TaskId(5), task)

        with (
            patch(
                "yascheduler.application.orchestrator.consume_task",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            pytest.raises(RuntimeError),
        ):
            await orch._task_consumer_consumer(
                cast("UMessage[TaskId, Task[Running]]", msg), Counter()
            )

        # finally released the guard even on exception
        assert TaskId(5) not in orch._consuming
