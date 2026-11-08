# region MODULE_CONTRACT
# PURPOSE: Unit tests for yascheduler/entrypoints/cli/daemon_common.py — run_daemon with mocked DI (no real DB/SSH).
# SCOPE: run_daemon shape (async, awaits make_daemon + orch.start, owns signal handlers) with mocked make_daemon.
# KEYWORDS: daemon_common, run_daemon, signal handlers
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from yascheduler.entrypoints.cli import daemon_common
from yascheduler.infra import MigrationState, MigrationStatus

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _current_migration_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unrelated daemon-core tests independent of a real database."""
    monkeypatch.setattr(
        daemon_common,
        "check_migration_status",
        MagicMock(
            return_value=MigrationStatus(
                MigrationState.CURRENT,
                "014",
                "014",
            )
        ),
    )


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Iterator[None]:
    """Snapshot and restore the root logger so handler additions don't leak across tests."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        yield
    finally:
        root.setLevel(original_level)
        root.handlers = list(original_handlers)


class TestRunDaemonShape:
    """run_daemon: async, awaits make_daemon + orch.start, owns signal handlers."""

    def test_run_daemon_is_async_def(self) -> None:
        assert inspect.iscoroutinefunction(daemon_common.run_daemon)

    def test_run_daemon_awaits_make_daemon_and_orch_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch = MagicMock()
        orch.start = AsyncMock()
        orch.stop = AsyncMock()

        make_daemon_mock = AsyncMock(return_value=orch)
        monkeypatch.setattr(daemon_common, "make_daemon", make_daemon_mock)

        config = MagicMock()
        logger = logging.getLogger("test-run-daemon")

        asyncio.run(daemon_common.run_daemon(config, logger))

        make_daemon_mock.assert_awaited_once_with(config)
        orch.start.assert_awaited_once()

    @pytest.mark.parametrize(
        ("state", "applied", "expected_message"),
        [
            (MigrationState.MISSING, None, "migration metadata is absent"),
            (MigrationState.EMPTY, None, "migrations are unapplied"),
            (MigrationState.BEHIND, "011", "applied: 011; required: 014"),
        ],
    )
    def test_run_daemon_rejects_unmigrated_database_before_construction(
        self,
        monkeypatch: pytest.MonkeyPatch,
        state: MigrationState,
        applied: str | None,
        expected_message: str,
    ) -> None:
        make_daemon_mock = AsyncMock()
        monkeypatch.setattr(daemon_common, "make_daemon", make_daemon_mock)
        monkeypatch.setattr(
            daemon_common,
            "check_migration_status",
            MagicMock(return_value=MigrationStatus(state, applied, "014")),
        )

        config = MagicMock()
        config.db.password = "supersecret"
        with pytest.raises(
            daemon_common._DatabaseMigrationCompatibilityError,
            match=expected_message,
        ) as exc:
            asyncio.run(daemon_common.run_daemon(config, logging.getLogger("t")))

        assert "yainit --schema" in str(exc.value)
        assert "supersecret" not in str(exc.value)
        make_daemon_mock.assert_not_awaited()

    def test_run_daemon_rejects_newer_database_without_migration_advice(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        make_daemon_mock = AsyncMock()
        monkeypatch.setattr(daemon_common, "make_daemon", make_daemon_mock)
        monkeypatch.setattr(
            daemon_common,
            "check_migration_status",
            MagicMock(return_value=MigrationStatus(MigrationState.AHEAD, "015", "014")),
        )

        with pytest.raises(
            daemon_common._DatabaseMigrationCompatibilityError,
            match="Install a compatible yascheduler version",
        ) as exc:
            asyncio.run(daemon_common.run_daemon(MagicMock(), logging.getLogger("t")))

        assert "yainit --schema" not in str(exc.value)
        make_daemon_mock.assert_not_awaited()

    def test_run_daemon_propagates_preflight_database_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        make_daemon_mock = AsyncMock()
        monkeypatch.setattr(daemon_common, "make_daemon", make_daemon_mock)
        monkeypatch.setattr(
            daemon_common,
            "check_migration_status",
            MagicMock(side_effect=RuntimeError("permission denied for scheduler")),
        )

        with (
            caplog.at_level(logging.ERROR, logger="test-daemon-preflight"),
            pytest.raises(RuntimeError, match="permission denied for scheduler"),
        ):
            asyncio.run(
                daemon_common.run_daemon(
                    MagicMock(),
                    logging.getLogger("test-daemon-preflight"),
                )
            )

        assert "database migration preflight failed" in caplog.text
        assert "permission denied for scheduler" in caplog.text
        make_daemon_mock.assert_not_awaited()

    def test_run_daemon_owns_signal_handlers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The signal handlers are registered on the running loop inside run_daemon;
        # verify by spying on loop.add_signal_handler.
        orch = MagicMock()
        orch.start = AsyncMock()
        orch.stop = AsyncMock()
        monkeypatch.setattr(daemon_common, "make_daemon", AsyncMock(return_value=orch))

        registered: list = []

        async def _drive() -> None:
            loop = asyncio.get_running_loop()

            def fake_add_signal_handler(
                sig: object,
                handler: object,
                *args: object,
            ) -> None:
                registered.append((sig, handler))

            # Bypass the strict asyncio type stub by going through object.__setattr__.
            object.__setattr__(loop, "add_signal_handler", fake_add_signal_handler)
            await daemon_common.run_daemon(MagicMock(), logging.getLogger("t"))

        asyncio.run(_drive())

        # SIGTERM and SIGINT registered (signal numbers may differ by platform but the
        # constants are importable).
        import signal as _signal

        sigs = [sig for sig, _ in registered]
        assert _signal.SIGTERM in sigs
        assert _signal.SIGINT in sigs

    def test_each_signal_handler_dispatches_its_own_signal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: the signal-handler closure must bind `sig` by value, not by
        reference. A bare closure over the loop variable would dispatch SIGINT for
        both signals (the loop's final value). Verify each handler produces a task
        whose wrapped coroutine carries the CORRECT signal.
        """
        import signal as _signal

        orch = MagicMock()
        orch.start = AsyncMock()
        orch.stop = AsyncMock()
        monkeypatch.setattr(daemon_common, "make_daemon", AsyncMock(return_value=orch))

        registered: dict = {}

        async def _drive() -> None:
            loop = asyncio.get_running_loop()

            def fake_add_signal_handler(
                sig: object,
                handler: object,
                *args: object,
            ) -> None:
                registered[_signal.Signals(sig)] = handler

            object.__setattr__(loop, "add_signal_handler", fake_add_signal_handler)
            await daemon_common.run_daemon(MagicMock(), logging.getLogger("t"))

        asyncio.run(_drive())

        # Each handler is a zero-arg callable that creates a task wrapping on_signal.
        # Inspect the on_signal call's `sig` argument via the closure cells.
        for sig, handler in registered.items():
            # The handler closure captures `on_signal`, `orch`, `shielded`, and `sig`.
            # The created task wraps `on_signal(orch, shielded, sig)`. We verify the
            # `sig` cell holds the same value as the registration key (not the loop's
            # final value).
            # Easiest robust check: call the handler and inspect the produced coroutine's
            # cr_frame locals is fragile; instead verify the closure cell for `sig`.
            closure = handler.__closure__
            assert closure is not None, "handler must be a closure"
            # Find the cell whose contents equals `sig` — the factory binds it by value.
            cell_values = [c.cell_contents for c in closure]
            assert sig in cell_values, (
                f"handler for {sig.name} did not bind sig by value; "
                f"closure holds {cell_values}"
            )
