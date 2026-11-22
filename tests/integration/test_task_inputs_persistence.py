"""Integration tests for task-owned input snapshot persistence."""
# region MODULE_CONTRACT
# PURPOSE: Verify the task input snapshot table, its owned-entry SQL, and the explicit snapshot reads that replaced record hydration — against real PostgreSQL via testcontainers.
# SCOPE: Streamed entry round-trip over text/binary/zero-length/empty-directory/nested shapes with exact kinds and bytes in path order, batched snapshots keyed by TaskId with inputless tasks absent, plain reads carrying no input field, snapshot immutability through run/complete/fail saves, duplicate and orphan rejection at the layer that enforces them, entry-write failure rolling back task creation, deletion cascade across two tasks, and payload-free insert boundary logs.
# DEPENDENCIES: USES API: PostgresUnitOfWork, pg8000.native, testcontainers.PostgresContainer
# READS: PostgreSQL container (postgres:16-alpine)
# KEYWORDS: task inputs, snapshot, streaming read, batched snapshot, cascade, atomicity, lifecycle
# endregion MODULE_CONTRACT

from __future__ import annotations

import base64
import logging
from dataclasses import fields
from typing import TYPE_CHECKING

import pg8000.native
import pytest
from pg8000.exceptions import DatabaseError

from tests.log_assertions import extra_fields
from yascheduler.domain import (
    InvalidTaskSnapshotError,
    TaskInputDirectory,
    TaskInputFile,
    TaskInputSnapshot,
    normalize_input_snapshot,
)
from yascheduler.domain.model import (
    Done,
    NewNode,
    NewTask,
    Running,
    TaskId,
    TaskStatus,
    Todo,
)
from yascheduler.shared.log import LogFormatter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from yascheduler.domain import AnyTask, TaskInputEntry
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

pytestmark = pytest.mark.integration

BINARY_BYTES = b"\x00\x01\xffbin\x00ary"
TEXT_BYTES = b"text input\n"
OTHER_BYTES = b"\x89other\x00bytes"
EXTRA_METADATA: dict[str, object] = {
    "priority": 7,
    "tags": ["baseline", "opt"],
    "nested": {"level": 2},
}


def _snapshot() -> TaskInputSnapshot:
    """Return the normalized snapshot covering every entry shape the round-trip asserts."""
    return normalize_input_snapshot(
        [
            TaskInputFile(path="inp", content=BINARY_BYTES),
            TaskInputFile(path="note.txt", content=TEXT_BYTES),
            TaskInputFile(path="empty.txt", content=b""),
            TaskInputDirectory(path="emptydir"),
            TaskInputDirectory(path="nested"),
            TaskInputFile(path="nested/deep/run.inp", content=b"nested-bytes"),
        ]
    )


def _query(
    conn: pg8000.native.Connection,
    sql: str,
    **params: object,
) -> list[dict[str, object]]:
    """Run raw SQL and return the rows as column-keyed dicts."""
    raw_rows: list[tuple[object, ...]] = conn.run(sql, **params) or []
    columns = [column["name"] for column in (conn.columns or [])]
    return [dict(zip(columns, row)) for row in raw_rows]


def _entry_rows(
    conn: pg8000.native.Connection,
    task_id: TaskId | None = None,
) -> list[dict[str, object]]:
    """Return stored input-entry rows, optionally narrowed to one task."""
    if task_id is None:
        return _query(
            conn,
            "SELECT task_id, path, kind, content"
            " FROM yascheduler_task_inputs ORDER BY task_id, path",
        )
    return _query(
        conn,
        "SELECT task_id, path, kind, content"
        " FROM yascheduler_task_inputs WHERE task_id = :task_id ORDER BY path",
        task_id=task_id.value,
    )


async def _reload(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: TaskId,
) -> AnyTask | None:
    """Reload one task record in a fresh UoW so the read path supplies it."""
    async with uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        await uow.commit()
    return task


