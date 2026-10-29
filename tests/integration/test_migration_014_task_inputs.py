"""Integration tests for migration 014 task-input snapshot conversion."""
# region MODULE_CONTRACT
# PURPOSE: Verify migration 014 against real PostgreSQL through testcontainers: fresh bootstrap seeds the latest prefix without engine context, 013 databases convert every task status into equivalent binary snapshot entries, and legacy databases upgrade from a pre-tracker schema without losing task identity, timestamps, state, bindings, or unrelated metadata.
# SCOPE: Fresh/current/legacy migration cohorts, exact input-table schema and constraints, UTF-8 and fort.9 lenient-base64 conversion, literal-directory and glob subtree expansion, metadata-key removal, bootstrap-precreated and runner-only table creation.
# DEPENDENCIES: USES API: apply_schema, apply_migrations, check_migration_status, pg8000.native, testcontainers.PostgresContainer
# READS: PostgreSQL container (postgres:16-alpine)
# KEYWORDS: migration 014, task inputs, snapshots, postgres, base64, legacy upgrade
# endregion MODULE_CONTRACT

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import pg8000.native
import pytest
from testcontainers.postgres import PostgresContainer

from yascheduler.infra.persistence import (
    MigrationState,
    PostgresDbConfig,
    apply_migrations,
    check_migration_status,
)
from yascheduler.infra.persistence.postgres_schema import apply_schema

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.integration

__all__: list[str] = []

ENGINE = "migration-014-engine"
DECLARATIONS = (
    "INPUT",
    "fort.9",
    "looks-like-base64.txt",
    "basis",
    "basis/nested/*.orb",
    "globbed/**/*.dat",
    "tree/*",
)
UTF8_TEXT = "line one\nline two — ü"
FORT9_TEXT = "AAEC\n/w"
BASE64_LOOKALIKE_TEXT = "SGVsbG8="
INPUT_PAYLOAD: dict[str, str] = {
    "INPUT": UTF8_TEXT,
    "fort.9": FORT9_TEXT,
    "looks-like-base64.txt": BASE64_LOOKALIKE_TEXT,
    "basis/one.orb": "orb-one",
    "basis/.hidden": "hidden-basis-data",
    "basis/empty.orb": "",
    "basis/nested/two.orb": "orb-two",
    "globbed/root.dat": "root-data",
    "globbed/nested/leaf.dat": "leaf-data",
    "tree/assets/configured.txt": "tree-config",
    "tree/assets/nested/deep.bin": "deep-tree-data",
}
UNRELATED_METADATA: dict[str, object] = {
    "priority": 7,
    "tags": ["baseline", "opt"],
    "nested": {"keep": True},
    "other.orb": "not selected",
    "globbed/notes.txt": "not selected by the dat glob",
}
EXPECTED_INPUT_ENTRIES: dict[str, tuple[str, bytes | None]] = {
    "INPUT": ("file", UTF8_TEXT.encode("utf-8")),
    "fort.9": ("file", b"\x00\x01\x02\xff"),
    "looks-like-base64.txt": ("file", BASE64_LOOKALIKE_TEXT.encode("utf-8")),
    "basis": ("directory", None),
    "basis/one.orb": ("file", b"orb-one"),
    "basis/.hidden": ("file", b"hidden-basis-data"),
    "basis/empty.orb": ("file", b""),
    "basis/nested": ("directory", None),
    "basis/nested/two.orb": ("file", b"orb-two"),
    "globbed": ("directory", None),
    "globbed/nested": ("directory", None),
    "globbed/root.dat": ("file", b"root-data"),
    "globbed/nested/leaf.dat": ("file", b"leaf-data"),
    "tree": ("directory", None),
    "tree/assets": ("directory", None),
    "tree/assets/configured.txt": ("file", b"tree-config"),
    "tree/assets/nested": ("directory", None),
    "tree/assets/nested/deep.bin": ("file", b"deep-tree-data"),
}
EXPECTED_INPUT_CONTRACT = {
    "columns": (
        ("task_id", "integer", "int4", "NO", None),
        ("path", "text", "text", "NO", None),
        ("kind", "text", "text", "NO", None),
        ("content", "bytea", "bytea", "YES", None),
    ),
    "constraints": (
        (
            "task_input_kind_content_invariants",
            "c",
            (
                "CHECK ((((kind = 'file'::text) AND (content IS NOT NULL)) OR "
                "((kind = 'directory'::text) AND (content IS NULL))))"
            ),
        ),
        (
            "task_input_kind_values",
            "c",
            "CHECK ((kind = ANY (ARRAY['file'::text, 'directory'::text])))",
        ),
        ("task_input_pk", "p", "PRIMARY KEY (task_id, path)"),
        (
            "yascheduler_task_inputs_task_id_fkey",
            "f",
            (
                "FOREIGN KEY (task_id) REFERENCES yascheduler_tasks(task_id) "
                "ON DELETE CASCADE"
            ),
        ),
    ),
    "indexes": (
        (
            "task_input_pk",
            (
                "CREATE UNIQUE INDEX task_input_pk ON "
                "public.yascheduler_task_inputs USING btree (task_id, path)"
            ),
        ),
    ),
}
PRESERVED_TASK_COLUMNS = (
    "task_id",
    "created_at",
    "updated_at",
    "title",
    "engine",
    "status",
    "allocated_node_id",
    "local_folder",
    "remote_folder",
    "webhook_url",
    "webhook_custom_params",
    "error",
)


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


