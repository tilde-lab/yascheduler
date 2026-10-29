# region MODULE_CONTRACT
# PURPOSE: Unit tests for OutputDownloader's V0 download classification and best-effort post-finalisation remote cleanup.
# SCOPE: Per-file download success/error classification without deletion, per-file SFTP isolation, cleanup success/already-absent/failure paths, and content-free cleanup logs.
# KEYWORDS: download_outputs, cleanup_remote, rmtree, SFTP isolation, retry classification
# endregion MODULE_CONTRACT

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncssh.sftp import SFTPError, SFTPFailure, SFTPNoSuchFile

from tests.log_assertions import extra_fields
from tests.unit.test_ssh_gateway import _make_state
from yascheduler.domain.model import TaskId
from yascheduler.infra.ssh.operations import OutputDownloader
from yascheduler.infra.ssh.operations import download as download_module
from yascheduler.infra.ssh.platform.types import SFTPConnectionLost
from yascheduler.infra.ssh.session import SSHMachineSession


@pytest.fixture(autouse=True)
def _no_sftp_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize my_retry so unit tests classify immediate failures without backoff sleep."""
    monkeypatch.setattr(download_module, "my_retry", lambda: lambda fn: fn)


def _wire_sftp(session: SSHMachineSession, sftp_mock: AsyncMock) -> SSHMachineSession:
    """Install ``sftp_mock`` as the SFTP client yielded by the session's conn."""
    session._adapter.path = PurePosixPath  # type: ignore[method-assign,misc]

    @asynccontextmanager
    async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
        yield sftp_mock

    session._conn.start_sftp_client = _sftp_ctx  # type: ignore[method-assign,misc,assignment]
    return session


def _make_session_with_sftp(sftp_mock: AsyncMock) -> SSHMachineSession:
    """Construct a session whose open_sftp() yields sftp_mock and whose .path is PurePosixPath."""
    session = _make_state()
    return _wire_sftp(session, sftp_mock)


def _make_output_downloader() -> OutputDownloader:
    return OutputDownloader()


def _make_cleanup_session(sftp_mock: AsyncMock) -> SimpleNamespace:
    """Minimal fake MachineSession for cleanup_remote, recording path() calls."""

    @asynccontextmanager
    async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
        yield sftp_mock

    return SimpleNamespace(
        open_sftp=_sftp_ctx,
        path=MagicMock(side_effect=PurePosixPath),
    )


def _make_broken_cleanup_session(error: Exception) -> SimpleNamespace:
    """Minimal fake MachineSession whose cleanup SFTP channel open fails."""

    @asynccontextmanager
    async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
        raise error
        yield AsyncMock()  # type: ignore[unreachable]

    return SimpleNamespace(
        open_sftp=_sftp_ctx,
        path=MagicMock(side_effect=PurePosixPath),
    )


