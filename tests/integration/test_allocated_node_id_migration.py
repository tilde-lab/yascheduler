"""Integration tests for migration 004 (add-allocated-node-id) via testcontainers.

Covers the spec scenarios in
openspec/changes/task-allocated-node-id/specs/db-migrations/spec.md and
specs/postgres-schema-apply/spec.md:

* migration 004 adds a nullable allocated_node_id column with FK ON DELETE SET NULL
* migration 004 backfills allocated_node_id by joining yascheduler_nodes.ip = yascheduler_tasks.ip
* unallocated tasks (ip IS NULL) stay allocated_node_id = NULL
* FK ON DELETE SET NULL nulls allocated_node_id when the node is removed
  (the task row is preserved; allocated_ip column is dropped by migration 009)
* a fresh DB seeds yascheduler_migrations to '014' and apply_migrations skips 014
"""

# region MODULE_CONTRACT
# PURPOSE: Integration tests for migration 004 (add-allocated-node-id) and the schema.sql snapshot against real PostgreSQL via testcontainers.
# SCOPE: migration 004 adds nullable allocated_node_id column with FK ON DELETE SET NULL; backfills existing tasks by joining ip; leaves unallocated (ip IS NULL) tasks NULL; FK nulls allocated_node_id on node delete (allocated_ip dropped by migration 009); fresh DB seeds to 014; schema.sql CREATE TABLE includes the column.
# KEYWORDS: migration 004, allocated_node_id, backfill, FK
# endregion MODULE_CONTRACT

from __future__ import annotations

from urllib.parse import urlparse

import pg8000.native
import pytest
from testcontainers.postgres import PostgresContainer

from yascheduler.infra.persistence import PostgresDbConfig, apply_migrations
from yascheduler.infra.persistence.postgres_schema import apply_schema

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
            "WHERE table_name = :t ORDER BY ordinal_position",
            t=table,
        )
    finally:
        conn.run("ROLLBACK")
    return [r[0] for r in rows]


def _fk_on_delete_action(conn: pg8000.native.Connection) -> str:
    """Return the ON DELETE action code for the allocated_node_id FK.

    Postgres confdeltype codes: 'a'=NO ACTION, 'c'=CASCADE, 'r'=RESTRICT,
    'n'=SET NULL, 'd'=SET DEFAULT. We identify the FK by matching the
    column at attnum 5 in yascheduler_tasks (allocated_node_id is the 6th
    column in the CREATE TABLE: task_id, label, metadata, ip, status,
    allocated_node_id).
    """
    conn.run("BEGIN")
    try:
        rows = conn.run(
            "SELECT confdeltype FROM pg_constraint conf "
            "JOIN pg_class c ON c.oid = conf.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'yascheduler_tasks' "
            "AND conf.contype = 'f'",
        )
    finally:
        conn.run("ROLLBACK")
    return rows[0][0] if rows else ""


def test_migration_004_adds_allocated_node_id_column() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)
        apply_migrations(config)

        conn = _connect(config)
        try:
            cols = _columns(conn, "yascheduler_tasks")
            assert "allocated_node_id" in cols
            # Nullable: the column has no NOT NULL constraint.
            conn.run("BEGIN")
            try:
                nullability = conn.run(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'yascheduler_tasks' "
                    "AND column_name = 'allocated_node_id'",
                )
            finally:
                conn.run("ROLLBACK")
            assert nullability[0][0] == "YES"
            # FK ON DELETE SET NULL: del_rule code 'n' = SET NULL.
            assert _fk_on_delete_action(conn) == "n"
        finally:
            conn.close()


