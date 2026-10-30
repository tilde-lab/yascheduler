# region MODULE_CONTRACT
# PURPOSE: Integration test for migration 013 (ncpus nullable with positive CHECK).
# SCOPE: testcontainers-based verification of migration 013 steps.
# KEYWORDS: migration 013, ncpus nullable, CHECK constraint
# endregion MODULE_CONTRACT

from __future__ import annotations

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


def test_migration_013_ncpus_nullable() -> None:
    """Migration 013: backfill ncpus=0 → NULL, add node_ncpus_positive CHECK."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        conn = _connect(config)
        try:
            # Seed tracker to '012' so apply_migrations runs 013
            conn.run(
                "CREATE TABLE yascheduler_migrations "
                "(migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('012')")

            # Pre-013 yascheduler_nodes: ncpus column without CHECK
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "node_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "enabled BOOLEAN DEFAULT TRUE, "
                "status VARCHAR(32) DEFAULT 'OTHER', "
                "hostname VARCHAR(255), "
                "port INTEGER NOT NULL DEFAULT 22, "
                "username VARCHAR(255) DEFAULT 'root', "
                "ncpus SMALLINT DEFAULT NULL, "
                "cloud VARCHAR(32) DEFAULT NULL, "
                "external_id VARCHAR(255), "
                "jump_host VARCHAR(255), "
                "jump_port INTEGER NOT NULL DEFAULT 22, "
                "jump_username VARCHAR(255) NOT NULL DEFAULT 'root')",
            )

            # Insert test data for the backfill scenario:
            # ncpus=0 (legacy sentinel — must be backfilled to NULL)
            # ncpus=8 (valid positive — must be left untouched)
            # ncpus=NULL (already "no limit" — must be left untouched)
            conn.run(
                "INSERT INTO yascheduler_nodes (hostname, ncpus) VALUES ('zero_node', 0)",
            )
            conn.run(
                "INSERT INTO yascheduler_nodes (hostname, ncpus) VALUES ('eight_node', 8)",
            )
            conn.run(
                "INSERT INTO yascheduler_nodes (hostname, ncpus) "
                "VALUES ('null_node', NULL)",
            )

            # Create yascheduler_tasks so apply_schema's trigger lookup works
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                "title VARCHAR(256), "
                "engine VARCHAR(64) NOT NULL, "
                "status VARCHAR(32) DEFAULT 'TO_DO', "
                "allocated_node_id INTEGER, "
                "local_folder VARCHAR(1024), "
                "remote_folder VARCHAR(1024), "
                "webhook_url VARCHAR(2048), "
                "webhook_custom_params JSONB NOT NULL DEFAULT '{}'::JSONB, "
                "error TEXT, "
                "extra JSONB NOT NULL DEFAULT '{}'::JSONB)",
            )
        finally:
            conn.close()

        # Apply schema (CREATE TABLE IF NOT EXISTS is a no-op on existing tables)
        apply_schema(config)
        # Apply migrations — runs exactly 013: tracker is at '012' and upto
        # caps the chain, so later migrations cannot leak into this era test.
        apply_migrations(config, upto="013")

        conn = _connect(config)
        try:
            # --- Scenario: Migration 013 installs the node_ncpus_positive CHECK ---
            conn.run("BEGIN")
            try:
                const_rows = conn.run(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name = 'yascheduler_nodes' "
                    "AND constraint_type = 'CHECK' "
                    "AND constraint_name = 'node_ncpus_positive'",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(const_rows) == 1, (
                "node_ncpus_positive CHECK constraint must exist"
            )

            # --- Scenario: Migration 013 is recorded in tracker ---
            assert "013" in _tracker_rows(conn), (
                "migration 013 must be recorded in tracker"
            )

            # --- Scenario: Migration 013 backfills zero rows to NULL ---
            conn.run("BEGIN")
            try:
                rows = conn.run(
                    "SELECT hostname, ncpus FROM yascheduler_nodes ORDER BY node_id",
                )
            finally:
                conn.run("ROLLBACK")
            assert len(rows) == 3
            # zero_node: ncpus=0 → NULL
            assert rows[0][0] == "zero_node", rows[0]
            assert rows[0][1] is None, (
                f"zero_node ncpus should be NULL after backfill, got {rows[0][1]}"
            )
            # eight_node: ncpus=8 → untouched
            assert rows[1][0] == "eight_node", rows[1]
            assert rows[1][1] == 8, (
                f"eight_node ncpus should remain 8, got {rows[1][1]}"
            )
            # null_node: ncpus=NULL → untouched
            assert rows[2][0] == "null_node", rows[2]
            assert rows[2][1] is None, (
                f"null_node ncpus should remain NULL, got {rows[2][1]}"
            )

            # --- Scenario: Migration 013 CHECK rejects future zero writes ---
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (hostname, ncpus) "
                    "VALUES ('bad_zero', 0)",
                )
                conn.run("ROLLBACK")
                assert False, "INSERT with ncpus=0 should be rejected by CHECK"
            except DatabaseError:
                conn.run("ROLLBACK")

            # UPDATE to ncpus=0 should also be rejected
            conn.run("BEGIN")
            try:
                conn.run(
                    "UPDATE yascheduler_nodes SET ncpus = 0 WHERE hostname = 'eight_node'",
                )
                conn.run("ROLLBACK")
                assert False, "UPDATE with ncpus=0 should be rejected by CHECK"
            except DatabaseError:
                conn.run("ROLLBACK")

            # --- Scenario: Migration 013 CHECK rejects negative writes ---
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (hostname, ncpus) "
                    "VALUES ('bad_neg', -1)",
                )
                conn.run("ROLLBACK")
                assert False, "INSERT with ncpus=-1 should be rejected by CHECK"
            except DatabaseError:
                conn.run("ROLLBACK")

            # UPDATE to ncpus=-1 should also be rejected
            conn.run("BEGIN")
            try:
                conn.run(
                    "UPDATE yascheduler_nodes SET ncpus = -1 "
                    "WHERE hostname = 'eight_node'",
                )
                conn.run("ROLLBACK")
                assert False, "UPDATE with ncpus=-1 should be rejected by CHECK"
            except DatabaseError:
                conn.run("ROLLBACK")

            # Verify valid positive writes are still accepted
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (hostname, ncpus) "
                    "VALUES ('good_node', 4)",
                )
            finally:
                conn.run("ROLLBACK")

            # Verify NULL writes are still accepted
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (hostname, ncpus) "
                    "VALUES ('null_node2', NULL)",
                )
            finally:
                conn.run("ROLLBACK")

        finally:
            conn.close()
