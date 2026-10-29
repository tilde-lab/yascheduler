"""Apply pending database schema/data migrations in forward-only order."""
# region MODULE_CONTRACT
# PURPOSE: Evolve the database schema and data forward in production and expose its compatibility state to daemon startup — so operators apply incremental changes explicitly before scheduling begins.
# SCOPE:
# - Forward-only DDL/DML migration application over sql/migrations/ via one pg8000 connection, one transaction per migration, with yascheduler_migrations tracker recording.
# - Read-only packaged-migration versus tracker inspection for daemon startup; NOT schema validation or migration application.
# - NOT: rollback / "down" path; NOT: migration generation tool; NOT: schema.sql generation tool; NOT: prefix_id uniqueness validation at runtime (a unit-test responsibility).
# INVARIANTS: The migration system is forward-only — once a row is recorded in yascheduler_migrations, the runner never deletes it and never reverses the migration; the runner does not validate prefix_id uniqueness across files — a unit test scanning the migrations directory asserts uniqueness; the schema.sql snapshot and individual migration files are hand-written, no tool generates either; apply_migrations is synchronous and opens a single pg8000 connection for the whole run.
# DEPENDENCIES: USES API: pg8000.Connection, READS: migration files (.sql, .py) from sql/migrations/, LOADS: Python migration modules dynamically via importlib
# RATIONALE:
# - Q: Why is the migration system forward-only with no down / rollback path?
#   A: Rolling back a migration in production typically requires data-loss decisions the runner cannot make (a column add cannot be reversed without dropping the column and its data; a data backfill cannot be reversed without the pre-image); the project ships a new forward migration to fix a bad one, and the yascheduler_migrations tracker row is the audit log of what landed.
# - Q: Why does the runner not validate prefix_id uniqueness at runtime?
#   A: A duplicate prefix_id is a static authoring defect — surfacing it via a unit test that scans the migrations directory gives the author feedback at CI time before the migration reaches any database, and keeps the runner's job (apply pending migrations) narrow.
# - Q: Why is there no migration generation tool?
#   A: Every migration in this project is small, schema-anchored, and hand-reviewed against the schema.sql snapshot (the Migration edit procedure requirement); auto-generation would couple the project to a schema-diff tool and bypass the human review that the snapshot-conformance step enforces.
# KEYWORDS: migration, apply, database, dml, ddl
# endregion MODULE_CONTRACT

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pg8000 import DatabaseError
from pg8000.native import Connection

from .migration_base import EngineInputsContext, Migration

if TYPE_CHECKING:
    from types import ModuleType

    from .db_config import PostgresDbConfig

__all__ = [
    "MigrationState",
    "MigrationStatus",
    "apply_migrations",
    "check_migration_status",
]
logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "sql" / "migrations"


# region CLASS_MigrationState
# PURPOSE: Name every possible packaged-migration versus database-tracker relationship so daemon startup can reject an incompatible database without applying a migration.
class MigrationState(str, Enum):
    """Database migration compatibility state."""

    MISSING = "missing"
    EMPTY = "empty"
    CURRENT = "current"
    BEHIND = "behind"
    AHEAD = "ahead"


# endregion CLASS_MigrationState


# region CLASS_MigrationStatus
# PURPOSE: Carry the read-only migration compatibility result and the involved version identifiers from persistence to the daemon startup boundary.
@dataclass(frozen=True)
class MigrationStatus:
    """Result of comparing the database tracker with packaged migrations."""

    state: MigrationState
    applied: str | None
    required: str


# endregion CLASS_MigrationStatus


def _prefix_id(filename: Path) -> str:
    return filename.name.split("_", 1)[0]


# region FUNC__scan_migrations
# PURPOSE: Hand the runner the complete, filename-sorted list of .sql and .py migration files so it can compute the pending set without re-globbing the directory per step.
# INVARIANTS: Returns paths sorted by Path.name string comparison — same key the runner uses for prefix_id ordering; scans _MIGRATIONS_DIR for both *.sql and *.py files; the returned list is a fresh list each call — no caching across apply_migrations invocations.
def _scan_migrations() -> list[Path]:
    files = list(_MIGRATIONS_DIR.glob("*.sql")) + list(_MIGRATIONS_DIR.glob("*.py"))
    return sorted(files, key=lambda f: f.name)


