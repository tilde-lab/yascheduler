# region MODULE_CONTRACT
# PURPOSE: Verify via a real Docker SSH server that OutputDownloader resolves task-relative output selections against a real remote task root over SFTP and recreates the selected tree under a local result root, rejecting links, unsupported entry types, and unsafe local destinations while never deleting the remote tree.
# SCOPE: Docker SSH fixture plus repository/session helpers; remote tree construction with nested text and binary files, hidden entries, empty directories, a task-root file, symbolic links, and a fifo; exact layout and byte checks for an explicit directory with an overlapping glob (each resolved path transferred once); permanent per-path link/fifo errors beside a successful selection; unmatched glob permanent error beside a successful explicit file; local destination symlink and file/directory conflict rejection with the link target untouched; a second merge pass replacing selected files while retaining an unrelated local entry; remote tree preservation after download.
# DEPENDENCIES: USES API: testcontainers (Docker SSH server), asyncssh SFTP
# KEYWORDS: OutputDownloader, download_outputs, output selection, recursive retrieval, SFTP, binary bytes, hidden entries, empty directory, symbolic link, fifo, destination conflict, merge, remote preservation
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

import asyncssh
import pytest
from asyncssh.sftp import (
    FILEXFER_TYPE_DIRECTORY,
    FILEXFER_TYPE_REGULAR,
    FILEXFER_TYPE_SYMLINK,
)
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from yascheduler.domain import UnsafeTaskPathError
from yascheduler.domain.model import Node, NodeId, TaskId
from yascheduler.infra.ssh.operations import OutputDownloader
from yascheduler.infra.ssh.repository import SSHMachineRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from pathlib import Path

    from asyncssh.sftp import SFTPClient

    from yascheduler.domain import MachineSession

pytestmark = pytest.mark.integration

TEXT_RESULT_BYTES = b"result row 1\nresult row 2\n"
NESTED_BINARY_BYTES = b"\x00\x01\x02binary\xff\xfe\x00end"
HIDDEN_FILE_BYTES = b"hidden\x00file"
HIDDEN_DIR_BYTES = b".cache\x00payload\xff"
SUMMARY_BYTES = b"summary at task root\x00"
TREE_KEEP_BYTES = b"kept inside the selected tree\n"
LINKED_TARGET_BYTES = b"link target\x00must never be copied"
PRESENT_BYTES = b"explicit selection still lands\x00"
INNER_BYTES = b"blocked directory child"
BLOCKED_FILE_BYTES = b"blocked file payload"
CONFLICT_LOCAL_BYTES = b"local file must not become a directory"
LOCAL_LINK_TARGET_BYTES = b"local symlink target stays untouched\x00"
FRESH_OUTPUT_BYTES = b"\x00fresh output\xff"
FRESH_BINARY_BYTES = b"\x00nested fresh\xfe"
STALE_OUTPUT_BYTES = b"stale local bytes"
UNRELATED_LOCAL_BYTES = b"unrelated local file survives"


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


def _unique_remote_root(label: str) -> PurePosixPath:
    """Return a unique remote directory path for one test's task root."""
    return PurePosixPath("/tmp") / f"yascheduler-{label}-{uuid4().hex}"


async def _run_remote_checked(session: MachineSession, command: str) -> str:
    """Run a remote shell command, require exit code zero, and return stdout."""
    result = await session.run(command)
    assert result.exit_code == 0, result.stderr
    return result.stdout


async def _make_remote_directories(
    session: MachineSession, *directories: PurePosixPath
) -> None:
    """Create every given remote directory, including missing parents."""
    quoted = " ".join(session.quote(str(directory)) for directory in directories)
    await _run_remote_checked(session, f"mkdir -p -- {quoted}")


