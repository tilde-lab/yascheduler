"""Unit test for the bounded graceful-drain at shutdown.

Regression: ``stop()`` iterates ``self._bg_jobs`` (a set, unordered) and
cancels each task one at a time. When workers are cancelled before their
coordinator reaches ``queue.join()``, items still in ``_queue`` have no
consumer and the unbounded ``join()`` hangs until the process receives
SIGKILL. The drain is now bounded by ``_DRAIN_TIMEOUT``.
"""
# region MODULE_CONTRACT
# PURPOSE: Unit test for the bounded graceful-drain in _create_producer_consumers.
# SCOPE: drain join is bounded — stop() returns within _DRAIN_TIMEOUT and warns when queued items have no consumer (workers cancelled before the join).
# KEYWORDS: shutdown, drain, queue.join, wait_for, _DRAIN_TIMEOUT, stop, CancelledError
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from pathlib import PurePosixPath
from unittest.mock import AsyncMock, MagicMock

import pytest

from yascheduler.application import orchestrator as orch_module
from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.orchestrator import Orchestrator
from yascheduler.application.queue import UMessage, UniqueQueue
from yascheduler.domain import Engine, EngineRepository, LocalSettings, RemoteDefaults
from yascheduler.entrypoints import Config


def _make_orchestrator() -> Orchestrator:
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

    engine = MagicMock(spec=Engine, sleep_interval=0)
    engine.name = "test_engine"
    engines = MagicMock(spec=EngineRepository)
    engines.values.return_value = [engine]

    repository = MagicMock()
    repository.__len__ = MagicMock(return_value=0)
    repository.disconnect_all = AsyncMock()

    return Orchestrator(
        local_settings=local,
        remote_defaults=remote,
        uow_factory=AsyncMock,
        clouds=MagicMock(stop=AsyncMock()),
        repository=repository,
        task_deployer=MagicMock(),
        output_downloader=MagicMock(),
        occupancy_checker=MagicMock(),
        engines=engines,
        config_clouds=[],
        local_tasks_dir=MagicMock(),  # type: ignore[arg-type]
        allocation_tracker=AllocationTracker(),
        active_clouds=[],
        allocation_lock=asyncio.Lock(),
        list_private_keys_fn=lambda _keys_dir: [],
        http_session=None,  # type: ignore[arg-type]
    )


class _BlockingAsyncGen:
    """Async iterable that parks forever on the first ``__anext__``.

    Keeps the coordinator inside the ``async for msg in producer():`` body of
    the try block so ``stop()``'s cancel is delivered there (not at the
    while-condition), deterministically routing into the ``except
    CancelledError`` drain path.
    """

    def __aiter__(self) -> _BlockingAsyncGen:
        return self

    async def __anext__(self) -> None:
        await asyncio.Event().wait()


class TestDrainBounded:
    """Drain join is bounded — no hang when consumers are gone before the join."""

    @pytest.mark.asyncio
    async def test_stop_returns_when_queued_items_have_no_consumer(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reproduce the shutdown hang.

        Two items pre-loaded directly on the queue. The single worker picks up
        item1 and blocks forever inside the consumer; item2 stays in ``_queue``.
        The producer parks the coordinator inside the try block so ``stop()``'s
        cancel routes it into the ``except CancelledError`` drain. The worker's
        ``finally`` drains item1 (via ``item_done``) but item2 has no consumer —
        the coordinator's ``queue.join()`` would hang forever without the
        bounded drain. With the fix it returns within ``_DRAIN_TIMEOUT`` and
        logs a drain-timeout warning.
        """
        # Shrink the drain window so the test is fast; production default is
        # 5s. The reference is looked up in module globals at call time.
        monkeypatch.setattr(orch_module, "_DRAIN_TIMEOUT", 0.3)

        orch = _make_orchestrator()
        q: UniqueQueue = UniqueQueue("test", maxsize=10)

        def producer() -> _BlockingAsyncGen:
            return _BlockingAsyncGen()

        async def _blocking_consumer(_msg: UMessage) -> None:
            await asyncio.Event().wait()  # never completes unless cancelled

        # Two items: worker picks up item1 and blocks; item2 stays in _queue.
        await q.put(UMessage(1, 1))
        await q.put(UMessage(2, 2))

        loop_task = asyncio.create_task(
            orch._create_producer_consumers(
                q, producer, _blocking_consumer, workers_num=1
            ),
        )
        orch._bg_jobs.add(loop_task)

        # Let the worker start, pick up item1, block; coordinator parks in producer.
        await asyncio.sleep(0.05)

        with caplog.at_level(logging.DEBUG, logger="yascheduler"):
            # If the drain is unbounded (regression), stop() hangs and the
            # outer wait_for raises TimeoutError — failing the test fast.
            await asyncio.wait_for(orch.stop(), timeout=5.0)

        assert orch._stopped is True

        # Operational warning (not a debug trace): human-readable %-format,
        # matching the sibling warnings in stop(). No structured extra dict.
        drain_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "drain timed out" in r.getMessage()
        ]
        assert drain_warnings, (
            "drain-timeout warning was not emitted when queued items had no consumer"
        )
        msg = drain_warnings[0].getMessage()
        assert "test" in msg, "queue name missing from drain warning"
        assert "0.3" in msg, "timeout value missing from drain warning"
