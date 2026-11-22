# region MODULE_CONTRACT
# PURPOSE: Integration test for migration 012 (node ip→hostname rename + new fields).
# SCOPE: testcontainers-based verification of migration 012 steps.
# KEYWORDS: migration 012, node rename, ip to hostname
# endregion MODULE_CONTRACT

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pg8000.native
import pytest
from pg8000 import DatabaseError
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
            "WHERE table_name = :t ORDER BY column_name",
            t=table,
        )
    finally:
        conn.run("ROLLBACK")
    return [r[0] for r in rows]


def test_migration_012_node_rename_and_fields() -> None:
    """Migration 012: ip→hostname, audit timestamps, jump fields, external_id, NODE_STATUS, port constraints."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        conn = _connect(config)
        try:
            # Seed tracker to '011' so apply_migrations runs 012
            conn.run(
                "CREATE TABLE yascheduler_migrations "
                "(migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('011')")

            # Pre-012 yascheduler_nodes: ip column, no new fields
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "node_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY, "
                "ip VARCHAR(15), "
                "port INTEGER DEFAULT 22, "
                "username VARCHAR(255) DEFAULT 'root', "
                "ncpus SMALLINT DEFAULT NULL, "
                "enabled BOOLEAN DEFAULT TRUE, "
                "cloud VARCHAR(32) DEFAULT NULL)",
            )

            # Insert test data for backfill scenario
            # Cloud node (node_id=1) — should get external_id backfilled
            conn.run(
                "INSERT INTO yascheduler_nodes (ip, cloud) VALUES ('10.0.0.1', 'aws')",
            )
            # Static node (node_id=2) — should NOT get external_id
            conn.run(
                "INSERT INTO yascheduler_nodes (ip, cloud) VALUES ('10.0.0.2', NULL)",
            )

            # Create yascheduler_tasks at a compatible era so apply_schema's trigger
            # lookup does not error.
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
                "metadata JSONB, ip VARCHAR(15), status SMALLINT)",
            )
        finally:
            conn.close()

        # Apply schema (CREATE TABLE IF NOT EXISTS is a no-op on existing tables)
        apply_schema(config)
        # Apply migrations — runs exactly 012: tracker is at '011' and upto
        # caps the chain, so later migrations cannot leak into this era test.
        apply_migrations(config, upto="012")

        conn = _connect(config)
        try:
            cols = _columns(conn, "yascheduler_nodes")

            # -- Scenario 1: ip renamed to hostname, widened to VARCHAR(255)
            assert "ip" not in cols, "ip column must be renamed to hostname"
            assert "hostname" in cols, "hostname column must exist after rename"
            conn.run("BEGIN")
            try:
                type_rows = conn.run(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name = 'yascheduler_nodes' "
                    "AND column_name = 'hostname'",
                )
            finally:
                conn.run("ROLLBACK")
            assert type_rows[0][0] == 255, (
                f"hostname should be VARCHAR(255), got {type_rows[0][0]}"
            )
            assert "012" in _tracker_rows(conn), (
                "migration 012 must be recorded in tracker"
            )

            # -- Scenario 2: created_at and updated_at with trigger
            assert "created_at" in cols, "created_at column must exist"
            assert "updated_at" in cols, "updated_at column must exist"

            conn.run("BEGIN")
            try:
                trig_rows = conn.run(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'yascheduler_nodes'::regclass "
                    "AND tgname = 'yascheduler_nodes_touch_updated_at'",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(trig_rows) == 1, (
                "yascheduler_nodes_touch_updated_at trigger must exist"
            )

            # Verify trigger: UPDATE advances updated_at, preserves created_at
            conn.run("BEGIN")
            try:
                row = conn.run(
                    "SELECT node_id, created_at, updated_at "
                    "FROM yascheduler_nodes WHERE node_id = 1",
                )
                assert len(row) == 1
                node_id = row[0][0]
                created_before = row[0][1]
                updated_before = row[0][2]

                import time as _time

                _time.sleep(0.05)

                conn.run(
                    "UPDATE yascheduler_nodes SET username = 'updated' "
                    "WHERE node_id = :nid",
                    nid=node_id,
                )
                row2 = conn.run(
                    "SELECT created_at, updated_at "
                    "FROM yascheduler_nodes WHERE node_id = :nid",
                    nid=node_id,
                )
                assert row2[0][0] == created_before, (
                    "created_at must NOT change on UPDATE"
                )
                assert row2[0][1] > updated_before, "updated_at must advance on UPDATE"
            finally:
                conn.run("ROLLBACK")

            # -- Scenario 3: external_id backfilled for cloud nodes only
            conn.run("BEGIN")
            try:
                rows = conn.run(
                    "SELECT hostname, cloud, external_id "
                    "FROM yascheduler_nodes ORDER BY node_id",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(rows) == 2
            # Cloud node: external_id = hostname
            assert rows[0][0] == "10.0.0.1", rows[0]
            assert rows[0][2] == "10.0.0.1", (
                f"Cloud node should have external_id backfilled, got {rows[0][2]}"
            )
            # Static node: external_id IS NULL
            assert rows[1][0] == "10.0.0.2", rows[1]
            assert rows[1][2] is None, (
                f"Static node should have external_id=NULL, got {rows[1][2]}"
            )

            # -- Scenario 4: NODE_STATUS enum and status column
            conn.run("BEGIN")
            try:
                enum_rows = conn.run(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'node_status' ORDER BY e.enumsortorder",
                )
            finally:
                conn.run("ROLLBACK")
            assert [r[0] for r in enum_rows] == ["OTHER"], (
                f"NODE_STATUS enum should have label 'OTHER', got {[r[0] for r in enum_rows]}"
            )

            assert "status" in cols, "status column must exist"

            conn.run("BEGIN")
            try:
                type_rows = conn.run(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'yascheduler_nodes' AND column_name = 'status'",
                )
            finally:
                conn.run("ROLLBACK")
            assert type_rows[0][0] == "USER-DEFINED", (
                "status column type should be USER-DEFINED (enum)"
            )

            # Verify default is 'OTHER'
            conn.run("BEGIN")
            try:
                status_rows = conn.run(
                    "SELECT status::text FROM yascheduler_nodes ORDER BY node_id",
                )
            finally:
                conn.run("ROLLBACK")
            assert status_rows[0][0] == "OTHER"
            assert status_rows[1][0] == "OTHER"

            # -- Scenario 5: port NOT NULL + CHECK constraint
            # Verify port NOT NULL rejects explicit NULL
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (hostname, port) "
                    "VALUES ('null_port', NULL)",
                )
                conn.run("ROLLBACK")
                assert False, "port=NULL should be rejected by NOT NULL"
            except DatabaseError:
                conn.run("ROLLBACK")

            # Verify port CHECK rejects 0
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (hostname, port) "
                    "VALUES ('bad_port', 0)",
                )
                conn.run("ROLLBACK")
                assert False, "port=0 should be rejected by CHECK"
            except DatabaseError:
                conn.run("ROLLBACK")

            # Verify port CHECK rejects 65536
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (hostname, port) "
                    "VALUES ('bad_port', 65536)",
                )
                conn.run("ROLLBACK")
                assert False, "port=65536 should be rejected by CHECK"
            except DatabaseError:
                conn.run("ROLLBACK")

            # Verify port CHECK accepts valid port
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (hostname, port) "
                    "VALUES ('good_port', 22)",
                )
            finally:
                conn.run("ROLLBACK")

            # Constraint name exists
            conn.run("BEGIN")
            try:
                const_rows = conn.run(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name = 'yascheduler_nodes' "
                    "AND constraint_type = 'CHECK' "
                    "AND constraint_name = 'node_port_range'",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(const_rows) == 1, "node_port_range CHECK constraint must exist"

            # -- Scenario 6: jump host fields
            assert "jump_host" in cols
            assert "jump_port" in cols
            assert "jump_username" in cols

            # jump_port NOT NULL DEFAULT 22
            conn.run("BEGIN")
            try:
                port_rows = conn.run(
                    "SELECT jump_port FROM yascheduler_nodes ORDER BY node_id",
                )
            finally:
                conn.run("ROLLBACK")
            assert port_rows[0][0] == 22
            assert port_rows[1][0] == 22

            # jump_username NOT NULL DEFAULT 'root'
            conn.run("BEGIN")
            try:
                user_rows = conn.run(
                    "SELECT jump_username FROM yascheduler_nodes ORDER BY node_id",
                )
            finally:
                conn.run("ROLLBACK")
            assert user_rows[0][0] == "root"
            assert user_rows[1][0] == "root"

            # jump_host nullable
            conn.run("BEGIN")
            try:
                host_rows = conn.run(
                    "SELECT jump_host FROM yascheduler_nodes ORDER BY node_id",
                )
            finally:
                conn.run("ROLLBACK")
            assert host_rows[0][0] is None
            assert host_rows[1][0] is None

            # jump_port CHECK constraint rejects 0
            conn.run("BEGIN")
            try:
                conn.run("UPDATE yascheduler_nodes SET jump_port = 0 WHERE node_id = 1")
                conn.run("ROLLBACK")
                assert False, "jump_port=0 should be rejected by CHECK"
            except DatabaseError:
                conn.run("ROLLBACK")

            # jump_port CHECK constraint rejects 65536
            conn.run("BEGIN")
            try:
                conn.run(
                    "UPDATE yascheduler_nodes SET jump_port = 65536 WHERE node_id = 1",
                )
                conn.run("ROLLBACK")
                assert False, "jump_port=65536 should be rejected by CHECK"
            except DatabaseError:
                conn.run("ROLLBACK")

            # -- Scenario 7: schema.sql snapshot updated
            schema_path = (
                Path(__file__).resolve().parents[2]
                / "yascheduler"
                / "infra"
                / "persistence"
                / "sql"
                / "schema.sql"
            )
            schema_sql = schema_path.read_text()
            assert "hostname VARCHAR(255)" in schema_sql
            assert "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in schema_sql
            assert "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in schema_sql
            assert "jump_host VARCHAR(255)" in schema_sql
            assert "jump_port INTEGER NOT NULL DEFAULT 22" in schema_sql
            assert "jump_username VARCHAR(255) NOT NULL DEFAULT 'root'" in schema_sql
            assert "external_id VARCHAR(255)" in schema_sql
            assert "status NODE_STATUS NOT NULL DEFAULT 'OTHER'" in schema_sql
            assert "CONSTRAINT node_port_range CHECK" in schema_sql
            assert "CONSTRAINT node_jump_port_range CHECK" in schema_sql
        finally:
            conn.close()