def _query(
    conn: pg8000.native.Connection,
    sql: str,
    **params: object,
) -> list[dict[str, object]]:
    raw_rows: list[tuple[object, ...]] = conn.run(sql, **params) or []
    columns = [column["name"] for column in (conn.columns or [])]
    return [dict(zip(columns, row)) for row in raw_rows]


def _tracker_rows(conn: pg8000.native.Connection) -> list[str]:
    return [
        str(row["migration_id"])
        for row in _query(
            conn,
            "SELECT migration_id FROM yascheduler_migrations ORDER BY migration_id",
        )
    ]


def _table_exists(conn: pg8000.native.Connection) -> bool:
    rows = conn.run("SELECT to_regclass('public.yascheduler_task_inputs')")
    return bool(rows and rows[0][0] is not None)


def _input_contract(
    conn: pg8000.native.Connection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    columns = _query(
        conn,
        "SELECT column_name, data_type, udt_name, is_nullable, "
        "character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'yascheduler_task_inputs' "
        "ORDER BY ordinal_position",
    )
    constraints = _query(
        conn,
        "SELECT conname, contype, pg_get_constraintdef(oid) AS definition "
        "FROM pg_constraint "
        "WHERE conrelid = 'yascheduler_task_inputs'::regclass "
        "ORDER BY conname",
    )
    indexes = _query(
        conn,
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'yascheduler_task_inputs' "
        "ORDER BY indexname",
    )
    return {
        "columns": tuple(tuple(row.values()) for row in columns),
        "constraints": tuple(tuple(row.values()) for row in constraints),
        "indexes": tuple(tuple(row.values()) for row in indexes),
    }


def _assert_input_contract(conn: pg8000.native.Connection) -> None:
    assert _input_contract(conn) == EXPECTED_INPUT_CONTRACT


def _task_rows(conn: pg8000.native.Connection) -> list[dict[str, object]]:
    return _query(
        conn,
        "SELECT task_id, created_at, updated_at, title, engine, "
        "status::text AS status, allocated_node_id, local_folder, "
        "remote_folder, webhook_url, webhook_custom_params::text AS "
        "webhook_custom_params, error, extra::text AS extra "
        "FROM yascheduler_tasks ORDER BY task_id",
    )


def _input_rows(
    conn: pg8000.native.Connection,
    task_id: int,
) -> dict[str, dict[str, object]]:
    rows = _query(
        conn,
        "SELECT task_id, path, kind, content FROM yascheduler_task_inputs "
        "WHERE task_id = :task_id ORDER BY path",
        task_id=task_id,
    )
    return {str(row["path"]): row for row in rows}


def _assert_converted_snapshot(
    conn: pg8000.native.Connection,
    task_id: int,
) -> None:
    rows = _input_rows(conn, task_id)
    assert set(rows) == set(EXPECTED_INPUT_ENTRIES)
    for path, (kind, content) in EXPECTED_INPUT_ENTRIES.items():
        assert rows[path]["task_id"] == task_id
        assert rows[path]["kind"] == kind, path
        if kind == "file":
            assert rows[path]["content"] == content, path
        else:
            assert rows[path]["content"] is None, path


def _payload(task_marker: str) -> dict[str, object]:
    return {
        **INPUT_PAYLOAD,
        **UNRELATED_METADATA,
        "marker": task_marker,
    }


def _expected_remaining_metadata(task_marker: str) -> dict[str, object]:
    return {
        **UNRELATED_METADATA,
        "marker": task_marker,
    }


def _insert_node(
    conn: pg8000.native.Connection,
    hostname: str,
    ncpus: int,
) -> int:
    row = conn.run(
        "INSERT INTO yascheduler_nodes (hostname, ncpus, enabled) "
        "VALUES (:hostname, :ncpus, TRUE) RETURNING node_id",
        hostname=hostname,
        ncpus=ncpus,
    )[0]
    return int(row[0])


def _insert_legacy_node(
    conn: pg8000.native.Connection,
    ip: str,
    ncpus: int,
) -> None:
    conn.run(
        "INSERT INTO yascheduler_nodes (ip, ncpus) VALUES (:ip, :ncpus)",
        ip=ip,
        ncpus=ncpus,
    )


def _insert_modern_task(
    conn: pg8000.native.Connection,
    *,
    title: str,
    status: str,
    extra: Mapping[str, object],
    created_at: str,
    updated_at: str,
    allocated_node_id: int | None = None,
    local_folder: str | None = None,
    remote_folder: str | None = None,
    webhook_url: str | None = None,
    webhook_custom_params: Mapping[str, object] | None = None,
    error: str | None = None,
) -> int:
    row = conn.run(
        "INSERT INTO yascheduler_tasks ("
        "created_at, updated_at, title, engine, status, allocated_node_id, "
        "local_folder, remote_folder, webhook_url, webhook_custom_params, "
        "error, extra"
        ") VALUES ("
        "CAST(:created_at AS TIMESTAMPTZ), CAST(:updated_at AS TIMESTAMPTZ), "
        ":title, :engine, CAST(:status AS task_status), :allocated_node_id, "
        ":local_folder, :remote_folder, :webhook_url, "
        "CAST(:webhook_custom_params AS JSONB), :error, CAST(:extra AS JSONB)"
        ") RETURNING task_id",
        created_at=created_at,
        updated_at=updated_at,
        title=title,
        engine=ENGINE,
        status=status,
        allocated_node_id=allocated_node_id,
        local_folder=local_folder,
        remote_folder=remote_folder,
        webhook_url=webhook_url,
        webhook_custom_params=json.dumps(webhook_custom_params or {}),
        error=error,
        extra=json.dumps(extra),
    )[0]
    return int(row[0])


def _seed_modern_tasks(
    conn: pg8000.native.Connection,
) -> tuple[int, int, tuple[int, int, int]]:
    node_one = _insert_node(conn, "10.14.0.1", 4)
    node_two = _insert_node(conn, "10.14.0.2", 8)
    task_ids = (
        _insert_modern_task(
            conn,
            title="modern-todo",
            status="TO_DO",
            extra=_payload("modern-todo"),
            created_at="2024-01-02T03:04:05+00:00",
            updated_at="2024-01-02T03:05:06+00:00",
            local_folder="/modern/local/todo",
            webhook_url="https://example.invalid/todo",
            webhook_custom_params={"marker": "modern-todo"},
        ),
        _insert_modern_task(
            conn,
            title="modern-running",
            status="RUNNING",
            extra=_payload("modern-running"),
            created_at="2024-02-03T04:05:06+00:00",
            updated_at="2024-02-03T04:06:07+00:00",
            allocated_node_id=node_one,
            local_folder="/modern/local/running",
            remote_folder="/modern/remote/running",
            webhook_url="https://example.invalid/running",
            webhook_custom_params={"marker": "modern-running"},
        ),
        _insert_modern_task(
            conn,
            title="modern-done",
            status="DONE",
            extra=_payload("modern-done"),
            created_at="2024-03-04T05:06:07+00:00",
            updated_at="2024-03-04T05:07:08+00:00",
            allocated_node_id=node_two,
            local_folder="/modern/local/done",
            remote_folder="/modern/remote/done",
            webhook_url="https://example.invalid/done",
            webhook_custom_params={"marker": "modern-done"},
            error="historical completion warning",
        ),
    )
    return node_one, node_two, task_ids


def _set_tracker_to_013(conn: pg8000.native.Connection) -> None:
    conn.run("DELETE FROM yascheduler_migrations")
    conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('013')")


def _assert_modern_rows_preserved(
    conn: pg8000.native.Connection,
    before: dict[int, dict[str, object]],
    task_ids: tuple[int, int, int],
) -> None:
    after = {cast(int, row["task_id"]): row for row in _task_rows(conn)}
    assert tuple(sorted(after)) == task_ids
    markers = {
        task_ids[0]: "modern-todo",
        task_ids[1]: "modern-running",
        task_ids[2]: "modern-done",
    }
    for task_id in task_ids:
        for column in PRESERVED_TASK_COLUMNS:
            assert after[task_id][column] == before[task_id][column], (
                task_id,
                column,
            )
        assert json.loads(str(after[task_id]["extra"])) == (
            _expected_remaining_metadata(markers[task_id])
        )
        _assert_converted_snapshot(conn, task_id)


def _create_legacy_schema(conn: pg8000.native.Connection) -> None:
    conn.run(
        "CREATE TABLE yascheduler_nodes ("
        "ip VARCHAR(15) UNIQUE, cloud VARCHAR(32) DEFAULT NULL, "
        "ncpus SMALLINT DEFAULT NULL)",
    )
    conn.run(
        "CREATE TABLE yascheduler_tasks ("
        "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
        "metadata JSONB, ip VARCHAR(15), status SMALLINT)",
    )


def _insert_legacy_task(
    conn: pg8000.native.Connection,
    *,
    title: str,
    status: int,
    ip: str,
    metadata: Mapping[str, object],
) -> int:
    row = conn.run(
        "INSERT INTO yascheduler_tasks (label, metadata, ip, status) "
        "VALUES (:title, CAST(:metadata AS JSONB), :ip, :status) "
        "RETURNING task_id",
        title=title,
        metadata=json.dumps(metadata),
        ip=ip,
        status=status,
    )[0]
    return int(row[0])


def _legacy_metadata(
    task_marker: str,
    **typed_fields: object,
) -> dict[str, object]:
    return {
        "engine": ENGINE,
        **typed_fields,
        **_payload(task_marker),
    }


def test_fresh_bootstrap_seeds_014_without_engine_context() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["014"]
            assert _table_exists(conn)
            _assert_input_contract(conn)
        finally:
            conn.close()

        status = check_migration_status(config)
        assert status.state is MigrationState.CURRENT
        assert status.applied == "014"
        assert status.required == "014"

        # There is no pending 014 conversion and therefore no engine context
        # is needed on a fresh bootstrap.
        apply_migrations(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["014"]
            _assert_input_contract(conn)
        finally:
            conn.close()


def test_modern_013_migration_creates_table_and_converts_all_statuses() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            conn.run("DROP TABLE yascheduler_task_inputs")
            _set_tracker_to_013(conn)
            assert not _table_exists(conn)
            _, _, task_ids = _seed_modern_tasks(conn)
            before = {cast(int, row["task_id"]): row for row in _task_rows(conn)}
        finally:
            conn.close()

        # upto caps the chain at 014 so future migrations cannot leak in.
        apply_migrations(config, engine_inputs={ENGINE: DECLARATIONS}, upto="014")

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["013", "014"]
            assert _table_exists(conn)
            _assert_input_contract(conn)
            _assert_modern_rows_preserved(conn, before, task_ids)
        finally:
            conn.close()


def test_modern_013_tolerates_bootstrap_precreated_input_table() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            # schema.sql already created the empty table; the migration must
            # tolerate that bootstrap state rather than failing on CREATE TABLE.
            assert _table_exists(conn)
            _set_tracker_to_013(conn)
            _, _, task_ids = _seed_modern_tasks(conn)
            before = {cast(int, row["task_id"]): row for row in _task_rows(conn)}
        finally:
            conn.close()

        # upto caps the chain at 014 so future migrations cannot leak in.
        apply_migrations(config, engine_inputs={ENGINE: DECLARATIONS}, upto="014")

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["013", "014"]
            assert _table_exists(conn)
            _assert_input_contract(conn)
            _assert_modern_rows_preserved(conn, before, task_ids)
        finally:
            conn.close()


def test_legacy_db_without_tracker_upgrades_all_migrations_and_inputs() -> None:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)

        conn = _connect(config)
        try:
            _create_legacy_schema(conn)
            _insert_legacy_node(conn, "10.14.1.1", 4)
            _insert_legacy_node(conn, "10.14.1.2", 8)
            task_ids = {
                "legacy-todo": _insert_legacy_task(
                    conn,
                    title="legacy-todo",
                    status=0,
                    ip="",
                    metadata=_legacy_metadata(
                        "legacy-todo",
                        local_folder="/legacy/local/todo",
                        webhook_url="https://example.invalid/legacy-todo",
                        webhook_custom_params={"marker": "legacy-todo"},
                    ),
                ),
                "legacy-running": _insert_legacy_task(
                    conn,
                    title="legacy-running",
                    status=1,
                    ip="10.14.1.1",
                    metadata=_legacy_metadata(
                        "legacy-running",
                        local_folder="/legacy/local/running",
                        remote_folder="/legacy/remote/running",
                        webhook_url="https://example.invalid/legacy-running",
                        webhook_custom_params={"marker": "legacy-running"},
                    ),
                ),
                "legacy-done": _insert_legacy_task(
                    conn,
                    title="legacy-done",
                    status=2,
                    ip="10.14.1.2",
                    metadata=_legacy_metadata(
                        "legacy-done",
                        local_folder="/legacy/local/done",
                        remote_folder="/legacy/remote/done",
                        webhook_url="https://example.invalid/legacy-done",
                        webhook_custom_params={"marker": "legacy-done"},
                        error="historical completion warning",
                    ),
                ),
            }
        finally:
            conn.close()

        # No tracker row existed before the upgrade. Bootstrap creates the
        # tracker and the latest empty input table, leaving all migrations
        # pending for the runner.
        apply_schema(config)

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == []
            assert _table_exists(conn)
        finally:
            conn.close()

        apply_migrations(config, engine_inputs={ENGINE: DECLARATIONS})

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == [
                "001",
                "002",
                "003",
                "004",
                "005",
                "006",
                "007",
                "008",
                "009",
                "010",
                "011",
                "012",
                "013",
                "014",
            ]
            _assert_input_contract(conn)

            rows = {str(row["title"]): row for row in _task_rows(conn)}
            assert set(rows) == set(task_ids)
            node_ids = {
                str(row["hostname"]): cast(int, row["node_id"])
                for row in _query(
                    conn,
                    "SELECT node_id, hostname FROM yascheduler_nodes",
                )
            }
            node_one = node_ids["10.14.1.1"]
            node_two = node_ids["10.14.1.2"]
            for title, task_id in task_ids.items():
                row = rows[title]
                assert row["task_id"] == task_id
                assert row["engine"] == ENGINE
                assert row["created_at"] is not None
                assert row["updated_at"] is not None
                assert json.loads(str(row["extra"])) == (
                    _expected_remaining_metadata(title)
                )
                _assert_converted_snapshot(conn, task_id)

            todo = rows["legacy-todo"]
            running = rows["legacy-running"]
            done = rows["legacy-done"]
            assert todo["status"] == "TO_DO"
            assert todo["allocated_node_id"] is None
            assert todo["remote_folder"] is None
            assert todo["local_folder"] == "/legacy/local/todo"

            assert running["status"] == "RUNNING"
            assert running["allocated_node_id"] == node_one
            assert running["remote_folder"] == "/legacy/remote/running"
            assert running["local_folder"] == "/legacy/local/running"

            assert done["status"] == "DONE"
            assert done["allocated_node_id"] == node_two
            assert done["remote_folder"] == "/legacy/remote/done"
            assert done["local_folder"] == "/legacy/local/done"
            assert done["error"] == "historical completion warning"
        finally:
            conn.close()


def test_migration_014_preserves_unrelated_high_precision_jsonb_number() -> None:
    """Migration 014 must not round unrelated JSONB numbers while removing inputs."""
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            _set_tracker_to_013(conn)
            task_id = _insert_modern_task(
                conn,
                title="precision-metadata",
                status="TO_DO",
                extra={"INPUT": "body", "kept": {"nested": True}},
                created_at="2024-04-05T06:07:08+00:00",
                updated_at="2024-04-05T06:08:09+00:00",
            )
            # Deliberately create the number in PostgreSQL so no Python float
            # can round-trip it before migration 014 sees the metadata.
            conn.run(
                "UPDATE yascheduler_tasks "
                "SET extra = jsonb_set("
                "extra, '{precision}', "
                "'1.234567890123456789'::jsonb, true"
                ") WHERE task_id = :task_id",
                task_id=task_id,
            )
            before = _query(
                conn,
                "SELECT (extra -> 'precision')::text AS precision "
                "FROM yascheduler_tasks WHERE task_id = :task_id",
                task_id=task_id,
            )[0]["precision"]
            assert before == "1.234567890123456789"
        finally:
            conn.close()

        # upto caps the chain at 014 so future migrations cannot leak in.
        apply_migrations(config, engine_inputs={ENGINE: ("INPUT",)}, upto="014")

        conn = _connect(config)
        try:
            after = _query(
                conn,
                "SELECT (extra -> 'precision')::text AS precision "
                "FROM yascheduler_tasks WHERE task_id = :task_id",
                task_id=task_id,
            )[0]["precision"]
            assert after == before
        finally:
            conn.close()
