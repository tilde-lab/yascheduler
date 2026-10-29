# region MODULE_CONTRACT
# PURPOSE: Unit-test the destination-confinement and fault-injection contract of TaskDeployer.start_task_on_machine (task-input-snapshots task 4.2) — stored entries are re-validated at the trust boundary, destinations are joined in the remote platform's path flavor, symlinks and file/directory conflicts are rejected without traversal, lstat errors propagate, and every upload failure or cancellation prevents spawn while releasing the session.
# SCOPE: Fake MachineSession + fake SFTP client with async lstat/makedirs/open, call recording, and per-path failure injection. Cases: root mkdir failure, mid-tree write failure, CancelledError, unsafe stored paths (absolute, '..', duplicate, file-ancestor conflict), symlink at file/directory target, file/directory type conflicts, existing ordinary file replacement, permission-denied lstat, PureWindowsPath/PurePosixPath joining, and the DESTINATION_REJECTED structured record.
# NOT: real SSH transport and exact remote bytes (integration test), spawn-command formatting, and the individual helper functions in isolation.
# KEYWORDS: TaskDeployer, destination confinement, symlink rejection, fault injection, SFTP, BUSY rollback
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import asyncssh
import pytest
from asyncssh.sftp import (
    FILEXFER_TYPE_DIRECTORY,
    FILEXFER_TYPE_REGULAR,
    FILEXFER_TYPE_SYMLINK,
    FX_FAILURE,
    FX_NO_SUCH_FILE,
    FX_PERMISSION_DENIED,
)

from tests.log_assertions import extra_fields
from yascheduler.domain import (
    Engine,
    InvalidTaskSnapshotError,
    TaskInputDirectory,
    TaskInputFile,
    UnsafeTaskPathError,
)
from yascheduler.domain.model import (
    ConnectedMachine,
    MachineState,
    NodeId,
    Running,
    Task,
    TaskId,
)
from yascheduler.infra.ssh.operations import TaskDeployer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from yascheduler.domain import MachineSession

pytestmark = pytest.mark.unit

DEPLOYMENT_LOGGER = "yascheduler.infra.ssh.operations.deployment"
TASK_ID = TaskId(7)
TASK_DIR = "/remote/tasks/7"


# =============================================================================
# Fakes — in-memory SFTP destination + MachineSession
# =============================================================================


class FakeSFTPFile:
    """Async context manager over one remote file; write() records or raises."""

    def __init__(self, client: FakeSFTPClient, path: str) -> None:
        self._client = client
        self._path = path

    async def __aenter__(self) -> FakeSFTPFile:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def write(self, data: bytes) -> None:
        err = self._client.write_errors.get(self._path)
        if err is not None:
            raise err
        self._client.write_calls.append((self._path, data))
        self._client.files[self._path] = data


class FakeSFTPClient:
    """SFTP destination recording every call as a POSIX-separated path string.

    ``types`` describes the pre-existing remote tree (path -> FILEXFER_TYPE_*).
    A path absent from it raises ``asyncssh.misc.Error(FX_NO_SUCH_FILE)`` from
    lstat, exactly like a real server. ``mkdir_errors``/``lstat_errors``/
    ``write_errors`` inject one failure per destination path.
    """

    def __init__(self, root: str) -> None:
        self.root = root
        self.types: dict[str, int] = {}
        self.files: dict[str, bytes] = {}
        self.makedirs_calls: list[str] = []
        self.lstat_calls: list[str] = []
        self.open_calls: list[tuple[str, str]] = []
        self.write_calls: list[tuple[str, bytes]] = []
        self.mkdir_errors: dict[str, BaseException] = {}
        self.lstat_errors: dict[str, BaseException] = {}
        self.write_errors: dict[str, BaseException] = {}

    @property
    def mutation_paths(self) -> list[str]:
        """Every destination path an upload mutated via mkdir or open."""
        return [*self.makedirs_calls, *(path for path, _mode in self.open_calls)]

    async def realpath(self, path: str) -> str:
        """Return the configured remote home directory."""
        return self.root

    async def lstat(self, path: str) -> SimpleNamespace:
        """Return the non-following type of ``path`` or raise "no such file"."""
        self.lstat_calls.append(path)
        err = self.lstat_errors.get(path)
        if err is not None:
            raise err
        kind = self.types.get(path)
        if kind is None:
            raise asyncssh.misc.Error(FX_NO_SUCH_FILE, "No such file")
        return SimpleNamespace(type=kind)

    async def makedirs(self, path: str, exist_ok: bool = False) -> None:
        """Create ``path`` (recorded even when the injected failure raises)."""
        self.makedirs_calls.append(path)
        err = self.mkdir_errors.get(path)
        if err is not None:
            raise err
        self.types.setdefault(path, FILEXFER_TYPE_DIRECTORY)

    def open(self, path: str, mode: str) -> FakeSFTPFile:
        """Start a binary write to ``path`` (recorded even when write fails)."""
        self.open_calls.append((path, mode))
        return FakeSFTPFile(self, path)


