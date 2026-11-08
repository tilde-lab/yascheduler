# region MODULE_CONTRACT
# PURPOSE: Unit tests for the _write_remote_file exception contract and the _exec_spawn_command SPAWN log contract under the content-free logging rule.
# SCOPE: Non-SFTP exception propagates (not swallowed); asyncssh.misc.Error logs the SFTPError marker with path/code/failure class but no reason or traceback, then re-raises; start_task_on_machine records UPLOAD_INPUTS_FAILED and aborts spawn on upload failure; successful write returns normally and the stored snapshot upload loop writes every file and materializes directory entries; _exec_spawn_command emits SPAWN debug log with hostname/task_id/cmd/cwd.
# KEYWORDS: _write_remote_file, exception contract, spawn abort, spawn log
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import asyncssh
import pytest
from asyncssh.sftp import FILEXFER_TYPE_DIRECTORY, FX_NO_SUCH_FILE

from tests.log_assertions import extra_fields
from yascheduler.domain import (
    Engine,
    TaskInputDirectory,
    TaskInputFile,
    normalize_input_snapshot,
)
from yascheduler.domain.model import NodeId, Running, Task, TaskId
from yascheduler.infra.ssh.operations import TaskDeployer
from yascheduler.infra.ssh.operations.deployment import _write_remote_file

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from asyncssh.sftp import SFTPClient

    from yascheduler.domain import TaskInputEntry
    from yascheduler.infra.ssh.session import SSHMachineSession


# =============================================================================
# Helpers
# =============================================================================


class _FakeSFTPFile:
    """Async context manager mimicking asyncssh SFTPFile — write() is configurable."""

    def __init__(self, write_side_effect: BaseException | None = None) -> None:
        self._write_side_effect = write_side_effect
        self.write = AsyncMock(side_effect=write_side_effect)

    async def __aenter__(self) -> _FakeSFTPFile:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSFTPClient:
    """SFTP client whose open() returns a configured _FakeSFTPFile context manager.

    ``lstat`` reports the directories created through ``makedirs`` as
    FILEXFER_TYPE_DIRECTORY and every other path as absent, mirroring the
    destination-confinement checks _upload_task_data performs before writing.
    """

    def __init__(
        self,
        file: _FakeSFTPFile,
        open_side_effect: BaseException | None = None,
    ) -> None:
        self._file = file
        self.directories: set[str] = set()
        self.open = (
            MagicMock(return_value=file)
            if open_side_effect is None
            else MagicMock(side_effect=open_side_effect)
        )
        self.makedirs = AsyncMock(side_effect=self._record_directory)
        self.realpath = AsyncMock(return_value="/root")

    async def _record_directory(self, path: str, exist_ok: bool = False) -> None:
        self.directories.add(path)

    async def lstat(self, path: str) -> SimpleNamespace:
        if path in self.directories:
            return SimpleNamespace(type=FILEXFER_TYPE_DIRECTORY)
        raise asyncssh.misc.Error(FX_NO_SUCH_FILE, "No such file")


def _make_sftp_state(
    write_side_effect: BaseException | None = None,
    open_side_effect: BaseException | None = None,
) -> tuple[SSHMachineSession, _FakeSFTPClient]:
    """Build an SSHMachineSession wired so its SFTP session yields a _FakeSFTPClient.

    The _FakeSFTPFile.write() raises ``write_side_effect`` (if set) or returns
    None (success path). If ``open_side_effect`` is set, sftp.open() raises
    before the file context is entered (exercises the same try block). The
    connection's start_sftp_client returns the same _FakeSFTPClient on every
    entry so _upload_task_data and start_task_on_machine see a consistent mock.
    """
    from tests.unit.test_ssh_gateway import _make_state

    file = _FakeSFTPFile(write_side_effect=write_side_effect)
    sftp = _FakeSFTPClient(file, open_side_effect=open_side_effect)

    state = _make_state()

    @asynccontextmanager
    async def _sftp_ctx() -> AsyncGenerator[_FakeSFTPClient, None]:
        yield sftp

    state._conn.start_sftp_client = _sftp_ctx  # type: ignore[assignment]
    return state, sftp


