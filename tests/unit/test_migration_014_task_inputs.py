"""Focused unit tests for migration 014's pure input-conversion seam."""
# region MODULE_CONTRACT
# PURPOSE: Pin migration 014's pure legacy payload conversion and encoding rules without a database, protecting the seam already covered by PostgreSQL integration tests.
# SCOPE: Happy-path UTF-8/fort.9/not-guessed base64 encodings, one literal-directory/glob/overlap selection with unrelated metadata preservation, pure conversion failures with safe diagnostics and no caller-payload mutation, required engine context, started-task skip boundaries, storage/unexpected failures, and migration-wide failure context/log rendering.
# KEYWORDS: migration 014, task inputs, legacy payload, base64, selection, deduplication
# endregion MODULE_CONTRACT

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from tests.log_assertions import extra_fields
from yascheduler.domain import (
    InvalidTaskSnapshotError,
    MissingInputFileError,
    TaskInputDirectory,
    TaskInputFile,
    UnsafeTaskPathError,
)

pytestmark = pytest.mark.unit

__all__: list[str] = []

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "yascheduler"
    / "infra"
    / "persistence"
    / "sql"
    / "migrations"
    / "014_task_input_snapshots.py"
)


def _load_migration_module() -> ModuleType:
    """Load the numbered migration exactly as the runner does."""
    spec = importlib.util.spec_from_file_location(
        "yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots",
        _MIGRATION_PATH,
    )
    if spec is None or spec.loader is None:
        message = f"cannot load migration module {_MIGRATION_PATH}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION = _load_migration_module()


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("INPUT", "line one\nline two — ü", "line one\nline two — ü".encode()),
        ("empty.orb", "", b""),
        ("fort.9", "AAEC\n/w", b"\x00\x01\x02\xff"),
        ("looks-like-base64.txt", "SGVsbG8=", b"SGVsbG8="),
    ],
)
def test_legacy_bytes_preserves_recorded_encoding_meaning(
    path: str,
    value: str,
    expected: bytes,
) -> None:
    assert _MIGRATION._legacy_bytes(path, value) == expected


def test_legacy_fort9_decoder_keeps_historical_lenient_base64() -> None:
    """Pin deployment compatibility; invalid characters are deliberately discarded."""
    assert _MIGRATION._legacy_bytes("fort.9", "QUJD$") == b"ABC"
    assert _MIGRATION._legacy_bytes("fort.9", "!!!!") == b""


def test_convert_payload_selects_tree_glob_overlap_and_preserves_metadata() -> None:
    payload: dict[str, object] = {
        "INPUT": "body",
        "basis/one.orb": "one",
        "basis/.hidden.orb": "hidden",
        "basis/empty.orb": "",
        "basis/nested/two.orb": "two",
        "globbed/root.dat": "root",
        "globbed/nested/leaf.dat": "leaf",
        "globbed/notes.txt": "keep",
        "other.orb": "keep",
        "remote_folder": "/keep/typed-metadata",
        "priority": 7,
        "nested_metadata": {"keep": True},
    }

    snapshot, remaining = _MIGRATION._convert_payload(
        "test-engine",
        ("INPUT", "basis", "basis/nested/*.orb", "globbed/**/*.dat"),
        payload,
    )

    assert snapshot == (
        TaskInputFile(path="INPUT", content=b"body"),
        TaskInputDirectory(path="basis"),
        TaskInputFile(path="basis/.hidden.orb", content=b"hidden"),
        TaskInputFile(path="basis/empty.orb", content=b""),
        TaskInputDirectory(path="basis/nested"),
        TaskInputFile(path="basis/nested/two.orb", content=b"two"),
        TaskInputFile(path="basis/one.orb", content=b"one"),
        TaskInputDirectory(path="globbed"),
        TaskInputDirectory(path="globbed/nested"),
        TaskInputFile(path="globbed/nested/leaf.dat", content=b"leaf"),
        TaskInputFile(path="globbed/root.dat", content=b"root"),
    )
    assert remaining == {
        "globbed/notes.txt": "keep",
        "other.orb": "keep",
        "remote_folder": "/keep/typed-metadata",
        "priority": 7,
        "nested_metadata": {"keep": True},
    }


