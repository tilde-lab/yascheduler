# region MODULE_CONTRACT
# PURPOSE: Unit tests for yasubmit submit() argparse, content validation, exit codes, helpers, and AiiDA stdout contract.
# SCOPE: submit() argparse, content validation, exit codes, AiiDA stdout contract, and metadata helpers with mocked Config/CLIDeps.
# KEYWORDS: yasubmit, submit, argparse, AiiDA stdout contract
# endregion MODULE_CONTRACT

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from yascheduler.domain import (
    Engine,
    EngineRepository,
    TaskId,
    TaskInputDirectory,
    TaskInputFile,
    TaskInputSnapshot,
)
from yascheduler.entrypoints.di import CLIDeps

submit_mod = importlib.import_module("yascheduler.entrypoints.cli.submit")

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Mock helpers (mirror tests/unit/test_cli_behavioral.py)
# ---------------------------------------------------------------------------


def make_mock_config(
    webhook_url: str | None = None,
    *,
    input_files: tuple[str, ...] = ("input",),
) -> MagicMock:
    """Return a MagicMock Config with a known g09 engine and optional webhook_url."""
    engine = Engine(
        name="g09",
        spawn="run.sh",
        input_files=input_files,
        output_files=("OUTPUT",),
        platforms=("linux",),
        check_cmd="echo",
        check_pname=None,
    )

    engines = MagicMock(spec=EngineRepository)
    engines.get = MagicMock(return_value=engine)

    config = MagicMock()
    config.engines = engines
    config.clouds = []
    config.remote.username = "root"
    config.remote.engines_dir = "/opt/engines"
    config.remote.tasks_dir = Path("/tmp/tasks")
    config.local.webhook_url = webhook_url
    config.local.data_dir = "/tmp"
    config.db = MagicMock()
    return config


def make_mock_deps(config: MagicMock) -> MagicMock:
    """Return a MagicMock CLIDeps with deps.submit returning 42."""
    deps = MagicMock(spec=CLIDeps)
    deps.submit = AsyncMock(return_value=TaskId(42))
    deps.engines = config.engines
    return deps


@pytest.fixture
def stub_config_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock]:
    """Patch Config.from_config_parser and make_cli_deps; return (config, deps)."""
    config = make_mock_config()
    deps = make_mock_deps(config)
    monkeypatch.setattr(submit_mod, "parse_config", MagicMock(return_value=config))
    monkeypatch.setattr(submit_mod, "make_cli_deps", MagicMock(return_value=deps))
    return config, deps


def _run(argv: list[str]) -> None:
    """Invoke submit_mod.submit(argv); raise if it swallows SystemExit unexpectedly."""
    submit_mod.submit(argv)


def _use_engine_inputs(config: MagicMock, input_files: tuple[str, ...]) -> None:
    """Replace the mocked repository engine's input declarations."""
    config.engines.get = MagicMock(
        return_value=Engine(
            name="g09",
            spawn="run.sh",
            input_files=input_files,
            output_files=("OUTPUT",),
            platforms=("linux",),
        )
    )


def _submitted_input_entries(deps: MagicMock) -> TaskInputSnapshot:
    """Return the typed input snapshot passed to deps.submit."""
    call = deps.submit.call_args
    assert call is not None
    entries = call.kwargs["input_entries"]
    assert isinstance(entries, tuple)
    return entries


# ---------------------------------------------------------------------------
# Parsing / exit codes (tasks 6.4)
# ---------------------------------------------------------------------------


