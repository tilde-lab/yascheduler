"""Integration tests for the migration runner against real PostgreSQL.

Each test starts a fresh PostgresContainer so the three DB cohorts (fresh,
legacy, modern) can be set up independently. Tests 7.4/7.5 use a temp
migrations directory so synthetic migration files do not pollute the real
``sql/migrations/`` shipped with the package.
"""
# region MODULE_CONTRACT
# PURPOSE: Integration tests for the migration runner against real PostgreSQL via testcontainers.
# SCOPE: Fresh/legacy/modern DB cohorts; read-only migration-state preflight; .py best-effort reopen; .sql failure rollback; engine_inputs context supplied/absent; migration 002 backfills node_id SERIAL; migration 010 extracts typed columns from metadata JSONB; migration 011 adds task_status_field_invariants CHECK.
# KEYWORDS: migration runner, backfill, typed columns, JSONB extract, engine inputs context
# endregion MODULE_CONTRACT

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pg8000.native
import pytest
from pg8000 import DatabaseError
from testcontainers.postgres import PostgresContainer

from yascheduler.infra.persistence import (
    MigrationState,
    PostgresDbConfig,
    apply_migrations,
    check_migration_status,
)
from yascheduler.infra.persistence.postgres_schema import apply_schema

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _make_config(pg: PostgresContainer) -> PostgresDbConfig:
    url = urlparse(pg.get_connection_url())
    return PostgresDbConfig(
        user=url.username or "test",
        password=url.password or "test",
        database=url.path.lstrip("/"),
        host=url.hostname or "localhost",
        port=url.port or 5432,
    )


def _connect(config: PostgresDbConfig) -> pg8000.native.Connection:
    return pg8000.native.Connection(
        user=config.user,
        host=config.host,
        database=config.database,
        port=config.port,
        password=config.password,
    )


def _tracker_rows(conn: pg8000.native.Connection) -> list[str]:
    """Return migration_id rows from yascheduler_migrations (read in a rolled-back txn)."""
    conn.run("BEGIN")
    try:
        rows = conn.run(
            "SELECT migration_id FROM yascheduler_migrations ORDER BY migration_id",
        )
    finally:
        conn.run("ROLLBACK")
    return [r[0] for r in rows]


def _columns(conn: pg8000.native.Connection, table: str) -> list[str]:
    conn.run("BEGIN")
    try:
        rows = conn.run(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t ORDER BY column_name",
            t=table,
        )
    finally:
        conn.run("ROLLBACK")
    return [r[0] for r in rows]


def _table_exists(conn: pg8000.native.Connection, table: str) -> bool:
    conn.run("BEGIN")
    try:
        rows = conn.run("SELECT to_regclass(:t)", t=table)
    finally:
        conn.run("ROLLBACK")
    return rows[0][0] is not None


def _rows(conn: pg8000.native.Connection, sql: str) -> list[list[object]]:
    """Run a read query in a rolled-back txn and return its rows."""
    conn.run("BEGIN")
    try:
        rows = conn.run(sql)
    finally:
        conn.run("ROLLBACK")
    return rows


