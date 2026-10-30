"""Integration tests for migration 014 task-input snapshot conversion."""
# region MODULE_CONTRACT
# PURPOSE: Verify migration 014 against real PostgreSQL through testcontainers: fresh bootstrap exposes the task-input storage contract without engine context, 013 databases convert every task status into equivalent binary snapshot entries, legacy databases upgrade through 014 without losing task identity, timestamps, state, bindings, or unrelated metadata, and failed conversion retries atomically after fixture repair.
# SCOPE: Fresh input-storage contract and current/legacy cohorts through migration 014, exact input-table schema and constraints, UTF-8 and fort.9 lenient-base64 conversion, literal-directory and glob subtree expansion, metadata-key removal, bootstrap-precreated and runner-only table creation, failure rollback/trigger restoration for unknown engines, missing/unsafe inputs, malformed encodings and unsupported values, tracker/snapshot idempotency on rerun, and qualified-runner structured failure logs with payload redaction.
# DEPENDENCIES: USES API: apply_schema, apply_migrations, pg8000.native, testcontainers.PostgresContainer
# READS: PostgreSQL container (postgres:16-alpine)
# KEYWORDS: migration 014, task inputs, snapshots, postgres, base64, legacy upgrade, rollback, retry, idempotency
# endregion MODULE_CONTRACT

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import pg8000.native
import pytest
from testcontainers.postgres import PostgresContainer

from tests.log_assertions import extra_fields
from yascheduler.infra.persistence import (
    PostgresDbConfig,
    apply_migrations,
)
from yascheduler.infra.persistence.postgres_schema import apply_schema
from yascheduler.shared.log import LogFormatter

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
UNKNOWN_ENGINE = "migration-014-unknown-engine"
UNSAFE_ENGINE = "migration-014-unsafe-engine"
LOG_SECRET = "UNIQUE_MIGRATION_INPUT_SECRET_8e2f"
LOG_CREDENTIAL = "UNIQUE_MIGRATION_CREDENTIAL_46b1"
MIGRATION_LOGGER = (
    "yascheduler.infra.persistence.sql.migrations.014_task_input_snapshots"
)


# region CLASS__FailureCase
# PURPOSE: Describe one broken conversion scenario, its repaired fixture, and the exact retained metadata/snapshot so one generic real-PostgreSQL test can prove rollback, diagnostics, repair, and retry without duplicating setup.
@dataclass(frozen=True)
class _FailureCase:
    """Broken and repaired state for one migration 014 failure scenario."""

    label: str
    engine: str
    broken_payload: Mapping[str, object]
    repaired_payload: Mapping[str, object]
    failing_engine_inputs: Mapping[str, tuple[str, ...]]
    repaired_engine_inputs: Mapping[str, tuple[str, ...]]
    expected_diagnostic: tuple[str, ...]
    expected_entries: Mapping[str, tuple[str, bytes | None]]
    expected_remaining: Mapping[str, object]


# endregion CLASS__FailureCase


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


def _tracker_snapshot(conn: pg8000.native.Connection) -> list[dict[str, object]]:
    return _query(
        conn,
        "SELECT migration_id, created_at FROM yascheduler_migrations "
        "ORDER BY migration_id",
    )


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


def _all_input_rows(conn: pg8000.native.Connection) -> list[dict[str, object]]:
    return _query(
        conn,
        "SELECT task_id, path, kind, content FROM yascheduler_task_inputs "
        "ORDER BY task_id, path",
    )


def _assert_input_snapshot(
    conn: pg8000.native.Connection,
    task_id: int,
    expected: Mapping[str, tuple[str, bytes | None]],
) -> None:
    rows = _input_rows(conn, task_id)
    assert set(rows) == set(expected)
    for path, (kind, content) in expected.items():
        assert rows[path]["task_id"] == task_id
        assert rows[path]["kind"] == kind, path
        if kind == "file":
            assert rows[path]["content"] == content, path
        else:
            assert rows[path]["content"] is None, path


