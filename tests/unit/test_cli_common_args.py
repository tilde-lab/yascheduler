# region MODULE_CONTRACT
# PURPOSE: Table-driven check of the arguments every CLI command shares, so a command that stops registering --config or --log-level fails once here instead of passing five copies of the same test.
# SCOPE: --help output, nonexistent --config, rejected --log-level WARN, unknown flag — for init, check_status, manage_node, submit, show_nodes. Scenarios that need stubbed repositories stay in the per-command files.
# KEYWORDS: CLI argparse, --config, --log-level, shared arguments
# endregion MODULE_CONTRACT

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ArgvBuilder = Callable[[Path], list[str]]


def _no_args(_tmp_path: Path) -> list[str]:
    return []


def _script_args(tmp_path: Path) -> list[str]:
    """submit takes its input script positionally, ahead of the shared flags."""
    script = tmp_path / "s.in"
    script.write_text("ENGINE = g09\n")
    return [str(script)]


def _host_args(_tmp_path: Path) -> list[str]:
    """manage_node takes a host spec positionally, ahead of the shared flags."""
    return ["10.0.0.1"]


COMMANDS = [
    pytest.param("yascheduler.entrypoints.cli.init", "init", _no_args, id="init"),
    pytest.param(
        "yascheduler.entrypoints.cli.check_status",
        "check_status",
        _no_args,
        id="check_status",
    ),
    pytest.param(
        "yascheduler.entrypoints.cli.manage_node",
        "manage_node",
        _host_args,
        id="manage_node",
    ),
    pytest.param(
        "yascheduler.entrypoints.cli.submit", "submit", _script_args, id="submit"
    ),
    pytest.param(
        "yascheduler.entrypoints.cli.show_nodes",
        "show_nodes",
        _no_args,
        id="show_nodes",
    ),
]


def _entry(module_name: str, func_name: str) -> Any:
    return getattr(importlib.import_module(module_name), func_name)


@pytest.mark.parametrize(("module_name", "func_name", "base_argv"), COMMANDS)
def test_help_lists_shared_arguments(
    module_name: str,
    func_name: str,
    base_argv: ArgvBuilder,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as exc:
        _entry(module_name, func_name)([*base_argv(tmp_path), "--help"])
    assert exc.value.code == 0
    out, _ = capsys.readouterr()
    assert "--config" in out
    assert "--log-level" in out


@pytest.mark.parametrize(("module_name", "func_name", "base_argv"), COMMANDS)
def test_nonexistent_config_exits_two(
    module_name: str,
    func_name: str,
    base_argv: ArgvBuilder,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    argv = [*base_argv(tmp_path), "--config", "/nonexistent.conf"]
    with pytest.raises(SystemExit) as exc:
        _entry(module_name, func_name)(argv)
    assert exc.value.code == 2
    _, err = capsys.readouterr()
    assert "not a file" in err
    assert "/nonexistent.conf" in err


@pytest.mark.parametrize(("module_name", "func_name", "base_argv"), COMMANDS)
def test_log_level_warn_alias_rejected(
    module_name: str,
    func_name: str,
    base_argv: ArgvBuilder,
    tmp_path: Path,
) -> None:
    argv = [*base_argv(tmp_path), "--log-level", "WARN"]
    with pytest.raises(SystemExit) as exc:
        _entry(module_name, func_name)(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize(("module_name", "func_name", "base_argv"), COMMANDS)
def test_unknown_flag_exits_two(
    module_name: str,
    func_name: str,
    base_argv: ArgvBuilder,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    argv = [*base_argv(tmp_path), "--bogus"]
    with pytest.raises(SystemExit) as exc:
        _entry(module_name, func_name)(argv)
    assert exc.value.code == 2
    _, err = capsys.readouterr()
    assert err.strip(), "argparse prints a usage error"
