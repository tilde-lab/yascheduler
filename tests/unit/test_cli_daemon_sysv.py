# region MODULE_CONTRACT
# PURPOSE: Unit tests for yascheduler/entrypoints/cli/daemon_sysv.py — argparse, short flags, DaemonContext construction, configure_logger ordering.
# SCOPE: main() argparse behavior (--help, -p/-l short flags, --log-level long-only, defaults) and DaemonContext wiring with mocked daemon module.
# KEYWORDS: daemon_sysv, DaemonContext, short flags, configure_logger
# endregion MODULE_CONTRACT

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import MagicMock

import pytest

from yascheduler import LOG_FILE, PID_FILE
from yascheduler.entrypoints.cli import daemon_sysv as sysv_mod

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

pytestmark = pytest.mark.unit


def _run(argv: list[str]) -> None:
    sysv_mod.main(argv)


@pytest.fixture(autouse=True)
def _stub_daemon_module(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the real `daemon` module with a fake that records DaemonContext opens.

    The real python-daemon fork/detaches the process, which is hostile to unit tests.
    We inject a fake `daemon` module whose DaemonContext is a context manager that
    records its construction kwargs and whether the body ran.
    """
    fake_daemon: Any = types.ModuleType("daemon")
    fake_pidfile: Any = types.ModuleType("daemon.pidfile")

    context_state: dict = {}

    class FakeDaemonContext:
        def __init__(self, **kwargs: object) -> None:
            context_state["kwargs"] = kwargs
            context_state["entered"] = False

        def __enter__(self) -> FakeDaemonContext:
            context_state["entered"] = True
            return self

        def __exit__(self, *exc: object) -> Literal[False]:
            context_state["entered"] = False
            return False

    class FakeTimeoutPIDLockFile:
        def __init__(self, path: object) -> None:
            self.path = path

    fake_daemon.DaemonContext = FakeDaemonContext
    fake_pidfile.TimeoutPIDLockFile = FakeTimeoutPIDLockFile
    monkeypatch.setitem(sys.modules, "daemon", fake_daemon)
    monkeypatch.setitem(sys.modules, "daemon.pidfile", fake_pidfile)
    # Also patch the module-level references in daemon_sysv (imported at load time).
    monkeypatch.setattr(sysv_mod, "daemon", fake_daemon)
    monkeypatch.setattr(sysv_mod, "pidfile", fake_pidfile)
    return context_state


class TestDaemonSysvParsing:
    """argparse: --help, -p/-l short flags, --log-level long-only, defaults."""

    def test_help_exits_zero_prog_yascheduler(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--help"])
        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "usage: yascheduler" in out
        assert "-p" in out and "--pid-file" in out
        assert "-l" in out and "--log-file" in out
        assert "--config" in out
        assert "--log-level" in out

    def test_missing_config_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--config", "/nonexistent.conf"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert "not a file" in err
        assert "/nonexistent.conf" in err

    @pytest.mark.parametrize(
        ("extra_args", "expected_log_file"),
        [
            pytest.param(
                ["-p", "/var/run/yascheduler.pid", "-l", "/var/log/yascheduler.log"],
                "/var/log/yascheduler.log",
                id="short-flags",
            ),
            # -l is --log-file, --log-level is long-only → both parse cleanly.
            pytest.param(
                ["--log-level", "DEBUG", "-l", "/var/log/yascheduler.log"],
                "/var/log/yascheduler.log",
                id="log-level-long-only",
            ),
            pytest.param([], LOG_FILE, id="default-log-file"),
        ],
    )
    def test_parsed_log_file_reaches_configure_logger(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        extra_args: list[str],
        expected_log_file: str,
    ) -> None:
        cfg = tmp_path / "yascheduler.conf"
        cfg.write_text("[local]")
        cfg_logger_spy = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(sysv_mod, "configure_logger", cfg_logger_spy)
        monkeypatch.setattr(
            sysv_mod,
            "parse_config",
            MagicMock(return_value=MagicMock()),
        )

        def fake_run(coro: Coroutine) -> None:
            coro.close()

        monkeypatch.setattr(sysv_mod.asyncio, "run", fake_run)
        _run(["--config", str(cfg), *extra_args])
        assert cfg_logger_spy.call_args.args[0] == expected_log_file

    def test_default_pid_file_is_pid_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _stub_daemon_module: dict,
    ) -> None:
        cfg = tmp_path / "yascheduler.conf"
        cfg.write_text("[local]")
        monkeypatch.setattr(
            sysv_mod,
            "configure_logger",
            MagicMock(return_value=MagicMock()),
        )
        monkeypatch.setattr(
            sysv_mod,
            "parse_config",
            MagicMock(return_value=MagicMock()),
        )

        def fake_run(coro: Coroutine) -> None:
            coro.close()

        monkeypatch.setattr(sysv_mod.asyncio, "run", fake_run)
        _run(["--config", str(cfg)])
        # DaemonContext pidfile was constructed with PID_FILE.
        pidlockfile = _stub_daemon_module["kwargs"]["pidfile"]
        assert pidlockfile.path == PID_FILE


class TestDaemonSysvContext:
    """DaemonContext working_directory="/"; configure_logger called INSIDE the context."""

    def test_working_directory_is_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _stub_daemon_module: dict,
    ) -> None:
        cfg = tmp_path / "yascheduler.conf"
        cfg.write_text("[local]")
        monkeypatch.setattr(
            sysv_mod,
            "configure_logger",
            MagicMock(return_value=MagicMock()),
        )
        monkeypatch.setattr(
            sysv_mod,
            "parse_config",
            MagicMock(return_value=MagicMock()),
        )

        def fake_run(coro: Coroutine) -> None:
            coro.close()

        monkeypatch.setattr(sysv_mod.asyncio, "run", fake_run)
        _run(["--config", str(cfg)])
        assert _stub_daemon_module["kwargs"]["working_directory"] == "/"
        assert _stub_daemon_module["kwargs"]["umask"] == 0o002

    def test_configure_logger_called_inside_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _stub_daemon_module: dict,
    ) -> None:
        cfg = tmp_path / "yascheduler.conf"
        cfg.write_text("[local]")
        inside_when_called: list[bool] = []

        def spy_configure_logger(
            log_file: object, level: object, **kwargs: object
        ) -> MagicMock:
            inside_when_called.append(_stub_daemon_module["entered"])
            return MagicMock()

        monkeypatch.setattr(sysv_mod, "configure_logger", spy_configure_logger)
        monkeypatch.setattr(
            sysv_mod,
            "parse_config",
            MagicMock(return_value=MagicMock()),
        )

        def fake_run(coro: Coroutine) -> None:
            coro.close()

        monkeypatch.setattr(sysv_mod.asyncio, "run", fake_run)
        _run(["--config", str(cfg)])
        assert inside_when_called == [True]  # configure_logger ran inside the context