def _identity_quote(value: str) -> str:
    """Quote a spawn-command argument (no shell quoting needed in these tests)."""
    return value


class FakeMachineSession:
    """Structural MachineSession fake recording state transitions and spawn calls."""

    def __init__(
        self,
        *,
        hostname: str = "10.0.0.1",
        path_type: type[PurePath] = PurePosixPath,
        root: str = "/home/user",
    ) -> None:
        self._hostname = hostname
        self._path_type = path_type
        self._machine = ConnectedMachine(
            node_id=NodeId(1),
            platforms=("linux",),
            state=MachineState.FREE,
        )
        self.sftp = FakeSFTPClient(root)
        self.is_closed = False
        self.occupy_calls = 0
        self.release_calls = 0
        self.update_calls: list[ConnectedMachine] = []
        self.open_sftp_calls = 0
        self.spawn_calls: list[tuple[str, str | None]] = []

    @property
    def hostname(self) -> str:
        return self._hostname

    @property
    def machine(self) -> ConnectedMachine:
        return self._machine

    @property
    def path(self) -> type[PurePath]:
        return self._path_type

    @property
    def quote(self) -> Callable[[str], str]:
        return _identity_quote

    def occupy(self) -> None:
        self.occupy_calls += 1
        self._machine = self._machine.occupy()

    def release(self) -> None:
        self.release_calls += 1
        self._machine = self._machine.release()

    def update(self, machine: ConnectedMachine) -> None:
        self.update_calls.append(machine)
        self._machine = machine

    async def run_bg(self, cmd: str, *, cwd: str | None = None) -> None:
        self.spawn_calls.append((cmd, cwd))

    def open_sftp(self) -> AbstractAsyncContextManager[FakeSFTPClient]:
        self.open_sftp_calls += 1
        return self._sftp_scope()

    @asynccontextmanager
    async def _sftp_scope(self) -> AsyncGenerator[FakeSFTPClient, None]:
        yield self.sftp


# =============================================================================
# Helpers
# =============================================================================


def _make_engine() -> Engine:
    """Minimal Engine whose spawn command needs no shell quoting."""
    return Engine(name="test_engine", spawn="{engine_path} {task_path} {ncpus}")


def _make_task(
    entries: Sequence[TaskInputFile | TaskInputDirectory] = (),
    *,
    remote_folder: str = TASK_DIR,
) -> Task[Running]:
    """Build a RUNNING task carrying the raw stored snapshot (not normalized)."""
    return Task(
        task_id=TASK_ID,
        label="task-7",
        engine="test_engine",
        state=Running(allocated_node_id=NodeId(1), remote_folder=remote_folder),
        input_entries=tuple(entries),
    )


async def _deploy(session: FakeMachineSession, task: Task[Running]) -> None:
    """Run the real deployer against the fake session."""
    await TaskDeployer().start_task_on_machine(
        cast("MachineSession", session),
        _make_engine(),
        task,
        4,
        PurePosixPath("/engines"),
    )


def _assert_upload_failed_without_spawn(session: FakeMachineSession) -> None:
    """Upload failure SHALL prevent spawn and leave the session released to FREE."""
    assert session.spawn_calls == [], "spawn ran despite an upload failure"
    assert session.update_calls, "rollback did not reach session.update()"
    assert session.update_calls[-1].state is MachineState.FREE
    assert session.machine.state is MachineState.FREE


# =============================================================================
# Upload failures: no spawn, BUSY rollback
# =============================================================================


class TestUploadFailureRollback:
    """Every upload failure or cancellation SHALL prevent spawn and release."""

    async def test_root_mkdir_failure_aborts_and_releases(self) -> None:
        """A failing task-root mkdir aborts before any write and rolls BUSY back."""
        session = FakeMachineSession()
        err = asyncssh.misc.Error(FX_FAILURE, "Failure")
        session.sftp.mkdir_errors[TASK_DIR] = err

        with pytest.raises(asyncssh.misc.Error) as exc_info:
            await _deploy(session, _make_task((TaskInputFile("a.txt", b"a"),)))

        assert exc_info.value is err
        assert session.sftp.makedirs_calls == [TASK_DIR]
        assert session.sftp.open_calls == []
        _assert_upload_failed_without_spawn(session)

    async def test_file_write_failure_mid_tree_propagates_original_error(self) -> None:
        """A failure on the second file writes the first, skips the rest, releases."""
        session = FakeMachineSession()
        err = asyncssh.misc.Error(FX_FAILURE, "Disk full")
        session.sftp.write_errors[f"{TASK_DIR}/b.txt"] = err

        with pytest.raises(asyncssh.misc.Error) as exc_info:
            await _deploy(
                session,
                _make_task(
                    (
                        TaskInputFile("a.txt", b"alpha"),
                        TaskInputFile("b.txt", b"beta"),
                        TaskInputFile("c.txt", b"gamma"),
                    )
                ),
            )

        assert exc_info.value is err
        assert session.sftp.write_calls == [(f"{TASK_DIR}/a.txt", b"alpha")]
        assert [path for path, _mode in session.sftp.open_calls] == [
            f"{TASK_DIR}/a.txt",
            f"{TASK_DIR}/b.txt",
        ]
        _assert_upload_failed_without_spawn(session)

    async def test_cancellation_during_upload_prevents_spawn(self) -> None:
        """CancelledError during a file write propagates and still rolls BUSY back."""
        session = FakeMachineSession()
        session.sftp.write_errors[f"{TASK_DIR}/a.txt"] = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await _deploy(session, _make_task((TaskInputFile("a.txt", b"a"),)))

        assert session.sftp.write_calls == []
        _assert_upload_failed_without_spawn(session)


