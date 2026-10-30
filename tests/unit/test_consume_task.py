"""Unit tests for the V0 consume_task download/finalise/cleanup lifecycle."""
# region MODULE_CONTRACT
# PURPOSE: Verify consume_task's V0 contract: download via download_outputs, defer on any transient error, finalise on zero transient errors, persist before best-effort remote cleanup, release the tracker slot, and keep structured finalise/defer trajectory logs.
# SCOPE: consume_task success, permanent-only failure, transient-only deferral, mixed-error deferral, missing-engine failure, missing-task discard, persistence-before-cleanup ordering, and FINALISE_DECISION / RETRIEVAL_DEFER / FINALISE_OK / FINALISE_FAIL markers.
# KEYWORDS: consume_task, download, defer, finalise, cleanup, tracker, structured logs
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncssh.sftp import SFTPFailure

from tests.log_assertions import extra_fields
from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.consume_task import consume_task
from yascheduler.domain import (
    Task,
    TaskCompleted,
    TaskFailed,
    TaskId,
    TaskStatus,
    UnsafeTaskPathError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.application.uow import AbstractUnitOfWork
    from yascheduler.domain import EngineRepository

REMOTE_FOLDER = "/remote/tasks/20250101_120000_42"
OUTPUT_PATH = f"{REMOTE_FOLDER}/OUTPUT"
CONSUME_LOGGER = "yascheduler.application.consume_task"


@pytest.fixture
def mock_output_downloader() -> MagicMock:
    downloader = MagicMock()
    downloader.download_outputs = AsyncMock(return_value=([], []))
    downloader.cleanup_remote = AsyncMock(return_value=None)
    return downloader


def _make_uow(task: Task | None) -> AsyncMock:
    uow = AsyncMock()
    uow.tasks = AsyncMock()
    uow.tasks.get_running = AsyncMock(return_value=task)
    uow.tasks.save = AsyncMock()
    uow.commit = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    return uow


async def _run_consume(
    *,
    task_id: TaskId,
    session: SimpleNamespace,
    uow_factory: Callable[[], AbstractUnitOfWork],
    output_downloader: MagicMock,
    engines: EngineRepository,
    local_tasks_dir: Path,
    tracker: MagicMock,
) -> bool:
    return await consume_task(
        task_id=task_id,
        session=session,  # type: ignore[arg-type]
        output_downloader=output_downloader,
        engines=engines,
        uow_factory=uow_factory,
        local_tasks_dir=local_tasks_dir,
        tracker=tracker,
    )


def _marker_fields(caplog: pytest.LogCaptureFixture, marker: str) -> dict[str, object]:
    records = [record for record in caplog.records if record.getMessage() == marker]
    assert len(records) == 1
    fields = extra_fields(records[0])
    fields.pop("message", None)
    return fields


def _make_tracker() -> MagicMock:
    return MagicMock(spec=AllocationTracker)


class _OrderingHandler(logging.Handler):
    """Append finalise markers to the side-effect ordering trace as they are emitted."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__()
        self._trace = trace

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() in {"FINALISE_OK", "FINALISE_FAIL"}:
            self._trace.append(record.getMessage())


class TestConsumeTask:
    """consume_task V0 success, defer, and terminal-failure arcs."""

    async def test_success_saves_commits_discards_then_cleans_up(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No errors -> DONE saved and committed before tracker discard and cleanup."""
        uow = _make_uow(running_task)
        tracker = _make_tracker()
        session = SimpleNamespace()
        trace: list[str] = []

        async def save(task: Task) -> None:
            trace.append("save")

        async def commit() -> None:
            trace.append("commit")

        async def cleanup_remote(**_: object) -> None:
            trace.append("cleanup")

        uow.tasks.save.side_effect = save
        uow.commit.side_effect = commit
        tracker.discard.side_effect = lambda _task_id: trace.append("discard")
        mock_output_downloader.cleanup_remote.side_effect = cleanup_remote
        handler = _OrderingHandler(trace)
        logger = logging.getLogger(CONSUME_LOGGER)
        logger.addHandler(handler)
        try:
            with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
                result = await _run_consume(
                    task_id=running_task.task_id,
                    session=session,
                    uow_factory=MagicMock(return_value=uow),
                    output_downloader=mock_output_downloader,
                    engines=mock_engine_repo,
                    local_tasks_dir=tmp_path,
                    tracker=tracker,
                )
        finally:
            logger.removeHandler(handler)

        expected_store = str(tmp_path / "20250101_120000_42")
        assert result is True
        assert trace == ["save", "commit", "FINALISE_OK", "discard", "cleanup"]
        mock_output_downloader.download_outputs.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            local_dir=tmp_path / "20250101_120000_42",
            selections=["OUTPUT"],
            task_id=TaskId(1),
        )
        mock_output_downloader.cleanup_remote.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            task_id=TaskId(1),
        )

        uow.tasks.save.assert_awaited_once()
        saved_task = uow.tasks.save.call_args.args[0]
        assert saved_task.status is TaskStatus.DONE
        assert saved_task.state.error is None
        assert saved_task.local_folder == expected_store
        assert saved_task.state.remote_folder == REMOTE_FOLDER
        assert isinstance(saved_task.events[-1], TaskCompleted)
        assert saved_task.events[-1].local_folder == expected_store
        uow.commit.assert_awaited_once()
        tracker.discard.assert_called_once_with(TaskId(1))

        assert _marker_fields(caplog, "FINALISE_DECISION") == {
            "will_fail": False,
            "permanent": 0,
            "transient": 0,
        }
        assert _marker_fields(caplog, "FINALISE_OK") == {
            "task_id": 1,
            "label": "test",
            "local_folder": expected_store,
        }
        assert not any(
            record.getMessage() == "FINALISE_FAIL" for record in caplog.records
        )
        assert _marker_fields(caplog, "RETRIEVAL_ENTRIES") == {
            "task_id": 1,
            "transient": [],
            "permanent": [],
        }

    async def test_nested_tree_success_passes_raw_selections_and_finalises(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        engine,
        tmp_path: Path,
    ) -> None:
        """A nested output tree with no retrieval errors finalises the task as done."""
        nested_engine = replace(
            engine, output_files=("OUT.aiida", "OUT.aiida/**/*.dat")
        )
        mock_engine_repo.get.return_value = nested_engine
        uow = _make_uow(running_task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        result = await _run_consume(
            task_id=running_task.task_id,
            session=session,
            uow_factory=MagicMock(return_value=uow),
            output_downloader=mock_output_downloader,
            engines=mock_engine_repo,
            local_tasks_dir=tmp_path,
            tracker=tracker,
        )

        expected_store = str(tmp_path / "20250101_120000_42")
        assert result is True
        mock_output_downloader.download_outputs.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            local_dir=tmp_path / "20250101_120000_42",
            selections=["OUT.aiida", "OUT.aiida/**/*.dat"],
            task_id=TaskId(1),
        )
        saved_task = uow.tasks.save.call_args.args[0]
        assert saved_task.status is TaskStatus.DONE
        assert saved_task.state.error is None
        assert saved_task.local_folder == expected_store
        assert isinstance(saved_task.events[-1], TaskCompleted)
        uow.commit.assert_awaited_once()
        tracker.discard.assert_called_once_with(TaskId(1))
        mock_output_downloader.cleanup_remote.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            task_id=TaskId(1),
        )

    async def test_recorded_selections_win_over_changed_engine_config(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        engine,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Retrieval uses the accepted task's record, never the changed live config."""
        recorded = ("recorded/*.dat", "recorded.dat")
        task = replace(running_task, output_selections=recorded)
        mock_engine_repo.get.return_value = replace(
            engine, output_files=("config-after-submission.dat",)
        )
        uow = _make_uow(task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
            result = await _run_consume(
                task_id=task.task_id,
                session=session,
                uow_factory=MagicMock(return_value=uow),
                output_downloader=mock_output_downloader,
                engines=mock_engine_repo,
                local_tasks_dir=tmp_path,
                tracker=tracker,
            )

        assert result is True
        mock_output_downloader.download_outputs.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            local_dir=tmp_path / "20250101_120000_42",
            selections=list(recorded),
            task_id=TaskId(1),
        )
        assert _marker_fields(caplog, "OUTPUT_SELECTIONS_RESOLVED") == {
            "task_id": 1,
            "output_selections": list(recorded),
            "source": "task",
        }
        assert not any(
            "has no recorded output selections" in record.getMessage()
            for record in caplog.records
        )
        saved_task = uow.tasks.save.call_args.args[0]
        assert saved_task.status is TaskStatus.DONE
        assert saved_task.state.error is None
        assert saved_task.output_selections == recorded
        assert isinstance(saved_task.events[-1], TaskCompleted)

    async def test_task_without_record_uses_config_and_warns_with_identity(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        engine,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Legacy fallback is visible in the message and structured task identity."""
        configured = ("CONFIG/OUT.dat", "fallback.dat")
        mock_engine_repo.get.return_value = replace(engine, output_files=configured)
        assert running_task.output_selections == ()
        uow = _make_uow(running_task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
            result = await _run_consume(
                task_id=running_task.task_id,
                session=session,
                uow_factory=MagicMock(return_value=uow),
                output_downloader=mock_output_downloader,
                engines=mock_engine_repo,
                local_tasks_dir=tmp_path,
                tracker=tracker,
            )

        assert result is True
        mock_output_downloader.download_outputs.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            local_dir=tmp_path / "20250101_120000_42",
            selections=list(configured),
            task_id=TaskId(1),
        )
        fallback_records = [
            record
            for record in caplog.records
            if "has no recorded output selections" in record.getMessage()
        ]
        assert len(fallback_records) == 1
        message = fallback_records[0].getMessage()
        assert "task_id=1" in message
        assert "engine 'test_engine'" in message
        assert message == (
            "task_id=1 has no recorded output selections; "
            "using engine 'test_engine' configuration"
        )
        fallback_fields = extra_fields(fallback_records[0])
        fallback_fields.pop("message", None)
        assert fallback_fields == {
            "task_id": 1,
            "engine": "test_engine",
        }
        assert _marker_fields(caplog, "OUTPUT_SELECTIONS_RESOLVED") == {
            "task_id": 1,
            "output_selections": list(configured),
            "source": "engine",
        }
        saved_task = uow.tasks.save.call_args.args[0]
        assert saved_task.status is TaskStatus.DONE
        assert saved_task.state.error is None

    async def test_unmatched_pattern_is_permanent_and_names_the_selection(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A recorded unmatched selection remains a permanent error naming itself."""
        recorded_selection = "recorded/missing/*.orb"
        task = replace(running_task, output_selections=(recorded_selection,))
        permanent = [
            (recorded_selection, FileNotFoundError("selection matched no entries"))
        ]
        mock_output_downloader.download_outputs = AsyncMock(
            return_value=([], permanent)
        )
        uow = _make_uow(task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
            result = await _run_consume(
                task_id=task.task_id,
                session=session,
                uow_factory=MagicMock(return_value=uow),
                output_downloader=mock_output_downloader,
                engines=mock_engine_repo,
                local_tasks_dir=tmp_path,
                tracker=tracker,
            )

        expected_error = (
            f"Download error: {recorded_selection}: selection matched no entries"
        )
        assert result is True
        mock_output_downloader.download_outputs.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            local_dir=tmp_path / "20250101_120000_42",
            selections=[recorded_selection],
            task_id=TaskId(1),
        )
        saved_task = uow.tasks.save.call_args.args[0]
        assert saved_task.status is TaskStatus.DONE
        assert saved_task.state.error == expected_error
        assert isinstance(saved_task.events[-1], TaskFailed)
        assert saved_task.events[-1].reason == expected_error
        uow.commit.assert_awaited_once()
        tracker.discard.assert_called_once_with(TaskId(1))
        mock_output_downloader.cleanup_remote.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            task_id=TaskId(1),
        )
        assert _marker_fields(caplog, "FINALISE_DECISION") == {
            "will_fail": True,
            "permanent": 1,
            "transient": 0,
        }
        assert _marker_fields(caplog, "RETRIEVAL_ENTRIES") == {
            "task_id": 1,
            "transient": [],
            "permanent": [recorded_selection],
        }

    async def test_directory_creation_failure_is_transient_and_defers(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A transient directory-path failure leaves the task RUNNING without side effects."""
        transient = [("OUT/empty", SFTPFailure("directory creation failed"))]
        mock_output_downloader.download_outputs = AsyncMock(
            return_value=(transient, [])
        )
        uow = _make_uow(running_task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
            result = await _run_consume(
                task_id=running_task.task_id,
                session=session,
                uow_factory=MagicMock(return_value=uow),
                output_downloader=mock_output_downloader,
                engines=mock_engine_repo,
                local_tasks_dir=tmp_path,
                tracker=tracker,
            )

        assert result is False
        uow.tasks.save.assert_not_awaited()
        uow.commit.assert_not_awaited()
        tracker.discard.assert_not_called()
        mock_output_downloader.cleanup_remote.assert_not_awaited()
        assert _marker_fields(caplog, "RETRIEVAL_DEFER") == {
            "task_id": 1,
            "transient": 1,
            "permanent": 0,
        }
        assert _marker_fields(caplog, "RETRIEVAL_ENTRIES") == {
            "task_id": 1,
            "transient": ["OUT/empty"],
            "permanent": [],
        }
        assert not any(
            record.getMessage() == "FINALISE_DECISION" for record in caplog.records
        )

    async def test_retrieval_entries_record_reports_each_pass_exactly(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """RETRIEVAL_ENTRIES names every path-or-selection and an empty path for None."""
        transient = [
            (None, SFTPFailure("session lost")),
            ("OUT/empty", SFTPFailure("mkdir failed")),
        ]
        permanent = [
            ("missing/*.orb", FileNotFoundError("selection matched no entries")),
            ("OUT/run.log", UnsafeTaskPathError("OUT/run.log", "symbolic link")),
        ]
        mock_output_downloader.download_outputs = AsyncMock(
            return_value=(transient, permanent)
        )
        uow = _make_uow(running_task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
            result = await _run_consume(
                task_id=running_task.task_id,
                session=session,
                uow_factory=MagicMock(return_value=uow),
                output_downloader=mock_output_downloader,
                engines=mock_engine_repo,
                local_tasks_dir=tmp_path,
                tracker=tracker,
            )

        assert result is False
        assert _marker_fields(caplog, "RETRIEVAL_ENTRIES") == {
            "task_id": 1,
            "transient": ["", "OUT/empty"],
            "permanent": ["missing/*.orb", "OUT/run.log"],
        }
        assert _marker_fields(caplog, "RETRIEVAL_DEFER") == {
            "task_id": 1,
            "transient": 2,
            "permanent": 2,
        }
        uow.tasks.save.assert_not_awaited()
        uow.commit.assert_not_awaited()
        mock_output_downloader.cleanup_remote.assert_not_awaited()

    async def test_permanent_only_fails_with_formatted_download_error(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Permanent-only errors -> failed DONE task with the formatted download reason."""
        mock_output_downloader.download_outputs = AsyncMock(
            return_value=([], [(OUTPUT_PATH, OSError("disk full"))])
        )
        uow = _make_uow(running_task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
            result = await _run_consume(
                task_id=running_task.task_id,
                session=session,
                uow_factory=MagicMock(return_value=uow),
                output_downloader=mock_output_downloader,
                engines=mock_engine_repo,
                local_tasks_dir=tmp_path,
                tracker=tracker,
            )

        expected_error = f"Download error: {OUTPUT_PATH}: disk full"
        expected_store = str(tmp_path / "20250101_120000_42")
        assert result is True
        saved_task = uow.tasks.save.call_args.args[0]
        assert saved_task.status is TaskStatus.DONE
        assert saved_task.state.error == expected_error
        assert saved_task.local_folder == expected_store
        assert saved_task.state.remote_folder == REMOTE_FOLDER
        assert isinstance(saved_task.events[-1], TaskFailed)
        assert saved_task.events[-1].reason == expected_error
        uow.commit.assert_awaited_once()
        tracker.discard.assert_called_once_with(TaskId(1))
        mock_output_downloader.cleanup_remote.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            task_id=TaskId(1),
        )
        assert _marker_fields(caplog, "FINALISE_DECISION") == {
            "will_fail": True,
            "permanent": 1,
            "transient": 0,
        }
        assert _marker_fields(caplog, "FINALISE_FAIL") == {
            "task_id": 1,
            "label": "test",
            "local_folder": expected_store,
            "error": expected_error,
        }

    @pytest.mark.parametrize(
        ("transient", "permanent", "expected_permanent"),
        [
            pytest.param(
                [(OUTPUT_PATH, SFTPFailure("connection lost"))],
                [],
                0,
                id="transient-only",
            ),
            pytest.param(
                [(OUTPUT_PATH, SFTPFailure("connection lost"))],
                [(f"{REMOTE_FOLDER}/OTHER", OSError("permission denied"))],
                1,
                id="transient-and-permanent",
            ),
        ],
    )
    async def test_transient_error_defers_without_terminal_side_effects(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        transient: list[tuple[str, Exception]],
        permanent: list[tuple[str, Exception]],
        expected_permanent: int,
    ) -> None:
        """Any transient error defers (False) with no save, commit, discard, or cleanup."""
        mock_output_downloader.download_outputs = AsyncMock(
            return_value=(transient, permanent)
        )
        uow = _make_uow(running_task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
            result = await _run_consume(
                task_id=running_task.task_id,
                session=session,
                uow_factory=MagicMock(return_value=uow),
                output_downloader=mock_output_downloader,
                engines=mock_engine_repo,
                local_tasks_dir=tmp_path,
                tracker=tracker,
            )

        assert result is False
        uow.tasks.save.assert_not_awaited()
        uow.commit.assert_not_awaited()
        tracker.discard.assert_not_called()
        mock_output_downloader.cleanup_remote.assert_not_awaited()
        assert _marker_fields(caplog, "RETRIEVAL_DEFER") == {
            "task_id": 1,
            "transient": len(transient),
            "permanent": expected_permanent,
        }
        assert not any(
            record.getMessage() == "FINALISE_DECISION" for record in caplog.records
        )

    async def test_missing_engine_fails_and_cleans_up(
        self,
        mock_output_downloader: MagicMock,
        running_task: Task,
        mock_engine_repo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A recorded task still fails when its engine is removed from config."""
        mock_engine_repo.get.return_value = None
        task = replace(running_task, output_selections=("recorded.dat",))
        uow = _make_uow(task)
        tracker = _make_tracker()
        session = SimpleNamespace()

        with caplog.at_level(logging.DEBUG, logger=CONSUME_LOGGER):
            result = await _run_consume(
                task_id=task.task_id,
                session=session,
                uow_factory=MagicMock(return_value=uow),
                output_downloader=mock_output_downloader,
                engines=mock_engine_repo,
                local_tasks_dir=tmp_path,
                tracker=tracker,
            )

        expected_error = f"engine '{task.engine}' no longer in config"
        assert result is True
        mock_output_downloader.download_outputs.assert_not_awaited()
        saved_task = uow.tasks.save.call_args.args[0]
        assert saved_task.status is TaskStatus.DONE
        assert saved_task.state.error == expected_error
        assert saved_task.local_folder == ""
        assert saved_task.state.remote_folder == REMOTE_FOLDER
        assert isinstance(saved_task.events[-1], TaskFailed)
        assert saved_task.events[-1].reason == expected_error
        uow.commit.assert_awaited_once()
        tracker.discard.assert_called_once_with(TaskId(1))
        mock_output_downloader.cleanup_remote.assert_awaited_once_with(
            session=session,
            remote_dir=REMOTE_FOLDER,
            task_id=TaskId(1),
        )
        assert _marker_fields(caplog, "FINALISE_FAIL") == {
            "task_id": 1,
            "label": "test",
            "local_folder": "",
            "error": expected_error,
        }
        assert not any(
            record.getMessage() == "FINALISE_DECISION" for record in caplog.records
        )

    async def test_missing_task_discards_tracker_and_returns_true(
        self,
        mock_output_downloader: MagicMock,
        mock_engine_repo: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A missing/non-running task is vacuously finalised and releases its tracker slot."""
        uow = _make_uow(None)
        tracker = _make_tracker()
        session = SimpleNamespace()

        result = await _run_consume(
            task_id=TaskId(1),
            session=session,
            uow_factory=MagicMock(return_value=uow),
            output_downloader=mock_output_downloader,
            engines=mock_engine_repo,
            local_tasks_dir=tmp_path,
            tracker=tracker,
        )

        assert result is True
        uow.tasks.get_running.assert_awaited_once_with(TaskId(1))
        uow.tasks.save.assert_not_awaited()
        uow.commit.assert_not_awaited()
        tracker.discard.assert_called_once_with(TaskId(1))
        mock_output_downloader.download_outputs.assert_not_awaited()
        mock_output_downloader.cleanup_remote.assert_not_awaited()
