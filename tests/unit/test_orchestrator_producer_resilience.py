"""Unit tests for orchestrator producer and stats error resilience."""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for orchestrator producer/stats error resilience.
# SCOPE: producer Exception → loop continues; producer CancelledError → graceful-drain path preserved; worker registration in self._bg_jobs; stop() cancels workers; double-cancel idempotent; _print_stats Exception → loop continues; _print_stats CancelledError → propagates.
# KEYWORDS: producer resilience, CancelledError, _bg_jobs, _print_stats
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.log_assertions import extra_fields
from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.orchestrator import Orchestrator
from yascheduler.application.queue import UMessage, UniqueQueue
from yascheduler.domain import Engine, EngineRepository, LocalSettings, RemoteDefaults
from yascheduler.entrypoints import Config

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.application.uow import AbstractUnitOfWork


# =============================================================================
# Helpers
# =============================================================================


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


def _uow_factory(uow: object) -> Callable[[], AbstractUnitOfWork]:
    """Return a uow_factory callable returning ``uow`` regardless of signature."""

    def _factory() -> AbstractUnitOfWork:  # type: ignore[type-arg]
        return uow  # type: ignore[return-value]

    return _factory


class _EmptyAsyncGen:
    """Async iterable that yields nothing and yields control each iteration.

    Implements ``__aiter__``/``__anext__`` directly (instead of the
    ``return; yield`` async-generator idiom) so static analysers do not flag the
    body as unreachable. Each ``__anext__`` awaits a zero sleep to surrender
    control to the event loop, then raises StopAsyncIteration.
    """

    def __aiter__(self) -> _EmptyAsyncGen:
        return self

    async def __anext__(self) -> None:
        await asyncio.sleep(0)
        raise StopAsyncIteration


