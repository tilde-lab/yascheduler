# region MODULE_CONTRACT
# PURPOSE: Shared pytest fixtures for the whole yascheduler test suite.
# SCOPE: anyio backend fixture; log_records fixture handing every yascheduler DEBUG record to assertions.
# KEYWORDS: pytest fixtures, anyio, log capture
# endregion MODULE_CONTRACT

import logging
from collections.abc import Generator

import pytest

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