@pytest.mark.asyncio
async def test_download_outputs_success() -> None:
    """All files download OK; download_outputs returns success and never deletes."""
    sftp_mock = AsyncMock()
    sftp_mock.get = AsyncMock(return_value=None)
    sftp_mock.rmtree = AsyncMock(return_value=None)
    session = _make_session_with_sftp(sftp_mock)
    operations = _make_output_downloader()

    (
        local_folder,
        remote_folder,
        transient_errors,
        permanent_errors,
    ) = await operations.download_outputs(
        session,
        remote_dir="/remote/path",
        local_dir=Path("/local/path"),
        files=["f1.out", "f2.out"],
        task_id=TaskId(42),
    )

    assert local_folder == "/local/path"
    assert remote_folder == "/remote/path"
    assert transient_errors == []
    assert permanent_errors == []
    assert sftp_mock.get.await_count == 2
    sftp_mock.rmtree.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_outputs_per_file_permanent_error() -> None:
    """Per-file OSError (not SFTPRetryExc) -> permanent_errors; rmtree NOT called."""
    sftp_mock = AsyncMock()
    sftp_mock.get = AsyncMock(side_effect=OSError("permission denied"))
    sftp_mock.rmtree = AsyncMock(return_value=None)
    session = _make_session_with_sftp(sftp_mock)
    operations = _make_output_downloader()

    (
        local_folder,
        remote_folder,
        transient_errors,
        permanent_errors,
    ) = await operations.download_outputs(
        session,
        remote_dir="/remote/path",
        local_dir=Path("/local/path"),
        files=["f1.out", "f2.out"],
        task_id=TaskId(42),
    )

    assert local_folder == "/local/path"
    assert remote_folder == "/remote/path"
    assert transient_errors == []
    assert len(permanent_errors) == 2
    for file_name, exc in permanent_errors:
        assert file_name in ("f1.out", "f2.out")
        assert isinstance(exc, OSError)
    sftp_mock.rmtree.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_outputs_per_file_transient_error() -> None:
    """Per-file SFTPFailure (in SFTPRetryExc) -> transient_errors; rmtree NOT called."""
    sftp_mock = AsyncMock()
    sftp_mock.get = AsyncMock(side_effect=SFTPFailure("connection lost"))
    sftp_mock.rmtree = AsyncMock(return_value=None)
    session = _make_session_with_sftp(sftp_mock)
    operations = _make_output_downloader()

    (
        local_folder,
        remote_folder,
        transient_errors,
        permanent_errors,
    ) = await operations.download_outputs(
        session,
        remote_dir="/remote/path",
        local_dir=Path("/local/path"),
        files=["f1.out", "f2.out"],
        task_id=TaskId(42),
    )

    assert local_folder == "/local/path"
    assert remote_folder == "/remote/path"
    assert permanent_errors == []
    assert len(transient_errors) == 2
    for file_name, exc in transient_errors:
        assert file_name in ("f1.out", "f2.out")
        assert isinstance(exc, SFTPFailure)
    sftp_mock.rmtree.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_outputs_session_error() -> None:
    """open_sftp raises (session-level failure); caught in transient_errors; rmtree NOT called."""
    session = _make_state()
    session._adapter.path = PurePosixPath  # type: ignore[method-assign,misc]

    @asynccontextmanager
    async def bad_sftp() -> AsyncGenerator[Any, None]:
        raise OSError("no connection")
        yield  # type: ignore[unreachable]

    session._conn.start_sftp_client = bad_sftp  # type: ignore[method-assign,misc]
    operations = _make_output_downloader()

    (
        local_folder,
        remote_folder,
        transient_errors,
        permanent_errors,
    ) = await operations.download_outputs(
        session,
        remote_dir="/remote/path",
        local_dir=Path("/local/path"),
        files=["f1.out", "f2.out"],
        task_id=TaskId(42),
    )

    assert local_folder == "/local/path"
    assert remote_folder == "/remote/path"
    assert permanent_errors == []
    assert len(transient_errors) == 1
    remote_dir, exc = transient_errors[0]
    assert remote_dir == "/remote/path"
    assert isinstance(exc, OSError)


@pytest.mark.asyncio
async def test_download_outputs_task_id_in_signature() -> None:
    """Verify task_id param is accepted without error for log correlation."""
    sftp_mock = AsyncMock()
    sftp_mock.get = AsyncMock(return_value=None)
    sftp_mock.rmtree = AsyncMock(return_value=None)
    session = _make_session_with_sftp(sftp_mock)
    operations = _make_output_downloader()

    (
        local_folder,
        remote_folder,
        transient_errors,
        permanent_errors,
    ) = await operations.download_outputs(
        session,
        remote_dir="/r",
        local_dir=Path("/l"),
        files=["f.out"],
        task_id=TaskId(99),
    )
    assert transient_errors == []
    assert permanent_errors == []
    assert local_folder == "/l"
    assert remote_folder == "/r"

    # Without task_id (None default)
    (
        local_folder2,
        remote_folder2,
        transient_errors2,
        permanent_errors2,
    ) = await operations.download_outputs(
        session,
        remote_dir="/r",
        local_dir=Path("/l"),
        files=["f.out"],
    )
    assert transient_errors2 == []
    assert permanent_errors2 == []
    assert local_folder2 == "/l"
    assert remote_folder2 == "/r"


