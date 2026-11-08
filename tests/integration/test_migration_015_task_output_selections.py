# region MODULE_CONTRACT
# PURPOSE: Verify migration 015 adds the nullable task output-selection record without changing a pre-change row.
# SCOPE: Forward upgrade from tracker 014, TEXT[] nullability, and legacy NULL preservation.
# KEYWORDS: migration 015, task output selections, nullable TEXT[], postgres
# endregion MODULE_CONTRACT

from __future__ import annotations

from urllib.parse import urlparse

import pg8000.native
import pytest
from testcontainers.postgres import PostgresContainer

from yascheduler.infra.persistence import PostgresDbConfig, apply_migrations

pytestmark = pytest.mark.integration

__all__: list[str] = []


def _make_config(pg: PostgresContainer) -> PostgresDbConfig:
    url = urlparse(pg.get_connection_url())
    return PostgresDbConfig(
        user=url.username or "test",
        password=url.password or "test",
        database=url.path.lstrip("/"),
        host=url.hostname or "localhost",
        port=url.port or 5432,
    )


def test_migration_015_adds_nullable_output_selections_and_preserves_legacy_row() -> (
    None
):
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        conn = pg8000.native.Connection(
            user=config.user,
            host=config.host,
            database=config.database,
            port=config.port,
            password=config.password,
        )
        try:
            conn.run(
                "CREATE TABLE yascheduler_migrations ("
                "migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run(
                "INSERT INTO yascheduler_migrations (migration_id) VALUES ('014')",
            )
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id INTEGER PRIMARY KEY, title TEXT, engine TEXT NOT NULL)",
            )
            conn.run(
                "INSERT INTO yascheduler_tasks (task_id, title, engine) "
                "VALUES (1, 'legacy', 'fleur')",
            )
        finally:
            conn.close()

        apply_migrations(config)

        conn = pg8000.native.Connection(
            user=config.user,
            host=config.host,
            database=config.database,
            port=config.port,
            password=config.password,
        )
        try:
            assert conn.run(
                "SELECT migration_id FROM yascheduler_migrations ORDER BY migration_id",
            ) == [["014"], ["015"]]
            assert conn.run(
                "SELECT data_type, udt_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'yascheduler_tasks' "
                "AND column_name = 'output_selections'",
            ) == [["ARRAY", "_text", "YES"]]
            assert conn.run(
                "SELECT task_id, title, engine, output_selections "
                "FROM yascheduler_tasks WHERE task_id = 1",
            ) == [[1, "legacy", "fleur", None]]
        finally:
            conn.close()
