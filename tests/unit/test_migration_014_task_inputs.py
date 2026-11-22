"""Focused unit tests for migration 014's failure policy."""
# region MODULE_CONTRACT
# PURPOSE: Pin when migration 014 stops and when it skips, without a database, protecting the boundary already covered by PostgreSQL integration tests.
# SCOPE: Required engine context, started-task skip boundaries, the pre-start stop diagnostics, storage/unexpected failures, and migration-wide failure context/log rendering. Payload conversion itself belongs to the shared domain conversion, tested in test_legacy_inputs.
# KEYWORDS: migration 014, task inputs, skip, failure policy, deduplication
# endregion MODULE_CONTRACT

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from tests.log_assertions import extra_fields

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
    fields = extra_fields(failed[0])
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

    monkeypatch.setattr(_MIGRATION, "legacy_input_snapshot", _unexpected_conversion)
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
