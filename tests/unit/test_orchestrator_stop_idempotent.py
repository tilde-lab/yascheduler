"""Unit tests for Orchestrator.stop() idempotency and exception safety."""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for Orchestrator.stop() idempotency and exception safety.
# SCOPE: _stopped guard single-execution across sequential/interleaved/repeated callers; dead-bg-job tolerance via except Exception; CancelledError still reaches the graceful-drain path; per-step try/except isolation (clouds.stop/gateway.disconnect_all/http_session.close); http_session nulled after close; stop() before start() is a safe no-op.
# KEYWORDS: Orchestrator.stop, idempotency, exception safety, graceful-drain
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.orchestrator import Orchestrator
from yascheduler.domain import Engine, EngineRepository, LocalSettings, RemoteDefaults
from yascheduler.entrypoints import Config

if TYPE_CHECKING:
    import aiohttp


# =============================================================================
# Helpers
# =============================================================================


def _make_orchestrator(
    http_session: aiohttp.ClientSession | None = None,
    clouds_stop: AsyncMock | None = None,
    disconnect_all: AsyncMock | None = None,
) -> Orchestrator:
    """Build an Orchestrator with mocked deps. Inject cleanup-step AsyncMocks
    via the real dependency slots so tests can assert on the held references.
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

    engine = MagicMock(spec=Engine, sleep_interval=0)
    engine.name = "test_engine"
    engines = MagicMock(spec=EngineRepository)
    engines.values.return_value = [engine]

    clouds = MagicMock()
    clouds.stop = clouds_stop if clouds_stop is not None else AsyncMock()

    repository = MagicMock()
    repository.__len__ = MagicMock(return_value=0)
    repository.disconnect_all = (
        disconnect_all if disconnect_all is not None else AsyncMock()
    )
    task_deployer = MagicMock()
    output_downloader = MagicMock()
    occupancy_checker = MagicMock()

    return Orchestrator(
        local_settings=local,
        remote_defaults=remote,
        uow_factory=AsyncMock,
        clouds=clouds,
        repository=repository,
        task_deployer=task_deployer,
        output_downloader=output_downloader,
        occupancy_checker=occupancy_checker,
        engines=engines,
        config_clouds=[],
        local_tasks_dir=MagicMock(),  # type: ignore[arg-type]
        allocation_tracker=AllocationTracker(),
        active_clouds=[],
        allocation_lock=asyncio.Lock(),
        list_private_keys_fn=lambda _keys_dir: [],
        http_session=http_session,
    )


# =============================================================================
# 4.2: cleanup body runs once; http_session nulled after close
# =============================================================================


class TestStopIdempotent:
    """stop(): cleanup body executes exactly once across repeated calls."""

    async def test_stop_runs_cleanup_body_once(self) -> None:
        clouds_stop = AsyncMock()
        disconnect_all = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        orch = _make_orchestrator(
            http_session=http_session,
            clouds_stop=clouds_stop,
            disconnect_all=disconnect_all,
        )

        await orch.stop()
        await orch.stop()  # second call must be a no-op

        assert orch._stopped is True
        clouds_stop.assert_awaited_once()
        disconnect_all.assert_awaited_once()
        http_session.close.assert_awaited_once()
        # http_session nulled after the first call.
        assert orch._http_session is None


# =============================================================================
# 4.9: http_session nulled after close regardless of success/failure
# =============================================================================


class TestStopHttpSessionNulled:
    """http_session is nulled after close regardless of success/failure."""

    @pytest.mark.parametrize("close_raises", [False, True])
    async def test_stop_http_session_nulled_after_close(
        self,
        close_raises: bool,
    ) -> None:
        http_session = MagicMock()
        if close_raises:
            http_session.close = AsyncMock(side_effect=RuntimeError("close failed"))
        else:
            http_session.close = AsyncMock()
        orch = _make_orchestrator(http_session=http_session)

        await orch.stop()

        http_session.close.assert_awaited_once()
        assert orch._http_session is None


# =============================================================================
# 4.3: interleaved calls serialized by the guard
# =============================================================================


class TestStopInterleaved:
    """Two coroutines calling stop(): the second sees _stopped==True and no-ops."""

    async def test_stop_interleaved_calls_serialized_by_guard(self) -> None:
        # clouds.stop() awaits once before completing so the second caller
        # gets a chance to run mid-first-call (at the clouds.stop await).
        clouds_stop_calls = 0

        async def slow_clouds_stop() -> None:
            nonlocal clouds_stop_calls
            clouds_stop_calls += 1
            await asyncio.sleep(0)  # yield once -> lets second caller run

        disconnect_all = AsyncMock()
        orch = _make_orchestrator(
            clouds_stop=AsyncMock(side_effect=slow_clouds_stop),
            disconnect_all=disconnect_all,
        )

        async def caller_a() -> None:
            await orch.stop()

        async def caller_b() -> None:
            await orch.stop()

        await asyncio.gather(caller_a(), caller_b())

        # The guard was set synchronously by caller_a before its first await;
        # caller_b saw _stopped==True and returned as a no-op. Cleanup body
        # ran exactly once total.
        assert clouds_stop_calls == 1
        disconnect_all.assert_awaited_once()


# =============================================================================
# 4.4: dead bg job (terminated with RuntimeError) does not abort cleanup
# =============================================================================


class TestStopDeadBgJob:
    """A bg job already terminated with a non-CancelledError must not abort stop()."""

    async def test_stop_dead_bg_job_does_not_abort_cleanup(self) -> None:
        clouds_stop = AsyncMock()
        disconnect_all = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        orch = _make_orchestrator(
            http_session=http_session,
            clouds_stop=clouds_stop,
            disconnect_all=disconnect_all,
        )

        async def _raise_runtime() -> None:
            raise RuntimeError("bg job died before shutdown")

        dead_task = asyncio.create_task(_raise_runtime())
        # Let the task actually terminate with the exception so await re-raises it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert dead_task.done()
        orch._bg_jobs.add(dead_task)

        # Must not raise — the re-raised RuntimeError is caught by except Exception.
        await orch.stop()

        clouds_stop.assert_awaited_once()
        disconnect_all.assert_awaited_once()
        http_session.close.assert_awaited_once()
        assert orch._http_session is None


# =============================================================================
# 4.5: CancelledError preserves the graceful-drain path
# =============================================================================


class TestStopCancelledError:
    """A job that raises CancelledError on cancel keeps the existing drain path."""

    async def test_stop_cancellederror_preserves_drain(self) -> None:
        clouds_stop = AsyncMock()
        disconnect_all = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        orch = _make_orchestrator(
            http_session=http_session,
            clouds_stop=clouds_stop,
            disconnect_all=disconnect_all,
        )

        async def _hang_until_cancelled() -> None:
            await asyncio.Event().wait()

        live_task = asyncio.create_task(_hang_until_cancelled())
        orch._bg_jobs.add(live_task)

        await orch.stop()

        # except CancelledError caught the drain exception; live_task done cleanly.
        assert live_task.done()
        clouds_stop.assert_awaited_once()
        disconnect_all.assert_awaited_once()
        http_session.close.assert_awaited_once()


# =============================================================================
# 4.6 / 4.7: per-step isolation — one failing step does not skip the rest
# =============================================================================


class TestStopPerStepIsolation:
    """Each cleanup step is isolated; one failing step does not skip the others."""

    async def test_stop_failing_clouds_stop_does_not_skip_rest(self) -> None:
        clouds_stop = AsyncMock(side_effect=RuntimeError("clouds down"))
        disconnect_all = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        orch = _make_orchestrator(
            http_session=http_session,
            clouds_stop=clouds_stop,
            disconnect_all=disconnect_all,
        )

        await orch.stop()  # must not raise

        clouds_stop.assert_awaited_once()
        disconnect_all.assert_awaited_once()
        http_session.close.assert_awaited_once()
        assert orch._http_session is None

    async def test_stop_failing_disconnect_all_does_not_skip_http(self) -> None:
        clouds_stop = AsyncMock()
        disconnect_all = AsyncMock(side_effect=RuntimeError("gateway down"))
        http_session = MagicMock()
        http_session.close = AsyncMock()
        orch = _make_orchestrator(
            http_session=http_session,
            clouds_stop=clouds_stop,
            disconnect_all=disconnect_all,
        )

        await orch.stop()  # must not raise

        clouds_stop.assert_awaited_once()
        disconnect_all.assert_awaited_once()
        http_session.close.assert_awaited_once()
        assert orch._http_session is None


# =============================================================================
# 4.8: stop() before start() is a safe no-op
# =============================================================================


class TestStopBeforeStart:
    """stop() on a freshly-constructed orchestrator is a safe no-op."""

    async def test_stop_before_start_is_safe_noop(self) -> None:
        clouds_stop = AsyncMock()
        disconnect_all = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        orch = _make_orchestrator(
            http_session=http_session,
            clouds_stop=clouds_stop,
            disconnect_all=disconnect_all,
        )

        assert orch._bg_jobs == set()

        await orch.stop()  # no error

        assert orch._stopped is True
        # Empty _bg_jobs loop was a no-op; cleanup steps ran on idle resources.
        clouds_stop.assert_awaited_once()
        disconnect_all.assert_awaited_once()
        http_session.close.assert_awaited_once()
        assert orch._http_session is None
