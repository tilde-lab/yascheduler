"""Focused unit tests for migration 014's pure input-conversion seam."""
# region MODULE_CONTRACT
# PURPOSE: Pin migration 014's pure legacy payload conversion and encoding rules without a database, protecting the seam already covered by PostgreSQL integration tests.
# SCOPE: Happy-path UTF-8/fort.9/not-guessed base64 encodings and one literal-directory/glob/overlap selection with unrelated metadata preservation.
# KEYWORDS: migration 014, task inputs, legacy payload, base64, selection, deduplication
# endregion MODULE_CONTRACT

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from yascheduler.domain import TaskInputDirectory, TaskInputFile

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
    spec = importlib.util.spec_from_file_location("014", _MIGRATION_PATH)
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
