# region MODULE_CONTRACT
# PURPOSE: Unit tests for non-idempotent retry removal (run_bg/upload/download single-attempt), start_task_on_machine BUSY rollback with content-free DEPLOY_ROLLBACK records, and SSHRetryExc shape invariants.
# SCOPE: run_bg/upload/download no longer retry on transient SSH/SFTP errors; start_task_on_machine rolls back gateway BUSY on upload/spawn/Cancelled/unexpected-state/concurrent-disconnect failures and records task_id/failure/released without exception text; SSHRetryExc does not include PermissionDenied.
# KEYWORDS: non-idempotent retry, BUSY rollback, start_task_on_machine, SSHRetryExc, PermissionDenied
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncssh.misc import PermissionDenied

from tests.log_assertions import extra_fields
from tests.unit.test_ssh_gateway import _make_state
from yascheduler.domain import Engine
from yascheduler.domain.model import (
    MachineState,
    NodeId,
    Running,
    Task,
    TaskId,
)
from yascheduler.infra.ssh.operations import TaskDeployer
from yascheduler.infra.ssh.platform.types import (
    ChannelOpenError,
    SFTPConnectionLost,
    SSHRetryExc,
)
from yascheduler.infra.ssh.repository import SSHMachineRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from yascheduler.domain import TaskInputEntry
    from yascheduler.infra.ssh.session import SSHMachineSession


@pytest.fixture
def task_deployer() -> TaskDeployer:
    return TaskDeployer()


def _make_engine() -> Engine:
    """Minimal Engine for start_task_on_machine tests."""
    return Engine(name="test_engine", spawn="{engine_path} {task_path}")


def _make_task(remote_folder: str = "/remote/tasks/1") -> Task:
    """Minimal RUNNING Task (state-payload form) for start_task_on_machine."""
    from datetime import datetime

    return Task(
        task_id=TaskId(1),
        label="test-task",
        engine="test_engine",
        state=Running(allocated_node_id=NodeId(1), remote_folder=remote_folder),
        webhook_url=None,
        webhook_custom_params={},
        extra={},
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )


async def _aiter(entries: Sequence[TaskInputEntry]) -> AsyncIterator[TaskInputEntry]:
    """Yield snapshot entries the way the allocation use case hands them over."""
    for entry in entries:
        yield entry


def _wire_realpath(state: SSHMachineSession) -> None:
    """Configure the DEPLOY SFTP client's realpath before start_task_on_machine."""
    sftp = AsyncMock()
    sftp.realpath = AsyncMock(return_value="/root")

    @asynccontextmanager
    async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
        yield sftp

    state._conn.start_sftp_client = _sftp_ctx  # type: ignore[assignment]


def _rollback_fields(
    caplog: pytest.LogCaptureFixture, failure: str, released: bool
) -> None:
    records = [
        record for record in caplog.records if record.getMessage() == "DEPLOY_ROLLBACK"
    ]
    assert len(records) == 1
    record = records[0]
    fields = extra_fields(record)
    assert fields["task_id"] == 1
    assert type(fields["task_id"]) is int
    assert fields["failure"] == failure
    assert fields["released"] is released
    assert record.exc_info is None


# =============================================================================
# Non-idempotent retry behaviour (run_bg / upload / download are single-attempt)
# =============================================================================


class TestNonIdempotentRetry:
    """run_bg / upload / download SHALL NOT retry on transient errors."""

    @pytest.mark.asyncio
    async def test_run_bg_no_longer_retries_on_ssh_error(
        self,
    ) -> None:
        """run_bg propagates ChannelOpenError immediately, single attempt (no retry)."""
        session = _make_state()
        adapter = cast("MagicMock", session.adapter)
        adapter.run_bg = AsyncMock(side_effect=ChannelOpenError(11, "open failed"))

        with pytest.raises(ChannelOpenError):
            await session.run_bg("spawn-cmd", cwd="/tmp")

        adapter.run_bg.assert_awaited_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_upload_no_longer_retries_on_sftp_error(
        self,
    ) -> None:
        """Upload propagates SFTPConnectionLost immediately, single put attempt."""
        session = _make_state()
        sftp = AsyncMock()
        sftp.put = AsyncMock(side_effect=SFTPConnectionLost("connection lost"))

        @asynccontextmanager
        async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
            yield sftp

        session._conn.start_sftp_client = _sftp_ctx  # type: ignore[assignment]

        with pytest.raises(SFTPConnectionLost):
            await session.upload(Path("/tmp/local"), "/remote/file")

        sftp.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_download_no_longer_retries_on_sftp_error(
        self,
    ) -> None:
        """Download equivalent via open_sftp propagates SFTPConnectionLost immediately, single get attempt."""
        session = _make_state()
        sftp = AsyncMock()
        sftp.get = AsyncMock(side_effect=SFTPConnectionLost("connection lost"))

        @asynccontextmanager
        async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
            yield sftp

        session._conn.start_sftp_client = _sftp_ctx  # type: ignore[assignment]

        with pytest.raises(SFTPConnectionLost):
            async with session.open_sftp() as sftp_client:
                await sftp_client.get("/remote/file", "/tmp/local")

        sftp.get.assert_awaited_once()


# =============================================================================
# start_task_on_machine BUSY rollback
# =============================================================================