def test_fresh_db_seeds_last_and_skips_migrations() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            seeded = _tracker_rows(conn)
            assert seeded == ["015"]
            assert {"username", "port", "node_id"} <= set(
                _columns(conn, "yascheduler_nodes"),
            )
        finally:
            conn.close()

        apply_migrations(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["015"]
        finally:
            conn.close()


def test_migration_status_preflight_reads_real_tracker() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        missing = check_migration_status(config)
        assert missing.state is MigrationState.MISSING
        assert missing.applied is None
        assert missing.required == "015"

        apply_schema(config)

        current = check_migration_status(config)
        assert current.state is MigrationState.CURRENT
        assert current.applied == "015"
        assert current.required == "015"


@pytest.mark.parametrize(
    ("tracker_seed", "expected_before", "expected_after"),
    [
        pytest.param(
            None,
            [],
            [f"{n:03d}" for n in range(1, 16)],
            id="legacy-db-no-tracker",
        ),
        pytest.param(
            "000",
            ["000"],
            [f"{n:03d}" for n in range(16)],
            id="modern-db-tracker-at-000",
        ),
    ],
)
def test_legacy_and_modern_db_apply_pending_migrations(
    tracker_seed: str | None,
    expected_before: list[str],
    expected_after: list[str],
) -> None:
    """A legacy DB gets the full chain; a modern DB starts from its tracker."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        conn = _connect(config)
        try:
            if tracker_seed is not None:
                conn.run(
                    "CREATE TABLE yascheduler_migrations "
                    "(migration_id TEXT PRIMARY KEY, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
                )
                conn.run(
                    "INSERT INTO yascheduler_migrations (migration_id) VALUES (:m)",
                    m=tracker_seed,
                )
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "ip VARCHAR(15) UNIQUE, cloud VARCHAR(32) DEFAULT NULL, "
                "ncpus SMALLINT DEFAULT NULL)",
            )
            # Pre-create yascheduler_tasks at the pre-004 era schema (no
            # allocated_node_id) so migration 004's ALTER ADD COLUMN is
            # valid (apply_schema's CREATE TABLE IF NOT EXISTS is a no-op
            # on the existing table; without this, schema.sql would create
            # yascheduler_tasks WITH allocated_node_id and 004 would collide).
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
                "metadata JSONB, ip VARCHAR(15), status SMALLINT)",
            )
        finally:
            conn.close()

        apply_schema(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == expected_before
        finally:
            conn.close()

        apply_migrations(config, engine_inputs={})

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == expected_after
            assert {"username", "port", "node_id"} <= set(
                _columns(conn, "yascheduler_nodes"),
            )
        finally:
            conn.close()


def test_py_migration_best_effort_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "016_reopen.py").write_text(
        "from yascheduler.infra.persistence.migration_base import Migration\n"
        "class Reopen(Migration):\n"
        "    def migrate(self) -> None:\n"
        "        self.commit()\n"
        "        self.conn.run('CREATE TABLE test_reopen (id int)')\n",
    )
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )

    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)
        apply_migrations(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["015", "016"]
            assert _table_exists(conn, "test_reopen")
        finally:
            conn.close()


def test_sql_migration_failure_rolls_back_and_not_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "016_fail.sql").write_text(
        "CREATE TABLE fail_tbl (id int); CREATE TABLE fail_tbl (id int);",
    )
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )

    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        with pytest.raises(DatabaseError):
            apply_migrations(config)

        conn = _connect(config)
        try:
            assert "016" not in _tracker_rows(conn)
            assert not _table_exists(conn, "fail_tbl")
        finally:
            conn.close()


def test_py_migration_receives_supplied_engine_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_migrations(engine_inputs=...) exposes the mapping to a .py migration."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "901_ctx_supplied.py").write_text(
        "from yascheduler.infra.persistence.migration_base import Migration\n"
        "class CtxSupplied(Migration):\n"
        "    def migrate(self) -> None:\n"
        "        self.conn.run(\n"
        "            'CREATE TABLE ctx_supplied '\n"
        "            '(engine text NOT NULL, input_file text NOT NULL)'\n"
        "        )\n"
        "        for engine, files in sorted(self.engine_inputs.items()):\n"
        "            for input_file in files:\n"
        "                self.conn.run(\n"
        "                    'INSERT INTO ctx_supplied (engine, input_file) '\n"
        "                    'VALUES (:engine, :input_file)',\n"
        "                    engine=engine,\n"
        "                    input_file=input_file,\n"
        "                )\n",
    )
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )

    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)
        apply_migrations(config, engine_inputs={"dummy": ("fort.9", "fort.10")})

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["015", "901"]
            recorded = _rows(
                conn,
                "SELECT engine, input_file FROM ctx_supplied",
            )
            assert {tuple(row) for row in recorded} == {
                ("dummy", "fort.9"),
                ("dummy", "fort.10"),
            }
        finally:
            conn.close()


def test_py_migration_receives_no_engine_inputs_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_migrations(config) leaves the .py migration's engine_inputs as None."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "902_ctx_absent.py").write_text(
        "from yascheduler.infra.persistence.migration_base import Migration\n"
        "class CtxAbsent(Migration):\n"
        "    def migrate(self) -> None:\n"
        "        self.conn.run(\n"
        "            'CREATE TABLE ctx_absent '\n"
        "            '(engine_inputs_is_none boolean NOT NULL)'\n"
        "        )\n"
        "        self.conn.run(\n"
        "            'INSERT INTO ctx_absent (engine_inputs_is_none) '\n"
        "            'VALUES (:is_none)',\n"
        "            is_none=self.engine_inputs is None,\n"
        "        )\n",
    )
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )

    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)
        apply_migrations(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["015", "902"]
            assert _rows(
                conn,
                "SELECT engine_inputs_is_none FROM ctx_absent",
            ) == [[True]]
        finally:
            conn.close()


def test_sql_migration_still_applies_with_engine_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.sql migrations ignore engine_inputs and still apply when it is supplied."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "903_ctx_sql.sql").write_text(
        "CREATE TABLE ctx_sql (note text NOT NULL);"
        "INSERT INTO ctx_sql (note) VALUES ('sql path unaffected');",
    )
    monkeypatch.setattr(
        "yascheduler.infra.persistence.postgres_migrations._MIGRATIONS_DIR",
        migrations_dir,
    )

    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)
        apply_migrations(config, engine_inputs={"dummy": ("fort.9",)})

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["015", "903"]
            assert _rows(conn, "SELECT note FROM ctx_sql") == [
                ["sql path unaffected"],
            ]
        finally:
            conn.close()


def test_migration_002_adds_node_id_on_legacy_db() -> None:
    """Migration 002 adds node_id SERIAL PRIMARY KEY and backfills existing rows with sequential ids."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        # Build a legacy-style DB: yascheduler_nodes WITHOUT node_id, with rows.
        conn = _connect(config)
        try:
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "ip VARCHAR(15) UNIQUE, port INTEGER DEFAULT 22, "
                "username VARCHAR(255) DEFAULT 'root', ncpus SMALLINT DEFAULT NULL, "
                "enabled BOOLEAN DEFAULT TRUE, cloud VARCHAR(32) DEFAULT NULL)",
            )
            conn.run("INSERT INTO yascheduler_nodes (ip) VALUES ('10.0.0.1')")
            conn.run("INSERT INTO yascheduler_nodes (ip) VALUES ('10.0.0.2')")
            # Pre-create yascheduler_tasks at the pre-004 era schema (no
            # allocated_node_id) so migration 004's ALTER ADD COLUMN is valid.
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
                "metadata JSONB, ip VARCHAR(15), status SMALLINT)",
            )
        finally:
            conn.close()

        # apply_schema is a no-op on the existing tables (CREATE TABLE IF NOT
        # EXISTS); apply_migrations runs exactly 001 (adds username/port IF
        # NOT EXISTS — already present) and 002 (adds node_id SERIAL PRIMARY
        # KEY, backfilling existing rows) — upto caps the chain at this era.
        apply_schema(config)
        apply_migrations(config, upto="002")

        conn = _connect(config)
        try:
            # Tracker records exactly 001-002.
            assert _tracker_rows(conn) == ["001", "002"]
            # node_id column now exists.
            cols = _columns(conn, "yascheduler_nodes")
            assert "node_id" in cols
            # Existing rows were backfilled with sequential positive SERIAL values
            # (physical order; PG assigns 1, 2, ... — assert they're distinct & positive).
            conn.run("BEGIN")
            try:
                rows = conn.run(
                    "SELECT node_id, ip FROM yascheduler_nodes ORDER BY node_id",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(rows) == 2
            ids = [r[0] for r in rows]
            assert all(isinstance(i, int) and i > 0 for i in ids), ids
            assert len(set(ids)) == 2, f"node_id values must be distinct, got {ids}"
        finally:
            conn.close()


def test_migration_005_converts_serial_to_identity() -> None:
    """Migration 005 converts SERIAL PRIMARY KEY to GENERATED ALWAYS AS IDENTITY."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        # Build a pre-005 DB: both PKs are SERIAL, with one node row inserted so
        # the SERIAL sequence is at 1 (node_id=1). Seed the tracker to '004' so
        # apply_migrations runs only 005.
        conn = _connect(config)
        try:
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "node_id SERIAL PRIMARY KEY, ip VARCHAR(15), port INTEGER DEFAULT 22, "
                "username VARCHAR(255) DEFAULT 'root', ncpus SMALLINT DEFAULT NULL, "
                "enabled BOOLEAN DEFAULT TRUE, cloud VARCHAR(32) DEFAULT NULL)",
            )
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), metadata JSONB, "
                "ip VARCHAR(15), status SMALLINT, "
                "allocated_node_id INTEGER)",
            )
            conn.run("INSERT INTO yascheduler_nodes (ip) VALUES ('10.0.0.1')")
            # Note the SERIAL-assigned id (expected: 1).
            conn.run("BEGIN")
            try:
                assigned = conn.run(
                    "SELECT node_id FROM yascheduler_nodes WHERE ip = '10.0.0.1'",
                )
            finally:
                conn.run("ROLLBACK")
            pre_node_id = assigned[0][0]
            assert pre_node_id == 1, pre_node_id
            conn.run(
                "CREATE TABLE yascheduler_migrations "
                "(migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('004')")
        finally:
            conn.close()

        # apply_schema's DO block is a no-op (tracker exists); the CREATE TABLE
        # IF NOT EXISTS is a no-op on the existing tables. apply_migrations
        # runs only 005 (prefix_id '005' > '004'; upto caps the chain here).
        apply_schema(config)
        apply_migrations(config, upto="005")

        conn = _connect(config)
        try:
            # (a) Both PK columns are now GENERATED ALWAYS AS IDENTITY.
            conn.run("BEGIN")
            try:
                rows = conn.run(
                    "SELECT table_name, column_name, is_identity, "
                    "identity_generation FROM information_schema.columns "
                    "WHERE (table_name, column_name) IN "
                    "(('yascheduler_nodes','node_id'),"
                    "('yascheduler_tasks','task_id')) "
                    "ORDER BY table_name",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(rows) == 2, rows
            for _tbl, _col, is_identity, gen in rows:
                assert is_identity == "YES", (_tbl, _col, is_identity)
                assert gen == "ALWAYS", (_tbl, _col, gen)

            # The tracker records 004 (seeded) and 005 (applied) only.
            assert _tracker_rows(conn) == ["004", "005"]

            # (b) The identity sequence next value > the previously inserted id,
            # so the next insert will not collide. The identity sequence was
            # seeded via setval(..., MAX+1, false) so nextval returns MAX+1
            # (pre_node_id=1, MAX=1 → nextval returns 2). nextval consumes the
            # value; assertion (c) below then gets the following value (3).
            conn.run("BEGIN")
            try:
                next_id = conn.run(
                    "SELECT nextval(pg_get_serial_sequence('yascheduler_nodes',"
                    "'node_id'))",
                )
            finally:
                conn.run("ROLLBACK")
            assert next_id[0][0] > pre_node_id, (next_id[0][0], pre_node_id)

            # (c) A subsequent insert auto-assigns a unique id (no collision).
            conn.run("BEGIN")
            try:
                inserted = conn.run(
                    "INSERT INTO yascheduler_nodes (ip) VALUES ('10.0.0.2') "
                    "RETURNING node_id",
                )
                node_rows = conn.run(
                    "SELECT node_id FROM yascheduler_nodes ORDER BY node_id",
                )
            finally:
                conn.run("ROLLBACK")
            new_id = inserted[0][0]
            assert new_id > pre_node_id, (new_id, pre_node_id)
            assert {r[0] for r in node_rows} == {pre_node_id, new_id}, node_rows
        finally:
            conn.close()


def test_legacy_db_at_005_applies_006_010() -> None:
    """Legacy DB at migration 005: applying through 010 produces the era-010 schema shape."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        # Build a legacy DB at migration 005: pre-006 schema (label, status SMALLINT, ip, no created_at/updated_at).
        conn = _connect(config)
        try:
            conn.run(
                "CREATE TABLE yascheduler_migrations "
                "(migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('005')")
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "node_id SERIAL PRIMARY KEY, ip VARCHAR(15), "
                "port INTEGER DEFAULT 22, username VARCHAR(255) DEFAULT 'root', "
                "ncpus SMALLINT DEFAULT NULL, enabled BOOLEAN DEFAULT TRUE, "
                "cloud VARCHAR(32) DEFAULT NULL)",
            )
            # Pre-006 schema: label (not title), status SMALLINT, ip present.
            # allocated_node_id IS present (added by migration 004, which a DB
            # at 005 has already applied).
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
                "metadata JSONB, ip VARCHAR(15), status SMALLINT, "
                "allocated_node_id INTEGER)",
            )
            conn.run(
                "INSERT INTO yascheduler_tasks (label, ip, status, metadata) "
                "VALUES ('legacy_task', '10.0.0.1', 0, "
                '\'{"engine": "fleur", "fort.9": "QUJD"}\'::jsonb)',
            )
        finally:
            conn.close()

        apply_schema(config)
        # upto caps the chain at 010 (this test's era); engine context is not
        # needed because the 014 conversion never runs here.
        apply_migrations(config, upto="010")

        conn = _connect(config)
        try:
            cols = _columns(conn, "yascheduler_tasks")
            # label was renamed to title
            assert "label" not in cols
            assert "title" in cols
            # ip column was dropped
            assert "ip" not in cols
            # created_at and updated_at present
            assert "created_at" in cols
            assert "updated_at" in cols
            # Migration 010: metadata dropped, typed columns extracted
            assert "metadata" not in cols
            assert "engine" in cols
            assert "extra" in cols
            assert "remote_folder" in cols
            assert "local_folder" in cols
            assert "webhook_url" in cols
            assert "error" in cols
            assert "webhook_custom_params" in cols

            # status column type is task_status enum with correct labels
            conn.run("BEGIN")
            try:
                type_rows = conn.run(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'yascheduler_tasks' AND column_name = 'status'",
                )
            finally:
                conn.run("ROLLBACK")
            assert type_rows[0][0] == "USER-DEFINED"

            conn.run("BEGIN")
            try:
                enum_rows = conn.run(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'task_status' ORDER BY e.enumsortorder",
                )
            finally:
                conn.run("ROLLBACK")
            assert [r[0] for r in enum_rows] == ["TO_DO", "RUNNING", "DONE"]

            # Trigger exists
            conn.run("BEGIN")
            try:
                trig_rows = conn.run(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'yascheduler_tasks'::regclass "
                    "AND tgname = 'yascheduler_tasks_touch_updated_at'",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(trig_rows) == 1

            # Insert a row, commit, then UPDATE in a new transaction so the
            # trigger's NOW() (transaction-start time) differs from the INSERT's NOW().
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_tasks (title, status, engine) "
                    "VALUES ('trigger_test', 'TO_DO', 'fleur')",
                )
            finally:
                conn.run("COMMIT")

            conn.run("BEGIN")
            try:
                row = conn.run(
                    "SELECT task_id, created_at, updated_at "
                    "FROM yascheduler_tasks WHERE title = 'trigger_test'",
                )
                assert len(row) == 1
                task_id = row[0][0]
                created_before = row[0][1]
                updated_before = row[0][2]

                # Sleep a tiny bit so the timestamps differ.
                import time as _time

                _time.sleep(0.05)

                # Transition TO_DO → DONE (a valid task_status label at this
                # era; the 011 field-invariants CHECK does not exist yet and
                # is not what this step exercises).
                conn.run(
                    "UPDATE yascheduler_tasks SET status = 'DONE' WHERE task_id = :tid",
                    tid=task_id,
                )
                row2 = conn.run(
                    "SELECT created_at, updated_at "
                    "FROM yascheduler_tasks WHERE task_id = :tid",
                    tid=task_id,
                )
                assert row2[0][0] == created_before, (
                    "created_at must NOT change on UPDATE"
                )
                assert row2[0][1] > updated_before, "updated_at must advance on UPDATE"
            finally:
                conn.run("ROLLBACK")

            # Tracker records 005 (seeded) + 006-010 (applied) only.
            assert _tracker_rows(conn) == [
                "005",
                "006",
                "007",
                "008",
                "009",
                "010",
            ]

            # The legacy row's status was converted via the USING clause.
            conn.run("BEGIN")
            try:
                legacy = conn.run(
                    "SELECT title, status::text FROM yascheduler_tasks "
                    "WHERE title = 'legacy_task'",
                )
            finally:
                conn.run("ROLLBACK")
            assert legacy[0][1] == "TO_DO"
        finally:
            conn.close()