# endregion FUNC__scan_migrations


# region FUNC_check_migration_status
# PURPOSE: Report whether a configured database can run the installed daemon without mutating its schema, tracker, or data.
# REQUIRES: config describes a reachable PostgreSQL database and the packaged migrations directory contains at least one migration file.
# ENSURES: Returns a status for every readable tracker state; connection and query errors propagate unchanged instead of being recast as migration compatibility.
# INVARIANTS: Opens one short-lived connection; performs only SELECT queries; compares prefix tokens with the same lexicographic ordering as apply_migrations.
def check_migration_status(config: PostgresDbConfig) -> MigrationStatus:
    """Inspect the migration tracker without applying schema or migrations."""
    files = _scan_migrations()
    if not files:
        msg = "no packaged migrations found"
        raise RuntimeError(msg)
    required = _prefix_id(files[-1])

    conn: Connection | None = None
    try:
        # region BLOCK_open_connection
        conn = Connection(
            user=config.user,
            host=config.host,
            database=config.database,
            port=config.port,
            password=config.password,
        )
        # endregion BLOCK_open_connection

        # region BLOCK_read_tracker
        tracker_rows = conn.run("SELECT to_regclass('yascheduler_migrations')")
        if not tracker_rows or tracker_rows[0][0] is None:
            status = MigrationStatus(MigrationState.MISSING, None, required)
        else:
            rows = conn.run("SELECT MAX(migration_id) FROM yascheduler_migrations")
            applied = None if not rows or rows[0][0] is None else str(rows[0][0])
            if applied is None:
                status = MigrationStatus(MigrationState.EMPTY, None, required)
            elif applied < required:
                status = MigrationStatus(MigrationState.BEHIND, applied, required)
            elif applied > required:
                status = MigrationStatus(MigrationState.AHEAD, applied, required)
            else:
                status = MigrationStatus(MigrationState.CURRENT, applied, required)
        # endregion BLOCK_read_tracker

        logger.debug(
            "MIGRATION_STATUS",
            extra={
                "state": status.state.value,
                "applied": status.applied,
                "required": status.required,
            },
        )
        return status
    finally:
        # region BLOCK_close
        if conn is not None:
            conn.close()
        # endregion BLOCK_close


# endregion FUNC_check_migration_status


# region FUNC__last_applied
# PURPOSE: Determine the last successfully applied migration so the runner applies only newer files and avoids re-executing already-recorded steps.
# INVARIANTS: Runs SELECT MAX(migration_id) FROM yascheduler_migrations and returns str(rows[0][0]) — the tracker stores prefix_id strings, so the comparison is lexicographic, matching the filename sort; the try/except DatabaseError is defensive — apply_schema is contractually run before apply_migrations and creates the tracker, but the except branch treats an absent tracker as None ("apply all") rather than crashing; apply_migrations is therefore robust to a manually-dropped tracker on a database that has no migrations applied yet.
# ENSURES: Returns None when the tracker table is empty or absent (defensive — apply_schema is contractually run first).
def _last_applied(conn: Connection) -> str | None:
    try:
        rows = conn.run("SELECT MAX(migration_id) FROM yascheduler_migrations")
    except DatabaseError:
        # Defensive: apply_schema is contractually run first and creates the
        # tracker. Treat its absence as "apply all" rather than crashing.
        return None
    if not rows or rows[0][0] is None:
        return None
    return str(rows[0][0])


# endregion FUNC__last_applied