def _find_record(caplog: pytest.LogCaptureFixture, marker: str) -> logging.LogRecord:
    records = [record for record in caplog.records if marker in record.getMessage()]
    assert len(records) == 1
    return records[0]


def _find_upload_failure(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "UPLOAD_INPUTS_FAILED"
    ]
    assert len(records) == 1
    return records[0]


def _make_engine(input_files: tuple[str, ...] = ("input.txt",)) -> Engine:
    return Engine(
        name="test_engine",
        spawn="{engine_path} {task_path} {ncpus}",
        input_files=input_files,
    )


def _make_task() -> Task:
    """Build a RUNNING task; input contents are streamed separately."""
    from datetime import datetime

    return Task(
        task_id=TaskId(7),
        label="t7",
        engine="test_engine",
        state=Running(allocated_node_id=NodeId(1), remote_folder="/remote/tasks/7"),
        webhook_url=None,
        webhook_custom_params={},
        extra={},
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )


async def _aiter(entries: Sequence[TaskInputEntry]) -> AsyncIterator[TaskInputEntry]:
    """Yield snapshot entries the way the repository streams them to the deployer."""
    for entry in entries:
        yield entry


# =============================================================================
# _write_remote_file exception contract
# =============================================================================


class TestWriteRemoteFilePropagation:
    """_write_remote_file SHALL re-raise non-SFTP exceptions (not swallow) and
    SHALL log structured code/reason for asyncssh.misc.Error then re-raise.
    """

    @pytest.mark.asyncio
    async def test_non_sftp_exception_propagates(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A ValueError raised by f.write() propagates out of _write_remote_file.

        The asyncssh.misc.Error-branch structured log line ("Write <path> -
        SFTPError: ...") MUST NOT be emitted for a non-SFTP exception.
        """
        file = _FakeSFTPFile(write_side_effect=ValueError("bad data"))
        sftp = _FakeSFTPClient(file)

        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(ValueError, match="bad data"),
        ):
            await _write_remote_file(
                cast("SFTPClient", sftp),
                "/r/input.txt",
                b"data",
            )

        # The SFTPError diagnostic is reserved for asyncssh.misc.Error.
        assert not any("SFTPError" in r.getMessage() for r in caplog.records), (
            "asyncssh.misc.Error-branch log was emitted for a non-SFTP exception"
        )

    @pytest.mark.asyncio
    async def test_open_failure_propagates(self) -> None:
        """A non-SFTP exception raised by sftp.open() (before write) propagates.

        The try block wraps `async with sftp.open(...) as f:` — an exception
        from `open` takes the same propagation path as one from `f.write`.
        """
        file = _FakeSFTPFile()
        sftp = _FakeSFTPClient(file, open_side_effect=OSError("open boom"))

        with pytest.raises(OSError, match="open boom"):
            await _write_remote_file(
                cast("SFTPClient", sftp),
                "/r/input.txt",
                b"data",
            )

        # write was never reached (open failed first).
        file.write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_asyncssh_misc_error_logged_and_reraised(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An asyncssh.misc.Error is logged with structured code/reason and re-raised."""
        err = asyncssh.misc.Error(2, "No such file")
        file = _FakeSFTPFile(write_side_effect=err)
        sftp = _FakeSFTPClient(file)

        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(asyncssh.misc.Error) as exc_info,
        ):
            await _write_remote_file(
                cast("SFTPClient", sftp),
                "/r/input.txt",
                b"data",
            )

        # The same exception instance is re-raised (no swallow, no rewrap).
        assert exc_info.value is err
        record = _find_record(caplog, "SFTPError")
        assert record.levelno == logging.ERROR
        assert record.getMessage() == ("Write /r/input.txt - SFTPError: Error (2)")
        assert "reason" not in extra_fields(record)
        assert "exception" not in extra_fields(record)
        assert record.exc_info is None
        assert "No such file" not in caplog.text

    @pytest.mark.asyncio
    async def test_sftp_error_subclass_logged_and_reraised(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SFTPError (asyncssh.misc.Error subclass from sftp.open/f.write) is logged and re-raised."""
        from asyncssh.sftp import SFTPError

        err = SFTPError(3, "Permission denied")
        file = _FakeSFTPFile(write_side_effect=err)
        sftp = _FakeSFTPClient(file)

        with caplog.at_level(logging.ERROR), pytest.raises(SFTPError) as exc_info:
            await _write_remote_file(
                cast("SFTPClient", sftp),
                "/r/fort.9",
                b"x",
            )

        assert exc_info.value is err
        record = _find_record(caplog, "SFTPError")
        assert record.levelno == logging.ERROR
        assert record.getMessage() == ("Write /r/fort.9 - SFTPError: SFTPError (3)")
        assert "reason" not in extra_fields(record)
        assert "exception" not in extra_fields(record)
        assert record.exc_info is None
        assert "Permission denied" not in caplog.text


# =============================================================================
# start_task_on_machine abort contract
# =============================================================================


class TestStartTaskAbortOnUploadFailure:
    """When _upload_task_data fails, _exec_spawn_command SHALL NOT run."""

    @pytest.mark.asyncio
    async def test_non_sftp_upload_failure_aborts_spawn(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-SFTP exception during an input file write aborts start_task_on_machine:
        _exec_spawn_command is NOT called; the exception propagates; the upload
        failure record classifies it without echoing the exception text.
        """
        deployer = TaskDeployer()
        session, _sftp = _make_sftp_state(write_side_effect=ValueError("bad input"))

        spawn_calls: list[Any] = []
        deployer._exec_spawn_command = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: spawn_calls.append((a, kw)),
        )

        with (
            caplog.at_level(logging.DEBUG),
            pytest.raises(ValueError, match="bad input"),
        ):
            await deployer.start_task_on_machine(
                session,
                _make_engine(input_files=("input.txt",)),
                _make_task(),
                _aiter((TaskInputFile(path="input.txt", content=b"hello"),)),
                4,
                PurePosixPath("/engines"),
            )

        # spawn never runs — the abort contract.
        assert spawn_calls == [], (
            "_exec_spawn_command was called despite an input file write failure"
        )
        failed = extra_fields(_find_upload_failure(caplog))
        assert failed["task_id"] == 7
        assert type(failed["task_id"]) is int
        assert failed["entry_count"] == 1
        assert failed["path"] == "input.txt"
        assert failed["failure"] == "ValueError"
        assert "bad input" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)

    @pytest.mark.asyncio
    async def test_sftp_upload_failure_aborts_spawn(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An asyncssh.misc.Error during an input file write aborts start_task_on_machine."""
        deployer = TaskDeployer()
        err = asyncssh.misc.Error(2, "No such file")
        session, _sftp = _make_sftp_state(write_side_effect=err)

        deployer._exec_spawn_command = AsyncMock()  # type: ignore[method-assign]

        with caplog.at_level(logging.DEBUG), pytest.raises(asyncssh.misc.Error):
            await deployer.start_task_on_machine(
                session,
                _make_engine(input_files=("input.txt",)),
                _make_task(),
                _aiter((TaskInputFile(path="input.txt", content=b"hello"),)),
                4,
                PurePosixPath("/engines"),
            )

        deployer._exec_spawn_command.assert_not_awaited()  # type: ignore[attr-defined]
        # The SFTPError marker carries only path/code/failure class, the
        # upload-failure record carries task/path/entry-count context, and the
        # original exception reason never reaches rendered logs.
        sftp_error = _find_record(caplog, "SFTPError")
        assert sftp_error.getMessage() == (
            "Write /remote/tasks/7/input.txt - SFTPError: Error (2)"
        )
        assert sftp_error.exc_info is None
        failed = extra_fields(_find_upload_failure(caplog))
        assert failed["task_id"] == 7
        assert type(failed["task_id"]) is int
        assert failed["entry_count"] == 1
        assert failed["path"] == "input.txt"
        assert failed["failure"] == "Error"
        assert "No such file" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)


# =============================================================================
# Successful write path
# =============================================================================


class TestSuccessfulWrite:
    """A successful write returns normally and the per-file loop continues."""

    @pytest.mark.asyncio
    async def test_successful_write_no_log_no_raise(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_write_remote_file returns normally on success — no exception, no error log."""
        file = _FakeSFTPFile(write_side_effect=None)
        sftp = _FakeSFTPClient(file)

        with caplog.at_level(logging.ERROR):
            await _write_remote_file(
                cast("SFTPClient", sftp),
                "/r/input.txt",
                b"data",
            )

        file.write.assert_awaited_once_with(b"data")
        assert not any(r.levelno >= logging.ERROR for r in caplog.records), (
            "an error log was emitted on a successful write"
        )

    @pytest.mark.asyncio
    async def test_upload_loop_continues_across_files(self) -> None:
        """_upload_task_data writes every input file when none raise; returns True."""
        deployer = TaskDeployer()
        state, sftp = _make_sftp_state(write_side_effect=None)

        # Two input files; the _FakeSFTPFile is shared across calls so we can
        # count write invocations across the loop.
        ok = await deployer._upload_task_data(
            state,
            _make_task(),
            PurePosixPath("/remote/tasks/7"),
            # The stored snapshot is path-ordered; submission normalizes it.
            _aiter(
                normalize_input_snapshot(
                    (
                        TaskInputFile(path="input.txt", content=b"a"),
                        TaskInputFile(path="fort.9", content=b"AAAA"),
                    )
                )
            ),
        )

        assert ok is True
        # Both files were written (the loop did not bail after the first).
        assert sftp._file.write.await_count == 2
        assert [call.args[0] for call in sftp.open.call_args_list] == [
            "/remote/tasks/7/fort.9",
            "/remote/tasks/7/input.txt",
        ]
        assert all(call.args[1] == "wb" for call in sftp.open.call_args_list)

    @pytest.mark.asyncio
    async def test_upload_materializes_directory_entries(self) -> None:
        """_upload_task_data creates stored directory entries, including empty ones."""
        deployer = TaskDeployer()
        state, sftp = _make_sftp_state(write_side_effect=None)

        ok = await deployer._upload_task_data(
            state,
            _make_task(),
            PurePosixPath("/remote/tasks/7"),
            # Submission materializes parent directories; the deployer replays them.
            _aiter(normalize_input_snapshot((TaskInputDirectory(path="basis/empty"),))),
        )

        assert ok is True
        assert [call.args[0] for call in sftp.makedirs.await_args_list] == [
            "/remote/tasks/7",
            "/remote/tasks/7/basis",
            "/remote/tasks/7/basis/empty",
        ]
        sftp._file.write.assert_not_awaited()


# =============================================================================
# SPAWN log contract
# =============================================================================


class TestExecSpawnCommandLog:
    """_exec_spawn_command SHALL emit a SPAWN debug log with hostname, task_id, cmd, cwd."""

    @pytest.mark.asyncio
    async def test_spawn_log_emitted(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """After run_bg, a SPAWN debug record is present with structured fields."""
        from tests.unit.test_ssh_gateway import _make_state

        deployer = TaskDeployer()
        session = _make_state(hostname="10.0.0.1", node_id=1, platform="linux")
        engine = Engine(
            name="dummy",
            spawn="{engine_path}/dummyengine *",
        )
        task = _make_task()

        with caplog.at_level(logging.DEBUG):
            await deployer._exec_spawn_command(
                session=session,
                engine=engine,
                task=task,
                task_dir=PurePosixPath("data/tasks/7"),
                eng_path=PurePosixPath("data/engines/dummy"),
                ncpus=4,
            )

        spawn_records = [r for r in caplog.records if r.getMessage() == "SPAWN"]
        assert len(spawn_records) == 1, "expected exactly one SPAWN record"

        fields = extra_fields(spawn_records[0])
        assert fields["hostname"] == "10.0.0.1"
        assert fields["task_id"].value == 7
        assert fields["cmd"] == "data/engines/dummy/dummyengine *"
        assert fields["cwd"] == "data/tasks/7"
