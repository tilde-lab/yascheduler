"""Unit tests for yainit (entrypoints/cli/init.py).

Covers flag parsing, dispatch, exit codes (0/1/2), service overwrite, and
systemd-vs-sysv detection. apply_schema and the service helpers are mocked
or injected via the public unit_file/startup_file parameters.
"""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for yainit init() flag parsing, dispatch, exit codes, and service overwrite behavior.
# SCOPE: init() flag parsing, dispatch, exit codes, service overwrite behavior with mocked apply_schema + filesystem.
# KEYWORDS: yainit, init, flag parsing, service overwrite, apply_schema
# endregion MODULE_CONTRACT

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pg8000 import DatabaseError

from yascheduler.entrypoints.cli.init import (
    _init_schema,
    _init_systemd,
    _init_sysv,
    init,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def install_path() -> Path:
    """Real yascheduler/ package root — same as init() computes."""
    return Path(__file__).resolve().parent.parent.parent / "yascheduler"


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid touching the real CONFIG_FILE: stub Config.from_config_parser."""
    monkeypatch.setattr(
        "yascheduler.entrypoints.cli.init.parse_config",
        MagicMock(return_value=MagicMock(db=MagicMock())),
    )


@pytest.fixture(autouse=True)
def _stub_apply_migrations(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub apply_migrations so _init_schema tests don't open a real DB connection.

    add-db-migrations made _init_schema call apply_migrations after apply_schema.
    Tests that assert on apply_schema call counts are unaffected; tests that need
    apply_schema to raise keep their own apply_schema mock (it raises first, so
    apply_migrations is never reached). The stub is returned so tests can assert
    which engine_inputs context _init_schema forwards.
    """
    apply_migrations_mock = MagicMock()
    monkeypatch.setattr(
        "yascheduler.entrypoints.cli.init.apply_migrations",
        apply_migrations_mock,
    )
    return apply_migrations_mock


class TestInitFlags:
    """Flag parsing and dispatch."""

    @pytest.mark.parametrize(
        ("argv", "service_calls", "schema_calls"),
        [
            pytest.param([], 1, 1, id="no-flags"),
            pytest.param(["--schema"], 0, 1, id="schema-only"),
            pytest.param(["--daemon"], 1, 0, id="daemon-only"),
            pytest.param(["--schema", "--daemon"], 1, 1, id="both-flags"),
        ],
    )
    def test_flag_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        service_calls: int,
        schema_calls: int,
    ) -> None:
        """Each flag combination runs the right halves and exits 0."""
        systemd_mock = MagicMock()
        sysv_mock = MagicMock()
        apply_mock = MagicMock()
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init._init_systemd",
            systemd_mock,
        )
        monkeypatch.setattr("yascheduler.entrypoints.cli.init._init_sysv", sysv_mock)
        monkeypatch.setattr("yascheduler.entrypoints.cli.init.apply_schema", apply_mock)
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init.Path.is_dir",
            lambda self: True,
        )

        with pytest.raises(SystemExit) as exc:
            init(argv)

        assert exc.value.code == 0
        assert systemd_mock.call_count == service_calls
        assert sysv_mock.call_count == 0
        assert apply_mock.call_count == schema_calls

    def test_help_exits_zero(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--help → argparse prints usage mentioning --schema and --daemon; exit 0."""
        with pytest.raises(SystemExit) as exc:
            init(["--help"])

        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "--schema" in out
        assert "--daemon" in out


class TestInitErrors:
    """Exit-code contract on DatabaseError and OSError."""

    def test_database_error_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """DatabaseError from apply_schema → init prints error, exits 1."""
        apply_mock = MagicMock(side_effect=DatabaseError("connection refused"))
        monkeypatch.setattr("yascheduler.entrypoints.cli.init.apply_schema", apply_mock)

        with pytest.raises(SystemExit) as exc:
            init(["--schema"])

        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error" in err

    def test_service_write_oserror_exits_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """OSError writing service file → init prints 'Error: cannot write to'; exit 1."""
        # Force systemd path and make write_text raise OSError
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init.Path.is_dir",
            lambda self: True,
        )
        with (
            patch(
                "yascheduler.entrypoints.cli.init.Path.write_text",
                side_effect=OSError("Permission denied"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            init(["--daemon"])

        assert exc.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert "Error: cannot write to" in combined


class TestServiceInstall:
    """Service file overwrite, missing parent, and detection."""

    def test_overwrites_existing_systemd_unit(
        self,
        install_path: Path,
        tmp_path: Path,
    ) -> None:
        """_init_systemd overwrites an existing unit file with the fresh template."""
        unit_file = tmp_path / "yascheduler.service"
        unit_file.write_text("STALE CONTENT\n")

        _init_systemd(install_path, unit_file=unit_file)

        content = unit_file.read_text("utf-8")
        assert "STALE CONTENT" not in content
        assert "%YASCHEDULER_DAEMON_FILE%" not in content
        assert "daemon_systemd.py" in content

    def test_overwrites_existing_sysv_script(
        self,
        install_path: Path,
        tmp_path: Path,
    ) -> None:
        """_init_sysv overwrites an existing init script and applies chmod 0755."""
        startup_file = tmp_path / "yascheduler"
        startup_file.write_text("STALE CONTENT\n")

        _init_sysv(install_path, startup_file=startup_file)

        content = startup_file.read_text("utf-8")
        assert "STALE CONTENT" not in content
        assert "%YASCHEDULER_DAEMON_FILE%" not in content
        assert "daemon_sysv.py" in content
        mode = stat.S_IMODE(startup_file.stat().st_mode)
        assert mode == 0o755

    def test_missing_parent_dir_exits_one(
        self,
        install_path: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing parent dir → write_text raises OSError → exit 1 with message."""
        unit_file = tmp_path / "nonexistent_dir" / "yascheduler.service"

        with pytest.raises(SystemExit) as exc:
            _init_systemd(install_path, unit_file=unit_file)

        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error: cannot write to" in err

    @pytest.mark.parametrize(
        ("run_dir_exists", "systemd_calls", "sysv_calls"),
        [
            pytest.param(True, 1, 0, id="systemd-run-dir-present"),
            pytest.param(False, 0, 1, id="systemd-run-dir-absent"),
        ],
    )
    def test_service_detection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_dir_exists: bool,
        systemd_calls: int,
        sysv_calls: int,
    ) -> None:
        """/run/systemd/system presence selects the service flavor."""
        systemd_mock = MagicMock()
        sysv_mock = MagicMock()
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init._init_systemd",
            systemd_mock,
        )
        monkeypatch.setattr("yascheduler.entrypoints.cli.init._init_sysv", sysv_mock)
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init.Path.is_dir",
            lambda self: run_dir_exists,
        )

        with pytest.raises(SystemExit) as exc:
            init(["--daemon"])

        assert exc.value.code == 0
        assert systemd_mock.call_count == systemd_calls
        assert sysv_mock.call_count == sysv_calls

    def test_schema_idempotent_rerun(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-running --schema twice succeeds both times (schema.sql idempotency)."""
        apply_mock = MagicMock()
        monkeypatch.setattr("yascheduler.entrypoints.cli.init.apply_schema", apply_mock)

        for _ in range(2):
            with pytest.raises(SystemExit) as exc:
                init(["--schema"])
            assert exc.value.code == 0

        assert apply_mock.call_count == 2


# ---------------------------------------------------------------------------
# --config / --log-level scenarios (consolidate-daemon-entrypoints)
# ---------------------------------------------------------------------------


class TestInitConfigLogLevel:
    """--config and --log-level argparse + behavior scenarios (defaults WARNING)."""

    def test_config_custom_passed_to_init_schema(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """--config /custom.conf is passed through _init_schema(config_path) to parse_config."""
        custom_conf = tmp_path / "custom.conf"
        custom_conf.write_text("[local]")
        from_config_spy = MagicMock(return_value=MagicMock(db=MagicMock()))
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init.parse_config",
            from_config_spy,
        )
        apply_mock = MagicMock()
        monkeypatch.setattr("yascheduler.entrypoints.cli.init.apply_schema", apply_mock)

        with pytest.raises(SystemExit) as exc:
            init(["--schema", "--config", str(custom_conf)])
        assert exc.value.code == 0
        apply_mock.assert_called_once()
        # parse_config was called with the custom config path (via _init_schema).
        called_paths = [call.args[0] for call in from_config_spy.call_args_list]
        assert any(str(custom_conf) == str(p) for p in called_paths), (
            f"parse_config not called with {custom_conf}; got {called_paths}"
        )

    def test_default_config_is_config_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from yascheduler import CONFIG_FILE

        from_config_spy = MagicMock(return_value=MagicMock(db=MagicMock()))
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init.parse_config",
            from_config_spy,
        )
        apply_mock = MagicMock()
        monkeypatch.setattr("yascheduler.entrypoints.cli.init.apply_schema", apply_mock)

        with pytest.raises(SystemExit) as exc:
            init(["--schema"])
        assert exc.value.code == 0
        # Default --config is CONFIG_FILE; _init_schema receives it as config_path.
        called_paths = [str(call.args[0]) for call in from_config_spy.call_args_list]
        assert str(CONFIG_FILE) in called_paths

    def test_default_log_level_is_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import logging

        from_config_spy = MagicMock(return_value=MagicMock(db=MagicMock()))
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init.parse_config",
            from_config_spy,
        )
        apply_mock = MagicMock()
        monkeypatch.setattr("yascheduler.entrypoints.cli.init.apply_schema", apply_mock)
        root = logging.getLogger()
        original_level = root.level
        try:
            with pytest.raises(SystemExit) as exc:
                init(["--schema"])
            assert exc.value.code == 0
            assert root.level == logging.WARNING
        finally:
            root.setLevel(original_level)

    def test_daemon_systemd_path_assertion_unchanged(
        self,
        install_path: Path,
        tmp_path: Path,
    ) -> None:
        """The daemon_systemd.py path assertion (relocate-daemon-launchers-to-cli) remains valid."""
        unit_file = tmp_path / "yascheduler.service"
        _init_systemd(install_path, unit_file=unit_file)
        content = unit_file.read_text("utf-8")
        assert "daemon_systemd.py" in content

    def test_daemon_sysv_path_assertion_unchanged(
        self,
        install_path: Path,
        tmp_path: Path,
    ) -> None:
        """The daemon_sysv.py path assertion (relocate-daemon-launchers-to-cli) remains valid."""
        startup_file = tmp_path / "yascheduler"
        _init_sysv(install_path, startup_file=startup_file)
        content = startup_file.read_text("utf-8")
        assert "daemon_sysv.py" in content


class TestInitSchemaEngineInputs:
    """_init_schema forwards engine input declarations to migrations."""

    def test_passes_engine_inputs_from_parsed_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _stub_apply_migrations: MagicMock,
    ) -> None:
        """Parsed engines' input_files are passed as engine_inputs to apply_migrations."""
        db = MagicMock()
        engines = {
            "dummy": MagicMock(input_files=("fort.9", "fort.10")),
            "gaussian": MagicMock(input_files=("input.gjf",)),
        }
        config = MagicMock(db=db, engines=engines)
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init.parse_config",
            MagicMock(return_value=config),
        )
        monkeypatch.setattr(
            "yascheduler.entrypoints.cli.init.apply_schema",
            MagicMock(),
        )

        _init_schema()

        _stub_apply_migrations.assert_called_once_with(
            db,
            engine_inputs={
                "dummy": ("fort.9", "fort.10"),
                "gaussian": ("input.gjf",),
            },
        )