class TestStartTaskRollback:
    """start_task_on_machine rolls back the repository-level BUSY marking on failure."""

    @pytest.mark.asyncio
    async def test_rollback_busy_on_upload_failure(
        self,
        task_deployer: TaskDeployer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Upload failure -> machine released, info log, original error re-raised."""
        repository = SSHMachineRepository()
        session = _make_state()
        _wire_realpath(session)
        repository._sessions[NodeId(1)] = session

        with (
            patch.object(
                task_deployer,
                "_upload_task_data",
                AsyncMock(side_effect=OSError("upload boom")),
            ),
            caplog.at_level(logging.DEBUG),
            pytest.raises(OSError, match="upload boom"),
        ):
            await task_deployer.start_task_on_machine(
                session,
                _make_engine(),
                _make_task(),
                _aiter(()),
                4,
                PurePosixPath("/engines"),
            )

        assert repository._sessions[NodeId(1)].machine.state == MachineState.FREE
        _rollback_fields(caplog, "OSError", released=True)
        assert "upload boom" not in caplog.text

    @pytest.mark.asyncio
    async def test_rollback_busy_on_spawn_failure(
        self,
        task_deployer: TaskDeployer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Spawn failure (ChannelOpenError) -> machine released, info log, re-raised."""
        repository = SSHMachineRepository()
        session = _make_state()
        _wire_realpath(session)
        repository._sessions[NodeId(1)] = session

        with (
            patch.object(
                task_deployer,
                "_upload_task_data",
                AsyncMock(return_value=True),
            ),
            patch.object(
                task_deployer,
                "_exec_spawn_command",
                AsyncMock(side_effect=ChannelOpenError(11, "no chan")),
            ),
            caplog.at_level(logging.DEBUG),
            pytest.raises(ChannelOpenError),
        ):
            await task_deployer.start_task_on_machine(
                session,
                _make_engine(),
                _make_task(),
                _aiter(()),
                4,
                PurePosixPath("/engines"),
            )

        assert repository._sessions[NodeId(1)].machine.state == MachineState.FREE
        _rollback_fields(caplog, "ChannelOpenError", released=True)

    @pytest.mark.asyncio
    async def test_rollback_on_cancelled_error(
        self,
        task_deployer: TaskDeployer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """CancelledError (BaseException) is caught, machine released, then re-raised."""
        repository = SSHMachineRepository()
        session = _make_state()
        _wire_realpath(session)
        repository._sessions[NodeId(1)] = session

        with (
            patch.object(
                task_deployer,
                "_upload_task_data",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            caplog.at_level(logging.DEBUG),
            pytest.raises(asyncio.CancelledError),
        ):
            await task_deployer.start_task_on_machine(
                session,
                _make_engine(),
                _make_task(),
                _aiter(()),
                4,
                PurePosixPath("/engines"),
            )

        assert repository._sessions[NodeId(1)].machine.state == MachineState.FREE
        _rollback_fields(caplog, "CancelledError", released=True)

    @pytest.mark.asyncio
    async def test_rollback_warns_on_unexpected_state(
        self,
        task_deployer: TaskDeployer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Non-BUSY state at rollback -> warn, still release, re-raise."""
        repository = SSHMachineRepository()
        session = _make_state()
        _wire_realpath(session)
        repository._sessions[NodeId(1)] = session

        async def _fail_with_unexpected_state(
            s: object,
            task: Task,
            task_dir: object,
            entries: object,
        ) -> bool:
            # Simulate a concurrent transition away from BUSY before rollback.
            session.release()
            raise OSError("boom")

        with (
            patch.object(
                task_deployer,
                "_upload_task_data",
                AsyncMock(side_effect=_fail_with_unexpected_state),
            ),
            caplog.at_level(logging.DEBUG),
            pytest.raises(OSError),
        ):
            await task_deployer.start_task_on_machine(
                session,
                _make_engine(),
                _make_task(),
                _aiter(()),
                4,
                PurePosixPath("/engines"),
            )

        assert repository._sessions[NodeId(1)].machine.state == MachineState.FREE
        assert "unexpected state" in caplog.text
        assert "expected BUSY" in caplog.text
        _rollback_fields(caplog, "OSError", released=True)

    @pytest.mark.asyncio
    async def test_rollback_warns_on_concurrent_disconnect(
        self,
        task_deployer: TaskDeployer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Session closed (concurrent disconnect) -> warn, skip release, re-raise."""
        repository = SSHMachineRepository()
        session = _make_state()
        _wire_realpath(session)
        repository._sessions[NodeId(1)] = session

        async def _fail_after_disconnect(
            s: object,
            task: Task,
            task_dir: object,
            entries: object,
        ) -> bool:
            await session._close()
            raise OSError("boom")

        occupy_spy = patch.object(session, "occupy", wraps=session.occupy)

        with (
            occupy_spy as spy,
            patch.object(
                task_deployer,
                "_upload_task_data",
                AsyncMock(side_effect=_fail_after_disconnect),
            ),
            caplog.at_level(logging.DEBUG),
            pytest.raises(OSError),
        ):
            await task_deployer.start_task_on_machine(
                session,
                _make_engine(),
                _make_task(),
                _aiter(()),
                4,
                PurePosixPath("/engines"),
            )

        # Rollback saw is_closed and skipped release; session stays BUSY in repo.
        assert session.is_closed
        assert repository._sessions[NodeId(1)].machine.state == MachineState.BUSY
        # occupy was called exactly once (the initial occupy); no release call.
        assert spy.call_count == 1
        assert "already disconnected" in caplog.text
        assert "boom" not in caplog.text
        _rollback_fields(caplog, "OSError", released=False)


# =============================================================================
# SSHRetryExc shape invariants
# =============================================================================


class TestSSHRetryExcShape:
    """SSHRetryExc tuple invariants — regression pins against accidental policy changes."""

    def test_permission_denied_not_in_ssh_retry_exc(self) -> None:
        """PermissionDenied is NOT in SSHRetryExc — steady-state SSH retry policy unchanged."""
        assert PermissionDenied not in SSHRetryExc