def test_fresh_db_full_shape() -> None:
    """Fresh DB: apply_schema produces final shape; apply_migrations is no-op."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            # Tracker seeds to '015'
            assert _tracker_rows(conn) == ["015"]

            cols = _columns(conn, "yascheduler_tasks")
            assert "task_id" in cols
            assert "title" in cols  # not label
            assert "status" in cols
            assert "metadata" not in cols  # dropped by migration 010
            assert "engine" in cols
            assert "remote_folder" in cols
            assert "local_folder" in cols
            assert "webhook_url" in cols
            assert "error" in cols
            assert "webhook_custom_params" in cols
            assert "extra" in cols
            assert "output_selections" in cols
            assert "allocated_node_id" in cols
            assert "created_at" in cols
            assert "updated_at" in cols
            assert "ip" not in cols  # dropped

            # status column type is task_status enum
            conn.run("BEGIN")
            try:
                type_rows = conn.run(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'yascheduler_tasks' AND column_name = 'status'",
                )
            finally:
                conn.run("ROLLBACK")
            assert type_rows[0][0] == "USER-DEFINED"

            # Trigger exists
            conn.run("BEGIN")
            try:
                trig_rows = conn.run(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'yascheduler_tasks'::regclass "
                    "AND tgname = 'yascheduler_tasks_touch_updated_at'",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(trig_rows) == 1
        finally:
            conn.close()

        # apply_migrations finds MAX='015' and applies nothing.
        apply_migrations(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["015"]
        finally:
            conn.close()


def test_migration_008_fails_on_out_of_range_status() -> None:
    """Migration 008 fails (rolls back) when a row has out-of-range status (e.g. 3)."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        conn = _connect(config)
        try:
            conn.run(
                "CREATE TABLE yascheduler_migrations "
                "(migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('007')")
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "node_id SERIAL PRIMARY KEY, ip VARCHAR(15), "
                "port INTEGER DEFAULT 22, username VARCHAR(255) DEFAULT 'root', "
                "ncpus SMALLINT DEFAULT NULL, enabled BOOLEAN DEFAULT TRUE, "
                "cloud VARCHAR(32) DEFAULT NULL)",
            )
            # Pre-008 schema: status SMALLINT NOT NULL DEFAULT 0, no allocated_node_id, no type.
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
                "metadata JSONB, ip VARCHAR(15), "
                "status SMALLINT NOT NULL DEFAULT 0)",
            )
            # Row with out-of-range status = 3 (maps to NULL via USING CASE).
            conn.run(
                "INSERT INTO yascheduler_tasks (label, status, metadata) "
                "VALUES ('bad', 3, '{}'::jsonb)",
            )
        finally:
            conn.close()

        # apply_schema is a no-op on existing tables.
        apply_schema(config)

        # apply_migrations runs 008 (upto caps the chain at this era); it
        # should fail because status=3 → NULL violates NOT NULL.
        with pytest.raises(DatabaseError):
            apply_migrations(config, upto="008")

        conn = _connect(config)
        try:
            # Migration 008 NOT recorded in tracker.
            assert _tracker_rows(conn) == ["007"]
            # status column remains SMALLINT (rollback restored the schema).
            conn.run("BEGIN")
            try:
                type_rows = conn.run(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'yascheduler_tasks' AND column_name = 'status'",
                )
            finally:
                conn.run("ROLLBACK")
            assert type_rows[0][0] == "smallint"
        finally:
            conn.close()