async def _write_remote_files(
    session: MachineSession,
    root: PurePosixPath,
    files: Mapping[str, bytes],
) -> None:
    """Write binary remote files under ``root``, creating their parents first."""
    parent_directories = []
    for relative in files:
        parent = PurePosixPath(relative).parent
        if str(parent) != ".":
            parent_directories.append(root / parent)
    if parent_directories:
        await _make_remote_directories(session, *parent_directories)
    async with session.open_sftp() as sftp:
        for relative, content in files.items():
            async with sftp.open(root / relative, "wb") as remote_file:
                await remote_file.write(content)


async def _make_remote_symlink(
    session: MachineSession,
    link: PurePosixPath,
    target: PurePosixPath,
) -> None:
    """Create one remote symbolic link pointing at ``target``."""
    await _run_remote_checked(
        session,
        f"ln -s {session.quote(str(target))} {session.quote(str(link))}",
    )


async def _make_remote_fifo(session: MachineSession, path: PurePosixPath) -> None:
    """Create one remote fifo, an unsupported entry type for retrieval."""
    await _run_remote_checked(session, f"mkfifo {session.quote(str(path))}")


async def _remote_tree(sftp: SFTPClient, root: PurePosixPath) -> dict[str, str]:
    """Return a relative-path-to-kind mapping for the complete remote tree.

    Entries are classified with ``lstat`` so symbolic links are reported as
    links instead of being followed.
    """
    tree: dict[str, str] = {}

    async def _walk(directory: PurePosixPath, prefix: str) -> None:
        for entry in await sftp.readdir(directory):
            name = str(entry.filename)
            if name in (".", ".."):
                continue
            relative = f"{prefix}/{name}" if prefix else name
            path = directory / name
            attrs = await sftp.lstat(path)
            if attrs.type == FILEXFER_TYPE_DIRECTORY:
                tree[relative] = "directory"
                await _walk(path, relative)
            elif attrs.type == FILEXFER_TYPE_REGULAR:
                tree[relative] = "file"
            elif attrs.type == FILEXFER_TYPE_SYMLINK:
                tree[relative] = "symlink"
            else:
                tree[relative] = "other"

    await _walk(root, "")
    return tree


async def _read_remote_file(sftp: SFTPClient, path: PurePosixPath) -> bytes:
    """Read a remote file in binary mode and return its exact bytes."""
    async with sftp.open(path, "rb") as remote_file:
        return await remote_file.read()


def _local_tree(root: Path) -> dict[str, str]:
    """Return a relative-path-to-kind mapping for the complete local tree."""
    tree: dict[str, str] = {}

    def _walk(directory: Path, prefix: str) -> None:
        for entry in directory.iterdir():
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.is_symlink():
                tree[relative] = "symlink"
            elif entry.is_dir():
                tree[relative] = "directory"
                _walk(entry, relative)
            elif entry.is_file():
                tree[relative] = "file"
            else:
                tree[relative] = "other"

    _walk(root, "")
    return tree


def _error_paths(errors: Sequence[tuple[str | None, Exception]]) -> list[str]:
    """Return the reported paths of one retrieval error list."""
    return [path for path, _ in errors if path is not None]


def _retrieved_entry_paths(
    records: Sequence[logging.LogRecord],
    task_id: TaskId,
) -> list[str]:
    """Return the task-relative paths of the per-entry retrieval records."""
    paths: list[str] = []
    for record in records:
        if record.getMessage() != "OUTPUT_ENTRY":
            continue
        if getattr(record, "task_id", None) != task_id:
            continue
        path = getattr(record, "path", None)
        if path is not None:
            paths.append(str(path))
    return paths


async def _remove_remote_paths(
    session: MachineSession,
    *paths: PurePosixPath,
) -> None:
    """Remove remote paths recursively as test cleanup."""
    quoted = " ".join(session.quote(str(path)) for path in paths)
    await _run_remote_checked(session, f"rm -rf -- {quoted}")


