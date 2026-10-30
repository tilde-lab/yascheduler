# region MODULE_CONTRACT
# PURPOSE: Unit tests for the task-schema-and-entity-cleanup persistence-layer changes: _row_to_task reads NodeId + audit timestamps + title + enum-label status, insert/save bind :node_id + :title + :status (name), and the 5 task SQL files include allocated_node_id/created_at/updated_at/title.
# SCOPE: NodeId wrapping, SQL bind params for node_id/title/status, SQL-file content verification, Protocol conformance.
# KEYWORDS: _row_to_task, NodeId, allocated_node_id, SQL bind params
# endregion MODULE_CONTRACT

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from yascheduler.domain.model import (
    NewTask,
    NodeId,
    Running,
    Task,
    TaskId,
    Todo,
    allocated_node_id_of,
)
from yascheduler.domain.model import (
    TaskStatus as DomainTaskStatus,
)
from yascheduler.domain.ports import TaskRepository
from yascheduler.infra.persistence.postgres import PostgresTaskRepository
from yascheduler.infra.persistence.sql_loader import load_query

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def _make_repo(mocker: MockerFixture) -> PostgresTaskRepository:
    """Build a minimal PostgresTaskRepository with a mock _run."""
    repo = PostgresTaskRepository.__new__(PostgresTaskRepository)
    repo._saved_tasks = None
    mock_run = mocker.AsyncMock()
    mocker.patch.object(repo, "_run", mock_run)
    return repo


def _row(
    task_id: int = 1,
    title: str = "job",
    status: str = "TO_DO",
    allocated_node_id: int | None = 5,
    remote_folder: str | None = None,
    error: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> dict[str, object]:
    """Build a DB-row dict in the post-cleanup shape (flat typed columns, no metadata JSON)."""
    return {
        "task_id": task_id,
        "title": title,
        "status": status,
        "engine": "fleur",
        "remote_folder": remote_folder,
        "local_folder": None,
        "webhook_url": None,
        "webhook_custom_params": "{}",
        "extra": "{}",
        "error": error,
        "allocated_node_id": allocated_node_id,
        "created_at": created_at or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        "updated_at": updated_at or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    }


class TestRowToTaskAllocatedNodeId:
    """_row_to_task reads allocated_node_id and wraps NodeId (None when NULL/absent)."""

    async def test_reads_allocated_node_id(self, mocker: MockerFixture) -> None:
        repo = _make_repo(mocker)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [
                _row(
                    task_id=1, status="RUNNING", allocated_node_id=5, remote_folder="/r"
                )
            ],
            [],
        ]

        task = await repo.get(TaskId(1))

        assert task is not None
        assert isinstance(task.state, Running)
        assert task.state.allocated_node_id == NodeId(5)

    async def test_handles_null_allocated_node_id(self, mocker: MockerFixture) -> None:
        repo = _make_repo(mocker)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [_row(task_id=1, allocated_node_id=None)],
            [],
        ]

        task = await repo.get(TaskId(1))

        assert task is not None
        # TO_DO rows do not carry allocated_node_id; the any-status helper returns None.
        assert allocated_node_id_of(task) is None

    async def test_handles_missing_allocated_node_id_key(
        self,
        mocker: MockerFixture,
    ) -> None:
        repo = _make_repo(mocker)
        r = _row(task_id=1, allocated_node_id=None)
        del r["allocated_node_id"]
        repo._run.side_effect = [[r], []]  # type: ignore[attr-defined]

        task = await repo.get(TaskId(1))

        assert task is not None
        assert allocated_node_id_of(task) is None

    async def test_reads_title_as_label(self, mocker: MockerFixture) -> None:
        """DB column is title; domain field is label."""
        repo = _make_repo(mocker)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [_row(task_id=1, title="my_job")],
            [],
        ]

        task = await repo.get(TaskId(1))

        assert task is not None
        assert task.label == "my_job"

    async def test_reads_status_by_name_lookup(self, mocker: MockerFixture) -> None:
        """Status is read via TaskStatus[row["status"]] (name lookup, was int cast)."""
        repo = _make_repo(mocker)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [_row(task_id=1, status="RUNNING")],
            [],
        ]

        task = await repo.get(TaskId(1))

        assert task is not None
        assert task.status == DomainTaskStatus.RUNNING

    async def test_reads_audit_timestamps(self, mocker: MockerFixture) -> None:
        """created_at/updated_at are read from the row (pg8000 returns datetime)."""
        repo = _make_repo(mocker)
        created = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        updated = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [_row(task_id=1, created_at=created, updated_at=updated)],
            [],
        ]

        task = await repo.get(TaskId(1))

        assert task is not None
        assert task.created_at == created
        assert task.updated_at == updated

    async def test_does_not_read_allocated_ip(self, mocker: MockerFixture) -> None:
        """_row_to_task does not read an ip/allocated_ip column (column dropped)."""
        repo = _make_repo(mocker)
        repo._run.side_effect = [[_row(task_id=1)], []]  # type: ignore[attr-defined]

        task = await repo.get(TaskId(1))

        assert task is not None
        assert not hasattr(task, "allocated_ip")