# region FUNC__pending
# PURPOSE: Compute the set of not-yet-applied migrations so the runner applies only what is needed, preserving chronological order.
# INVARIANTS: Returns the full input list when last is None — fresh database, every migration is pending; otherwise filters by _prefix_id(f) > last — lexicographic comparison on the before-first-_ token, matching the filename sort key; a non-None upto caps the result inclusively at _prefix_id(f) <= upto (same lexicographic key), so a caller can apply exactly one step of the chain; preserves the sorted order of the input list — no re-sort.
def _pending(
    last: str | None,
    files: list[Path],
    upto: str | None = None,
) -> list[Path]:
    if last is None:
        candidates = list(files)
    else:
        candidates = [f for f in files if _prefix_id(f) > last]
    if upto is None:
        return candidates
    return [f for f in candidates if _prefix_id(f) <= upto]


# endregion FUNC__pending


# region FUNC__one_migration_subclass
# PURPOSE: Extract the single Migration subclass from a .py migration file so the runner can call migrate() — fails loud on ambiguous or empty files to prevent silent skips.
# INVARIANTS: Uses inspect.getmembers(module, inspect.isclass) filtered to issubclass(cls, Migration) and cls is not Migration and cls.__module__ == module.__name__ — imported Migration re-exports and unrelated Migration subclasses from other modules are NOT counted; candidate count MUST be exactly 1, otherwise raises RuntimeError with a message naming module.__file__ and the candidate count.
# RATIONALE:
# - Q: Why filter by cls.__module__ == module.__name__ in addition to issubclass(cls, Migration)?
#   A: A .py migration file typically imports from yascheduler.infra.persistence.migration_base import Migration to declare its subclass — without the __module__ filter, Migration itself would appear as a class in the module and pollute the candidate list; filtering to classes defined locally to the migration file keeps the discovery exact.
def _one_migration_subclass(module: ModuleType) -> type[Migration]:
    candidates = [
        cls
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, Migration)
        and cls is not Migration
        and cls.__module__ == module.__name__
    ]
    if len(candidates) != 1:
        msg = (
            f"{module.__file__}: expected exactly one Migration subclass, "
            f"found {len(candidates)}"
        )
        raise RuntimeError(msg)
    return candidates[0]


# endregion FUNC__one_migration_subclass


# region FUNC__rollback
# PURPOSE: Drain the runner's open transaction on the failure path of every migration step so a half-applied migration does not leave a BEGIN-state transaction behind for the next migration to inherit.
# ENSURES: On call, issues ROLLBACK on the wrapped connection inside a contextlib.suppress(Exception) — best-effort: a connection-side failure during rollback is silenced because the caller is already on the exception path and will re-raise the original error.
def _rollback(conn: Connection) -> None:
    # region BLOCK_rollback
    with contextlib.suppress(Exception):
        conn.run("ROLLBACK")
    # endregion BLOCK_rollback


# endregion FUNC__rollback


# region FUNC__apply_sql_migration
# PURPOSE: Execute a .sql migration file atomically and record it in the tracker so the schema evolves safely and re-runs are prevented.
# INVARIANTS: Issues BEGIN then conn.run(path.read_text()) — the file text is executed as a multi-statement string in one round-trip, mirroring psql's default behavior; the tracker INSERT runs inside the same transaction as the SQL body so a schema change and its tracker record commit atomically.
# ENSURES: On success, a row (<prefix_id>, <default-timestamp>) exists in yascheduler_migrations and the migration's SQL is committed; on any error from BEGIN through COMMIT, issues best-effort ROLLBACK via _rollback(conn) and re-raises — no tracker row is inserted for the failed migration.
def _apply_sql_migration(conn: Connection, path: Path, prefix_id: str) -> None:
    conn.run("BEGIN")
    try:
        # region BLOCK_apply_sql
        conn.run(path.read_text())
        conn.run(
            "INSERT INTO yascheduler_migrations (migration_id) VALUES (:p)",
            p=prefix_id,
        )
        conn.run("COMMIT")
        logger.debug("APPLY_SQL", extra={"prefix": prefix_id})
        # endregion BLOCK_apply_sql
    except Exception:
        _rollback(conn)
        raise


# endregion FUNC__apply_sql_migration