async def test_retrieves_nested_tree_with_directory_and_overlapping_glob(
    repository: SSHMachineRepository,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Directory plus overlapping glob recreate the exact tree, each path once."""
    session = _get_session(repository)
    remote_root = _unique_remote_root("outputs-tree")
    result_root = tmp_path / "results"

    try:
        await _make_remote_directories(
            session,
            remote_root,
            remote_root / "OUT.aiida" / "nested",
            remote_root / "OUT.aiida" / ".hidden-dir",
            remote_root / "OUT.aiida" / "emptydir",
        )
        await _write_remote_files(
            session,
            remote_root,
            {
                "OUT.aiida/nested/result.dat": TEXT_RESULT_BYTES,
                "OUT.aiida/nested/blob.bin": NESTED_BINARY_BYTES,
                "OUT.aiida/.hidden-dir/.hidden-file": HIDDEN_DIR_BYTES,
                "OUT.aiida/.hidden-file": HIDDEN_FILE_BYTES,
                "summary.txt": SUMMARY_BYTES,
            },
        )

        expected_layout = {
            "OUT.aiida": "directory",
            "OUT.aiida/.hidden-dir": "directory",
            "OUT.aiida/.hidden-dir/.hidden-file": "file",
            "OUT.aiida/.hidden-file": "file",
            "OUT.aiida/emptydir": "directory",
            "OUT.aiida/nested": "directory",
            "OUT.aiida/nested/blob.bin": "file",
            "OUT.aiida/nested/result.dat": "file",
            "summary.txt": "file",
        }
        expected_bytes = {
            "OUT.aiida/.hidden-dir/.hidden-file": HIDDEN_DIR_BYTES,
            "OUT.aiida/.hidden-file": HIDDEN_FILE_BYTES,
            "OUT.aiida/nested/blob.bin": NESTED_BINARY_BYTES,
            "OUT.aiida/nested/result.dat": TEXT_RESULT_BYTES,
            "summary.txt": SUMMARY_BYTES,
        }

        caplog.set_level(logging.DEBUG)
        (
            local_folder,
            remote_folder,
            transient_errors,
            permanent_errors,
        ) = await OutputDownloader().download_outputs(
            session,
            remote_dir=str(remote_root),
            local_dir=result_root,
            selections=[
                "OUT.aiida",
                "OUT.aiida/nested/*.dat",
                "summary.txt",
            ],
            task_id=TaskId(1),
        )

        assert local_folder == str(result_root)
        assert remote_folder == str(remote_root)
        assert transient_errors == []
        assert permanent_errors == []

        # Exact task-relative layout: hidden entries and the empty directory
        # survive, and the task-root file gains no enclosing directory.
        assert _local_tree(result_root) == expected_layout
        for relative, expected in expected_bytes.items():
            local_path = result_root / PurePosixPath(relative)
            assert local_path.read_bytes() == expected
        assert list((result_root / "OUT.aiida" / "emptydir").iterdir()) == []

        # The explicit directory and the overlapping glob resolve to each path
        # once: the per-entry boundary records every resolved path exactly once,
        # so no path is transferred twice.
        entry_paths = _retrieved_entry_paths(caplog.records, TaskId(1))
        assert sorted(entry_paths) == sorted(expected_layout)
    finally:
        await _remove_remote_paths(session, remote_root)


async def test_remote_symlinks_and_unsupported_entries_are_permanent(
    repository: SSHMachineRepository,
    tmp_path: Path,
) -> None:
    """Remote links and a fifo are permanent per-path errors; other selections land."""
    session = _get_session(repository)
    remote_root = _unique_remote_root("outputs-links")
    link_target_root = _unique_remote_root("outputs-link-target")
    result_root = tmp_path / "results"

    try:
        await _make_remote_directories(
            session,
            remote_root,
            remote_root / "tree",
            link_target_root,
        )
        await _write_remote_files(
            session,
            remote_root,
            {"tree/keep.txt": TREE_KEEP_BYTES},
        )
        await _write_remote_files(
            session,
            link_target_root,
            {"target.txt": LINKED_TARGET_BYTES},
        )
        await _make_remote_symlink(
            session,
            remote_root / "tree" / "link-inside.txt",
            link_target_root / "target.txt",
        )
        await _make_remote_symlink(
            session,
            remote_root / "link-root.txt",
            link_target_root / "target.txt",
        )
        await _make_remote_fifo(session, remote_root / "tree" / "pipe.fifo")

        (
            _local_folder,
            _remote_folder,
            transient_errors,
            permanent_errors,
        ) = await OutputDownloader().download_outputs(
            session,
            remote_dir=str(remote_root),
            local_dir=result_root,
            selections=["tree", "link-root.txt"],
            task_id=TaskId(1),
        )

        assert transient_errors == []
        # An explicitly selected link, a link inside the selected directory,
        # and an unsupported fifo are each reported as a permanent error
        # naming the offending task-relative path.
        assert sorted(_error_paths(permanent_errors)) == [
            "link-root.txt",
            "tree/link-inside.txt",
            "tree/pipe.fifo",
        ]
        assert all(isinstance(err, UnsafeTaskPathError) for _, err in permanent_errors)

        # The remaining selection still lands: the selected tree is retrieved
        # without the link or the fifo.
        assert _local_tree(result_root) == {
            "tree": "directory",
            "tree/keep.txt": "file",
        }
        assert (result_root / "tree" / "keep.txt").read_bytes() == TREE_KEEP_BYTES

        async with session.open_sftp() as sftp:
            # The link target outside the selected tree was never read or
            # copied, and every remote entry is still in place.
            assert await _remote_tree(sftp, link_target_root) == {
                "target.txt": "file",
            }
            assert await _read_remote_file(sftp, link_target_root / "target.txt") == (
                LINKED_TARGET_BYTES
            )
            assert await _remote_tree(sftp, remote_root) == {
                "link-root.txt": "symlink",
                "tree": "directory",
                "tree/keep.txt": "file",
                "tree/link-inside.txt": "symlink",
                "tree/pipe.fifo": "other",
            }
    finally:
        await _remove_remote_paths(session, remote_root, link_target_root)


async def test_unmatched_glob_is_permanent_while_explicit_file_lands(
    repository: SSHMachineRepository,
    tmp_path: Path,
) -> None:
    """A glob matching nothing is a permanent error naming it; others still land."""
    session = _get_session(repository)
    remote_root = _unique_remote_root("outputs-unmatched")
    result_root = tmp_path / "results"

    try:
        await _make_remote_directories(session, remote_root)
        await _write_remote_files(
            session,
            remote_root,
            {"present.txt": PRESENT_BYTES},
        )

        (
            _local_folder,
            _remote_folder,
            transient_errors,
            permanent_errors,
        ) = await OutputDownloader().download_outputs(
            session,
            remote_dir=str(remote_root),
            local_dir=result_root,
            selections=["missing/*.dat", "present.txt"],
            task_id=TaskId(1),
        )

        assert transient_errors == []
        assert _error_paths(permanent_errors) == ["missing/*.dat"]
        assert all(str(err) for _, err in permanent_errors)

        assert _local_tree(result_root) == {"present.txt": "file"}
        assert (result_root / "present.txt").read_bytes() == PRESENT_BYTES
    finally:
        await _remove_remote_paths(session, remote_root)


async def test_unsafe_local_destinations_are_permanent_and_untouched(
    repository: SSHMachineRepository,
    tmp_path: Path,
) -> None:
    """A local destination link and file/directory conflicts are rejected unwritten."""
    session = _get_session(repository)
    remote_root = _unique_remote_root("outputs-conflict")
    result_root = tmp_path / "results"
    link_target_dir = tmp_path / "local-link-target"

    try:
        await _make_remote_directories(
            session,
            remote_root,
            remote_root / "nested",
            remote_root / "blocked-dir",
        )
        await _write_remote_files(
            session,
            remote_root,
            {
                "nested/result.dat": TEXT_RESULT_BYTES,
                "blocked-dir/inner.txt": INNER_BYTES,
                "blocked-file": BLOCKED_FILE_BYTES,
            },
        )

        # Unsafe local destinations: a symlinked directory where the remote has
        # a real one, a regular file where the remote needs a directory, and a
        # directory where the remote needs a regular file.
        result_root.mkdir()
        link_target_dir.mkdir()
        (link_target_dir / "kept.txt").write_bytes(LOCAL_LINK_TARGET_BYTES)
        (result_root / "nested").symlink_to(link_target_dir, target_is_directory=True)
        (result_root / "blocked-dir").write_bytes(CONFLICT_LOCAL_BYTES)
        (result_root / "blocked-file").mkdir()

        (
            _local_folder,
            _remote_folder,
            transient_errors,
            permanent_errors,
        ) = await OutputDownloader().download_outputs(
            session,
            remote_dir=str(remote_root),
            local_dir=result_root,
            selections=["nested", "blocked-dir", "blocked-file"],
            task_id=TaskId(1),
        )

        assert transient_errors == []
        # Every resolved entry whose destination is unsafe is a permanent
        # error for its own task-relative path.
        assert set(_error_paths(permanent_errors)) == {
            "blocked-dir",
            "blocked-dir/inner.txt",
            "blocked-file",
            "nested",
            "nested/result.dat",
        }
        offending_paths = set()
        for _, err in permanent_errors:
            assert isinstance(err, UnsafeTaskPathError)
            offending_paths.add(err.path)
        # The domain error names the offending local component, not only the
        # entry that was being prepared.
        assert offending_paths == {"blocked-dir", "blocked-file", "nested"}

        # Nothing was written through a link or a conflicting entry.
        assert _local_tree(result_root) == {
            "blocked-dir": "file",
            "blocked-file": "directory",
            "nested": "symlink",
        }
        assert (result_root / "blocked-dir").read_bytes() == CONFLICT_LOCAL_BYTES
        assert list((result_root / "blocked-file").iterdir()) == []
        assert (result_root / "nested").is_symlink()

        # The local link target directory keeps exactly its original contents:
        # no selected output was written through the link.
        assert sorted(entry.name for entry in link_target_dir.iterdir()) == [
            "kept.txt",
        ]
        assert (link_target_dir / "kept.txt").read_bytes() == (LOCAL_LINK_TARGET_BYTES)
    finally:
        await _remove_remote_paths(session, remote_root)


async def test_second_pass_replaces_selected_files_and_keeps_unrelated_entry(
    repository: SSHMachineRepository,
    tmp_path: Path,
) -> None:
    """A second retrieval pass replaces selected files and retains unrelated ones."""
    session = _get_session(repository)
    remote_root = _unique_remote_root("outputs-merge")
    result_root = tmp_path / "results"

    try:
        await _make_remote_directories(
            session,
            remote_root,
            remote_root / "OUT" / "nested",
        )
        await _write_remote_files(
            session,
            remote_root,
            {
                "OUT/result.txt": FRESH_OUTPUT_BYTES,
                "OUT/nested/data.bin": FRESH_BINARY_BYTES,
            },
        )

        first = await OutputDownloader().download_outputs(
            session,
            remote_dir=str(remote_root),
            local_dir=result_root,
            selections=["OUT"],
            task_id=TaskId(1),
        )
        assert first[2] == []
        assert first[3] == []
        assert (result_root / "OUT" / "result.txt").read_bytes() == (FRESH_OUTPUT_BYTES)

        # Simulate a stale selected file and an unrelated local result that no
        # selection covers.
        (result_root / "OUT" / "result.txt").write_bytes(STALE_OUTPUT_BYTES)
        unrelated = result_root / "unrelated.log"
        unrelated.write_bytes(UNRELATED_LOCAL_BYTES)

        second = await OutputDownloader().download_outputs(
            session,
            remote_dir=str(remote_root),
            local_dir=result_root,
            selections=["OUT"],
            task_id=TaskId(1),
        )
        assert second[2] == []
        assert second[3] == []

        assert _local_tree(result_root) == {
            "OUT": "directory",
            "OUT/nested": "directory",
            "OUT/nested/data.bin": "file",
            "OUT/result.txt": "file",
            "unrelated.log": "file",
        }
        assert (result_root / "OUT" / "result.txt").read_bytes() == (FRESH_OUTPUT_BYTES)
        assert (result_root / "OUT" / "nested" / "data.bin").read_bytes() == (
            FRESH_BINARY_BYTES
        )
        assert unrelated.read_bytes() == UNRELATED_LOCAL_BYTES
    finally:
        await _remove_remote_paths(session, remote_root)


async def test_download_outputs_keeps_the_remote_tree(
    repository: SSHMachineRepository,
    tmp_path: Path,
) -> None:
    """download_outputs never deletes the remote tree, not even rejected entries."""
    session = _get_session(repository)
    remote_root = _unique_remote_root("outputs-preserved")
    link_target_root = _unique_remote_root("outputs-preserved-target")
    result_root = tmp_path / "results"

    expected_remote_layout = {
        "OUT": "directory",
        "OUT/.hidden": "directory",
        "OUT/.hidden/hidden.txt": "file",
        "OUT/emptydir": "directory",
        "OUT/link": "symlink",
        "OUT/nested": "directory",
        "OUT/nested/result.dat": "file",
        "OUT/pipe.fifo": "other",
        "summary.txt": "file",
    }

    try:
        await _make_remote_directories(
            session,
            remote_root,
            remote_root / "OUT" / ".hidden",
            remote_root / "OUT" / "emptydir",
            remote_root / "OUT" / "nested",
            link_target_root,
        )
        await _write_remote_files(
            session,
            remote_root,
            {
                "OUT/.hidden/hidden.txt": HIDDEN_FILE_BYTES,
                "OUT/nested/result.dat": TEXT_RESULT_BYTES,
                "summary.txt": SUMMARY_BYTES,
            },
        )
        await _write_remote_files(
            session,
            link_target_root,
            {"target.txt": LINKED_TARGET_BYTES},
        )
        await _make_remote_symlink(
            session,
            remote_root / "OUT" / "link",
            link_target_root / "target.txt",
        )
        await _make_remote_fifo(session, remote_root / "OUT" / "pipe.fifo")

        async with session.open_sftp() as sftp:
            assert await _remote_tree(sftp, remote_root) == expected_remote_layout

        (
            _local_folder,
            _remote_folder,
            transient_errors,
            permanent_errors,
        ) = await OutputDownloader().download_outputs(
            session,
            remote_dir=str(remote_root),
            local_dir=result_root,
            selections=["OUT", "summary.txt"],
            task_id=TaskId(1),
        )

        assert transient_errors == []
        assert set(_error_paths(permanent_errors)) == {
            "OUT/link",
            "OUT/pipe.fifo",
        }

        async with session.open_sftp() as sftp:
            assert await sftp.isdir(remote_root)
            # The complete remote tree, including hidden and empty entries and
            # the rejected link and fifo, is unchanged after retrieval.
            assert await _remote_tree(sftp, remote_root) == expected_remote_layout
            assert await _read_remote_file(
                sftp, remote_root / "OUT" / "nested" / "result.dat"
            ) == (TEXT_RESULT_BYTES)
            assert await _read_remote_file(sftp, remote_root / "summary.txt") == (
                SUMMARY_BYTES
            )
            assert await _read_remote_file(sftp, link_target_root / "target.txt") == (
                LINKED_TARGET_BYTES
            )

        # The safe subset of the same selections was still retrieved locally.
        assert _local_tree(result_root) == {
            "OUT": "directory",
            "OUT/.hidden": "directory",
            "OUT/.hidden/hidden.txt": "file",
            "OUT/emptydir": "directory",
            "OUT/nested": "directory",
            "OUT/nested/result.dat": "file",
            "summary.txt": "file",
        }
    finally:
        await _remove_remote_paths(session, remote_root, link_target_root)