@pytest.mark.asyncio
async def test_download_outputs_never_calls_rmtree_even_on_full_success() -> None:
    """A fully successful download leaves the remote tree untouched for cleanup_remote."""
    sftp_mock = AsyncMock()
    sftp_mock.get = AsyncMock(return_value=None)
    sftp_mock.rmtree = AsyncMock(return_value=None)
    session = _make_session_with_sftp(sftp_mock)
    operations = _make_output_downloader()

    _, _, transient_errors, permanent_errors = await operations.download_outputs(
        session,
        remote_dir="/r",
        local_dir=Path("/l"),
        files=["f1", "f2"],
    )

    assert transient_errors == []
    assert permanent_errors == []
    assert sftp_mock.get.await_count == 2
    sftp_mock.rmtree.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_outputs_per_file_sftp_isolation() -> None:
    """A dropped SFTP client on file 2 does not fail-fast file 3 (fresh client per file)."""
    session = _make_state()
    session._adapter.path = PurePosixPath  # type: ignore[method-assign,misc]
    operations = _make_output_downloader()

    clients: list[AsyncMock] = []

    @asynccontextmanager
    async def fresh_sftp() -> AsyncGenerator[AsyncMock, None]:
        idx = len(clients)
        sftp = AsyncMock()
        # file 2 (index 1) drops its connection mid-transfer; others succeed.
        if idx == 1:
            sftp.get = AsyncMock(side_effect=SFTPConnectionLost("dropped"))
        else:
            sftp.get = AsyncMock(return_value=None)
        sftp.rmtree = AsyncMock(return_value=None)
        clients.append(sftp)
        yield sftp

    session._conn.start_sftp_client = fresh_sftp  # type: ignore[method-assign,misc,assignment]

    _, _, transient_errors, permanent_errors = await operations.download_outputs(
        session,
        remote_dir="/r",
        local_dir=Path("/l"),
        files=["f1", "f2", "f3"],
        task_id=TaskId(7),
    )

    assert len(clients) == 3
    assert len(transient_errors) == 1
    assert transient_errors[0][0] == "f2"
    assert isinstance(transient_errors[0][1], SFTPConnectionLost)
    assert permanent_errors == []
    assert clients[2].get.await_count == 1
    for client in clients:
        client.rmtree.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_outputs_session_level_failure_transient() -> None:
    """open_sftp OPEN failure -> outer handler -> session-level transient (remote dir preserved)."""
    session = _make_state()
    session._adapter.path = PurePosixPath  # type: ignore[method-assign,misc]
    operations = _make_output_downloader()

    @asynccontextmanager
    async def broken_sftp() -> AsyncGenerator[Any, None]:
        raise SFTPConnectionLost("no session")
        yield  # type: ignore[unreachable]

    session._conn.start_sftp_client = broken_sftp  # type: ignore[method-assign,misc]

    (
        local_folder,
        remote_folder,
        transient_errors,
        permanent_errors,
    ) = await operations.download_outputs(
        session,
        remote_dir="/remote/path",
        local_dir=Path("/local/path"),
        files=["f1.out", "f2.out"],
        task_id=TaskId(42),
    )

    assert local_folder == "/local/path"
    assert remote_folder == "/remote/path"
    assert permanent_errors == []
    assert len(transient_errors) == 1
    remote_dir, exc = transient_errors[0]
    assert remote_dir == "/remote/path"
    assert isinstance(exc, SFTPConnectionLost)


# =============================================================================
# cleanup_remote — best-effort post-finalisation deletion
# =============================================================================

_CONTENT_SECRET = "UNIQUE_CLEANUP_FILE_CONTENT_SECRET_4f19"
_CREDENTIAL_SECRET = "UNIQUE_CLEANUP_CREDENTIAL_SECRET_8b2c"