def _assert_converted_snapshot(
    conn: pg8000.native.Connection,
    task_id: int,
) -> None:
    _assert_input_snapshot(conn, task_id, EXPECTED_INPUT_ENTRIES)


def _timestamp_trigger_state(conn: pg8000.native.Connection) -> str:
    rows = _query(
        conn,
        "SELECT tgenabled FROM pg_trigger "
        "WHERE tgrelid = 'yascheduler_tasks'::regclass "
        "AND tgname = 'yascheduler_tasks_touch_updated_at'",
    )
    assert len(rows) == 1
    return str(rows[0]["tgenabled"])


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


# region FUNC__failure_cases
# PURPOSE: Build the five broken/repaired migration fixtures required by task 3.4 while keeping each failure isolated to the later task and reusing the existing rich success payload.
def _failure_cases() -> tuple[_FailureCase, ...]:
    marker = "rollback-broken"
    full_payload = _payload(marker)
    expected_remaining = _expected_remaining_metadata(marker)
    missing_input_payload = {
        key: value for key, value in full_payload.items() if key != "INPUT"
    }
    unsafe_payload: dict[str, object] = {
        "INPUT": UTF8_TEXT,
        **UNRELATED_METADATA,
        "marker": marker,
    }
    return (
        _FailureCase(
            label="unknown-engine",
            engine=UNKNOWN_ENGINE,
            broken_payload=full_payload,
            repaired_payload=full_payload,
            failing_engine_inputs={ENGINE: DECLARATIONS},
            repaired_engine_inputs={
                ENGINE: DECLARATIONS,
                UNKNOWN_ENGINE: DECLARATIONS,
            },
            expected_diagnostic=(
                f"engine {UNKNOWN_ENGINE!r} has no input declarations",
                "supply its configuration",
            ),
            expected_entries=EXPECTED_INPUT_ENTRIES,
            expected_remaining=expected_remaining,
        ),
        _FailureCase(
            label="missing-input",
            engine=ENGINE,
            broken_payload=missing_input_payload,
            repaired_payload=full_payload,
            failing_engine_inputs={ENGINE: DECLARATIONS},
            repaired_engine_inputs={ENGINE: DECLARATIONS},
            expected_diagnostic=(f"missing input file 'INPUT' for engine: {ENGINE}",),
            expected_entries=EXPECTED_INPUT_ENTRIES,
            expected_remaining=expected_remaining,
        ),
        _FailureCase(
            label="unsafe-path",
            engine=UNSAFE_ENGINE,
            broken_payload=unsafe_payload,
            repaired_payload=unsafe_payload,
            failing_engine_inputs={ENGINE: DECLARATIONS, UNSAFE_ENGINE: ("../escape",)},
            repaired_engine_inputs={ENGINE: DECLARATIONS, UNSAFE_ENGINE: ("INPUT",)},
            expected_diagnostic=(
                "unsafe task path '../escape'",
                "'..' component escapes the task root",
            ),
            expected_entries={"INPUT": ("file", UTF8_TEXT.encode("utf-8"))},
            expected_remaining=expected_remaining,
        ),
        _FailureCase(
            label="malformed-fort9",
            engine=ENGINE,
            broken_payload={
                **full_payload,
                "fort.9": f"{LOG_SECRET}!",
                "unrelated_credential": LOG_CREDENTIAL,
            },
            repaired_payload=full_payload,
            failing_engine_inputs={ENGINE: DECLARATIONS},
            repaired_engine_inputs={ENGINE: DECLARATIONS},
            expected_diagnostic=(
                "invalid task input snapshot at 'fort.9'",
                "legacy fort.9 value is not valid base64",
            ),
            expected_entries=EXPECTED_INPUT_ENTRIES,
            expected_remaining=expected_remaining,
        ),
        _FailureCase(
            label="unsupported-value",
            engine=ENGINE,
            broken_payload={**full_payload, "INPUT": 123},
            repaired_payload=full_payload,
            failing_engine_inputs={ENGINE: DECLARATIONS},
            repaired_engine_inputs={ENGINE: DECLARATIONS},
            expected_diagnostic=(
                "invalid task input snapshot at 'INPUT'",
                "legacy input value must be a str, got int",
            ),
            expected_entries=EXPECTED_INPUT_ENTRIES,
            expected_remaining=expected_remaining,
        ),
    )


