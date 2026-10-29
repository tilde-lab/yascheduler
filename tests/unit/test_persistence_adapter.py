# region MODULE_CONTRACT
# PURPOSE: Unit tests for yascheduler.infra.persistence.
# SCOPE: load_query file-reading and caching behaviour; PostgresUnitOfWork lifecycle; PostgresTaskRepository and PostgresNodeRepository CRUD via fake _run.
# KEYWORDS: load_query, PostgresUnitOfWork, CRUD, fake _run
# endregion MODULE_CONTRACT

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from yascheduler.application.message_bus import MessageBus
from yascheduler.domain.model import (
    NewTask,
    NodeId,
    Running,
    Task,
    TaskId,
    Todo,
)
from yascheduler.domain.model import (
    TaskStatus as DomainTaskStatus,
)
from yascheduler.infra.persistence.exceptions import (
    TaskRowNotFoundError,
    UnitOfWorkNotInitializedError,
)
from yascheduler.infra.persistence.postgres import (
    PostgresNodeRepository,
    PostgresTaskRepository,
)
from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork
from yascheduler.infra.persistence.sql_loader import load_query


def _make_task_row(**overrides: Any) -> dict[str, Any]:
    """Build a fake _run row dict for a Task with sensible defaults; overrides win."""
    base = {
        "task_id": 1,
        "title": "test_label",
        "status": "TO_DO",
        "engine": "fleur",
        "remote_folder": None,
        "local_folder": None,
        "webhook_url": None,
        "webhook_custom_params": "{}",
        "extra": "{}",
        "error": None,
        "allocated_node_id": None,
        "created_at": None,
        "updated_at": None,
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_load_query_first_call_reads_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_query reads the file on first call and returns its content."""
    load_query.cache_clear()
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir(parents=True)
    sql_file = sql_dir / "task" / "get_by_id.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELECT * FROM tasks WHERE id = :task_id")

    monkeypatch.setattr("yascheduler.infra.persistence.sql_loader._SQL_DIR", sql_dir)

    result = load_query("task/get_by_id")
    assert result == "SELECT * FROM tasks WHERE id = :task_id"

    # File change after first call should NOT affect future results (cached).
    sql_file.write_text("MUTATED")
    assert load_query("task/get_by_id") == "SELECT * FROM tasks WHERE id = :task_id"


@pytest.mark.unit
def test_load_query_second_call_uses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_query returns the cached value; file mutation has no effect."""
    load_query.cache_clear()
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir(parents=True)
    sql_file = sql_dir / "node" / "list_enabled.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELECT * FROM nodes WHERE enabled = TRUE")

    monkeypatch.setattr("yascheduler.infra.persistence.sql_loader._SQL_DIR", sql_dir)

    # First call — reads from disk.
    result_a = load_query("node/list_enabled")
    assert result_a == "SELECT * FROM nodes WHERE enabled = TRUE"

    # Mutate the file.
    sql_file.write_text("SELECT 1")

    # Second call — should return cached value.
    result_b = load_query("node/list_enabled")
    assert result_b == "SELECT * FROM nodes WHERE enabled = TRUE"
    assert result_b != "SELECT 1"


async def test_uow_enter_creates_repositories(mocker: MockerFixture) -> None:
    """__aenter__ instantiates both task and node repositories."""
    mock_conn = mocker.MagicMock()
    mocker.patch(
        "yascheduler.infra.persistence.postgres_uow.Connection",
        return_value=mock_conn,
    )
    config = mocker.MagicMock()
    bus = MessageBus()
    uow = PostgresUnitOfWork(config, bus)

    async with uow:
        assert isinstance(uow.tasks, PostgresTaskRepository)
        assert isinstance(uow.nodes, PostgresNodeRepository)


async def test_collect_events_preserves_shared_list(mocker: MockerFixture) -> None:
    """collect_events preserves shared list reference between UoW and repo."""
    mock_conn = mocker.MagicMock()
    mocker.patch(
        "yascheduler.infra.persistence.postgres_uow.Connection",
        return_value=mock_conn,
    )
    config = mocker.MagicMock()
    bus = MessageBus()
    uow = PostgresUnitOfWork(config, bus)

    async with uow:
        assert uow.tasks._saved_tasks is uow._saved_tasks
        await uow.collect_events()
        assert uow.tasks._saved_tasks is uow._saved_tasks


