# region MODULE_CONTRACT
# PURPOSE: Guard every console-script entry point against becoming a coroutine function, which the console_scripts wrapper would call, discard, and silently do nothing.
# SCOPE: daemonize, init, show_nodes, submit, manage_node, check_status — importable, callable, plain sync, not wrapped by an async-to-sync bridge.
# KEYWORDS: CLI entry points, console_scripts, sync callables
# endregion MODULE_CONTRACT

from __future__ import annotations

import importlib
import inspect

import pytest

ENTRY_POINTS: tuple[tuple[str, str], ...] = (
    ("yascheduler.entrypoints.cli.daemonize", "daemonize"),
    ("yascheduler.entrypoints.cli.init", "init"),
    ("yascheduler.entrypoints.cli.show_nodes", "show_nodes"),
    ("yascheduler.entrypoints.cli.submit", "submit"),
    ("yascheduler.entrypoints.cli.manage_node", "manage_node"),
    ("yascheduler.entrypoints.cli.check_status", "check_status"),
)


@pytest.mark.parametrize(
    ("module_name", "func_name"), ENTRY_POINTS, ids=[name for _, name in ENTRY_POINTS]
)
def test_entry_point_is_a_plain_sync_callable(module_name: str, func_name: str) -> None:
    """console_scripts resolves the attribute directly; an async def would return an unawaited coroutine."""
    func = getattr(importlib.import_module(module_name), func_name)
    assert callable(func)
    assert not inspect.iscoroutinefunction(func)
    assert not hasattr(func, "__wrapped__")
