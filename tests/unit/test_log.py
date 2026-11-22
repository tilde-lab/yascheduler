# region MODULE_CONTRACT
# PURPOSE: Unit tests for LogFormatter in yascheduler.shared.log.
# SCOPE: LogFormatter extra-diff discriminator rendering; introspection-derived _NATIVE_KEYS; _PACKAGE derivation; single-formatter-both-handlers wiring.
# KEYWORDS: LogFormatter, extra-diff, _NATIVE_KEYS, structured logging
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import pytest


class _RecordCollector(logging.Handler):
    """Handler that collects emitted LogRecords for test assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


_LoggerFactory = Callable[..., tuple[logging.Logger, _RecordCollector]]


@pytest.fixture
def make_logger() -> Iterator[_LoggerFactory]:
    """Yield a factory whose logger level, handlers, and propagation are restored after the test."""
    saved: list[tuple[logging.Logger, int, list[logging.Handler], bool]] = []

    def _make(
        name: str = "yascheduler.test.module",
        level: int = logging.DEBUG,
    ) -> tuple[logging.Logger, _RecordCollector]:
        """Return a plain logging.Logger with a RecordCollector handler at the given level."""
        logger = logging.getLogger(name)
        saved.append((logger, logger.level, logger.handlers[:], logger.propagate))
        logger.setLevel(level)
        logger.handlers.clear()
        collector = _RecordCollector()
        logger.addHandler(collector)
        logger.propagate = False
        return logger, collector

    yield _make

    # Restore every touched logger so its production configuration is intact for
    # later tests that log through the same names.
    for logger, level, handlers, propagate in reversed(saved):
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.propagate = propagate


# ── Test 1: Gherkin scenario — trace record renders with module, function, lineno, message, and sorted fields ──


def test_trace_record_renders_with_module_funcname_lineno_message_and_sorted_fields(
    make_logger: _LoggerFactory,
) -> None:
    """Trace record renders [module][funcName]:lineno msg sorted k=v."""
    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter()
    logger, collector = make_logger("yascheduler.application.allocate_task")

    logger.debug("ALLOCATED", extra={"ip": "10.0.0.1", "task_id": 7})

    assert len(collector.records) == 1
    output = formatter.format(collector.records[0])

    # Must contain the trace markers and be grep-friendly
    assert output.startswith("[application.allocate_task]")
    assert "[test_trace_record_renders" in output  # funcName
    assert "10.0.0.1" in output
    assert "7" in output
    assert "ALLOCATED" in output


# ── Test 2: Gherkin scenario — trace fields are sorted alphabetically ──


def test_trace_fields_sorted_alphabetically(make_logger: _LoggerFactory) -> None:
    """Trace fields appear in alphabetical key order for deterministic output."""
    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter()
    logger, collector = make_logger("yascheduler.test.module")

    logger.debug("B", extra={"zebra": 1, "alpha": 2})

    assert len(collector.records) == 1
    output = formatter.format(collector.records[0])

    # alpha should appear before zebra
    alpha_idx = output.index("alpha=2")
    zebra_idx = output.index("zebra=1")
    assert alpha_idx < zebra_idx


# ── Test 3: Gherkin scenario — DEBUG without extra renders as regular narrative ──


def test_debug_without_extra_renders_regular(make_logger: _LoggerFactory) -> None:
    """DEBUG record with no extra renders as LEVEL name: message, no trace markers."""
    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter()
    logger, collector = make_logger("yascheduler.test.module")

    logger.debug("progress: ok")

    assert len(collector.records) == 1
    output = formatter.format(collector.records[0])

    # Regular layout: LEVEL name: message
    assert output.startswith("DEBUG ")
    assert "yascheduler.test.module" in output
    assert "progress: ok" in output
    # No trace markers
    assert "[" not in output
    assert "=" not in output


# ── Test 4: Gherkin scenario — out-of-package DEBUG with extra renders as regular narrative ──


def test_out_of_package_debug_with_extra_renders_regular(
    make_logger: _LoggerFactory,
) -> None:
    """Third-party logger (e.g. asyncssh) with DEBUG+extra renders as LEVEL name: message."""
    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter()
    logger, collector = make_logger("asyncssh")

    logger.debug("key exchange done", extra={"host": "10.0.0.1"})

    assert len(collector.records) == 1
    output = formatter.format(collector.records[0])

    # Regular layout because out-of-package
    assert output.startswith("DEBUG ")
    assert "asyncssh" in output
    assert "key exchange done" in output
    # No trace markers
    assert "[" not in output
    assert "=" not in output


# ── Test 5: Gherkin scenario — INFO/WARN/ERROR renders as regular narrative ──


def test_info_warn_error_renders_regular(make_logger: _LoggerFactory) -> None:
    """INFO/WARN/ERROR records render as LEVEL name: message with no trace markers."""
    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter()
    logger, collector = make_logger("yascheduler.test.module")

    logger.info("info msg")
    logger.warning("warn msg")
    logger.error("error msg")

    assert len(collector.records) == 3
    for i, (level, msg) in enumerate(
        [("INFO", "info msg"), ("WARNING", "warn msg"), ("ERROR", "error msg")],
    ):
        output = formatter.format(collector.records[i])
        assert output.startswith(f"{level} "), f"Expected {level} prefix, got: {output}"
        assert "yascheduler.test.module" in output
        assert msg in output
        # No trace markers
        assert "[" not in output
        assert "=" not in output


# ── Test 6: Gherkin scenario — native LogRecord set is derived by introspection ──


def test_regular_exception_renders_type_message_and_traceback(
    make_logger: _LoggerFactory,
) -> None:
    """A regular exception-bearing record retains its diagnostic traceback."""
    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter()
    logger, collector = make_logger("yascheduler.infra.persistence")

    try:
        raise RuntimeError("column hostname does not exist")
    except RuntimeError:
        logger.exception("database query failed")

    output = formatter.format(collector.records[0])

    assert output.startswith(
        "ERROR yascheduler.infra.persistence: database query failed"
    )
    assert "Traceback (most recent call last):" in output
    assert "RuntimeError: column hostname does not exist" in output


def test_structured_exception_renders_type_message_and_traceback(
    make_logger: _LoggerFactory,
) -> None:
    """A structured DEBUG record keeps sorted fields before its traceback."""
    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter()
    logger, collector = make_logger("yascheduler.infra.persistence")

    try:
        raise RuntimeError("tracker query failed")
    except RuntimeError:
        logger.debug("MIGRATION_STATUS", exc_info=True, extra={"zebra": 1, "alpha": 2})

    output = formatter.format(collector.records[0])

    assert output.startswith("[infra.persistence]")
    assert output.index("alpha=2") < output.index("zebra=1")
    assert "Traceback (most recent call last):" in output
    assert "RuntimeError: tracker query failed" in output


# ── Test 6: Gherkin scenario — native LogRecord set is derived by introspection ──


def test_native_keys_derived_by_introspection() -> None:
    """_NATIVE_KEYS is non-empty and contains expected native attributes."""
    from yascheduler.shared.log import _NATIVE_KEYS  # type: ignore[attr-defined]

    assert isinstance(_NATIVE_KEYS, frozenset)
    assert len(_NATIVE_KEYS) > 0
    # Must contain essential native attributes
    assert "name" in _NATIVE_KEYS
    assert "msg" in _NATIVE_KEYS
    assert "funcName" in _NATIVE_KEYS
    assert "levelno" in _NATIVE_KEYS
    assert "lineno" in _NATIVE_KEYS
    assert "args" in _NATIVE_KEYS
    # Verify non-native key is NOT in the set
    assert "task_id" not in _NATIVE_KEYS


# ── Test 7: Gherkin scenario — package prefix derived from formatter module name ──


def test_package_prefix_derived_from_name() -> None:
    """_PACKAGE is derived from __name__ top segment, not hardcoded."""
    from yascheduler.shared.log import _PACKAGE  # type: ignore[attr-defined]

    assert isinstance(_PACKAGE, str)
    assert _PACKAGE == "yascheduler"
    # Verify it's NOT a hardcoded literal: check we can derive the same
    # value from the module's __name__
    from yascheduler.shared.log import __name__ as log_module_name

    expected = log_module_name.split(".", 1)[0]
    assert expected == _PACKAGE


# ── Test 8: Gherkin scenario — single formatter serves both handlers ──


def test_single_formatter_serves_both_handlers() -> None:
    """configure_logger wires the same LogFormatter onto stderr and file handlers."""
    import logging
    import tempfile

    from yascheduler.entrypoints.logger import configure_logger
    from yascheduler.shared.log import LogFormatter

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_root_level = root.level
    asyncssh_logger = logging.getLogger("asyncssh")
    saved_asyncssh_level = asyncssh_logger.level
    root.handlers.clear()

    try:
        with tempfile.NamedTemporaryFile(suffix=".log") as tf:
            root = configure_logger(tf.name, logging.DEBUG)

            handlers = root.handlers
            stderr_handler = None
            file_handler = None
            for h in handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(
                    h,
                    logging.FileHandler,
                ):
                    stderr_handler = h
                elif isinstance(h, logging.FileHandler):
                    file_handler = h

            assert stderr_handler is not None, "No StreamHandler found"
            assert file_handler is not None, "No FileHandler found"
            assert stderr_handler.formatter is file_handler.formatter
            assert isinstance(stderr_handler.formatter, LogFormatter)
    finally:
        root.handlers.clear()
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_root_level)
        asyncssh_logger.setLevel(saved_asyncssh_level)


# ── Test 9: Gherkin scenario — timestamp-enabled trace record prepends ISO 8601 prefix ──


def test_timestamp_enabled_trace_record_prepends_iso8601_prefix(
    make_logger: _LoggerFactory,
) -> None:
    """A timestamp-enabled LogFormatter prepends an ISO 8601 prefix to a trace record."""
    import re

    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter(timestamp=True)
    logger, collector = make_logger("yascheduler.application.allocate_task")

    logger.debug("ALLOCATED", extra={"task_id": 7})

    assert len(collector.records) == 1
    output = formatter.format(collector.records[0])

    # ISO 8601 prefix, then space, then the trace layout.
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2},\d{3}) ",
        output,
    )
    assert m is not None, f"Expected ISO 8601 prefix, got: {output!r}"
    # Trace layout follows after the space.
    assert output[len(m.group(0)) :].startswith("[application.allocate_task]"), (
        f"Expected trace layout after timestamp, got: {output!r}"
    )


# ── Test 10: Gherkin scenario — timestamp-enabled regular record prepends ISO 8601 prefix ──


def test_timestamp_enabled_regular_record_prepends_iso8601_prefix(
    make_logger: _LoggerFactory,
) -> None:
    """A timestamp-enabled LogFormatter prepends an ISO 8601 prefix to a regular record."""
    import re

    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter(timestamp=True)
    logger, collector = make_logger("yascheduler.test.module")

    logger.warning("webhook retry to %s", "https://example.com/hook")

    assert len(collector.records) == 1
    output = formatter.format(collector.records[0])

    m = re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2},\d{3} ", output)
    assert m is not None, f"Expected ISO 8601 prefix, got: {output!r}"
    assert output[len(m.group(0)) :].startswith("WARNING yascheduler.test.module:"), (
        f"Expected regular layout after timestamp, got: {output!r}"
    )


# ── Test 11: Gherkin scenario — timestamp-disabled formatter renders without a prefix ──


def test_timestamp_disabled_renders_without_prefix(make_logger: _LoggerFactory) -> None:
    """A timestamp-disabled LogFormatter renders trace and regular layouts with NO leading timestamp."""
    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter(timestamp=False)
    trace_logger, trace_collector = make_logger("yascheduler.test.trace_module")
    regular_logger, regular_collector = make_logger("yascheduler.test.regular_module")

    trace_logger.debug("B", extra={"k": 1})
    regular_logger.warning("plain")

    trace_out = formatter.format(trace_collector.records[0])
    regular_out = formatter.format(regular_collector.records[0])

    assert trace_out.startswith("[test.trace_module]"), (
        f"Expected no timestamp prefix on trace, got: {trace_out!r}"
    )
    assert regular_out.startswith("WARNING "), (
        f"Expected no timestamp prefix on regular, got: {regular_out!r}"
    )


# ── Test 12: Gherkin scenario — timestamp matches the exact YYYY-MM-DDTHH:MM:SS,mmm pattern ──


def test_timestamp_matches_exact_pattern() -> None:
    """The timestamp prefix matches YYYY-MM-DDTHH:MM:SS,mmm (10 digits-dash-... with comma-ms, no offset)."""
    import re

    from yascheduler.shared.log import LogFormatter

    # Pin a known created value so the date/time parts are deterministic.
    fixed = 1_750_000_000.123456  # 2025-06-15T... in UTC; local varies, but pattern is fixed.
    rendered = LogFormatter._iso8601(fixed)

    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2},\d{3}$")
    assert pattern.match(rendered), (
        f"Timestamp rendering {rendered!r} does not match YYYY-MM-DDTHH:MM:SS,mmm"
    )
    # Sanity: ensure NO offset sign leaks in.
    assert "+" not in rendered, f"Unexpected utc offset in timestamp: {rendered!r}"
    assert rendered.count("-") == 2, (
        f"Expected exactly two date dashes, got: {rendered!r}"
    )


# ── Test 13: Gherkin scenario — timestamp reflects the record emit time, not the render time ──


def test_timestamp_reflects_record_emit_time_not_render_time() -> None:
    """The leading timestamp derives from record.created (emit time), not format()-time."""
    import logging
    import re
    import time

    from yascheduler.shared.log import LogFormatter

    formatter = LogFormatter(timestamp=True)

    # Construct a record whose created time is pinned far in the past.
    past_created = time.time() - 90 * 24 * 3600  # ~3 months ago.
    record = logging.LogRecord(
        name="yascheduler.test.module",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="EMITTED",
        args=(),
        exc_info=None,
    )
    record.created = past_created
    record.funcName = "test_func"

    output = formatter.format(record)
    emit_local = (
        __import__("datetime")
        .datetime.fromtimestamp(past_created)
        .astimezone()
        .strftime("%Y-%m-%d")
    )
    render_local = time.strftime("%Y-%m-%d")

    m = re.match(r"^(\d{4}-\d{2}-\d{2})T", output)
    assert m is not None, f"Expected timestamp prefix, got: {output!r}"
    assert m.group(1) == emit_local, (
        f"Timestamp {m.group(1)!r} != emit-time date {emit_local!r}; output={output!r}"
    )
    assert emit_local != render_local, (
        "Test precondition: emit date and render date must differ for this assertion to be meaningful"
    )
