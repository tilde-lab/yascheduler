"""Unit tests for the migration runner (yascheduler.infra.persistence.postgres_migrations).

Covers the pure helpers (_prefix_id, _pending, _scan_migrations,
_one_migration_subclass) and two whole-repository invariants that the runner
does NOT enforce itself: prefix_id uniqueness across migrations/ and the
schema.sql last_migration CONSTANT matching the latest migration's prefix_id.
"""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for the migration runner helpers and migration-directory invariants.
# SCOPE: Migration runner helpers: _prefix_id, _pending, _scan_migrations, _one_migration_subclass, apply_migrations upto capping; prefix_id uniqueness and CONSTANT drift guard.
# KEYWORDS: migration runner, _prefix_id, _scan_migrations, uniqueness
# endregion MODULE_CONTRACT

from __future__ import annotations

import re
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from yascheduler.infra.persistence.migration_base import Migration
from yascheduler.infra.persistence.postgres_migrations import (
    _MIGRATIONS_DIR,
    MigrationState,
    _one_migration_subclass,
    _pending,
    _prefix_id,
    _scan_migrations,
    apply_migrations,
    check_migration_status,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "filename,expected",
    [
        (Path("001_add_username_port.sql"), "001"),
        (Path("20260701120000_add_index.sql"), "20260701120000"),
        (Path("010_backfill.py"), "010"),
    ],
)
def test_prefix_id_extracts_token_before_first_underscore(
    filename: Path,
    expected: str,
) -> None:
    assert _prefix_id(filename) == expected


def test_pending_returns_all_when_last_is_none() -> None:
    files = [Path("001_a.sql"), Path("002_b.sql")]
    assert _pending(None, files) == files


def test_pending_filters_greater_than_last() -> None:
    files = [Path("001_a.sql"), Path("002_b.sql"), Path("010_c.sql")]
    assert _pending("001", files) == [Path("002_b.sql"), Path("010_c.sql")]


def test_pending_upto_caps_inclusively() -> None:
    files = [Path("001_a.sql"), Path("002_b.sql"), Path("010_c.sql")]
    assert _pending("001", files, upto="002") == [Path("002_b.sql")]
    assert _pending("001", files, upto="010") == [
        Path("002_b.sql"),
        Path("010_c.sql"),
    ]


def test_pending_upto_at_or_below_last_applies_nothing() -> None:
    files = [Path("001_a.sql"), Path("002_b.sql"), Path("010_c.sql")]
    assert _pending("002", files, upto="002") == []
    assert _pending("002", files, upto="001") == []


def test_pending_upto_none_keeps_head_of_chain() -> None:
    files = [Path("001_a.sql"), Path("002_b.sql"), Path("010_c.sql")]
    assert _pending("001", files, upto=None) == [
        Path("002_b.sql"),
        Path("010_c.sql"),
    ]


def test_scan_migrations_sorts_by_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "010_c.sql").write_text("-- c")
    (migrations_dir / "001_a.sql").write_text("-- a")
    (migrations_dir / "002_b.py").write_text("# b")
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )

    result = _scan_migrations()

    assert [p.name for p in result] == ["001_a.sql", "002_b.py", "010_c.sql"]


def _make_module(name: str, body: str) -> types.ModuleType:
    """Build a module whose defined classes carry __module__ == name."""
    mod = types.ModuleType(name)
    mod.__file__ = name
    exec(compile(body, name, "exec"), mod.__dict__)
    return mod


_ONE = (
    "from yascheduler.infra.persistence.migration_base import Migration\n"
    "class A(Migration):\n"
    "    def migrate(self) -> None: ...\n"
)
_ZERO = "from yascheduler.infra.persistence.migration_base import Migration\nx = 1\n"
_TWO = (
    "from yascheduler.infra.persistence.migration_base import Migration\n"
    "class A(Migration):\n"
    "    def migrate(self) -> None: ...\n"
    "class B(Migration):\n"
    "    def migrate(self) -> None: ...\n"
)


def test_one_migration_subclass_finds_exactly_one() -> None:
    mod = _make_module("tmp_one", _ONE)
    klass = _one_migration_subclass(mod)
    assert klass.__name__ == "A"
    assert issubclass(klass, Migration)


def test_one_migration_subclass_zero_raises() -> None:
    mod = _make_module("tmp_zero", _ZERO)
    with pytest.raises(RuntimeError, match="exactly one Migration subclass"):
        _one_migration_subclass(mod)


def test_one_migration_subclass_two_raises() -> None:
    mod = _make_module("tmp_two", _TWO)
    with pytest.raises(RuntimeError, match="exactly one Migration subclass"):
        _one_migration_subclass(mod)


def test_prefix_id_uniqueness_across_migrations() -> None:
    files = sorted(p.name for p in _MIGRATIONS_DIR.glob("*") if p.is_file())
    assert files, "migrations/ must contain at least one migration file"
    ids = [_prefix_id(Path(name)) for name in files]
    dupes = {pid for pid in ids if ids.count(pid) > 1}
    assert not dupes, f"duplicate prefix_id(s) across migrations/: {dupes}"


