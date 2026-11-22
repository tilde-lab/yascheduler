# region MODULE_CONTRACT
# PURPOSE: Unit tests for linux_list_processes self-process filtering and ps line parsing.
# SCOPE: linux_list_processes against a fake SSHClientConnection emitting canned ps output.
# KEYWORDS: linux, list_processes, pgrep, self-skip, occupancy, regression
# endregion MODULE_CONTRACT

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from yascheduler.infra.ssh.platform.linux import linux_list_processes

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import Any

# linux_list_processes splits ps lines on whitespace (split(None, 2));
# SEP here only keeps the fixture lines readable with wide column gaps.
SEP = " " * 10


def _line(pid: str, comm: str, args: str) -> str:
    return f"{pid}{SEP}{comm}{SEP}{args}"


class _FakeStdout:
    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = list(lines)

    async def readline(self) -> str:
        if not self._lines:
            return ""
        return self._lines.pop(0)

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> str:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProcess:
    def __init__(self, lines: Sequence[str]) -> None:
        self.stdout = _FakeStdout(lines)

    async def __aenter__(self) -> _FakeProcess:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _conn(lines: Iterable[str]) -> Any:
    conn = MagicMock()
    conn.create_process.return_value = _FakeProcess(list(lines))
    return conn


async def test_skips_self_wrapper_for_sh_login_shell() -> None:
    """Regression: pgrep self-match on sh-based login shells.

    The wrapper running ps_cmd surfaces as `sh -c <ps_cmd>` in ps output.
    The old filter hardcoded `bash -c`, so on sh the wrapper survived,
    pgrep self-matched, and occupancy reported BUSY forever.
    """
    ps_cmd = (
        'pids=$(pgrep -f dummyengine); [ -n "$pids" ] '
        '&& ps -o pid,comm,args -ww $pids'
    )
    header = "PID COMMAND COMMAND"
    self_line = _line("10761", "sh", f"sh -c {ps_cmd}")
    engine_line = _line("12345", "dummyengine", "./dummyengine --input foo")
    conn = _conn([header, self_line, engine_line])

    results = [
        p async for p in linux_list_processes(conn, query="pgrep -f dummyengine")
    ]

    assert len(results) == 1
    assert results[0].pid == 12345
    assert results[0].name == "dummyengine"


async def test_skips_self_wrapper_for_bash_login_shell() -> None:
    """Legacy `bash -c` wrapper must still be skipped."""
    ps_cmd = (
        'pids=$(pgrep -f dummyengine); [ -n "$pids" ] '
        '&& ps -o pid,comm,args -ww $pids'
    )
    header = "PID COMMAND COMMAND"
    self_line = _line("10761", "bash", f"bash -c {ps_cmd}")
    engine_line = _line("12345", "dummyengine", "./dummyengine --input foo")
    conn = _conn([header, self_line, engine_line])

    results = [
        p async for p in linux_list_processes(conn, query="pgrep -f dummyengine")
    ]

    assert len(results) == 1
    assert results[0].pid == 12345


async def test_query_command_is_posix_portable() -> None:
    """The query branch must not depend on GNU-only xargs flags.

    Stock BSD/macOS xargs rejects `--no-run-if-empty` before ps runs,
    which reproduces the exact incident this module guards against, so
    the constructed command is pinned to the POSIX `[ -n ... ] &&` guard.
    """
    conn = _conn(["PID COMMAND COMMAND"])

    results = [
        p async for p in linux_list_processes(conn, query="pgrep -f dummyengine")
    ]

    assert results == []
    cmd = conn.create_process.call_args[0][0]
    assert "xargs" not in cmd
    assert cmd.startswith("pids=$(pgrep -f dummyengine)")
    assert '[ -n "$pids" ]' in cmd


async def test_no_query_yields_all_without_self_filtering() -> None:
    """`ps -axo` (no pgrep) has no wrapper process — nothing to skip."""
    header = "PID COMMAND COMMAND"
    conn = _conn([header, _line("1", "init", "/sbin/init"), _line("2", "bash", "bash")])

    results = [p async for p in linux_list_processes(conn)]

    assert len(results) == 2


async def test_skips_broken_lines() -> None:
    """Lines with fewer than 3 columns are skipped, not yielded."""
    header = "PID COMMAND COMMAND"
    conn = _conn([header, _line("1", "sh", "sh"), "12345"])

    results = [p async for p in linux_list_processes(conn)]

    assert len(results) == 1
    assert results[0].pid == 1