class _RaisingAsyncGen:
    """Async iterable that raises ``exc`` on first ``__anext__`` (after yielding
    control once). Models a producer whose dependency fails mid-read.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __aiter__(self) -> _RaisingAsyncGen:
        return self

    async def __anext__(self) -> None:
        await asyncio.sleep(0)
        raise self._exc


def _idle_producer() -> _EmptyAsyncGen:
    """Producer factory that returns an async iterable yielding nothing.

    Each producer cycle calls ``producer()`` (once per ``while`` iteration),
    then iterates the returned async iterable, which yields control then ends.
    """
    return _EmptyAsyncGen()


def _raising_producer(exc: BaseException) -> _RaisingAsyncGen:
    """Producer factory that returns an async iterable raising ``exc``."""
    return _RaisingAsyncGen(exc)


# =============================================================================
# Test 1: producer Exception is logged and the loop retries on the next tick
# =============================================================================


class TestProducerResilience:
    """Producer raises Exception on first call → logged, loop continues, second call runs."""

    @pytest.mark.asyncio
    async def test_producer_exception_continues_loop(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        orch = _make_orchestrator(sleep_interval=0)
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        call_count = {"n": 0}
        success = {"n": 0}

        def producer() -> _RaisingAsyncGen | _EmptyAsyncGen:
            call_count["n"] += 1
            n = call_count["n"]
            if n == 1:
                # First cycle: producer dependency raises (logged, loop continues).
                return _RaisingAsyncGen(RuntimeError("db timeout"))
            # call >= 2: yield nothing, record a successful cycle, then request
            # shutdown so the loop exits via its own while-condition.
            success["n"] += 1
            orch._cancellation_event.set()
            return _EmptyAsyncGen()

        consumer = AsyncMock()

        with caplog.at_level(logging.DEBUG, logger="yascheduler"):
            await orch._create_producer_consumers(q, producer, consumer, workers_num=1)

        # The loop exits via the while-condition (cancellation_event set), NOT
        # via CancelledError — so the `except CancelledError` drain path does
        # NOT run and the worker remains blocked on queue.get(). Cancel and
        # await it explicitly to avoid "Event loop is closed" unraisable
        # warnings when pytest-asyncio tears the loop down.
        for t in orch._bg_jobs:
            t.cancel()
        await asyncio.gather(*orch._bg_jobs, return_exceptions=True)

        assert call_count["n"] >= 2, "producer was not retried after raising"
        assert success["n"] >= 1, "second producer cycle did not complete"
        assert any(r.getMessage() == "PRODUCER_ERROR" for r in caplog.records), (
            "producer Exception was not logged as PRODUCER_ERROR trace"
        )


# =============================================================================
# Test 2: producer CancelledError preserves the graceful-drain path
# =============================================================================


class TestCancelledErrorDrain:
    """Producer raising CancelledError reaches the drain, not the Exception handler."""

    @pytest.mark.asyncio
    async def test_producer_cancellederror_preserves_drain(self) -> None:
        """When the producer raises CancelledError on its very first iteration,
        the graceful-drain path runs (workers cancelled + awaited) and the
        producer-error `except Exception` does NOT swallow the CancelledError.
        Observed via: exactly one producer invocation (no retry), no consumer
        call (no message enqueued), and the function returns cleanly (the drain
        awaited the workers instead of propagating an unhandled error).
        """
        orch = _make_orchestrator(sleep_interval=0)
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        consumer_calls = {"n": 0}

        async def _consumer(_msg: UMessage) -> None:
            consumer_calls["n"] += 1

        call_count = {"n": 0}

        def producer() -> _RaisingAsyncGen:
            call_count["n"] += 1
            return _raising_producer(asyncio.CancelledError())

        # Drains and returns; no retry, no consumer call.
        await orch._create_producer_consumers(q, producer, _consumer, workers_num=2)

        assert call_count["n"] == 1, "producer was retried — drain did not run"
        assert consumer_calls["n"] == 0, (
            "consumer ran despite producer raising CancelledError before yielding"
        )

    @pytest.mark.asyncio
    async def test_producer_cancellederror_not_swallowed_by_except_exception(
        self,
    ) -> None:
        """The PRODUCER_ERROR log line must NOT appear for a CancelledError."""
        orch = _make_orchestrator(sleep_interval=0)
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        def producer() -> _RaisingAsyncGen:
            return _raising_producer(asyncio.CancelledError())

        consumer = AsyncMock()
        handler = _ListHandler()
        parent = logging.getLogger("yascheduler")
        parent.addHandler(handler)
        parent.setLevel(logging.DEBUG)

        try:
            await orch._create_producer_consumers(q, producer, consumer, workers_num=1)
        finally:
            parent.removeHandler(handler)

        assert not any(r.getMessage() == "PRODUCER_ERROR" for r in handler.records), (
            "CancelledError was swallowed by except Exception"
        )


# =============================================================================
# Test 3: workers registered in self._bg_jobs; stop() cancels them; double-cancel idempotent
# =============================================================================


class TestWorkerRegistration:
    """Workers spawned by _create_producer_consumers are registered in self._bg_jobs."""

    @pytest.mark.asyncio
    async def test_workers_registered_in_bg_jobs(self) -> None:
        """start()-style: parent coroutine task + N workers all in _bg_jobs.

        _create_producer_consumers adds the workers to self._bg_jobs (Decision 2);
        the parent coroutine is registered by the caller (mirrors start()).
        """
        orch = _make_orchestrator(sleep_interval=0)
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        # Idle producer that yields nothing each cycle; shuts down after cycle 3.
        cycles = {"n": 0}

        def producer() -> _EmptyAsyncGen:
            cycles["n"] += 1
            if cycles["n"] >= 3:
                orch._cancellation_event.set()
            return _idle_producer()

        consumer = AsyncMock()

        before = len(orch._bg_jobs)
        # Mirror start(): wrap _create_producer_consumers in a task and register
        # the parent coroutine task in _bg_jobs, exactly as start() does.
        loop_task = asyncio.create_task(
            orch._create_producer_consumers(q, producer, consumer, workers_num=2),
        )
        orch._bg_jobs.add(loop_task)

        # Let the loop run so workers get created, registered, and the producer
        # advances at least one cycle. A bounded sleep is deterministic here:
        # workers are created synchronously before the first producer() call.
        await asyncio.sleep(0.05)

        # parent task (loop_task) + 2 workers = +3 in _bg_jobs.
        assert len(orch._bg_jobs) - before == 3, (
            f"expected +3 bg_jobs, got +{len(orch._bg_jobs) - before}"
        )

        # Let it self-terminate via cancellation_event (set after cycle 3).
        for _ in range(500):
            if loop_task.done():
                break
            await asyncio.sleep(0.001)
        if not loop_task.done():
            loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        # The parent returns normally (cancellation_event set, not CancelledError),
        # so the `except CancelledError` drain does NOT run and workers remain
        # blocked on queue.get() — cancel them explicitly to avoid a hang.
        for t in orch._bg_jobs:
            t.cancel()
        await asyncio.gather(*orch._bg_jobs, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_stop_cancels_workers(self) -> None:
        """stop() cancels worker tasks registered in _bg_jobs (blocked on queue.get)."""
        orch = _make_orchestrator(sleep_interval=0)
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        def producer() -> _EmptyAsyncGen:
            return _idle_producer()

        async def _blocking_consumer(_msg: UMessage) -> None:
            await asyncio.Event().wait()

        await q.put(UMessage(1, 1))

        loop_task = asyncio.create_task(
            orch._create_producer_consumers(
                q,
                producer,
                _blocking_consumer,
                workers_num=1,
            ),
        )
        # Mirror start() so stop()'s cascade cancels the parent too.
        orch._bg_jobs.add(loop_task)

        # Let workers start and pick up the queued item.
        await asyncio.sleep(0.05)

        worker_tasks = {t for t in orch._bg_jobs if t is not loop_task}
        assert len(worker_tasks) == 1

        await orch.stop()

        worker = worker_tasks.pop()
        assert worker.done(), "worker was not awaited by stop()"
        cancelled = worker.cancelled()
        raised_cancel = False
        if not cancelled:
            try:
                exc = worker.exception()
            except asyncio.CancelledError:
                raised_cancel = True
            else:
                raised_cancel = isinstance(exc, asyncio.CancelledError)
        assert cancelled or raised_cancel, "worker did not exit with CancelledError"

        assert loop_task.done(), "parent loop task was not cancelled by stop()"
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

    @pytest.mark.asyncio
    async def test_double_cancel_is_idempotent(self) -> None:
        """Cancelling a worker twice (stop + drain) raises no error and awaits once."""
        orch = _make_orchestrator(sleep_interval=0)
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        def producer() -> _EmptyAsyncGen:
            return _idle_producer()

        async def _blocking_consumer(_msg: UMessage) -> None:
            await asyncio.Event().wait()

        await q.put(UMessage(1, 1))

        loop_task = asyncio.create_task(
            orch._create_producer_consumers(
                q,
                producer,
                _blocking_consumer,
                workers_num=1,
            ),
        )
        orch._bg_jobs.add(loop_task)

        await asyncio.sleep(0.05)

        worker = next(iter(t for t in orch._bg_jobs if t is not loop_task))

        # Two cancels (mimics stop() via _bg_jobs + parent drain) must be no-ops.
        worker.cancel()
        worker.cancel()

        await asyncio.gather(worker, return_exceptions=True)
        assert worker.done()
        assert worker.cancelled(), (
            "worker should be cancelled, not exited with exception"
        )

        orch._cancellation_event.set()
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        await asyncio.gather(*orch._bg_jobs, return_exceptions=True)


# =============================================================================
# Test 4: _print_stats resilience
# =============================================================================


class _BoomUow:
    """Raises on first nodes access, succeeds on second."""

    def __init__(self) -> None:
        self.calls = 0

    async def __aenter__(self) -> _BoomUow:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    @property
    def nodes(self) -> object:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("db timeout")
        return AsyncMock(count_by_status=AsyncMock(return_value={}))

    @property
    def tasks(self) -> object:
        return AsyncMock(count_by_status=AsyncMock(return_value={}))


class _CancelUow:
    """Raises CancelledError on __aenter__."""

    async def __aenter__(self) -> object:
        raise asyncio.CancelledError

    async def __aexit__(self, *args: object) -> bool:
        return False


class TestStatsResilience:
    """_print_stats transient Exception → logged, loop continues; CancelledError → propagates."""

    @pytest.mark.asyncio
    async def test_print_stats_exception_continues_loop(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        orch = _make_orchestrator(sleep_interval=0)
        orch._repository.list_connected = MagicMock(return_value=[])  # type: ignore[method-assign]

        boom = _BoomUow()
        orch._uow_factory = _uow_factory(boom)  # type: ignore[method-assign]

        # _print_stats uses a hardcoded 10s tick via _asleep_until. Patch
        # _asleep_until to a zero-duration sleep so the loop advances instantly
        # but still yields control to the event loop between ticks.
        async def _noop_sleep(_end: object) -> None:
            await asyncio.sleep(0)

        with (
            caplog.at_level(logging.DEBUG, logger="yascheduler"),
            patch(
                "yascheduler.application.orchestrator._asleep_until",
                new=_noop_sleep,
            ),
        ):
            stats_task = asyncio.create_task(orch._print_stats())
            # Shut down after the second nodes access (raise then succeed).
            for _ in range(800):
                if boom.calls >= 2:
                    orch._cancellation_event.set()
                    break
                await asyncio.sleep(0)
            stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stats_task

        assert boom.calls >= 2, "stats loop did not continue after the first error"
        assert any(
            r.getMessage() == "ERROR" and extra_fields(r).get("context") == "stats"
            for r in caplog.records
        ), "stats Exception was not logged with the ERROR trace and context=stats"

    @pytest.mark.asyncio
    async def test_print_stats_cancellederror_still_propagates(self) -> None:
        orch = _make_orchestrator(sleep_interval=0)
        cancel_uow = _CancelUow()
        orch._uow_factory = _uow_factory(cancel_uow)  # type: ignore[method-assign]

        # Body enters the try, __aenter__ raises CancelledError, which must
        # propagate past `except Exception` and out of _print_stats. Patch
        # _asleep_until so the `finally` sleep does not add a 10s delay.
        async def _noop_sleep(_end: object) -> None:
            await asyncio.sleep(0)

        with (
            patch(
                "yascheduler.application.orchestrator._asleep_until",
                new=_noop_sleep,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await orch._print_stats()


class _EmptyMappingsUow:
    """UoW whose count_by_status return empty mappings (no rows / no enabled).

    Models a fresh DB (yascheduler_nodes empty) — reproduces the
    KeyError(True) regression on ncounters[True] in _print_stats.
    """

    async def __aenter__(self) -> _EmptyMappingsUow:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    @property
    def nodes(self) -> object:
        return AsyncMock(count_by_status=AsyncMock(return_value={}))

    @property
    def tasks(self) -> object:
        return AsyncMock(count_by_status=AsyncMock(return_value={}))


class TestStatsEmptyDbRegression:
    """_print_stats on empty yascheduler_nodes SHALL NOT raise KeyError.

    Regression: ncounters[True] raised KeyError(True) (str→'True', logged as
    err=True) every 10s when the nodes table was empty or had no enabled rows.
    """

    @pytest.mark.asyncio
    async def test_print_stats_succeeds_on_empty_nodes(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        orch = _make_orchestrator(sleep_interval=0)
        orch._repository.list_connected = MagicMock(return_value=[])  # type: ignore[method-assign]
        orch._uow_factory = _uow_factory(_EmptyMappingsUow())  # type: ignore[method-assign]

        async def _noop_sleep(_end: object) -> None:
            await asyncio.sleep(0)

        with (
            caplog.at_level(logging.INFO, logger="yascheduler"),
            patch(
                "yascheduler.application.orchestrator._asleep_until",
                new=_noop_sleep,
            ),
        ):
            stats_task = asyncio.create_task(orch._print_stats())
            # Let one full tick execute, then stop.
            for _ in range(50):
                if any(r.getMessage().startswith("THREADS:") for r in caplog.records):
                    break
                await asyncio.sleep(0)
            orch._cancellation_event.set()
            stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stats_task
        assert any(
            r.getMessage().startswith("THREADS:")
            and "enabled:0/total:0" in r.getMessage()
            for r in caplog.records
        ), "stats did not log successfully on empty nodes table"
        assert not any(
            r.getMessage() == "ERROR" and extra_fields(r).get("context") == "stats"
            for r in caplog.records
        ), "stats raised on empty nodes table (KeyError regression)"