# =============================================================================
# Trust boundary: unsafe stored entries never reach SFTP
# =============================================================================


class TestUnsafeStoredEntries:
    """Unsafe stored paths SHALL raise before the first SFTP mutation."""

    @pytest.mark.parametrize(
        ("entries", "expected_error", "expected_path"),
        [
            (
                (TaskInputFile("/etc/passwd", b"x"),),
                UnsafeTaskPathError,
                "/etc/passwd",
            ),
            (
                (TaskInputFile("../escape.txt", b"x"),),
                UnsafeTaskPathError,
                "../escape.txt",
            ),
            (
                (
                    TaskInputFile("dup.txt", b"1"),
                    TaskInputFile("dup.txt", b"2"),
                ),
                InvalidTaskSnapshotError,
                "dup.txt",
            ),
            (
                (
                    TaskInputFile("sub", b"x"),
                    TaskInputFile("sub/child.txt", b"y"),
                ),
                InvalidTaskSnapshotError,
                "sub",
            ),
        ],
        ids=[
            "absolute-path",
            "parent-component",
            "duplicate-path",
            "file-ancestor-conflict",
        ],
    )
    async def test_unsafe_snapshot_rejected_before_sftp(
        self,
        entries: tuple[TaskInputFile | TaskInputDirectory, ...],
        expected_error: type[UnsafeTaskPathError | InvalidTaskSnapshotError],
        expected_path: str,
    ) -> None:
        """A domain error naming the offending path precedes any remote mutation."""
        session = FakeMachineSession()

        with pytest.raises(expected_error) as exc_info:
            await _deploy(session, _make_task(entries))

        assert exc_info.value.path == expected_path
        assert session.sftp.mutation_paths == []
        _assert_upload_failed_without_spawn(session)


# =============================================================================
# Destination confinement: symlinks and file/directory conflicts
# =============================================================================