async def _stream(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: TaskId,
) -> list[TaskInputEntry]:
    """Drain the per-task entry stream in a fresh UoW."""
    async with uow_factory() as uow:
        entries = [entry async for entry in uow.tasks.iter_input_entries(task_id)]
        await uow.commit()
    return entries


async def _snapshots(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_ids: list[TaskId],
) -> Mapping[TaskId, TaskInputSnapshot]:
    """Read the batched snapshots for the given tasks in a fresh UoW."""
    async with uow_factory() as uow:
        snapshots = await uow.tasks.list_input_snapshots(task_ids)
        await uow.commit()
    return snapshots


def _by_path(entries: Iterable[TaskInputEntry]) -> dict[str, TaskInputEntry]:
    """Key entries by task-relative path so comparisons ignore read ordering."""
    return {entry.path: entry for entry in entries}


# region FUNC_test_streamed_entries_round_trip_a_mixed_tree
# PURPOSE: Prove the per-task stream returns every shape of the stored tree in path order with exact kinds and bytes, and stops after the last entry.
async def test_streamed_entries_round_trip_a_mixed_tree(
    uow_factory: Callable[[], PostgresUnitOfWork],
    pg_conn: pg8000.native.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Text, binary, zero-length, empty-directory, and nested entries stream identically and in order."""
    snapshot = _snapshot()
    caplog.set_level(logging.DEBUG, logger="yascheduler.infra.persistence.postgres")
    async with uow_factory() as uow:
        inserted = await uow.tasks.insert(
            NewTask(
                label="with-inputs",
                engine="fleur",
                extra=dict(EXTRA_METADATA),
                input_entries=snapshot,
            )
        )
        await uow.commit()
        task_id = inserted.task_id

    insert_start = [
        record
        for record in caplog.records
        if record.getMessage() == "persist_task_inputs_START"
    ]
    assert len(insert_start) == 1
    assert {
        key: value
        for key, value in extra_fields(insert_start[0]).items()
        if key != "message"
    } == {
        "task_id": task_id.value,
        "engine": "fleur",
        "entry_count": len(snapshot),
    }
    insert_done = [
        record
        for record in caplog.records
        if record.getMessage() == "persist_task_inputs"
    ]
    assert len(insert_done) == 1
    assert {
        key: value
        for key, value in extra_fields(insert_done[0]).items()
        if key != "message"
    } == {
        "task_id": task_id.value,
        "engine": "fleur",
        "entry_count": len(snapshot),
    }

    async with uow_factory() as uow:
        stream = uow.tasks.iter_input_entries(task_id)
        streamed = [entry async for entry in stream]
        # A further pass over the exhausted iterator yields nothing.
        assert [entry async for entry in stream] == []
        await uow.commit()

    paths = tuple(entry.path for entry in streamed)
    # Deterministic canonical order: byte order puts parents before descendants.
    assert paths == (
        "empty.txt",
        "emptydir",
        "inp",
        "nested",
        "nested/deep",
        "nested/deep/run.inp",
        "note.txt",
    )
    for path in paths:
        if "/" in path:
            parent = path.rsplit("/", 1)[0]
            assert paths.index(parent) < paths.index(path)

    by_path = {entry.path: entry for entry in streamed}
    binary = by_path["inp"]
    assert isinstance(binary, TaskInputFile)
    assert binary.content == BINARY_BYTES
    text = by_path["note.txt"]
    assert isinstance(text, TaskInputFile)
    assert text.content == TEXT_BYTES
    empty_file = by_path["empty.txt"]
    assert isinstance(empty_file, TaskInputFile)
    assert empty_file.content == b""
    nested_file = by_path["nested/deep/run.inp"]
    assert isinstance(nested_file, TaskInputFile)
    assert nested_file.content == b"nested-bytes"
    empty_directory = by_path["emptydir"]
    assert isinstance(empty_directory, TaskInputDirectory)
    # An empty file is a contentless directory's opposite: the file has bytes
    # (an empty byte string), the directory has no content carrier at all.
    assert not isinstance(empty_directory, TaskInputFile)

    # A fresh iterator over the same task replays the same tree.
    assert await _stream(uow_factory, task_id) == streamed

    # Storage keeps that distinction explicitly rather than by an empty value.
    stored = {row["path"]: row for row in _entry_rows(pg_conn, task_id)}
    assert stored["empty.txt"]["kind"] == "file"
    assert stored["empty.txt"]["content"] == b""
    assert stored["emptydir"]["kind"] == "directory"
    assert stored["emptydir"]["content"] is None
    assert stored["inp"]["content"] == BINARY_BYTES


# endregion FUNC_test_streamed_entries_round_trip_a_mixed_tree


# region FUNC_test_plain_reads_carry_no_input_field
# PURPOSE: Pin that record reads no longer carry input contents — every plain read returns a record whose fields cannot expose the snapshot, while the explicit read still returns it.
async def test_plain_reads_carry_no_input_field(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """get/list_by_status/list_by_jobs return records without any input field."""
    snapshot = _snapshot()
    async with uow_factory() as uow:
        inserted = await uow.tasks.insert(
            NewTask(
                label="reads",
                engine="fleur",
                extra=dict(EXTRA_METADATA),
                input_entries=snapshot,
            )
        )
        await uow.commit()
        task_id = inserted.task_id

    async with uow_factory() as uow:
        by_id = await uow.tasks.get(task_id)
        by_status = await uow.tasks.list_by_status({TaskStatus.TO_DO})
        by_jobs = await uow.tasks.list_by_jobs([task_id])
        await uow.commit()

    assert by_id is not None
    assert len(by_status) == 1
    assert len(by_jobs) == 1
    for loaded in (by_id, by_status[0], by_jobs[0]):
        assert loaded.task_id == task_id
        assert "input_entries" not in {field.name for field in fields(loaded)}
        assert not hasattr(loaded, "input_entries")
        assert loaded.extra == EXTRA_METADATA

    # The bytes are not lost, only displaced: the explicit read still has them.
    assert _by_path(await _stream(uow_factory, task_id)) == _by_path(snapshot)


# endregion FUNC_test_plain_reads_carry_no_input_field


# region FUNC_test_list_input_snapshots_batches_complete_trees
# PURPOSE: Prove one batched read returns each task's complete snapshot keyed by TaskId, leaves inputless tasks out, and returns an empty mapping for an empty id list.
async def test_list_input_snapshots_batches_complete_trees(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """Batched snapshots are complete, keyed by task, and omit tasks with no entries."""
    mixed = _snapshot()
    other = normalize_input_snapshot(
        (TaskInputFile(path="only.bin", content=OTHER_BYTES),)
    )
    async with uow_factory() as uow:
        mixed_task = await uow.tasks.insert(
            NewTask(label="mixed", engine="fleur", input_entries=mixed)
        )
        other_task = await uow.tasks.insert(
            NewTask(label="other", engine="fleur", input_entries=other)
        )
        empty_task = await uow.tasks.insert(NewTask(label="empty", engine="fleur"))
        await uow.commit()
        mixed_id = mixed_task.task_id
        other_id = other_task.task_id
        empty_id = empty_task.task_id

    snapshots = await _snapshots(uow_factory, [mixed_id, other_id, empty_id])

    assert set(snapshots) == {mixed_id, other_id}
    assert _by_path(snapshots[mixed_id]) == _by_path(mixed)
    assert _by_path(snapshots[other_id]) == _by_path(other)
    # The batched read agrees with the streamed read.
    assert _by_path(snapshots[mixed_id]) == _by_path(
        await _stream(uow_factory, mixed_id)
    )

    by_path = {entry.path: entry for entry in snapshots[mixed_id]}
    binary = by_path["inp"]
    assert isinstance(binary, TaskInputFile)
    assert binary.content == BINARY_BYTES
    empty_file = by_path["empty.txt"]
    assert isinstance(empty_file, TaskInputFile)
    assert empty_file.content == b""
    empty_directory = by_path["emptydir"]
    assert isinstance(empty_directory, TaskInputDirectory)

    assert await _snapshots(uow_factory, []) == {}


# endregion FUNC_test_list_input_snapshots_batches_complete_trees


# region FUNC_test_empty_snapshot_emits_insert_boundaries_and_reads_nothing
# PURPOSE: Pin that an inputless task is still an observable insert boundary with zero entries and is invisible to both explicit reads.
async def test_empty_snapshot_emits_insert_boundaries_and_reads_nothing(
    uow_factory: Callable[[], PostgresUnitOfWork],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty snapshot logs zero entries and yields nothing on either explicit read."""
    caplog.set_level(logging.DEBUG, logger="yascheduler.infra.persistence.postgres")
    async with uow_factory() as uow:
        inserted = await uow.tasks.insert(NewTask(label="empty", engine="fleur"))
        await uow.commit()
        task_id = inserted.task_id

    async with uow_factory() as uow:
        loaded = await uow.tasks.get(task_id)
        await uow.commit()

    assert loaded is not None
    assert await _stream(uow_factory, task_id) == []
    assert await _snapshots(uow_factory, [task_id]) == {}
    for marker, expected in (
        (
            "persist_task_inputs_START",
            {"task_id": task_id.value, "engine": "fleur", "entry_count": 0},
        ),
        (
            "persist_task_inputs",
            {"task_id": task_id.value, "engine": "fleur", "entry_count": 0},
        ),
    ):
        records = [record for record in caplog.records if record.getMessage() == marker]
        assert len(records) == 1
        assert {
            key: value
            for key, value in extra_fields(records[0]).items()
            if key != "message"
        } == expected

    # Reads no longer hydrate: no hydration boundary is emitted at all.
    assert not [
        record
        for record in caplog.records
        if record.getMessage().startswith("hydrate_task_inputs")
    ]


# endregion FUNC_test_empty_snapshot_emits_insert_boundaries_and_reads_nothing


async def test_normalize_input_snapshot_rejects_duplicate_paths() -> None:
    """The domain layer rejects duplicate and kind-conflicting entry paths."""
    with pytest.raises(InvalidTaskSnapshotError):
        normalize_input_snapshot(
            [
                TaskInputFile(path="dup", content=b"one"),
                TaskInputFile(path="dup", content=b"two"),
            ]
        )

    with pytest.raises(InvalidTaskSnapshotError):
        normalize_input_snapshot(
            [
                TaskInputDirectory(path="dup"),
                TaskInputFile(path="dup", content=b"one"),
            ]
        )


async def test_database_rejects_duplicate_entry_path_for_one_task(
    uow_factory: Callable[[], PostgresUnitOfWork],
    pg_conn: pg8000.native.Connection,
) -> None:
    """The (task_id, path) primary key rejects a duplicate row a caller smuggles past normalization."""
    async with uow_factory() as uow:
        task = await uow.tasks.insert(
            NewTask(
                label="dups",
                engine="fleur",
                input_entries=(TaskInputFile(path="dup", content=b"one"),),
            )
        )
        await uow.commit()
        task_id = task.task_id

    with pytest.raises(DatabaseError):
        pg_conn.run(
            "INSERT INTO yascheduler_task_inputs (task_id, path, kind, content)"
            " VALUES (:task_id, 'dup', 'file', :content)",
            task_id=task_id.value,
            content=b"two",
        )

    assert [row["path"] for row in _entry_rows(pg_conn, task_id)] == ["dup"]
    assert _entry_rows(pg_conn, task_id)[0]["content"] == b"one"


async def test_database_rejects_orphan_entry(
    pg_conn: pg8000.native.Connection,
) -> None:
    """A foreign-key violation rejects an entry whose task does not exist."""
    with pytest.raises(DatabaseError):
        pg_conn.run(
            "INSERT INTO yascheduler_task_inputs (task_id, path, kind, content)"
            " VALUES (:task_id, 'orphan', 'file', :content)",
            task_id=999_999,
            content=b"orphan-bytes",
        )

    assert _entry_rows(pg_conn) == []


async def test_entry_write_failure_rolls_back_task_creation(
    uow_factory: Callable[[], PostgresUnitOfWork],
    pg_conn: pg8000.native.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing entry batch insert leaves neither the task row nor any entry behind.

    The duplicate tuple bypasses ``normalize_input_snapshot`` on purpose: it makes
    the database entry write fail *after* the task row insert, proving both share
    the UoW transaction instead of committing separately.
    """
    input_secret = b"UNIQUE_PERSISTENCE_INPUT_SECRET_2da9"
    credential = b"UNIQUE_PERSISTENCE_CREDENTIAL_71c4"
    duplicate_entries = (
        TaskInputFile(path="dup", content=input_secret),
        TaskInputFile(path="dup", content=credential),
    )

    with (
        caplog.at_level(logging.DEBUG, logger="yascheduler.infra.persistence.postgres"),
        pytest.raises(DatabaseError),
    ):
        async with uow_factory() as uow:
            await uow.tasks.insert(
                NewTask(
                    label="atomic",
                    engine="fleur",
                    input_entries=duplicate_entries,
                )
            )
            await uow.commit()

    failed = [
        record
        for record in caplog.records
        if record.getMessage() == "persist_task_inputs_FAILED"
    ]
    assert len(failed) == 1
    fields_ = extra_fields(failed[0])
    assert fields_["engine"] == "fleur"
    assert fields_["entry_count"] == 2
    assert fields_["failure"] == "DatabaseError"
    assert isinstance(fields_["task_id"], int)
    assert not any(
        record.getMessage() == "persist_task_inputs" for record in caplog.records
    )
    rendered = LogFormatter().format(failed[0])
    assert "persist_task_inputs_FAILED" in rendered
    assert f"task_id={fields_['task_id']}" in rendered
    assert "failure='DatabaseError'" in rendered
    leaked_markers = (
        input_secret.decode(),
        credential.decode(),
        base64.b64encode(input_secret).decode(),
        base64.b64encode(credential).decode(),
    )
    all_fields = repr([extra_fields(record) for record in caplog.records])
    for marker in leaked_markers:
        assert marker not in rendered
        assert marker not in all_fields

    assert _query(pg_conn, "SELECT count(*) AS n FROM yascheduler_tasks")[0]["n"] == 0
    assert _entry_rows(pg_conn) == []


async def test_deleting_a_task_cascades_only_to_its_own_entry_rows(
    uow_factory: Callable[[], PostgresUnitOfWork],
    pg_conn: pg8000.native.Connection,
) -> None:
    """Deleting one task removes exactly its own input entries."""
    async with uow_factory() as uow:
        kept = await uow.tasks.insert(
            NewTask(
                label="kept",
                engine="fleur",
                input_entries=normalize_input_snapshot(
                    (TaskInputFile(path="keep/inp", content=b"keep"),)
                ),
            )
        )
        dropped = await uow.tasks.insert(
            NewTask(
                label="dropped",
                engine="fleur",
                input_entries=normalize_input_snapshot(
                    (
                        TaskInputFile(path="drop/inp", content=b"drop"),
                        TaskInputDirectory(path="drop/empty"),
                    )
                ),
            )
        )
        await uow.commit()
        kept_id = kept.task_id
        dropped_id = dropped.task_id

    pg_conn.run(
        "DELETE FROM yascheduler_tasks WHERE task_id = :task_id",
        task_id=dropped_id.value,
    )

    remaining = _entry_rows(pg_conn)
    assert {row["path"] for row in remaining} == {"keep", "keep/inp"}
    assert {row["task_id"] for row in remaining} == {kept_id.value}

    assert await _reload(uow_factory, dropped_id) is None
    snapshots = await _snapshots(uow_factory, [kept_id, dropped_id])
    assert set(snapshots) == {kept_id}
    assert tuple(entry.path for entry in snapshots[kept_id]) == (
        "keep",
        "keep/inp",
    )
    kept_entry = snapshots[kept_id][1]
    assert isinstance(kept_entry, TaskInputFile)
    assert kept_entry.content == b"keep"


# region FUNC_test_snapshot_survives_run_complete_and_fail_saves
# PURPOSE: Prove lifecycle saves rewrite task state only — the stored task_inputs rows stay byte-identical and both explicit reads keep returning the accepted tree.
async def test_snapshot_survives_run_complete_and_fail_saves(
    uow_factory: Callable[[], PostgresUnitOfWork],
    pg_conn: pg8000.native.Connection,
) -> None:
    """Lifecycle saves alter task state fields but never rewrite the input snapshot."""
    snapshot = _snapshot()
    async with uow_factory() as uow:
        node = await uow.nodes.insert(
            NewNode(hostname="10.0.0.9", ncpus=2, enabled=True),
        )
        to_complete = await uow.tasks.insert(
            NewTask(label="completes", engine="fleur", input_entries=snapshot)
        )
        to_fail = await uow.tasks.insert(
            NewTask(label="fails", engine="fleur", input_entries=snapshot)
        )
        await uow.commit()
        node_id = node.node_id
        complete_id = to_complete.task_id
        fail_id = to_fail.task_id

    complete_rows = _entry_rows(pg_conn, complete_id)
    fail_rows = _entry_rows(pg_conn, fail_id)
    assert {row["path"] for row in complete_rows} == {entry.path for entry in snapshot}

    # TO_DO → RUNNING keeps the snapshot.
    async with uow_factory() as uow:
        todo_task = await uow.tasks.get_todo(complete_id)
        assert todo_task is not None
        assert _by_path(
            [entry async for entry in uow.tasks.iter_input_entries(complete_id)]
        ) == _by_path(snapshot)
        running = todo_task.run(node_id, "/remote/completes")
        await uow.tasks.save(running, expected_status=TaskStatus.TO_DO)
        await uow.commit()

    reloaded = await _reload(uow_factory, complete_id)
    assert reloaded is not None
    assert isinstance(reloaded.state, Running)
    assert _by_path(await _stream(uow_factory, complete_id)) == _by_path(snapshot)
    assert _entry_rows(pg_conn, complete_id) == complete_rows

    # RUNNING → DONE (success) keeps the snapshot.
    async with uow_factory() as uow:
        running_task = await uow.tasks.get_running(complete_id)
        assert running_task is not None
        done = running_task.complete(
            local_folder="/local/completes",
            remote_folder="/remote/completes",
        )
        await uow.tasks.save(done, expected_status=TaskStatus.RUNNING)
        await uow.commit()

    reloaded = await _reload(uow_factory, complete_id)
    assert reloaded is not None
    assert isinstance(reloaded.state, Done)
    assert reloaded.state.error is None
    assert _by_path((await _snapshots(uow_factory, [complete_id]))[complete_id]) == (
        _by_path(snapshot)
    )
    assert _entry_rows(pg_conn, complete_id) == complete_rows

    # The second task takes the RUNNING → DONE (failure) path.
    async with uow_factory() as uow:
        todo_task = await uow.tasks.get_todo(fail_id)
        assert todo_task is not None
        assert isinstance(todo_task.state, Todo)
        running = todo_task.run(node_id, "/remote/fails")
        await uow.tasks.save(running, expected_status=TaskStatus.TO_DO)
        await uow.commit()

    async with uow_factory() as uow:
        running_task = await uow.tasks.get_running(fail_id)
        assert running_task is not None
        failed = running_task.fail(
            "scheduler error",
            local_folder="/local/fails",
            remote_folder="/remote/fails",
        )
        await uow.tasks.save(failed, expected_status=TaskStatus.RUNNING)
        await uow.commit()

    reloaded = await _reload(uow_factory, fail_id)
    assert reloaded is not None
    assert isinstance(reloaded.state, Done)
    assert reloaded.state.error == "scheduler error"
    assert _by_path(await _stream(uow_factory, fail_id)) == _by_path(snapshot)
    assert _entry_rows(pg_conn, fail_id) == fail_rows


# endregion FUNC_test_snapshot_survives_run_complete_and_fail_saves