# region FUNC__record_py_tracker
# PURPOSE: Persist the tracker record for a .py migration after migrate() succeeds so the same step is never re-applied, even if the runner crashes after the SQL landed.
# INVARIANTS: Issues INSERT INTO yascheduler_migrations (migration_id) VALUES (:p) then COMMIT; on DatabaseError, reopens with BEGIN and retries the INSERT / COMMIT exactly once — the retry path handles transient DB-side errors like a deadlock on the tracker table; a non-transient failure such as a duplicate-prefix_id primary-key violation re-raises from the retry and surfaces the real defect.
# ENSURES: On success, the row (<prefix_id>, <default-timestamp>) exists in yascheduler_migrations; works in BOTH the normal case — migrate's transaction still open, the INSERT/COMMIT commits inside it — and the commit-closed case — migrate called self.commit() and did not reopen, the INSERT autocommits and the trailing COMMIT is a no-op warning rather than an error.
# RATIONALE:
# - Q: Why does the tracker record succeed in BOTH the normal case and the self.commit()-closed case?
#   A: pg8000 native AUTOCOMMITS statements issued outside an open transaction, and a bare COMMIT with no open transaction is a no-op warning rather than an error — so a Python migration that legitimately closed the runner's transaction for a non-transactional operation (CREATE INDEX CONCURRENTLY, VACUUM) is still tracker-recorded; the contract is "migrate() applied <=> tracker recorded".
# - Q: Why does the function retry the INSERT/COMMIT once on DatabaseError?
#   A: The retry is a defensive guard for transient DB-side errors on the tracker record (e.g. a deadlock between two concurrent yainit invocations); the reopen-and-retry turns a transient conflict into a success, while a non-transient failure (duplicate-prefix_id PK violation) re-raises from the retry and lets the caller's ROLLBACK handle it.
def _record_py_tracker(conn: Connection, prefix_id: str) -> None:
    # region BLOCK_tracker_record
    # pg8000 native AUTOCOMMITS statements issued outside an open
    # transaction, and a bare COMMIT with no open transaction is a no-op
    # warning rather than an error. Consequently, when migrate() closed the
    # runner's transaction via self.commit() (for a non-transactional op like
    # CREATE INDEX CONCURRENTLY), the INSERT below simply autocommits and
    # COMMIT is a no-op — the tracker is recorded in both the normal and the
    # commit-closed cases (migrate() applied <=> tracker recorded).
    #
    # The except-DatabaseError reopen branch is a defensive guard for
    # transient DB-side errors on the tracker INSERT/COMMIT (e.g. a deadlock):
    # it reopens a fresh transaction and retries the record once. A real
    # failure (e.g. a duplicate-prefix_id PK violation) is re-raised by the
    # retry and handled by the caller's ROLLBACK.
    try:
        conn.run(
            "INSERT INTO yascheduler_migrations (migration_id) VALUES (:p)",
            p=prefix_id,
        )
        conn.run("COMMIT")
    except DatabaseError as exc:
        logger.debug("TRACKER_RECORD", extra={"exc": exc})
        conn.run("BEGIN")
        conn.run(
            "INSERT INTO yascheduler_migrations (migration_id) VALUES (:p)",
            p=prefix_id,
        )
        conn.run("COMMIT")
    logger.debug("TRACKER_RECORD", extra={"prefix": prefix_id})
    # endregion BLOCK_tracker_record


# endregion FUNC__record_py_tracker