async def test_uow_commit_called(mocker: MockerFixture) -> None:
    """commit() calls connection.run('COMMIT')."""
    mock_conn = mocker.MagicMock()
    mocker.patch(
        "yascheduler.infra.persistence.postgres_uow.Connection",
        return_value=mock_conn,
    )
    config = mocker.MagicMock()
    bus = MessageBus()
    uow = PostgresUnitOfWork(config, bus)

    async with uow:
        await uow.commit()

    mock_conn.run.assert_any_call("COMMIT")


async def test_uow_rollback_on_exception(mocker: MockerFixture) -> None:
    """Exception inside context triggers rollback and closes connection."""
    mock_conn = mocker.MagicMock()
    mocker.patch(
        "yascheduler.infra.persistence.postgres_uow.Connection",
        return_value=mock_conn,
    )
    config = mocker.MagicMock()
    bus = MessageBus()
    uow = PostgresUnitOfWork(config, bus)

    with pytest.raises(ValueError):
        async with uow:
            raise ValueError("test error")

    mock_conn.run.assert_any_call("ROLLBACK")
    mock_conn.close.assert_called_once()


async def test_uow_closes_connection(mocker: MockerFixture) -> None:
    """connection.close() is called on normal exit."""
    mock_conn = mocker.MagicMock()
    mocker.patch(
        "yascheduler.infra.persistence.postgres_uow.Connection",
        return_value=mock_conn,
    )
    config = mocker.MagicMock()
    bus = MessageBus()
    uow = PostgresUnitOfWork(config, bus)

    async with uow:
        pass

    mock_conn.close.assert_called_once()


async def test_uow_commit_after_exit_raises(mocker: MockerFixture) -> None:
    """commit() raises UnitOfWorkNotInitializedError when called outside 'async with' block."""
    mock_conn = mocker.MagicMock()
    mocker.patch(
        "yascheduler.infra.persistence.postgres_uow.Connection",
        return_value=mock_conn,
    )
    config = mocker.MagicMock()
    bus = MessageBus()
    uow = PostgresUnitOfWork(config, bus)

    async with uow:
        pass  # connection now closed by __aexit__

    with pytest.raises(
        UnitOfWorkNotInitializedError,
        match="Connection not initialized",
    ):
        await uow.commit()


async def test_uow_double_commit(mocker: MockerFixture) -> None:
    """Second commit within the same context is accepted by pg8000 (idempotent)."""
    mock_conn = mocker.MagicMock()
    mocker.patch(
        "yascheduler.infra.persistence.postgres_uow.Connection",
        return_value=mock_conn,
    )
    config = mocker.MagicMock()
    bus = MessageBus()
    uow = PostgresUnitOfWork(config, bus)

    async with uow:
        await uow.commit()
        await uow.commit()

    # Both commits executed
    assert mock_conn.run.call_count >= 2  # BEGIN + COMMIT + COMMIT
    commit_calls = [c for c in mock_conn.run.call_args_list if c[0][0] == "COMMIT"]
    assert len(commit_calls) == 2


# ============================================================================
# PostgresTaskRepository — unit tests with mocked _run
# ============================================================================