class TestDestinationConfinement:
    """Destination traversal SHALL reject symlinks and type conflicts."""

    async def test_symlink_at_file_target_rejected(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A symlink at a file target fails without opening the link."""
        link = f"{TASK_DIR}/sub/link.txt"
        session = FakeMachineSession()
        session.sftp.types[link] = FILEXFER_TYPE_SYMLINK

        with (
            caplog.at_level(logging.DEBUG, logger=DEPLOYMENT_LOGGER),
            pytest.raises(UnsafeTaskPathError) as exc_info,
        ):
            await _deploy(session, _make_task((TaskInputFile("sub/link.txt", b"x"),)))

        assert link in str(exc_info.value)
        assert exc_info.value.path == link
        assert session.sftp.open_calls == []
        _assert_upload_failed_without_spawn(session)

        records = [
            record
            for record in caplog.records
            if record.getMessage() == "DESTINATION_REJECTED"
        ]
        assert len(records) == 1
        assert records[0].name == DEPLOYMENT_LOGGER
        assert records[0].levelno == logging.DEBUG
        fields = extra_fields(records[0])
        assert fields["path"] == link
        assert fields["reason"] == "symbolic link"
        assert fields["task_id"] == TASK_ID

    async def test_symlink_at_directory_target_rejected(self) -> None:
        """A symlink at a directory target fails without mkdir through the link."""
        link = f"{TASK_DIR}/sub/linkdir"
        session = FakeMachineSession()
        session.sftp.types[link] = FILEXFER_TYPE_SYMLINK

        with pytest.raises(UnsafeTaskPathError) as exc_info:
            await _deploy(session, _make_task((TaskInputDirectory("sub/linkdir"),)))

        assert link in str(exc_info.value)
        assert exc_info.value.path == link
        assert link not in session.sftp.makedirs_calls
        assert session.sftp.open_calls == []
        _assert_upload_failed_without_spawn(session)

    async def test_file_target_existing_as_directory_rejected(self) -> None:
        """A directory at a file target fails, naming the path, without writing."""
        path = f"{TASK_DIR}/data.txt"
        session = FakeMachineSession()
        session.sftp.types[path] = FILEXFER_TYPE_DIRECTORY

        with pytest.raises(UnsafeTaskPathError) as exc_info:
            await _deploy(session, _make_task((TaskInputFile("data.txt", b"x"),)))

        assert path in str(exc_info.value)
        assert exc_info.value.path == path
        assert session.sftp.open_calls == []
        _assert_upload_failed_without_spawn(session)

    async def test_directory_target_existing_as_regular_file_rejected(self) -> None:
        """A regular file at a directory target fails, naming the path, no mkdir."""
        path = f"{TASK_DIR}/out"
        session = FakeMachineSession()
        session.sftp.types[path] = FILEXFER_TYPE_REGULAR

        with pytest.raises(UnsafeTaskPathError) as exc_info:
            await _deploy(session, _make_task((TaskInputDirectory("out"),)))

        assert path in str(exc_info.value)
        assert exc_info.value.path == path
        assert path not in session.sftp.makedirs_calls
        _assert_upload_failed_without_spawn(session)


# =============================================================================
# Successful replacement of an existing ordinary file
# =============================================================================


class TestExistingFileReplacement:
    """An existing ordinary regular file at a file target SHALL be replaced."""

    async def test_existing_regular_file_is_rewritten_in_binary_mode(self) -> None:
        """The stored bytes replace the old content and spawn follows the upload."""
        path = f"{TASK_DIR}/a.txt"
        session = FakeMachineSession()
        session.sftp.types[path] = FILEXFER_TYPE_REGULAR
        session.sftp.files[path] = b"stale"

        await _deploy(session, _make_task((TaskInputFile("a.txt", b"fresh"),)))

        assert session.sftp.open_calls == [(path, "wb")]
        assert session.sftp.files[path] == b"fresh"
        assert len(session.spawn_calls) == 1
        assert session.spawn_calls[0][1] == TASK_DIR
        assert session.update_calls == []
        assert session.machine.state is MachineState.BUSY


# =============================================================================
# lstat failure propagation
# =============================================================================


class TestLstatFailurePropagation:
    """Only "no such file" means absent; other lstat errors abort the upload."""

    async def test_permission_denied_on_lstat_propagates_without_write(self) -> None:
        """Permission denied on lstat is not treated as an absent destination."""
        target = f"{TASK_DIR}/sub/data.bin"
        session = FakeMachineSession()
        err = asyncssh.misc.Error(FX_PERMISSION_DENIED, "Permission denied")
        session.sftp.lstat_errors[target] = err

        with pytest.raises(asyncssh.misc.Error) as exc_info:
            await _deploy(session, _make_task((TaskInputFile("sub/data.bin", b"x"),)))

        assert exc_info.value is err
        assert session.sftp.open_calls == []
        _assert_upload_failed_without_spawn(session)


# =============================================================================
# Remote platform path joining
# =============================================================================


class TestRemotePlatformPathJoining:
    """Destinations SHALL be joined in the remote flavor and sent POSIX-separated."""

    @pytest.mark.parametrize(
        ("path_type", "remote_folder", "root", "prefix"),
        [
            (PureWindowsPath, "C:/tasks", "C:/Users/user", "C:/tasks"),
            (PurePosixPath, "/tasks", "/home/user", "/tasks"),
        ],
        ids=["windows", "posix"],
    )
    async def test_sftp_calls_use_posix_separated_remote_paths(
        self,
        path_type: type[PurePath],
        remote_folder: str,
        root: str,
        prefix: str,
    ) -> None:
        """Every mkdir/lstat/open receives '<prefix>/...' with forward slashes only."""
        session = FakeMachineSession(path_type=path_type, root=root)
        entries = (
            TaskInputDirectory("empty"),
            TaskInputFile("sub/data.bin", b"\x00\x01"),
        )

        await _deploy(session, _make_task(entries, remote_folder=remote_folder))

        nested_file = f"{prefix}/sub/data.bin"
        assert session.sftp.makedirs_calls == [
            prefix,
            f"{prefix}/empty",
            f"{prefix}/sub",
        ]
        assert session.sftp.open_calls == [(nested_file, "wb")]
        assert session.sftp.lstat_calls == [
            prefix,
            f"{prefix}/empty",
            f"{prefix}/sub",
            nested_file,
        ]
        every_path = [
            *session.sftp.makedirs_calls,
            *session.sftp.lstat_calls,
            *(path for path, _mode in session.sftp.open_calls),
        ]
        assert all("\\" not in path for path in every_path)
        assert len(session.spawn_calls) == 1