# region FUNC__apply_py_migration
# PURPOSE: Execute a Python migration step inside a transaction — load the module, call migrate(), and record the tracker — so complex DDL/DML logic runs safely and is never re-applied.
# INVARIANTS: Loads the migration module from path under its qualified persistence module name, so module-local loggers retain source identity and structured trace rendering; tracker identity remains prefix_id. Raises RuntimeError if the spec or loader is None; the runner opens BEGIN BEFORE loading the module, so a module-level statement that touches the DB (rare but legal) runs inside the migration's transaction.
# ENSURES: On success, the migration's migrate() body has run AND the tracker row for <prefix_id> is recorded (the tracker recording is delegated to _record_py_tracker); on any error from module load, migrate(), or tracker recording, issues best-effort ROLLBACK via _rollback(conn) and re-raises — no tracker row is inserted for the failed migration.
def _apply_py_migration(
    conn: Connection,
    path: Path,
    prefix_id: str,
    config: PostgresDbConfig,
    engine_inputs: EngineInputsContext | None = None,
) -> None:
    module_name = f"{__package__}.sql.migrations.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        msg = f"{path}: cannot load migration module"
        raise RuntimeError(msg)
    conn.run("BEGIN")
    try:
        # region BLOCK_apply_py
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        klass = _one_migration_subclass(module)
        migration = klass(config, conn, logger, engine_inputs=engine_inputs)
        migration.migrate()
        # endregion BLOCK_apply_py

        _record_py_tracker(conn, prefix_id)
    except Exception:
        _rollback(conn)
        raise


# endregion FUNC__apply_py_migration


# region FUNC_apply_migrations
# PURPOSE: Apply all pending schema/data migrations in forward order so the database is up-to-date on every deployment without manual SQL intervention.
# REQUIRES: config is a validated PostgresDbConfig with a reachable database; the database already has the yascheduler_migrations tracker table — apply_schema runs first in yainit and in test fixtures.
# INVARIANTS: Synchronous — opens ONE pg8000 Connection for the whole run and closes it in finally; pending list is computed once from _last_applied + _scan_migrations + _pending — the directory is NOT re-scanned per migration; dispatches on path.suffix — .sql files route to _apply_sql_migration, everything else routes to _apply_py_migration; the runner does NOT validate prefix_id uniqueness at runtime — that is a unit-test responsibility; the runner does NOT delete tracker rows nor reverse applied migrations — the system is forward-only.
# ENSURES: On success, every file in the pending list has a tracker row; on failure, the failing migration's tracker row is NOT inserted and the error propagates out of apply_migrations — the connection is closed in finally regardless.
# INVARIANTS: engine_inputs is an optional engine-name-to-input-declarations mapping; None (default) preserves the pre-context behavior for callers without engine configuration; only .py migrations receive it — SQL migrations never depend on engine context; upto optionally caps the pending set inclusively at its prefix_id (``upto="012"`` with a tracker at '011' applies exactly migration 012) — None (default) applies through the head of the chain; upto does not override the tracker — steps at or below the last applied migration are never re-applied.
def apply_migrations(
    config: PostgresDbConfig,
    engine_inputs: EngineInputsContext | None = None,
    upto: str | None = None,
) -> None:
    """Apply pending migrations from sql/migrations/ to the DB described by config, each in its own transaction.

    ``engine_inputs`` optionally supplies each engine's declared input file
    selections to Python migrations (the input conversion needs them);
    ``None`` keeps the historical behavior. ``upto`` optionally caps the run
    inclusively at that migration prefix so callers (tests) can apply exactly
    one step of the chain; ``None`` applies everything pending.
    """
    conn: Connection | None = None
    try:
        # region BLOCK_open_connection
        conn = Connection(
            user=config.user,
            host=config.host,
            database=config.database,
            port=config.port,
            password=config.password,
        )
        # endregion BLOCK_open_connection

        # region BLOCK_read_last
        last = _last_applied(conn)
        files = _scan_migrations()
        pending = _pending(last, files, upto)
        logger.debug(
            "READ_LAST",
            extra={"last": last, "upto": upto, "pending": len(pending)},
        )
        # endregion BLOCK_read_last

        # region BLOCK_apply_pending
        for path in pending:
            pid = _prefix_id(path)
            if path.suffix == ".sql":
                _apply_sql_migration(conn, path, pid)
            else:
                _apply_py_migration(conn, path, pid, config, engine_inputs)
            logger.debug("APPLY_PENDING", extra={"prefix": pid})
        # endregion BLOCK_apply_pending
    finally:
        # region BLOCK_close
        if conn is not None:
            conn.close()
        # endregion BLOCK_close


# endregion FUNC_apply_migrations