def test_schema_sql_last_migration_constant_matches_latest_migration() -> None:
    schema_text = (_MIGRATIONS_DIR.parent / "schema.sql").read_text()
    match = re.search(r"last_migration\s+CONSTANT\s+TEXT\s+:=\s*'([^']+)'", schema_text)
    assert match, "schema.sql must declare last_migration CONSTANT TEXT := '<id>'"
    constant = match.group(1)

    files = [p for p in _MIGRATIONS_DIR.glob("*") if p.is_file()]
    assert files, "migrations/ must contain at least one migration file"
    latest = max(_prefix_id(p) for p in files)

    assert constant == latest, (
        f"schema.sql last_migration CONSTANT '{constant}' does not match "
        f"the latest migration prefix_id '{latest}' (forgot edit step 2?)"
    )


def test_schema_sql_uses_identity_not_serial() -> None:
    schema_text = (_MIGRATIONS_DIR.parent / "schema.sql").read_text()
    assert "GENERATED ALWAYS AS IDENTITY" in schema_text
    assert "SERIAL PRIMARY KEY" not in schema_text


def _db_config() -> SimpleNamespace:
    """Return only the PostgresDbConfig attributes a connection needs."""
    return SimpleNamespace(
        user="scheduler",
        host="db.example",
        database="yascheduler",
        port=5432,
        password="secret",
    )


class _MigrationStatusConnection:
    """In-memory pg8000 double for read-only migration-status checks."""

    def __init__(self, tracker: str | None, applied: str | None) -> None:
        self.tracker = tracker
        self.applied = applied
        self.queries: list[str] = []
        self.closed = False
        self.error: Exception | None = None

    def run(self, query: str) -> list[list[str | None]]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        if "to_regclass" in query:
            return [[self.tracker]]
        return [[self.applied]]

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("tracker", "applied", "expected"),
    [
        (None, None, MigrationState.MISSING),
        ("yascheduler_migrations", None, MigrationState.EMPTY),
        ("yascheduler_migrations", "001", MigrationState.BEHIND),
        ("yascheduler_migrations", "002", MigrationState.CURRENT),
        ("yascheduler_migrations", "003", MigrationState.AHEAD),
    ],
)
def test_check_migration_status_reads_tracker_without_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: str | None,
    applied: str | None,
    expected: MigrationState,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_first.sql").write_text("-- first")
    (migrations_dir / "002_second.py").write_text("# second")
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )
    conn = _MigrationStatusConnection(tracker, applied)
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations.Connection",
        lambda **_kwargs: conn,
    )

    status = check_migration_status(_db_config())  # type: ignore[arg-type]

    assert status.state is expected
    assert status.applied == applied
    assert status.required == "002"
    assert conn.closed
    assert conn.queries
    assert all(query.startswith("SELECT") for query in conn.queries)


def test_check_migration_status_propagates_database_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_first.sql").write_text("-- first")
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )
    conn = _MigrationStatusConnection("yascheduler_migrations", "001")
    conn.error = RuntimeError("permission denied")
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations.Connection",
        lambda **_kwargs: conn,
    )

    with pytest.raises(RuntimeError, match="permission denied"):
        check_migration_status(_db_config())  # type: ignore[arg-type]

    assert conn.closed


class _ApplyMigrationsConnection:
    """In-memory pg8000 double recording tracker inserts for apply_migrations."""

    def __init__(self, last: str | None) -> None:
        self.last = last
        self.applied: list[str] = []
        self.closed = False

    def run(self, query: str, **params: object) -> list[list[object]]:
        if "MAX(migration_id)" in query:
            return [[self.last]]
        if "INSERT INTO yascheduler_migrations" in query:
            self.applied.append(str(params["p"]))
        return []

    def close(self) -> None:
        self.closed = True


def test_apply_migrations_upto_applies_only_the_capped_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upto isolates one chain step: tracker at '001', upto='002' applies 002 only."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_a.sql").write_text("-- a")
    (migrations_dir / "002_b.sql").write_text("-- b")
    (migrations_dir / "003_c.sql").write_text("-- c")
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )
    conn = _ApplyMigrationsConnection("001")
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations.Connection",
        lambda **_kwargs: conn,
    )

    apply_migrations(_db_config(), upto="002")  # type: ignore[arg-type]

    assert conn.applied == ["002"]
    assert conn.closed


def test_apply_migrations_upto_none_applies_through_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upto=None preserves the historical head-of-chain behavior."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_a.sql").write_text("-- a")
    (migrations_dir / "002_b.sql").write_text("-- b")
    (migrations_dir / "003_c.sql").write_text("-- c")
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )
    conn = _ApplyMigrationsConnection("001")
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations.Connection",
        lambda **_kwargs: conn,
    )

    apply_migrations(_db_config())  # type: ignore[arg-type]

    assert conn.applied == ["002", "003"]
    assert conn.closed
