# region MODULE_CONTRACT
# PURPOSE: Verify via a real Docker SSH server that TaskDeployer reconstructs a task's stored input tree under the remote task root and only then runs spawn.
# SCOPE: Docker SSH fixture, repository/session setup, nested binary/hidden/empty file and empty-directory snapshot deployment, exact remote byte/path checks, and cleanup.
# DEPENDENCIES: USES API: testcontainers (Docker SSH server), asyncssh SFTP
# KEYWORDS: TaskDeployer, input snapshot, SFTP, binary bytes, empty directory, spawn ordering
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

import asyncssh
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from yascheduler.domain import (
    Engine,
    TaskInputDirectory,
    TaskInputFile,
    normalize_input_snapshot,
)
from yascheduler.domain.model import (
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