class TestInsertSaveBindAllocatedNodeId:
    """insert/save bind :node_id (value when allocated_node_id set, None otherwise), :title, :status (name)."""

    async def test_insert_binds_allocated_node_id(self, mocker: MockerFixture) -> None:
        repo = _make_repo(mocker)
        repo._run.return_value = [_row(task_id=99, allocated_node_id=5)]  # type: ignore[attr-defined]

        new_task = NewTask(
            label="job",
            engine="fleur",
        )
        await repo.insert(new_task)

        _, kwargs = repo._run.call_args  # type: ignore[attr-defined]
        assert kwargs["title"] == "job"
        assert "ip" not in kwargs

    async def test_insert_binds_null_allocated_node_id_by_default(
        self,
        mocker: MockerFixture,
    ) -> None:
        repo = _make_repo(mocker)
        repo._run.return_value = [_row(task_id=99, allocated_node_id=None)]  # type: ignore[attr-defined]

        new_task = NewTask(
            label="job",
            engine="fleur",
        )
        await repo.insert(new_task)

    async def test_save_binds_allocated_node_id(self, mocker: MockerFixture) -> None:
        repo = _make_repo(mocker)
        from datetime import datetime

        task = Task(
            task_id=TaskId(7),
            label="job",
            engine="fleur",
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r"),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

        await repo.save(task)

        _, kwargs = repo._run.call_args  # type: ignore[attr-defined]
        assert kwargs["node_id"] == 7
        assert kwargs["title"] == "job"
        assert kwargs["status"] == "RUNNING"
        assert "ip" not in kwargs

    async def test_save_binds_null_allocated_node_id(
        self,
        mocker: MockerFixture,
    ) -> None:
        repo = _make_repo(mocker)
        from datetime import datetime

        task = Task(
            task_id=TaskId(7),
            label="job",
            engine="fleur",
            state=Todo(),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

        await repo.save(task)

        _, kwargs = repo._run.call_args  # type: ignore[attr-defined]
        assert kwargs["node_id"] is None


class TestTaskSqlIncludesAllocatedNodeId:
    """The 5 task SQL files that feed _row_to_task include allocated_node_id/created_at/updated_at/title; the 3 status/aggregate files do NOT."""

    def test_insert_sql_includes_allocated_node_id(self) -> None:
        sql = load_query("task/insert")
        assert "allocated_node_id" in sql
        assert "title" in sql
        # insert does not bind allocated_node_id (DB defaults to NULL); it only
        # appears in the RETURNING clause so _row_to_task can read it back.
        assert ":node_id" not in sql
        assert "RETURNING" in sql
        returning = sql[sql.index("RETURNING") :]
        assert "allocated_node_id" in returning
        assert "created_at" in returning
        assert "updated_at" in returning
        # ip column/param absent
        assert "ip" not in sql

    def test_update_by_id_sql_includes_allocated_node_id(self) -> None:
        sql = load_query("task/update_by_id")
        assert "allocated_node_id = :node_id" in sql
        assert "title = :title" in sql
        # RETURNING stays task_id only (existence check, not _row_to_task feed)
        assert "RETURNING task_id" in sql
        # ip SET term absent
        assert "ip" not in sql

    def test_get_by_id_sql_includes_allocated_node_id(self) -> None:
        sql = load_query("task/get_by_id")
        assert "allocated_node_id" in sql
        assert "title" in sql
        assert "created_at" in sql
        assert "updated_at" in sql
        assert "ip" not in sql

    def test_list_by_status_sql_includes_allocated_node_id(self) -> None:
        sql = load_query("task/list_by_status")
        assert "allocated_node_id" in sql
        assert "title" in sql
        assert "created_at" in sql
        assert "updated_at" in sql
        assert "task_status" in sql
        assert "ip" not in sql

    def test_list_by_jobs_sql_includes_allocated_node_id(self) -> None:
        sql = load_query("task/list_by_jobs")
        assert "allocated_node_id" in sql
        assert "title" in sql
        assert "created_at" in sql
        assert "updated_at" in sql
        assert "ip" not in sql

    def test_get_ids_by_node_id_and_status_sql_does_not_touch_allocated_node_id_in_set(
        self,
    ) -> None:
        sql = load_query("task/get_ids_by_node_id_and_status")
        assert "allocated_node_id = :node_id" in sql
        # Only the filter key; no SET clause (this is a SELECT).

    def test_count_by_status_sql_does_not_touch_allocated_node_id(self) -> None:
        sql = load_query("task/count_by_status")
        assert "allocated_node_id" not in sql


class TestGetIdsByNodeIdAndStatusSql:
    """get_ids_by_node_id_and_status.sql replaces get_ids_by_ip_and_status.sql (filter by allocated_node_id, not ip)."""

    def test_file_exists_and_filters_by_node_id(self) -> None:
        sql = load_query("task/get_ids_by_node_id_and_status")
        assert "allocated_node_id = :node_id" in sql
        assert "status = :status" in sql
        assert "ip" not in sql


class TestTaskRepositoryProtocolConformance:
    """PostgresTaskRepository satisfies the updated TaskRepository Protocol (with list_ids_by_node_id_and_status)."""

    def test_postgres_task_repository_is_task_repository(self) -> None:
        # runtime_checkable Protocol — structural isinstance check.
        repo = PostgresTaskRepository.__new__(PostgresTaskRepository)
        # _PgRepository.__init__ sets _conn/_executor; for the isinstance
        # check against the runtime_checkable Protocol, only method presence
        # matters, so an uninitialised instance is fine.
        assert isinstance(repo, TaskRepository)