# endregion FUNC__failure_cases


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


def test_fresh_bootstrap_exposes_migration_014_input_storage_without_engine_context() -> (
    None
):
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            assert _table_exists(conn)
            _assert_input_contract(conn)
        finally:
            conn.close()

        # The fresh snapshot already carries migration 014 input storage;
        # the capped runner call must not need engine context.
        apply_migrations(config, upto="014")

        conn = _connect(config)
        try:
            assert _table_exists(conn)
            _assert_input_contract(conn)
        finally:
            conn.close()


def test_modern_013_migration_creates_table_and_converts_all_statuses(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
        with caplog.at_level(logging.DEBUG, logger=MIGRATION_LOGGER):
            apply_migrations(config, engine_inputs={ENGINE: DECLARATIONS}, upto="014")

        overall_start = [
            record
            for record in caplog.records
            if record.getMessage() == "MIGRATE_INPUTS_START"
        ]
        assert len(overall_start) == 1
        assert overall_start[0].name == MIGRATION_LOGGER
        assert {
            key: value
            for key, value in extra_fields(overall_start[0]).items()
            if key != "message"
        } == {"entry_count": 0}

        overall_done = [
            record
            for record in caplog.records
            if record.getMessage() == "MIGRATE_INPUTS_DONE"
        ]
        assert len(overall_done) == 1
        assert overall_done[0].name == MIGRATION_LOGGER
        assert {
            key: value
            for key, value in extra_fields(overall_done[0]).items()
            if key != "message"
        } == {
            "tasks": len(task_ids),
            "entry_count": len(task_ids) * len(EXPECTED_INPUT_ENTRIES),
        }
        for task_id in task_ids:
            storage_done = [
                record
                for record in caplog.records
                if record.getMessage() == "MIGRATE_TASK_STORAGE_DONE"
                and getattr(record, "task_id", None) == task_id
            ]
            assert len(storage_done) == 1
            assert {
                key: value
                for key, value in extra_fields(storage_done[0]).items()
                if key != "message"
            } == {
                "task_id": task_id,
                "engine": ENGINE,
                "entry_count": len(EXPECTED_INPUT_ENTRIES),
            }

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


def test_legacy_db_without_tracker_upgrades_through_014_and_inputs() -> None:
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

        apply_migrations(
            config,
            engine_inputs={ENGINE: DECLARATIONS},
            upto="014",
        )

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


# region FUNC_test_migration_014_failure_rolls_back_and_repaired_retry_is_idempotent
# PURPOSE: Prove every required conversion failure rolls back prior task inserts, metadata removals, table DDL, tracker recording, and trigger state; repair only the broken fixture; then verify exact retry output and context-free idempotency.
@pytest.mark.parametrize(
    "precreated",
    [False, True],
    ids=["migration-created-table", "bootstrap-precreated-table"],
)
@pytest.mark.parametrize("case", _failure_cases(), ids=lambda case: case.label)
def test_migration_014_failure_rolls_back_and_repaired_retry_is_idempotent(
    precreated: bool,
    case: _FailureCase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_marker = "rollback-first"
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        config = _make_config(pg)
        apply_schema(config)

        conn = _connect(config)
        try:
            if not precreated:
                conn.run("DROP TABLE yascheduler_task_inputs")
            _set_tracker_to_013(conn)
            node_id = _insert_node(conn, "10.14.2.1", 4)
            first_task_id = _insert_modern_task(
                conn,
                title="rollback-first-valid",
                status="RUNNING",
                extra=_payload(first_marker),
                created_at="2024-05-06T07:08:09+00:00",
                updated_at="2024-05-06T07:09:10+00:00",
                allocated_node_id=node_id,
                local_folder="/rollback/local/first",
                remote_folder="/rollback/remote/first",
                webhook_url="https://example.invalid/rollback-first",
                webhook_custom_params={"marker": first_marker},
            )
            broken_task_id = _insert_modern_task(
                conn,
                title=f"rollback-broken-{case.label}",
                status="DONE",
                extra=case.broken_payload,
                created_at="2024-06-07T08:09:10+00:00",
                updated_at="2024-06-07T08:10:11+00:00",
                local_folder="/rollback/local/broken",
                remote_folder="/rollback/remote/broken",
                error="historical rollback warning",
            )
            if case.engine != ENGINE:
                conn.run(
                    "UPDATE yascheduler_tasks SET engine = :engine "
                    "WHERE task_id = :task_id",
                    engine=case.engine,
                    task_id=broken_task_id,
                )
            before_failure_tasks = _task_rows(conn)
            before_failure_tracker = _tracker_snapshot(conn)
        finally:
            conn.close()

        with (
            caplog.at_level(logging.DEBUG, logger=MIGRATION_LOGGER),
            pytest.raises(RuntimeError) as exc_info,
        ):
            apply_migrations(
                config,
                engine_inputs=case.failing_engine_inputs,
                upto="014",
            )

        diagnostic = str(exc_info.value)
        assert f"task {broken_task_id}, engine {case.engine!r}" in diagnostic
        for fragment in case.expected_diagnostic:
            assert fragment in diagnostic

        assert caplog.records[0].getMessage() == "MIGRATE_INPUTS_START"
        overall_failed = [
            record
            for record in caplog.records
            if record.getMessage() == "MIGRATE_INPUTS_FAILED"
        ]
        assert len(overall_failed) == 1
        overall_failed_fields = extra_fields(overall_failed[0])
        assert overall_failed_fields["task_id"] == broken_task_id
        assert overall_failed_fields["engine"] == case.engine
        assert overall_failed_fields["failure"] == "RuntimeError"
        assert overall_failed_fields["entry_count"] == len(EXPECTED_INPUT_ENTRIES)

        task_failed = [
            record
            for record in caplog.records
            if record.getMessage() == "MIGRATE_TASK_INPUTS_FAILED"
            and getattr(record, "task_id", None) == broken_task_id
        ]
        assert len(task_failed) == 1
        failed_record = task_failed[0]
        assert failed_record.name == MIGRATION_LOGGER
        failed_fields = extra_fields(failed_record)
        expected_failure = {
            "unknown-engine": "ValueError",
            "missing-input": "MissingInputFileError",
            "unsafe-path": "UnsafeTaskPathError",
            "malformed-fort9": "InvalidTaskSnapshotError",
            "unsupported-value": "InvalidTaskSnapshotError",
        }[case.label]
        assert failed_fields["engine"] == case.engine
        assert failed_fields["entry_count"] == 0
        assert failed_fields["failure"] == expected_failure
        expected_path = {
            "unknown-engine": None,
            "missing-input": "INPUT",
            "unsafe-path": "../escape",
            "malformed-fort9": "fort.9",
            "unsupported-value": "INPUT",
        }[case.label]
        if expected_path is None:
            assert "path" not in failed_fields
            assert "selection" not in failed_fields
        else:
            assert failed_fields["path"] == expected_path
        if case.label in {"missing-input", "unsafe-path"}:
            assert failed_fields["selection"] == expected_path
        else:
            assert "selection" not in failed_fields

        assert not any(
            record.getMessage() == "MIGRATE_TASK_INPUTS"
            and getattr(record, "task_id", None) == broken_task_id
            for record in caplog.records
        )
        assert not any(
            record.getMessage() == "MIGRATE_TASK_STORAGE_DONE"
            and getattr(record, "task_id", None) == broken_task_id
            for record in caplog.records
        )
        assert not any(
            record.getMessage() == "MIGRATE_INPUTS_DONE" for record in caplog.records
        )

        rendered = LogFormatter().format(failed_record)
        assert "MIGRATE_TASK_INPUTS_FAILED" in rendered
        assert f"task_id={broken_task_id}" in rendered
        assert f"failure='{expected_failure}'" in rendered
        if expected_path is not None:
            assert f"path='{expected_path}'" in rendered
        all_field_dump = repr([extra_fields(record) for record in caplog.records])
        for marker in (
            LOG_SECRET,
            LOG_CREDENTIAL,
            base64.b64encode(LOG_SECRET.encode()).decode(),
        ):
            assert marker not in rendered
            assert marker not in all_field_dump
        assert all(record.exc_info is None for record in caplog.records)

        # A fresh connection must see the complete pre-migration state: the
        # first task's snapshot insert and metadata-key removal rolled back,
        # the tracker did not record 014, and the timestamp trigger is on.
        conn = _connect(config)
        try:
            assert _tracker_snapshot(conn) == before_failure_tracker
            assert _task_rows(conn) == before_failure_tasks
            assert _timestamp_trigger_state(conn) == "O"
            if precreated:
                assert _table_exists(conn)
                assert _all_input_rows(conn) == []
            else:
                assert not _table_exists(conn)

            # Repair only the broken payload. Unknown-engine and unsafe-path
            # scenarios need only their engine declaration repaired instead.
            if case.repaired_payload != case.broken_payload:
                conn.run(
                    "UPDATE yascheduler_tasks SET extra = CAST(:extra AS JSONB) "
                    "WHERE task_id = :task_id",
                    extra=json.dumps(case.repaired_payload),
                    task_id=broken_task_id,
                )
            before_retry_tasks = {
                cast(int, row["task_id"]): row for row in _task_rows(conn)
            }
        finally:
            conn.close()

        apply_migrations(
            config,
            engine_inputs=case.repaired_engine_inputs,
            upto="014",
        )

        conn = _connect(config)
        try:
            assert _tracker_rows(conn) == ["013", "014"]
            assert _table_exists(conn)
            _assert_converted_snapshot(conn, first_task_id)
            _assert_input_snapshot(conn, broken_task_id, case.expected_entries)

            after_success_tasks = {
                cast(int, row["task_id"]): row for row in _task_rows(conn)
            }
            assert set(after_success_tasks) == {first_task_id, broken_task_id}
            for task_id, expected_extra in (
                (first_task_id, _expected_remaining_metadata(first_marker)),
                (broken_task_id, case.expected_remaining),
            ):
                for column in PRESERVED_TASK_COLUMNS:
                    assert (
                        after_success_tasks[task_id][column]
                        == before_retry_tasks[task_id][column]
                    ), (task_id, column)
                assert json.loads(str(after_success_tasks[task_id]["extra"])) == (
                    expected_extra
                )

            first_inputs = _input_rows(conn, first_task_id)
            # Non-fort.9 strings are never guessed as base64; this text
            # happens to be valid base64 but must remain its literal UTF-8.
            assert first_inputs["looks-like-base64.txt"][
                "content"
            ] == BASE64_LOOKALIKE_TEXT.encode("utf-8")
            assert first_inputs["looks-like-base64.txt"]["content"] != b"Hello"
            # FORT9_TEXT has whitespace and missing padding, both historically
            # accepted; the malformed case above uses the reliably invalid 'a'.
            assert first_inputs["fort.9"]["content"] == b"\x00\x01\x02\xff"
            assert _timestamp_trigger_state(conn) == "O"

            success_tasks = _task_rows(conn)
            success_inputs = _all_input_rows(conn)
            success_tracker = _tracker_snapshot(conn)
        finally:
            conn.close()

        # No 014 is pending after success. Running without engine context must
        # be a no-op; if conversion reran, the missing context would fail.
        apply_migrations(config, upto="014")

        conn = _connect(config)
        try:
            assert _task_rows(conn) == success_tasks
            assert _all_input_rows(conn) == success_inputs
            assert _tracker_snapshot(conn) == success_tracker
            assert _timestamp_trigger_state(conn) == "O"
        finally:
            conn.close()


# endregion FUNC_test_migration_014_failure_rolls_back_and_repaired_retry_is_idempotent