class TestSubmitParsing:
    """argparse behavior: --help, no-args, missing file, extra positional, unknown flag."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--help"])
        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "yasubmit" in out  # prog name
        assert "script" in out  # positional argument

    def test_help_shows_prog_yasubmit(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _run(["--help"])
        out, _ = capsys.readouterr()
        # usage line starts with "yasubmit" not the console_script path
        assert "usage: yasubmit" in out

    def test_no_args_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _run([])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert err.strip()  # argparse usage error

    def test_nonexistent_file_exits_two(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["/nonexistent/script.in"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert "not a file" in err

    def test_extra_positional_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        script = tmp_path / "script.in"
        script.write_text("ENGINE = g09\n")
        with pytest.raises(SystemExit) as exc:
            _run([str(script), str(script)])
        assert exc.value.code == 2

    def test_unknown_flag_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--bogus"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert err.strip()


# ---------------------------------------------------------------------------
# Happy path (tasks 6.5)
# ---------------------------------------------------------------------------


class TestSubmitHappyPath:
    """Valid script → stdout str(task_id), deps.submit called correctly, exit 0."""

    def test_success_prints_task_id_only(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        script = tmp_path / "test.in"
        script.write_text("LABEL = Test job\nENGINE = g09\n")
        (tmp_path / "input").write_text("dummy input")
        monkeypatch.chdir(tmp_path)

        _run([str(script)])  # no SystemExit on success

        out, err = capsys.readouterr()
        assert out.strip() == "42"
        assert err == ""

    def test_submit_called_with_correct_args(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        _config, deps = stub_config_deps
        script = tmp_path / "test.in"
        script.write_text("LABEL = Test job\nENGINE = g09\n")
        (tmp_path / "input").write_text("dummy input")
        monkeypatch.chdir(tmp_path)

        _run([str(script)])

        deps.submit.assert_called_once()
        call_args = deps.submit.call_args
        assert call_args[0][0] == "Test job"  # label
        assert call_args[0][2] == "g09"  # engine_name
        metadata = call_args[0][1]
        assert metadata["local_folder"] == str(tmp_path)
        assert "input" not in metadata
        assert call_args.kwargs["input_entries"] == (
            TaskInputFile(path="input", content=b"dummy input"),
        )

    def test_success_stdout_is_exact_bare_task_id(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)

        _run([str(script)])

        out, err = capsys.readouterr()
        assert out == "42\n"
        assert err == ""

    def test_default_label_when_missing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        _config, deps = stub_config_deps
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)

        _run([str(script)])

        deps.submit.assert_called_once()
        assert deps.submit.call_args[0][0] == "AiiDA job"


# ---------------------------------------------------------------------------
# Local input-snapshot capture (task 2.3)
# ---------------------------------------------------------------------------


class TestSubmitSnapshotCapture:
    """yasubmit captures typed input entries from CWD before deps.submit."""

    def test_binary_input_bytes_are_preserved_exactly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        config, deps = stub_config_deps
        _use_engine_inputs(config, ("binary.dat",))
        payload = b"\xff\xfe\x00\x01"
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "binary.dat").write_bytes(payload)
        monkeypatch.chdir(tmp_path)

        _run([str(script)])

        assert _submitted_input_entries(deps) == (
            TaskInputFile(path="binary.dat", content=payload),
        )

    def test_tree_inputs_include_hidden_and_empty_directories(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        config, deps = stub_config_deps
        _use_engine_inputs(config, ("INPUT", "basis"))
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "INPUT").write_bytes(b"root")
        basis = tmp_path / "basis"
        (basis / "deep").mkdir(parents=True)
        (basis / ".hidden").write_bytes(b"hidden")
        (basis / "deep" / "child.orb").write_bytes(b"child")
        (basis / "empty").mkdir()
        (basis / "visible.txt").write_bytes(b"visible")
        monkeypatch.chdir(tmp_path)

        _run([str(script)])

        assert _submitted_input_entries(deps) == (
            TaskInputFile(path="INPUT", content=b"root"),
            TaskInputDirectory(path="basis"),
            TaskInputFile(path="basis/.hidden", content=b"hidden"),
            TaskInputDirectory(path="basis/deep"),
            TaskInputFile(path="basis/deep/child.orb", content=b"child"),
            TaskInputDirectory(path="basis/empty"),
            TaskInputFile(path="basis/visible.txt", content=b"visible"),
        )

    def test_glob_declaration_captures_matches_and_materializes_parents(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        config, deps = stub_config_deps
        _use_engine_inputs(config, ("basis/**/*.orb",))
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        basis = tmp_path / "basis"
        (basis / "deep").mkdir(parents=True)
        (basis / "a.orb").write_bytes(b"a")
        (basis / "deep" / "b.orb").write_bytes(b"b")
        monkeypatch.chdir(tmp_path)

        _run([str(script)])

        assert _submitted_input_entries(deps) == (
            TaskInputDirectory(path="basis"),
            TaskInputFile(path="basis/a.orb", content=b"a"),
            TaskInputDirectory(path="basis/deep"),
            TaskInputFile(path="basis/deep/b.orb", content=b"b"),
        )

    def test_inputs_resolve_from_cwd_not_script_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        _config, deps = stub_config_deps
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        script = scripts / "test.in"
        script.write_text("ENGINE = g09\n")
        (cwd / "input").write_bytes(b"from-cwd")
        monkeypatch.chdir(cwd)

        _run([str(script)])

        assert _submitted_input_entries(deps) == (
            TaskInputFile(path="input", content=b"from-cwd"),
        )

    def test_input_next_to_script_is_not_found_when_cwd_differs(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        script = scripts / "test.in"
        script.write_text("ENGINE = g09\n")
        (scripts / "input").write_bytes(b"script-dir-input")
        monkeypatch.chdir(cwd)

        with pytest.raises(SystemExit) as exc:
            _run([str(script)])

        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert err.startswith("Error:")
        assert "input" in err

    def test_engine_without_inputs_forwards_empty_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        config, deps = stub_config_deps
        _use_engine_inputs(config, ())
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        monkeypatch.chdir(tmp_path)

        _run([str(script)])

        assert _submitted_input_entries(deps) == ()
        assert deps.submit.call_args is not None
        assert deps.submit.call_args.args[1] == {"local_folder": str(tmp_path)}


class TestSubmitSnapshotFailures:
    """Capture failures keep the CLI runtime-error contract."""

    def test_missing_required_input_exits_one_with_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        with pytest.raises(SystemExit) as exc:
            _run([str(script)])

        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert err.startswith("Error:")
        assert "input" in err

    def test_selected_symlink_exits_one_with_path(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        target = tmp_path / "target"
        target.write_bytes(b"link-target")
        (cwd / "input").symlink_to(target)
        monkeypatch.chdir(cwd)

        with pytest.raises(SystemExit) as exc:
            _run([str(script)])

        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert err.startswith("Error:")
        assert "input" in err
        assert "symbolic link" in err.lower()


# ---------------------------------------------------------------------------
# Content validation (tasks 6.6)
# ---------------------------------------------------------------------------


class TestSubmitContentValidation:
    """ENGINE key missing / engine name unknown → exit 1, stderr message, stdout empty."""

    def test_engine_key_missing_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        script = tmp_path / "test.in"
        script.write_text("LABEL = Test job\nSOMETHING = else\n")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exc:
            _run([str(script)])
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert "Script has not defined an engine" in err

    def test_unknown_engine_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        config, _deps = stub_config_deps
        config.engines.get = MagicMock(return_value=None)
        script = tmp_path / "test.in"
        script.write_text("LABEL = Test\nENGINE = unknown\n")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exc:
            _run([str(script)])
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert "Engine unknown is not supported" in err


# ---------------------------------------------------------------------------
# Webhook branch of _build_metadata (tasks 6.7)
# ---------------------------------------------------------------------------


class TestSubmitWebhook:
    """_build_metadata webhook branch — PARENT + webhook_url, PARENT absent, webhook_url None."""

    def test_webhook_added_when_parent_and_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = make_mock_config(webhook_url="https://example.com/hook")
        monkeypatch.chdir(tmp_path)
        (tmp_path / "input").write_text("data")

        metadata = submit_mod._build_metadata(
            {"ENGINE": "g09", "PARENT": "42"},
            config,
            str(tmp_path),
        )
        assert metadata["webhook_url"] == "https://example.com/hook"
        assert metadata["webhook_custom_params"] == {"parent": "42"}

    def test_no_webhook_when_parent_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = make_mock_config(webhook_url="https://example.com/hook")
        (tmp_path / "input").write_text("data")
        monkeypatch.chdir(tmp_path)

        metadata = submit_mod._build_metadata({"ENGINE": "g09"}, config, str(tmp_path))
        assert "webhook_url" not in metadata
        assert "webhook_custom_params" not in metadata

    def test_no_webhook_when_url_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = make_mock_config(webhook_url=None)
        (tmp_path / "input").write_text("data")
        monkeypatch.chdir(tmp_path)

        metadata = submit_mod._build_metadata(
            {"ENGINE": "g09", "PARENT": "42"},
            config,
            str(tmp_path),
        )
        assert "webhook_url" not in metadata
        assert "webhook_custom_params" not in metadata


# ---------------------------------------------------------------------------
# Helper functions (tasks 6.8)
# ---------------------------------------------------------------------------


class TestSubmitHelpers:
    """_parse_script_metadata and _build_metadata pure behavior."""

    def test_parse_script_metadata_key_value(self) -> None:
        result = submit_mod._parse_script_metadata("LABEL = Test job\nENGINE = g09\n")
        assert result == {"LABEL": "Test job", "ENGINE": "g09"}

    def test_parse_script_metadata_malformed_ignored(self) -> None:
        result = submit_mod._parse_script_metadata(
            "LABEL = Test\nmalformed line\nENGINE = g09\n",
        )
        assert result == {"LABEL": "Test", "ENGINE": "g09"}

    def test_parse_script_metadata_empty(self) -> None:
        assert submit_mod._parse_script_metadata("") == {}

    def test_build_metadata_local_folder_always_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = make_mock_config(webhook_url=None)
        (tmp_path / "input").write_text("data")
        monkeypatch.chdir(tmp_path)

        metadata = submit_mod._build_metadata({"ENGINE": "g09"}, config, str(tmp_path))
        assert metadata["local_folder"] == str(tmp_path)

    def test_build_metadata_never_contains_input_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = make_mock_config(webhook_url=None)
        (tmp_path / "input").write_text("merged-content")
        monkeypatch.chdir(tmp_path)

        metadata = submit_mod._build_metadata({"ENGINE": "g09"}, config, str(tmp_path))

        assert metadata == {"local_folder": str(tmp_path)}
        assert "input" not in metadata


# ---------------------------------------------------------------------------
# Exit codes (tasks 6.9)
# ---------------------------------------------------------------------------


class TestSubmitExitCodes:
    """Exit-code contract: 0 success, 1 runtime error, 2 argparse error."""

    def test_success_exits_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)

        # No SystemExit raised on success → implicit exit 0.
        _run([str(script)])

    def test_db_error_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        _config, deps = stub_config_deps
        deps.submit = AsyncMock(side_effect=RuntimeError("db down"))
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exc:
            _run([str(script)])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err

    def test_config_error_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            submit_mod,
            "parse_config",
            MagicMock(side_effect=RuntimeError("bad config")),
        )
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")

        with pytest.raises(SystemExit) as exc:
            _run([str(script)])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err

    def test_unexpected_exception_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        _config, deps = stub_config_deps
        deps.submit = AsyncMock(side_effect=ValueError("unexpected"))
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exc:
            _run([str(script)])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err

    def test_argparse_error_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _run([])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# argv injection (tasks 6.10)
# ---------------------------------------------------------------------------


class TestSubmitArgvInjection:
    """submit(argv) takes an explicit argv list — no patch('sys.argv') needed."""

    def test_explicit_argv_no_sys_argv_patch(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        # Deliberately set sys.argv to something unrelated to prove argv wins.
        monkeypatch.setattr("sys.argv", ["python", "-c", "unrelated"])
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)

        _run([str(script)])

        out, _ = capsys.readouterr()
        assert out.strip() == "42"

    def test_explicit_argv_with_multiple_args(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["python"])
        script = tmp_path / "test.in"
        script.write_text("ENGINE = g09\n")

        with pytest.raises(SystemExit) as exc:
            _run([str(script), "extra.in"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# --config / --log-level scenarios (consolidate-daemon-entrypoints)
# ---------------------------------------------------------------------------


class TestSubmitConfigLogLevel:
    """--config and --log-level argparse + behavior scenarios."""

    def test_help_lists_config_and_log_level(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--help"])
        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "--config" in out
        assert "--log-level" in out

    def test_config_nonexistent_exits_two(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        script = tmp_path / "s.in"
        script.write_text("ENGINE = g09\n")
        with pytest.raises(SystemExit) as exc:
            _run([str(script), "--config", "/nonexistent.conf"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert "not a file" in err
        assert "/nonexistent.conf" in err

    def test_log_level_warn_rejected_exits_two(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        script = tmp_path / "s.in"
        script.write_text("ENGINE = g09\n")
        with pytest.raises(SystemExit) as exc:
            _run([str(script), "--log-level", "WARN"])
        assert exc.value.code == 2

    def test_log_level_debug_sets_root_to_debug(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, MagicMock],
    ) -> None:
        import logging

        root = logging.getLogger()
        original_level = root.level
        script = tmp_path / "s.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)
        try:
            _run([str(script), "--log-level", "DEBUG"])
            assert root.level == logging.DEBUG
        finally:
            root.setLevel(original_level)

    def test_config_custom_passed_to_from_config_parser(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        script = tmp_path / "s.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)
        custom_conf = tmp_path / "custom.conf"
        custom_conf.write_text("[local]")
        from_config_spy = MagicMock(return_value=make_mock_config())
        monkeypatch.setattr(submit_mod, "parse_config", from_config_spy)
        monkeypatch.setattr(
            submit_mod,
            "make_cli_deps",
            MagicMock(return_value=make_mock_deps(make_mock_config())),
        )
        _run([str(script), "--config", str(custom_conf)])
        from_config_spy.assert_called_once_with(custom_conf)

    def test_default_config_is_config_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from yascheduler import CONFIG_FILE

        script = tmp_path / "s.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)
        from_config_spy = MagicMock(return_value=make_mock_config())
        monkeypatch.setattr(submit_mod, "parse_config", from_config_spy)
        monkeypatch.setattr(
            submit_mod,
            "make_cli_deps",
            MagicMock(return_value=make_mock_deps(make_mock_config())),
        )
        _run([str(script)])
        # Default --config is CONFIG_FILE (a string path).
        called_with = from_config_spy.call_args.args[0]
        # existing_path returns Path; default is the CONFIG_FILE string, converted by argparse type.
        assert str(called_with) == str(CONFIG_FILE)

    def test_default_log_level_is_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import logging

        root = logging.getLogger()
        original_level = root.level
        script = tmp_path / "s.in"
        script.write_text("ENGINE = g09\n")
        (tmp_path / "input").write_text("x")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            submit_mod,
            "parse_config",
            MagicMock(return_value=make_mock_config()),
        )
        monkeypatch.setattr(
            submit_mod,
            "make_cli_deps",
            MagicMock(return_value=make_mock_deps(make_mock_config())),
        )
        try:
            _run([str(script)])
            assert root.level == logging.WARNING
        finally:
            root.setLevel(original_level)
