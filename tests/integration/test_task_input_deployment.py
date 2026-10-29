# region MODULE_CONTRACT
# PURPOSE: Verify via a real Docker SSH server that TaskDeployer reconstructs a task's stored input tree under the remote task root and only then runs spawn, and that an unsafe destination prevents spawn and returns the session to FREE.
# SCOPE: Docker SSH fixture, repository/session setup, nested binary/hidden/empty file and empty-directory snapshot deployment, exact remote byte/path checks, symbolic-link and file/directory-conflict rejection with rollback, replacement of a stale ordinary file, and cleanup.
# DEPENDENCIES: USES API: testcontainers (Docker SSH server), asyncssh SFTP
# KEYWORDS: TaskDeployer, input snapshot, SFTP, binary bytes, empty directory, spawn ordering, symbolic link, destination conflict, rollback
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

import asyncssh
import pytest
from asyncssh.sftp import FILEXFER_TYPE_SYMLINK
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from yascheduler.domain import (
    Engine,
    TaskInputDirectory,
    TaskInputFile,
    TaskInputSnapshot,
    UnsafeTaskPathError,
    normalize_input_snapshot,
)
from yascheduler.domain.model import (
    MachineState,
    Node,
    NodeId,
    Running,
    RunningTask,
    Task,
    TaskId,
)
from yascheduler.infra.ssh.operations import TaskDeployer
from yascheduler.infra.ssh.repository import SSHMachineRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from asyncssh.sftp import SFTPClient

    from yascheduler.domain import MachineSession

pytestmark = pytest.mark.integration

BINARY_BYTES = b"\x00\x01\x02binary\x80\xff\x00end\xf0\x28\x8c\x28"
HIDDEN_BYTES = b"hidden\x00bytes"
CACHE_KEY_BYTES = b".cache-key\xff"
LINKED_BYTES = b"through-link\x00bytes"
CONFLICT_BYTES = b"conflicting-entry"
STALE_INPUT_BYTES = b"stale"
FRESH_INPUT_BYTES = b"\x00fresh\xffinput\x00"


class SSHContainerInfo(TypedDict):
    """Connection fields yielded by the Docker SSH container fixture."""

    host: str
    port: int
    username: str
    key_path: PurePosixPath


