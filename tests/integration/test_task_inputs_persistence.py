"""Integration tests for task-owned input snapshot persistence."""
# region MODULE_CONTRACT
# PURPOSE: Verify the task input snapshot table, its owned-entry SQL, and the repository insert/hydration behavior against real PostgreSQL via testcontainers.
# SCOPE: Binary/empty-file/empty-directory/nested round-trip through get/list_by_status/list_by_jobs; duplicate and orphan rejection at the layer that enforces them; entry-write failure rolling back task creation; deletion cascade across two tasks; snapshot survival through run/complete/fail saves.
# DEPENDENCIES: USES API: PostgresUnitOfWork, pg8000.native, testcontainers.PostgresContainer
# READS: PostgreSQL container (postgres:16-alpine)
# KEYWORDS: task inputs, snapshot, round-trip, hydration, cascade, atomicity, lifecycle
# endregion MODULE_CONTRACT

from __future__ import annotations

from typing import TYPE_CHECKING

import pg8000.native
import pytest
from pg8000.exceptions import DatabaseError

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

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.domain import AnyTask
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

pytestmark = pytest.mark.integration

BINARY_BYTES = b"\x00\x01\xffbin\x00ary"
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
    """Reload one task in a fresh UoW so hydration, not identity map, supplies the snapshot."""
    async with uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        await uow.commit()
    return task


async def test_snapshot_round_trips_through_insert_and_every_read_path(
    uow_factory: Callable[[], PostgresUnitOfWork],
    pg_conn: pg8000.native.Connection,
) -> None:
    """Binary, empty-file, empty-directory, and nested entries reload identically on every read path."""
    snapshot = _snapshot()
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

    # The insert result itself carries the snapshot the caller supplied.
    assert inserted.input_entries == snapshot

    async with uow_factory() as uow:
        by_id = await uow.tasks.get(task_id)
        by_status = await uow.tasks.list_by_status({TaskStatus.TO_DO})
        by_jobs = await uow.tasks.list_by_jobs([task_id])
        await uow.commit()

    assert len(by_status) == 1
    assert len(by_jobs) == 1
    for loaded in (by_id, by_status[0], by_jobs[0]):
        assert loaded is not None
        assert loaded.task_id == task_id
        assert loaded.input_entries == snapshot
        assert loaded.extra == EXTRA_METADATA

    # Paths are deterministic and canonical: parents precede their children.
    assert by_id is not None
    assert tuple(entry.path for entry in by_id.input_entries) == (
        "empty.txt",
        "emptydir",
        "inp",
        "nested",
        "nested/deep",
        "nested/deep/run.inp",
    )
    by_path = {entry.path: entry for entry in by_id.input_entries}
    binary = by_path["inp"]
    assert isinstance(binary, TaskInputFile)
    assert binary.content == BINARY_BYTES
    empty_file = by_path["empty.txt"]
    assert isinstance(empty_file, TaskInputFile)
    assert empty_file.content == b""
    empty_directory = by_path["emptydir"]
    assert isinstance(empty_directory, TaskInputDirectory)
    # An empty file is a contentless directory's opposite: the file has bytes
    # (an empty byte string), the directory has no content carrier at all.
    assert not isinstance(empty_directory, TaskInputFile)

    # Storage keeps that distinction explicitly rather than by an empty value.
    stored = {row["path"]: row for row in _entry_rows(pg_conn, task_id)}
    assert stored["empty.txt"]["kind"] == "file"
    assert stored["empty.txt"]["content"] == b""
    assert stored["emptydir"]["kind"] == "directory"
    assert stored["emptydir"]["content"] is None
    assert stored["inp"]["content"] == BINARY_BYTES


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
) -> None:
    """A failing entry batch insert leaves neither the task row nor any entry behind.

    The duplicate tuple bypasses ``normalize_input_snapshot`` on purpose: it makes
    the database entry write fail *after* the task row insert, proving both share
    the UoW transaction instead of committing separately.
    """
    duplicate_entries = (
        TaskInputFile(path="dup", content=b"one"),
        TaskInputFile(path="dup", content=b"two"),
    )

    with pytest.raises(DatabaseError):
        async with uow_factory() as uow:
            await uow.tasks.insert(
                NewTask(
                    label="atomic",
                    engine="fleur",
                    input_entries=duplicate_entries,
                )
            )
            await uow.commit()

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
    reloaded = await _reload(uow_factory, kept_id)
    assert reloaded is not None
    assert tuple(entry.path for entry in reloaded.input_entries) == (
        "keep",
        "keep/inp",
    )
    kept_entry = reloaded.input_entries[1]
    assert isinstance(kept_entry, TaskInputFile)
    assert kept_entry.content == b"keep"


async def test_snapshot_survives_run_complete_and_fail_saves(
    uow_factory: Callable[[], PostgresUnitOfWork],
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

    # TO_DO → RUNNING keeps the snapshot.
    async with uow_factory() as uow:
        todo_task = await uow.tasks.get_todo(complete_id)
        assert todo_task is not None
        assert todo_task.input_entries == snapshot
        running = todo_task.run(node_id, "/remote/completes")
        await uow.tasks.save(running, expected_status=TaskStatus.TO_DO)
        await uow.commit()

    reloaded = await _reload(uow_factory, complete_id)
    assert reloaded is not None
    assert isinstance(reloaded.state, Running)
    assert reloaded.input_entries == snapshot

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
    assert reloaded.input_entries == snapshot

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
    assert reloaded.input_entries == snapshot