class TestPostgresTaskRepository:
    """PostgresTaskRepository CRUD operations via fake in-memory _run."""

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _make_repo(mocker: MockerFixture) -> PostgresTaskRepository:
        """Build a minimal PostgresTaskRepository with a mock _run."""
        repo = PostgresTaskRepository.__new__(PostgresTaskRepository)
        repo._saved_tasks = None
        mock_run = mocker.AsyncMock()
        mocker.patch.object(repo, "_run", mock_run)
        return repo

    # -- get -------------------------------------------------------------------

    async def test_get_returns_task(self, mocker: MockerFixture) -> None:
        """Get returns a Task hydrated from the row returned by _run."""
        repo = self._make_repo(mocker)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [
                _make_task_row(
                    task_id=42,
                    status="RUNNING",
                    allocated_node_id=5,
                    remote_folder="/r",
                )
            ],
            [],
        ]

        task = await repo.get(TaskId(42))

        assert task is not None
        assert task.task_id == TaskId(42)
        assert task.label == "test_label"
        assert not hasattr(task, "allocated_ip")
        assert task.engine == "fleur"
        assert task.status == DomainTaskStatus.RUNNING

    async def test_get_returns_none_when_not_found(self, mocker: MockerFixture) -> None:
        """Get returns None when _run returns an empty list."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        task = await repo.get(TaskId(999))

        assert task is None

    async def test_get_with_none_ip_and_extra_fields(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Get handles null allocated_node_id and extra metadata fields."""
        repo = self._make_repo(mocker)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [
                _make_task_row(
                    task_id=1,
                    title="no-ip-task",
                    engine="cp2k",
                    remote_folder="/remote/path",
                    local_folder="/local/path",
                    webhook_url="https://hook.example.com",
                    webhook_custom_params=json.dumps({"key": "val"}),
                    extra=json.dumps({"extra_field": "extra_val"}),
                )
            ],
            [],
        ]

        task = await repo.get(TaskId(1))

        assert task is not None
        assert task.task_id == TaskId(1)
        assert task.label == "no-ip-task"
        assert not hasattr(task, "allocated_ip")
        assert task.status == DomainTaskStatus.TO_DO
        assert task.engine == "cp2k"
        assert task.state.remote_folder == "/remote/path"
        # local_folder lives on Done state only; a TO_DO task does not carry it.
        assert not hasattr(task.state, "local_folder")
        assert task.webhook_url == "https://hook.example.com"
        assert task.webhook_custom_params == {"key": "val"}
        assert task.extra == {"extra_field": "extra_val"}

    # -- insert ----------------------------------------------------------------

    async def test_insert_returns_task_with_id(self, mocker: MockerFixture) -> None:
        """Insert runs INSERT SQL and returns Task with generated ID."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [_make_task_row(task_id=99, title="label")]  # type: ignore[attr-defined]

        new_task = NewTask(
            label="label",
            engine="fleur",
        )
        result = await repo.insert(new_task)

        assert result.task_id == TaskId(99)
        assert result.label == "label"
        assert "INSERT INTO yascheduler_tasks" in repo._run.call_args[0][0]  # type: ignore[attr-defined]

    # -- save ------------------------------------------------------------------

    async def test_save_calls_update_by_id(self, mocker: MockerFixture) -> None:
        """Save calls _run with the update_by_id query and all task fields."""
        repo = self._make_repo(mocker)
        from datetime import datetime

        task = Task(
            task_id=TaskId(7),
            label="my-job",
            engine="fleur",
            state=Todo(remote_folder="/remote"),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

        await repo.save(task)

        repo._run.assert_awaited_once()  # type: ignore[attr-defined]
        call_args = repo._run.call_args  # type: ignore[attr-defined]
        assert call_args is not None
        (sql,), kwargs = call_args
        assert sql == load_query("task/update_by_id")
        assert kwargs["task_id"] == 7
        assert kwargs["title"] == "my-job"
        assert kwargs["status"] == "TO_DO"
        assert "ip" not in kwargs
        assert kwargs["engine"] == "fleur"
        assert kwargs["remote_folder"] == "/remote"
        # default expected_status=None preserves the unconditional-write behavior
        assert kwargs["expected_status"] is None

    async def test_save_with_expected_status_passes_guard_param(
        self, mocker: MockerFixture
    ) -> None:
        """save(expected_status=...) forwards the status name to the SQL guard."""
        repo = self._make_repo(mocker)
        from datetime import datetime

        task = Task(
            task_id=TaskId(9),
            label="claim",
            engine="fleur",
            state=Running(allocated_node_id=NodeId(1), remote_folder="/r"),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

        await repo.save(task, expected_status=DomainTaskStatus.TO_DO)

        _, kwargs = repo._run.call_args  # type: ignore[attr-defined]
        assert kwargs["expected_status"] == "TO_DO"

    async def test_save_with_expected_status_raises_on_zero_rows(
        self, mocker: MockerFixture
    ) -> None:
        """save(expected_status=...) raises TaskRowNotFoundError when the guard rejects (0 rows)."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]
        from datetime import datetime

        task = Task(
            task_id=TaskId(11),
            label="lost-update",
            engine="fleur",
            state=Running(allocated_node_id=NodeId(1), remote_folder="/r"),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

        with pytest.raises(TaskRowNotFoundError):
            await repo.save(task, expected_status=DomainTaskStatus.TO_DO)

    async def test_save_running_task(self, mocker: MockerFixture) -> None:
        """Save persists a RUNNING task with its allocated_node_id."""
        repo = self._make_repo(mocker)
        from datetime import datetime

        task = Task(
            task_id=TaskId(3),
            label="running-job",
            engine="vasp",
            state=Running(allocated_node_id=NodeId(5), remote_folder="/r"),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

        await repo.save(task)

        _, kwargs = repo._run.call_args  # type: ignore[attr-defined]
        assert kwargs["status"] == "RUNNING"
        assert kwargs["node_id"] == 5
        assert "ip" not in kwargs

    # -- list_by_status --------------------------------------------------------

    async def test_list_by_status_returns_tasks(self, mocker: MockerFixture) -> None:
        """list_by_status returns a Task for each row."""
        repo = self._make_repo(mocker)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [
                _make_task_row(task_id=1, title="a"),
                _make_task_row(
                    task_id=2,
                    title="b",
                    status="RUNNING",
                    engine="cp2k",
                    allocated_node_id=5,
                    remote_folder="/r",
                ),
            ],
            [],
        ]

        tasks = await repo.list_by_status(
            {DomainTaskStatus.TO_DO, DomainTaskStatus.RUNNING},
        )

        assert len(tasks) == 2
        assert tasks[0].task_id == TaskId(1)
        assert tasks[0].status == DomainTaskStatus.TO_DO
        assert tasks[1].task_id == TaskId(2)
        assert tasks[1].status == DomainTaskStatus.RUNNING

    async def test_list_by_status_empty(self, mocker: MockerFixture) -> None:
        """list_by_status returns empty list when no rows."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        tasks = await repo.list_by_status({DomainTaskStatus.DONE})

        assert tasks == []

    # -- list_by_jobs ----------------------------------------------------------

    async def test_list_by_jobs_returns_tasks(self, mocker: MockerFixture) -> None:
        """list_by_jobs returns tasks matching the given job ids."""
        repo = self._make_repo(mocker)
        repo._run.side_effect = [  # type: ignore[attr-defined]
            [
                _make_task_row(task_id=10, title="job-x", status="DONE"),
                _make_task_row(task_id=20, title="job-y", status="DONE", engine="cp2k"),
            ],
            [],
        ]

        tasks = await repo.list_by_jobs([TaskId(10), TaskId(20)])

        assert len(tasks) == 2
        assert tasks[0].task_id == TaskId(10)
        assert tasks[0].status == DomainTaskStatus.DONE
        assert tasks[1].task_id == TaskId(20)

    async def test_list_by_jobs_empty(self, mocker: MockerFixture) -> None:
        """list_by_jobs returns empty list when _run returns no rows."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        tasks = await repo.list_by_jobs([TaskId(999)])

        assert tasks == []

    # -- count_by_status -------------------------------------------------------

    async def test_count_by_status_returns_mapping(self, mocker: MockerFixture) -> None:
        """count_by_status returns a mapping of TaskStatus -> count (name lookup)."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            {"status": "TO_DO", "count": 5},
            {"status": "RUNNING", "count": 3},
            {"status": "DONE", "count": 10},
        ]

        counts = await repo.count_by_status()

        assert counts == {
            DomainTaskStatus.TO_DO: 5,
            DomainTaskStatus.RUNNING: 3,
            DomainTaskStatus.DONE: 10,
        }

    async def test_count_by_status_empty(self, mocker: MockerFixture) -> None:
        """count_by_status returns empty mapping when no rows."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        counts = await repo.count_by_status()

        assert counts == {}