def _cleanup_fields(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    records = [
        record for record in caplog.records if record.getMessage() == "CLEANUP_REMOTE"
    ]
    assert len(records) == 1
    fields = extra_fields(records[0])
    fields.pop("message", None)
    return fields


def _assert_cleanup_logs_are_content_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup logs must not render file contents or credentials."""
    rendered = caplog.text
    for record in caplog.records:
        fields = extra_fields(record)
        rendered = f"{rendered}\n{record.getMessage()} {fields!r}"
    assert _CONTENT_SECRET not in rendered
    assert _CREDENTIAL_SECRET not in rendered


@pytest.mark.asyncio
async def test_cleanup_remote_success_uses_fresh_channel_and_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful cleanup returns None and rmtree receives session.path(remote_dir)."""
    sftp_mock = AsyncMock()
    sftp_mock.rmtree = AsyncMock(return_value=None)
    session = _make_cleanup_session(sftp_mock)
    operations = _make_output_downloader()

    with caplog.at_level(logging.DEBUG, logger=download_module.__name__):
        await operations.cleanup_remote(
            session,  # type: ignore[arg-type]
            remote_dir="/remote/path",
            task_id=TaskId(42),
        )
    session.path.assert_called_once_with("/remote/path")
    sftp_mock.rmtree.assert_awaited_once_with(PurePosixPath("/remote/path"))
    sftp_mock.get.assert_not_awaited()
    assert _cleanup_fields(caplog) == {
        "task_id": TaskId(42),
        "remote_dir": "/remote/path",
    }
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    _assert_cleanup_logs_are_content_free(caplog)


@pytest.mark.asyncio
async def test_cleanup_remote_already_absent_logs_debug_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SFTPNoSuchFile is a debug no-op, not a warning, and cleanup still returns None."""
    sftp_mock = AsyncMock()
    sftp_mock.rmtree = AsyncMock(side_effect=SFTPNoSuchFile("No such file"))
    session = _make_cleanup_session(sftp_mock)
    operations = _make_output_downloader()

    with caplog.at_level(logging.DEBUG, logger=download_module.__name__):
        await operations.cleanup_remote(
            session,  # type: ignore[arg-type]
            remote_dir="/remote/path",
            task_id=TaskId(42),
        )
    session.path.assert_called_once_with("/remote/path")
    sftp_mock.rmtree.assert_awaited_once_with(PurePosixPath("/remote/path"))
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    absent_records = [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG and "already absent" in record.getMessage()
    ]
    assert len(absent_records) == 1
    assert not any(record.getMessage() == "CLEANUP_REMOTE" for record in caplog.records)
    _assert_cleanup_logs_are_content_free(caplog)


@pytest.mark.asyncio
async def test_cleanup_remote_failure_warns_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A generic SFTPError is logged as a warning and swallowed."""
    sftp_mock = AsyncMock()
    sftp_mock.rmtree = AsyncMock(side_effect=SFTPError(4, "permission denied"))
    session = _make_cleanup_session(sftp_mock)
    operations = _make_output_downloader()

    with caplog.at_level(logging.DEBUG, logger=download_module.__name__):
        await operations.cleanup_remote(
            session,  # type: ignore[arg-type]
            remote_dir="/remote/path",
            task_id=TaskId(42),
        )
    sftp_mock.rmtree.assert_awaited_once_with(PurePosixPath("/remote/path"))
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "Cannot clean remote dir" in message
    assert "(left for node teardown)" in message
    assert "permission denied" in message
    assert not any(record.getMessage() == "CLEANUP_REMOTE" for record in caplog.records)
    _assert_cleanup_logs_are_content_free(caplog)


@pytest.mark.asyncio
async def test_cleanup_remote_open_sftp_oserror_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError opening the cleanup SFTP channel is swallowed with a warning."""
    session = _make_broken_cleanup_session(OSError("channel open failed"))
    operations = _make_output_downloader()

    with caplog.at_level(logging.DEBUG, logger=download_module.__name__):
        await operations.cleanup_remote(
            session,  # type: ignore[arg-type]
            remote_dir="/remote/path",
            task_id=TaskId(42),
        )
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "Cannot clean remote dir" in message
    assert "(left for node teardown)" in message
    assert "channel open failed" in message
    _assert_cleanup_logs_are_content_free(caplog)