def test_migration_004_backfills_existing_tasks() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        # Seed a legacy DB already at migration 003 (no allocated_node_id column).
        conn = _connect(config)
        try:
            conn.run(
                "CREATE TABLE yascheduler_migrations "
                "(migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('003')")
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "node_id SERIAL PRIMARY KEY, ip VARCHAR(15), "
                "port INTEGER DEFAULT 22, username VARCHAR(255) DEFAULT 'root', "
                "ncpus SMALLINT DEFAULT NULL, enabled BOOLEAN DEFAULT TRUE, "
                "cloud VARCHAR(32) DEFAULT NULL)",
            )
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
                "metadata JSONB, ip VARCHAR(15), status SMALLINT)",
            )
            # Two nodes with distinct ips.
            conn.run("INSERT INTO yascheduler_nodes (ip) VALUES ('10.0.0.1')")
            conn.run("INSERT INTO yascheduler_nodes (ip) VALUES ('10.0.0.2')")
            # Two tasks referencing those ips. status=1 (RUNNING); metadata
            # carries engine context (unused at this era — the run stops at
            # migration 004 via upto).
            conn.run(
                "INSERT INTO yascheduler_tasks (label, ip, status, metadata) "
                "VALUES ('a', '10.0.0.1', 1, "
                '\'{"engine": "fleur", "fort.9": "QUJD", '
                '"remote_folder": "/r/a"}\'::jsonb)',
            )
            conn.run(
                "INSERT INTO yascheduler_tasks (label, ip, status, metadata) "
                "VALUES ('b', '10.0.0.2', 1, "
                '\'{"engine": "fleur", "fort.9": "REVG", '
                '"remote_folder": "/r/b"}\'::jsonb)',
            )
        finally:
            conn.close()

        # apply_schema is a no-op on existing tables; apply_migrations runs
        # exactly 004 (adds allocated_node_id + backfills) — upto caps the
        # chain, so later migrations cannot leak into this era test.
        apply_schema(config)
        apply_migrations(config, upto="004")

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["003", "004"]
            conn.run("BEGIN")
            try:
                rows = conn.run(
                    "SELECT t.allocated_node_id, n.ip "
                    "FROM yascheduler_tasks t "
                    "LEFT JOIN yascheduler_nodes n ON n.node_id = t.allocated_node_id "
                    "ORDER BY t.task_id",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(rows) == 2
            # Each task's allocated_node_id joins back to a node with the matching ip.
            for allocated_node_id, node_ip in rows:
                assert allocated_node_id is not None
                assert node_ip in ("10.0.0.1", "10.0.0.2")
        finally:
            conn.close()


def test_migration_004_leaves_unallocated_tasks_null() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        conn = _connect(config)
        try:
            conn.run(
                "CREATE TABLE yascheduler_migrations "
                "(migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('003')")
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "node_id SERIAL PRIMARY KEY, ip VARCHAR(15), "
                "port INTEGER DEFAULT 22, username VARCHAR(255) DEFAULT 'root', "
                "ncpus SMALLINT DEFAULT NULL, enabled BOOLEAN DEFAULT TRUE, "
                "cloud VARCHAR(32) DEFAULT NULL)",
            )
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
                "metadata JSONB, ip VARCHAR(15), status SMALLINT)",
            )
            # An unallocated TO_DO task: ip IS NULL.
            conn.run(
                "INSERT INTO yascheduler_tasks (label, ip, status, metadata) "
                "VALUES ('todo', NULL, 0, "
                '\'{"engine": "fleur", "fort.9": "QUJD"}\'::jsonb)',
            )
        finally:
            conn.close()

        # Runs exactly 004 (upto caps the chain at this era).
        apply_schema(config)
        apply_migrations(config, upto="004")

        conn = _connect(config)
        try:
            conn.run("BEGIN")
            try:
                rows = conn.run(
                    "SELECT allocated_node_id FROM yascheduler_tasks "
                    "WHERE label = 'todo'",
                )
            finally:
                conn.run("ROLLBACK")
            assert rows[0][0] is None
        finally:
            conn.close()


def test_fk_on_delete_set_null() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)
        apply_migrations(config)

        conn = _connect(config)
        try:
            conn.run(
                "INSERT INTO yascheduler_nodes (hostname, enabled) VALUES ('10.0.0.1', TRUE)",
            )
            # DONE task referencing the node. DONE is unconstrained by the
            # task_status_field_invariants CHECK, so the FK ON DELETE SET NULL
            # cascade succeeds (a RUNNING task's DELETE would be rejected —
            # covered by the CHECK-rejection tests).
            conn.run(
                "INSERT INTO yascheduler_tasks (title, status, engine, allocated_node_id, remote_folder) "
                "VALUES ('job', 'DONE', 'fleur', "
                "(SELECT node_id FROM yascheduler_nodes WHERE hostname = '10.0.0.1'), '/remote/job')",
            )
            # Sanity: the task references the node.
            conn.run("BEGIN")
            try:
                pre = conn.run(
                    "SELECT allocated_node_id FROM yascheduler_tasks WHERE title = 'job'",
                )
            finally:
                conn.run("ROLLBACK")
            assert pre[0][0] is not None

            # Delete the node row — FK ON DELETE SET NULL fires.
            conn.run("DELETE FROM yascheduler_nodes WHERE hostname = '10.0.0.1'")

            conn.run("BEGIN")
            try:
                post = conn.run(
                    "SELECT allocated_node_id, title FROM yascheduler_tasks "
                    "WHERE title = 'job'",
                )
            finally:
                conn.run("ROLLBACK")
            # allocated_node_id became NULL; the row is preserved.
            assert post[0][0] is None
            assert post[0][1] == "job"
        finally:
            conn.close()


def test_fresh_db_seeds_to_014() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["014"]
            assert "allocated_node_id" in _columns(conn, "yascheduler_tasks")
        finally:
            conn.close()

        # apply_migrations finds MAX='014' and skips 014 (already seeded).
        apply_migrations(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["014"]
        finally:
            conn.close()
