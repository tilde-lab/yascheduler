"""Unit tests for orchestrator consumer-worker error resilience."""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for orchestrator consumer-worker error resilience.
# SCOPE: consumer Exception → logged and worker continues processing subsequent messages; consumer CancelledError → propagates past `except Exception` to the graceful-drain path.
# KEYWORDS: consumer resilience, CancelledError, error recovery
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock

import pytest

from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.orchestrator import Orchestrator
from yascheduler.application.queue import UMessage, UniqueQueue
from yascheduler.domain import (
    Engine,
    EngineRepository,
    LocalSettings,
    RemoteDefaults,
    TaskId,
)
from yascheduler.entrypoints import Config
from yascheduler.infra.persistence import TaskRowNotFoundError


def _make_orchestrator(sleep_interval: int = 0) -> Orchestrator:
    """Build an Orchestrator with mocked deps; real Engine so _sleep_interval
    is configurable and _asleep_until returns immediately when interval is 0.
    """
    local = MagicMock(spec=LocalSettings)
    local.conn_machine_pending = 10
    local.allocate_pending = 5
    local.consume_pending = 3
    local.deallocate_pending = 2
    local.conn_machine_limit = 1
    local.allocate_limit = 1
    local.consume_limit = 1
    local.deallocate_limit = 1

    remote = MagicMock(spec=RemoteDefaults)
    remote.tasks_dir = PurePosixPath("/tmp/tasks")
    remote.data_dir = PurePosixPath("/tmp/data")
    remote.engines_dir = PurePosixPath("/tmp/engines")
    remote.username = "root"

    config = MagicMock(spec=Config)
    config.local = local
    config.remote = remote

    mock_uow = AsyncMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock(spec=Engine, sleep_interval=sleep_interval)
    engine.name = "test_engine"
    engines = MagicMock(spec=EngineRepository)
    engines.values.return_value = [engine]

    repository = MagicMock()
    repository.__len__ = MagicMock(return_value=1)
    repository.disconnect_all = AsyncMock()
    task_deployer = MagicMock()
    output_downloader = MagicMock()
    occupancy_checker = MagicMock()

    return Orchestrator(
        local_settings=local,
        remote_defaults=remote,
        uow_factory=lambda: mock_uow,
        clouds=AsyncMock(),
        repository=repository,
        task_deployer=task_deployer,
        output_downloader=output_downloader,
        occupancy_checker=occupancy_checker,
        engines=engines,
        config_clouds=[],
        local_tasks_dir=Path("/tmp"),
        allocation_tracker=AllocationTracker(),
        active_clouds=[],
        allocation_lock=asyncio.Lock(),
        list_private_keys_fn=lambda _keys_dir: [],
    )


class _ListHandler(logging.Handler):
    """Collects emitted LogRecords in a list for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _EmptyAsyncGen:
    """Async iterable that yields nothing and yields control each iteration."""

    def __aiter__(self) -> _EmptyAsyncGen:
        return self

    async def __anext__(self) -> None:
        await asyncio.sleep(0)
        raise StopAsyncIteration


def _idle_producer() -> _EmptyAsyncGen:
    return _EmptyAsyncGen()


# =============================================================================
# Test 1: consumer Exception is logged and the worker continues the loop
# =============================================================================


class TestConsumerResilience:
    """Consumer raises Exception on first message → logged, worker continues,
    subsequent messages are still processed (worker task NOT killed).
    """

    @pytest.mark.asyncio
    async def test_consumer_exception_continues_loop(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        orch = _make_orchestrator(sleep_interval=0)
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        processed: list[int] = []
        first_call = {"n": 0}

        async def consumer(msg: UMessage[int, int]) -> None:
            if first_call["n"] == 0:
                first_call["n"] += 1
                # Mirror the fix-save-silent-zero-rows race: save() on a
                # concurrently-deleted task raises TaskRowNotFoundError.
                raise TaskRowNotFoundError(TaskId(msg.id))
            processed.append(msg.id)

        def producer() -> _EmptyAsyncGen:
            return _idle_producer()

        # Pre-enqueue two messages: the first raises, the second must still be
        # processed by the same worker (proves the worker did not die).
        await q.put(UMessage(1, 1))
        await q.put(UMessage(2, 2))

        with caplog.at_level(logging.DEBUG, logger="yascheduler"):
            loop_task = asyncio.create_task(
                orch._create_producer_consumers(q, producer, consumer, workers_num=1),
            )
            orch._bg_jobs.add(loop_task)

            # Let the worker process both messages.
            for _ in range(500):
                if processed:
                    break
                await asyncio.sleep(0.001)

        # Shutdown: cancel the loop and workers (the producer never self-cancels
        # here, so the `except CancelledError` drain runs on cancel).
        for t in orch._bg_jobs:
            t.cancel()
        await asyncio.gather(*orch._bg_jobs, return_exceptions=True)

        assert first_call["n"] == 1, "first consumer call did not run"
        assert processed == [2], (
            f"worker did not continue to the second message; processed={processed}"
        )
        assert any(r.getMessage() == "CONSUMER_ERROR" for r in caplog.records), (
            "consumer Exception was not logged as CONSUMER_ERROR trace"
        )


# =============================================================================
# Test 2: consumer CancelledError preserves the graceful-drain path
# =============================================================================


class TestConsumerCancelledErrorDrain:
    """Consumer raising CancelledError reaches the drain, not the Exception handler."""

    @pytest.mark.asyncio
    async def test_consumer_cancellederror_not_swallowed_by_except_exception(
        self,
    ) -> None:
        """The CONSUMER_ERROR log line must NOT appear for a CancelledError, and
        the worker exits cleanly (CancelledError propagates past `except Exception`
        to the `finally: queue.item_done(msg)` and onward to the drain).
        """
        orch = _make_orchestrator(sleep_interval=0)
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        async def consumer(_msg: UMessage[int, int]) -> None:
            raise asyncio.CancelledError

        def producer() -> _EmptyAsyncGen:
            return _idle_producer()

        await q.put(UMessage(1, 1))

        handler = _ListHandler()
        parent = logging.getLogger("yascheduler")
        parent.addHandler(handler)
        parent.setLevel(logging.DEBUG)

        loop_task = asyncio.create_task(
            orch._create_producer_consumers(q, producer, consumer, workers_num=1),
        )
        orch._bg_jobs.add(loop_task)

        # Cancel the parent coroutine so the drain path runs.
        await asyncio.sleep(0.05)
        for t in orch._bg_jobs:
            t.cancel()
        await asyncio.gather(*orch._bg_jobs, return_exceptions=True)

        parent.removeHandler(handler)
        assert not any(r.getMessage() == "CONSUMER_ERROR" for r in handler.records), (
            "CancelledError was swallowed by except Exception"
        )