def test_convert_payload_glob_leaves_unsafe_key_in_extra() -> None:
    payload: dict[str, object] = {
        "good.orb": "good",
        "../evil.orb": "unrelated-payload",
    }

    snapshot, remaining = _MIGRATION._convert_payload(
        "test-engine", ("*.orb",), payload
    )

    assert snapshot == (TaskInputFile(path="good.orb", content=b"good"),)
    assert remaining == {"../evil.orb": "unrelated-payload"}
    assert payload == {
        "good.orb": "good",
        "../evil.orb": "unrelated-payload",
    }


def test_convert_payload_reports_missing_selection_without_mutating_payload() -> None:
    payload: dict[str, object] = {"INPUT": "body"}

    with pytest.raises(MissingInputFileError) as info:
        _MIGRATION._convert_payload("test-engine", ("INPUT", "missing.orb"), payload)

    assert info.value.engine_name == "test-engine"
    assert info.value.filename == "missing.orb"
    assert "test-engine" in str(info.value)
    assert "missing.orb" in str(info.value)
    assert payload == {"INPUT": "body"}


def test_convert_payload_rejects_unsafe_declaration_without_mutating_payload() -> None:
    payload: dict[str, object] = {"INPUT": "body"}

    with pytest.raises(UnsafeTaskPathError) as info:
        _MIGRATION._convert_payload("test-engine", ("../outside",), payload)

    assert info.value.path == "../outside"
    assert "outside" in str(info.value)
    assert payload == {"INPUT": "body"}


def test_convert_payload_reports_malformed_fort9_without_leaking_or_mutating() -> None:
    malformed = "c2VjcmV0Q"
    payload: dict[str, object] = {"fort.9": malformed}

    with pytest.raises(InvalidTaskSnapshotError) as info:
        _MIGRATION._convert_payload("test-engine", ("fort.9",), payload)

    assert info.value.path == "fort.9"
    assert "base64" in str(info.value)
    assert malformed not in str(info.value)
    assert payload == {"fort.9": malformed}


def test_convert_payload_reports_non_string_value_without_leaking_or_mutating() -> None:
    selected_value: dict[str, object] = {"secret": "do-not-log"}
    payload: dict[str, object] = {"INPUT": selected_value}

    with pytest.raises(InvalidTaskSnapshotError) as info:
        _MIGRATION._convert_payload("test-engine", ("INPUT",), payload)

    assert info.value.path == "INPUT"
    assert "got dict" in str(info.value)
    assert "do-not-log" not in str(info.value)
    assert payload == {"INPUT": selected_value}


def test_trigger_restore_failure_has_no_stale_task_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A migration-wide trigger failure is not attributed to the last converted task."""
    secret = "UNIQUE_TRIGGER_FAILURE_SECRET_7d1a"

    class _FailingEnableConnection:
        def run(self, sql: str, **params: object) -> list[tuple[object, ...]] | None:
            if sql == _MIGRATION._LIST_LEGACY_INPUTS:
                return [(12, "test-engine", "TO_DO", {"INPUT": "body"})]
            if sql == _MIGRATION._ENABLE_TIMESTAMP_TRIGGER:
                raise RuntimeError(f"trigger restore failed with {secret}")
            return None

    migration = _MIGRATION.TaskInputSnapshots(
        MagicMock(),
        _FailingEnableConnection(),
        logging.getLogger("test-migration-014"),
        engine_inputs={"test-engine": ("INPUT",)},
    )

    with (
        caplog.at_level(
            logging.DEBUG,
            logger="yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots",
        ),
        pytest.raises(RuntimeError),
    ):
        migration.migrate()

    failed = [
        record
        for record in caplog.records
        if record.getMessage() == "MIGRATE_INPUTS_FAILED"
    ]
    assert len(failed) == 1
    assert failed[0].name == (
        "yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots"
    )
    fields = {
        key: value for key, value in extra_fields(failed[0]).items() if key != "message"
    }
    assert fields == {
        "tasks": 1,
        "entry_count": 1,
        "failure": "RuntimeError",
    }
    assert not any(
        record.getMessage() == "MIGRATE_INPUTS_DONE" for record in caplog.records
    )
    rendered = repr([extra_fields(record) for record in caplog.records])
    assert secret not in rendered
    assert secret not in caplog.text


def test_migrate_requires_engine_context_before_touching_the_database(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing context is a caller error even when the pending database is empty."""
    conn = MagicMock()
    migration = _MIGRATION.TaskInputSnapshots(
        MagicMock(),
        conn,
        logging.getLogger("test-migration-014"),
        engine_inputs=None,
    )

    with (
        caplog.at_level(
            logging.DEBUG,
            logger="yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots",
        ),
        pytest.raises(RuntimeError, match="requires engine input declarations"),
    ):
        migration.migrate()

    conn.run.assert_not_called()
    assert not any(
        record.getMessage() == "MIGRATE_INPUTS_DONE" for record in caplog.records
    )