@pytest.fixture(scope="session")
async def ssh_container(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[SSHContainerInfo, None]:
    """Start a Docker SSH server and generate the key pair used to connect to it."""
    key_dir = tmp_path_factory.mktemp("ssh_keys")
    key_path = key_dir / "id_rsa"

    key = asyncssh.generate_private_key("ssh-rsa")
    public_key_str = key.export_public_key("openssh").decode().strip()
    key.write_private_key(str(key_path))

    container = DockerContainer("serversideup/docker-ssh")
    container.with_env("SSH_USER", "testuser")
    container.with_env("AUTHORIZED_KEYS", public_key_str)
    container.with_env("ALLOWED_IPS", "AllowUsers testuser")
    container.with_exposed_ports(2222)
    container.waiting_for(LogMessageWaitStrategy("Server listening on"))

    container.start()
    try:
        await asyncio.sleep(1)

        yield {
            "host": container.get_container_host_ip(),
            "port": int(container.get_exposed_port(2222)),
            "username": "testuser",
            "key_path": PurePosixPath(str(key_path)),
        }
    finally:
        container.stop()


@pytest.fixture
async def repository(
    ssh_container: SSHContainerInfo,
) -> AsyncGenerator[SSHMachineRepository, None]:
    """Connect an SSHMachineRepository to the Docker SSH server."""
    repo = SSHMachineRepository()
    await repo.connect(
        node=_container_node(1, ssh_container),
        client_keys=[ssh_container["key_path"]],
    )
    try:
        yield repo
    finally:
        await repo.disconnect_all()


def _container_node(node_id: int, ssh_container: SSHContainerInfo) -> Node:
    """Build a Node pointing at the testcontainers SSH endpoint."""
    return Node(
        node_id=NodeId(node_id),
        hostname=ssh_container["host"],
        ncpus=4,
        enabled=True,
        cloud=None,
        username=ssh_container["username"],
        port=ssh_container["port"],
    )


def _get_session(repository: SSHMachineRepository) -> MachineSession:
    """Return the repository's connected FREE session."""
    sessions = repository.list_free(None)
    assert len(sessions) == 1
    return sessions[0]


async def _remote_tree(sftp: SFTPClient, root: PurePosixPath) -> dict[str, str]:
    """Return a relative-path-to-kind mapping for the complete remote tree."""
    tree: dict[str, str] = {}

    async def _walk(directory: PurePosixPath, prefix: str) -> None:
        for entry in await sftp.readdir(directory):
            name = str(entry.filename)
            if name in (".", ".."):
                continue
            relative = f"{prefix}/{name}" if prefix else name
            path = directory / name
            if await sftp.isdir(path):
                tree[relative] = "directory"
                await _walk(path, relative)
            elif await sftp.isfile(path):
                tree[relative] = "file"
            else:
                tree[relative] = "other"

    await _walk(root, "")
    return tree


async def _read_remote_file(sftp: SFTPClient, path: PurePosixPath) -> bytes:
    """Read a remote file in binary mode and return its exact bytes."""
    async with sftp.open(path, "rb") as remote_file:
        return await remote_file.read()


async def _run_remote_checked(session: MachineSession, command: str) -> str:
    """Run a remote shell command, require exit code zero, and return stdout."""
    result = await session.run(command)
    assert result.exit_code == 0, result.stderr
    return result.stdout


async def _remote_is_file(session: MachineSession, path: PurePosixPath) -> bool:
    """Return whether a remote path is an existing regular file."""
    result = await session.run(f"test -f {session.quote(str(path))}")
    return result.exit_code == 0


async def _remove_remote_paths(
    session: MachineSession,
    *paths: PurePosixPath,
) -> None:
    """Remove remote paths recursively as test cleanup."""
    quoted = " ".join(session.quote(str(path)) for path in paths)
    await _run_remote_checked(session, f"rm -rf -- {quoted}")


def _build_deployment(
    remote_root: PurePosixPath,
    snapshot: TaskInputSnapshot,
    spawn: str,
) -> tuple[Engine, RunningTask]:
    """Build the engine and RUNNING task that deploys one snapshot."""
    engine = Engine(name="snapshot_engine", spawn=spawn)
    task: RunningTask = Task(
        task_id=TaskId(1),
        engine=engine.name,
        label="snapshot-unsafe-destination",
        state=Running(allocated_node_id=NodeId(1), remote_folder=str(remote_root)),
        input_entries=snapshot,
    )
    return engine, task


async def test_deploy_reconstructs_stored_input_tree(
    repository: SSHMachineRepository,
) -> None:
    """Stored dirs/files land at exact paths and bytes before spawn creates its marker."""
    session = _get_session(repository)
    remote_root = PurePosixPath("/tmp") / f"yascheduler-task-inputs-{uuid4().hex}"

    snapshot = normalize_input_snapshot(
        [
            TaskInputFile(path="sub/deep/data.bin", content=BINARY_BYTES),
            TaskInputFile(path=".hidden", content=HIDDEN_BYTES),
            TaskInputFile(path=".cache/key", content=CACHE_KEY_BYTES),
            TaskInputFile(path="empty.txt", content=b""),
            TaskInputDirectory(path="emptydir"),
        ]
    )
    engine = Engine(
        name="snapshot_engine",
        spawn=(
            "test -f {task_path}/sub/deep/data.bin"
            " && test -f {task_path}/.hidden"
            " && test -f {task_path}/.cache/key"
            " && test -f {task_path}/empty.txt"
            " && test -d {task_path}/emptydir"
            " && touch {task_path}/__spawned__"
        ),
    )
    task: RunningTask = Task(
        task_id=TaskId(1),
        engine=engine.name,
        label="snapshot-deployment",
        state=Running(allocated_node_id=NodeId(1), remote_folder=str(remote_root)),
        input_entries=snapshot,
    )

    expected_files = {
        entry.path: entry.content
        for entry in snapshot
        if isinstance(entry, TaskInputFile)
    }
    expected_directories = {
        entry.path for entry in snapshot if isinstance(entry, TaskInputDirectory)
    }
    expected_tree = {
        **dict.fromkeys(expected_directories, "directory"),
        **dict.fromkeys(expected_files, "file"),
    }

    assert set(expected_files) == {
        ".cache/key",
        ".hidden",
        "empty.txt",
        "sub/deep/data.bin",
    }
    assert "emptydir" in expected_directories

    try:
        await TaskDeployer().start_task_on_machine(
            session,
            engine,
            task,
            ncpus=4,
            engines_dir=PurePosixPath("/tmp/engines"),
        )

        async with session.open_sftp() as sftp:
            remote_tree = await _remote_tree(sftp, remote_root)

            # Exact reconstruction: the snapshot paths plus the spawn marker,
            # with no unrecorded files or directories.
            assert remote_tree == {
                **expected_tree,
                "__spawned__": "file",
            }

            for path, expected_bytes in expected_files.items():
                assert await _read_remote_file(sftp, remote_root / path) == (
                    expected_bytes
                )

            empty_file = remote_root / "empty.txt"
            empty_file_attrs = await sftp.stat(empty_file)
            assert empty_file_attrs.size == 0
            assert await sftp.isfile(empty_file)

            empty_directory = remote_root / "emptydir"
            assert await sftp.isdir(empty_directory)

            assert await sftp.isfile(remote_root / "__spawned__")
    finally:
        result = await session.run(
            f"rm -rf -- {session.quote(str(remote_root))}",
        )
        assert result.exit_code == 0


async def test_deploy_rejects_symlinked_destination_directory(
    repository: SSHMachineRepository,
) -> None:
    """A symlink where the snapshot needs a parent directory is rejected before spawn."""
    session = _get_session(repository)
    remote_root = PurePosixPath("/tmp") / f"yascheduler-symlink-{uuid4().hex}"
    link_target = PurePosixPath("/tmp") / f"yascheduler-link-target-{uuid4().hex}"
    linked_directory = remote_root / "linked"
    snapshot = normalize_input_snapshot(
        [TaskInputFile(path="linked/data.bin", content=LINKED_BYTES)]
    )
    engine, task = _build_deployment(
        remote_root,
        snapshot,
        "touch {task_path}/__spawned__",
    )

    try:
        # The link target is a sibling directory the deployer must never write into.
        await _run_remote_checked(
            session,
            f"mkdir -p {session.quote(str(remote_root))}"
            f" {session.quote(str(link_target))}",
        )
        await _run_remote_checked(
            session,
            f"ln -s {session.quote(str(link_target))}"
            f" {session.quote(str(linked_directory))}",
        )

        with pytest.raises(UnsafeTaskPathError) as excinfo:
            await TaskDeployer().start_task_on_machine(
                session,
                engine,
                task,
                ncpus=4,
                engines_dir=PurePosixPath("/tmp/engines"),
            )

        assert excinfo.value.path == str(linked_directory)
        assert "symbolic link" in excinfo.value.reason
        # The same session object was rolled back from BUSY to FREE.
        assert session.machine.state == MachineState.FREE

        async with session.open_sftp() as sftp:
            linked_attrs = await sftp.lstat(str(linked_directory))
            assert linked_attrs.type == FILEXFER_TYPE_SYMLINK

        # Nothing was written through the link: the target directory stays empty
        # and the spawn marker never appeared.
        assert (
            await _run_remote_checked(
                session,
                f"ls -A {session.quote(str(link_target))}",
            )
            == ""
        )
        assert not await _remote_is_file(session, link_target / "data.bin")
        assert not await _remote_is_file(session, remote_root / "__spawned__")
    finally:
        await _remove_remote_paths(session, remote_root, link_target)


async def test_deploy_rejects_file_where_snapshot_needs_directory(
    repository: SSHMachineRepository,
) -> None:
    """A regular file at a snapshot directory path is rejected before spawn."""
    session = _get_session(repository)
    remote_root = PurePosixPath("/tmp") / f"yascheduler-conflict-{uuid4().hex}"
    conflicting_entry = remote_root / "sub"
    snapshot = normalize_input_snapshot(
        [TaskInputFile(path="sub/data.bin", content=CONFLICT_BYTES)]
    )
    engine, task = _build_deployment(
        remote_root,
        snapshot,
        "touch {task_path}/__spawned__",
    )

    try:
        await _run_remote_checked(
            session, f"mkdir -p {session.quote(str(remote_root))}"
        )
        await _run_remote_checked(
            session,
            f"printf %s {session.quote(CONFLICT_BYTES.decode())}"
            f" > {session.quote(str(conflicting_entry))}",
        )

        with pytest.raises(UnsafeTaskPathError) as excinfo:
            await TaskDeployer().start_task_on_machine(
                session,
                engine,
                task,
                ncpus=4,
                engines_dir=PurePosixPath("/tmp/engines"),
            )

        # Normalization materializes the parent directory entry for
        # "sub/data.bin", so the conflict is reported against "sub".
        assert excinfo.value.path == str(conflicting_entry)
        assert "non-directory" in excinfo.value.reason
        assert session.machine.state == MachineState.FREE

        # The conflicting file was neither replaced nor removed, and no spawn
        # marker appeared.
        async with session.open_sftp() as sftp:
            assert await _read_remote_file(sftp, conflicting_entry) == CONFLICT_BYTES
        assert not await _remote_is_file(session, remote_root / "__spawned__")
    finally:
        await _remove_remote_paths(session, remote_root)


async def test_deploy_replaces_existing_ordinary_file(
    repository: SSHMachineRepository,
) -> None:
    """An existing ordinary file at a snapshot file path is replaced with new bytes."""
    session = _get_session(repository)
    remote_root = PurePosixPath("/tmp") / f"yascheduler-replace-{uuid4().hex}"
    stale_input = remote_root / "INPUT"
    snapshot = normalize_input_snapshot(
        [TaskInputFile(path="INPUT", content=FRESH_INPUT_BYTES)]
    )
    engine, task = _build_deployment(
        remote_root,
        snapshot,
        "test -f {task_path}/INPUT && touch {task_path}/__spawned__",
    )

    try:
        await _run_remote_checked(
            session, f"mkdir -p {session.quote(str(remote_root))}"
        )
        await _run_remote_checked(
            session,
            f"printf %s {session.quote(STALE_INPUT_BYTES.decode())}"
            f" > {session.quote(str(stale_input))}",
        )
        async with session.open_sftp() as sftp:
            assert await _read_remote_file(sftp, stale_input) == STALE_INPUT_BYTES

        assert (
            await TaskDeployer().start_task_on_machine(
                session,
                engine,
                task,
                ncpus=4,
                engines_dir=PurePosixPath("/tmp/engines"),
            )
            is True
        )

        async with session.open_sftp() as sftp:
            assert await _read_remote_file(sftp, stale_input) == FRESH_INPUT_BYTES
        assert await _remote_is_file(session, remote_root / "__spawned__")
        # A successful deployment keeps the session BUSY for the allocation.
        assert session.machine.state == MachineState.BUSY
    finally:
        await _remove_remote_paths(session, remote_root)
