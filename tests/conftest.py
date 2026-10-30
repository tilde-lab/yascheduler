# region MODULE_CONTRACT
# PURPOSE: Shared pytest fixtures and a collection-time Python 3.9 compatibility guard for the yascheduler test suite.
# SCOPE: anyio backend fixture; log_records fixture handing every yascheduler DEBUG record to assertions; pytest_configure hook that scans yascheduler/ and tests/ for PEP 604 union annotations without `from __future__ import annotations`.
# KEYWORDS: pytest fixtures, anyio, log capture, Python 3.9, annotation guard
# endregion MODULE_CONTRACT

import logging
import os
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.py39_compat_guard import scan_paths

_PACKAGE = "yascheduler"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _RecordSink(logging.Handler):
    """Collects emitted records into a list for assertions."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def log_records() -> Generator[list[logging.LogRecord], None, None]:
    """Every yascheduler DEBUG record emitted while the test runs.

    pytest's own caplog cannot back this: it rebinds its record list on every
    test phase, so a list handed out by a fixture stops receiving records.
    """
    logger = logging.getLogger(_PACKAGE)
    sink = _RecordSink()
    previous_level = logger.level
    logger.addHandler(sink)
    logger.setLevel(logging.DEBUG)
    try:
        yield sink.records
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous_level)


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("YASCHEDULER_SKIP_PY39_GUARD") == "1":
        return
    root = Path(__file__).resolve().parent.parent
    violations = scan_paths([root / "yascheduler", root / "tests"])
    if not violations:
        return
    lines = [
        "",
        "Python 3.9 compatibility guard failed.",
        "",
        "PEP 604 `X | Y` annotations found without `from __future__ import annotations`.",
        "CPython evaluates these at import time and crashes on 3.9 (the project minimum",
        "declared in pyproject.toml). Affected locations:",
        "",
    ]
    lines += [f"  {v.render()}" for v in violations]
    lines += [
        "",
        "Fix: add `from __future__ import annotations` to each listed module, after any",
        "module docstring and before other imports.",
        "",
    ]
    pytest.exit("\n".join(lines), returncode=1)