def test_migrate_accepts_an_explicit_empty_engine_mapping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty mapping is a valid declaration context, distinct from no context."""

    class _EmptyConnection:
        def run(self, sql: str, **params: object) -> list[tuple[object, ...]] | None:
            assert sql in {
                _MIGRATION._CREATE_INPUTS,
                _MIGRATION._DISABLE_TIMESTAMP_TRIGGER,
                _MIGRATION._LIST_LEGACY_INPUTS,
                _MIGRATION._ENABLE_TIMESTAMP_TRIGGER,
            }
            return [] if sql == _MIGRATION._LIST_LEGACY_INPUTS else None

    migration = _MIGRATION.TaskInputSnapshots(
        MagicMock(),
        _EmptyConnection(),
        logging.getLogger("test-migration-014"),
        engine_inputs={},
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots",
    ):
        migration.migrate()

    assert any(
        record.getMessage() == "MIGRATE_INPUTS_DONE" for record in caplog.records
    )
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize(
    "fail_sql",
    [_MIGRATION._INSERT_INPUTS, _MIGRATION._UPDATE_LEGACY_INPUT_METADATA],
    ids=["insert-inputs", "update-metadata"],
)
@pytest.mark.parametrize(
    "error",
    [RuntimeError("input storage failed"), ValueError("input storage failed")],
    ids=["runtime-error", "conversion-class"],
)
@pytest.mark.parametrize("status", ["RUNNING", "DONE"], ids=["running", "done"])
def test_storage_failure_aborts_for_started_task(
    status: str,
    error: Exception,
    fail_sql: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A database write fault is not a conversion failure and must never be skipped."""

    class _FailingStorageConnection:
        def run(self, sql: str, **params: object) -> list[tuple[object, ...]] | None:
            if sql == _MIGRATION._LIST_LEGACY_INPUTS:
                return [(21, "test-engine", status, {"INPUT": "body"})]
            if sql == fail_sql:
                raise error
            return None

    migration = _MIGRATION.TaskInputSnapshots(
        MagicMock(),
        _FailingStorageConnection(),
        logging.getLogger("test-migration-014"),
        engine_inputs={"test-engine": ("INPUT",)},
    )

    with (
        caplog.at_level(
            logging.DEBUG,
            logger="yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots",
        ),
        pytest.raises(type(error), match="input storage failed"),
    ):
        migration.migrate()

    messages = [record.getMessage() for record in caplog.records]
    assert "MIGRATE_TASK_STORAGE_FAILED" in messages
    assert "MIGRATE_TASK_INPUTS_FAILED" in messages
    assert "MIGRATE_TASK_INPUTS_SKIPPED" not in messages
    assert "MIGRATE_INPUTS_DONE" not in messages
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("unexpected conversion failure"), KeyboardInterrupt()],
    ids=["runtime-error", "base-exception"],
)
def test_unexpected_error_is_not_skipped_for_started_task(
    error: BaseException,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only recognized conversion errors are skippable; all others stay fatal."""

    def _unexpected_conversion(*_args: object, **_kwargs: object) -> None:
        raise error

    class _StartedTaskConnection:
        def run(self, sql: str, **params: object) -> list[tuple[object, ...]] | None:
            if sql == _MIGRATION._LIST_LEGACY_INPUTS:
                return [(22, "test-engine", "DONE", {"INPUT": "body"})]
            return None

    monkeypatch.setattr(_MIGRATION, "_convert_payload", _unexpected_conversion)
    migration = _MIGRATION.TaskInputSnapshots(
        MagicMock(),
        _StartedTaskConnection(),
        logging.getLogger("test-migration-014"),
        engine_inputs={"test-engine": ("INPUT",)},
    )

    with (
        caplog.at_level(
            logging.DEBUG,
            logger="yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots",
        ),
        pytest.raises(type(error)),
    ):
        migration.migrate()

    messages = [record.getMessage() for record in caplog.records]
    assert "MIGRATE_TASK_INPUTS_FAILED" in messages
    assert "MIGRATE_TASK_INPUTS_SKIPPED" not in messages
    assert "MIGRATE_INPUTS_DONE" not in messages
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


def test_todo_abort_diagnostic_bounds_keys_and_includes_remedy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TO_DO diagnostics stay deterministic and bounded without payload content."""
    long_key = "oversized-" + "x" * 100
    payload = {
        long_key: "secret-value",
        **{f"key-{index}": index for index in range(12)},
    }

    class _AbortConnection:
        def run(self, sql: str, **params: object) -> list[tuple[object, ...]] | None:
            if sql == _MIGRATION._LIST_LEGACY_INPUTS:
                return [(23, "retired-engine", "TO_DO", payload)]
            return None

    migration = _MIGRATION.TaskInputSnapshots(
        MagicMock(),
        _AbortConnection(),
        logging.getLogger("test-migration-014"),
        engine_inputs={},
    )

    with (
        caplog.at_level(
            logging.DEBUG,
            logger="yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots",
        ),
        pytest.raises(RuntimeError) as info,
    ):
        migration.migrate()

    diagnostic = str(info.value)
    displayed_keys = [key[:80] for key in list(payload)[:10]]
    assert (
        f"payload keys (13 total, first 10, up to 80 characters each): "
        f"{displayed_keys!r}"
    ) in diagnostic
    assert (
        "configure the engine input_files to match these keys or mark the task DONE"
        in diagnostic
    )
    assert repr(long_key[:80]) in diagnostic
    assert long_key not in diagnostic
    assert "secret-value" not in diagnostic
    assert "MIGRATE_TASK_INPUTS_SKIPPED" not in [
        record.getMessage() for record in caplog.records
    ]


def test_skip_diagnostics_aggregate_one_warning_per_engine(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skipped rows report per-row debug fields and one warning per engine."""
    rows: list[tuple[int, str, str, dict[str, str]]] = [
        (31, "retired-a", "RUNNING", {"INPUT": "body", "run": "secret"}),
        (32, "retired-a", "DONE", {"INPUT": "body", "done": "secret"}),
        (33, "retired-b", "DONE", {"INPUT": "body", "other": "secret"}),
    ]

    class _SkipConnection:
        def run(
            self, sql: str, **params: object
        ) -> list[tuple[int, str, str, dict[str, str]]] | None:
            if sql == _MIGRATION._LIST_LEGACY_INPUTS:
                return rows
            return None

    migration = _MIGRATION.TaskInputSnapshots(
        MagicMock(),
        _SkipConnection(),
        logging.getLogger("test-migration-014"),
        engine_inputs={},
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots",
    ):
        migration.migrate()

    skipped = [
        record
        for record in caplog.records
        if record.getMessage() == "MIGRATE_TASK_INPUTS_SKIPPED"
    ]
    assert len(skipped) == 3
    assert [
        {key: value for key, value in extra_fields(record).items() if key != "message"}
        for record in skipped
    ] == [
        {
            "task_id": task_id,
            "engine": engine,
            "entry_count": 0,
            "failure": "ValueError",
            "key_count": len(payload),
        }
        for task_id, engine, _status, payload in rows
    ]
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert [record.getMessage() for record in warnings] == [
        "Migration 014 left 2 task(s) unconverted for engine 'retired-a'",
        "Migration 014 left 1 task(s) unconverted for engine 'retired-b'",
    ]
    assert [
        {key: value for key, value in extra_fields(record).items() if key != "message"}
        for record in warnings
    ] == [
        {"engine": "retired-a", "skipped_count": 2},
        {"engine": "retired-b", "skipped_count": 1},
    ]
    assert "secret" not in caplog.text
    assert any(
        record.getMessage() == "MIGRATE_INPUTS_DONE" for record in caplog.records
    )
