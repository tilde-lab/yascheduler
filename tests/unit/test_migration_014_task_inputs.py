"""Focused unit tests for migration 014's pure input-conversion seam."""
# region MODULE_CONTRACT
# PURPOSE: Pin migration 014's pure legacy payload conversion and encoding rules without a database, protecting the seam already covered by PostgreSQL integration tests.
# SCOPE: Happy-path UTF-8/fort.9/not-guessed base64 encodings, one literal-directory/glob/overlap selection with unrelated metadata preservation, pure conversion failures with safe diagnostics and no caller-payload mutation, and migration-wide failure context/log rendering.
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
                return [(12, "test-engine", {"INPUT": "body"})]
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
