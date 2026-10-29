# region MODULE_CONTRACT
# PURPOSE: Prove that a task input snapshot captured at public submission survives source edits/deletion, a fresh dependency/config reload with changed engine input declarations, and a real daemon/SFTP deployment using the stored entries.
# SCOPE: Real PostgreSQL + real SSH e2e test: public client submission of a mixed text/binary/hidden/empty tree, fresh public/domain reload, daemon lifecycle to DONE, pass-through run_bg observer that asserts the exact remote tree/bytes before the real harmless spawn, and single-file output retrieval.
# INVARIANTS: Expected paths, bytes, and public projections are independent literals in this module; the observer never bypasses the real SSH spawn boundary; no shared session config is modified in place.
# DEPENDENCIES: USES API: testcontainers fixtures (PostgreSQL + SSH), asyncssh SFTP, Yascheduler public client, capture_input_snapshot, make_daemon, CLI node management.
# KEYWORDS: e2e, task input snapshot, binary bytes, hidden files, empty directories, SFTP, spawn ordering, source independence
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import base64
import contextlib
import shutil
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from asyncssh.sftp import FILEXFER_TYPE_DIRECTORY, FILEXFER_TYPE_REGULAR

from yascheduler.domain import TaskInputFile
from yascheduler.domain.model import TaskId, TaskStatus, error_of
from yascheduler.entrypoints import Config, Yascheduler
from yascheduler.entrypoints.cli.manage_node import _manage_node_async
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.entrypoints.di import make_daemon
from yascheduler.infra import capture_input_snapshot
from yascheduler.infra.ssh.session import SSHMachineSession

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from yascheduler.domain import MachineSession
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

pytestmark = pytest.mark.e2e

__all__ = ["test_task_input_snapshot_survives_source_removal_and_deployment"]

# Independent expected inputs. The source tree is seeded from these literals and
# every later assertion compares against them rather than against the snapshot
# returned by the system under test.
_TEXT_INPUT = b"line one\nline two\r\n"
_BINARY_INPUT = b"\x00\x01\x02binary\x80\xff\x00end\xf0\x28\x8c\x28"
_ORBITAL_INPUT = b"orbital basis\n"
_HIDDEN_ROOT_INPUT = b"hidden-root\x00\xff"
_HIDDEN_NESTED_INPUT = b"hidden-nested\n"
_NESTED_RESULT = b"nested-result-2\n"
_ROOT_RESULT = b"root-result-1\n"
_OTHER_RESULT = b"other-result-3\n"
_OUTPUT_BYTES = b"snapshot-output-ok\n"

_EXPECTED_FILES: dict[str, bytes] = {
    "INPUT": _TEXT_INPUT,
    ".hidden/secret.bin": _HIDDEN_ROOT_INPUT,
    "basis/Si.orb": _ORBITAL_INPUT,
    "basis/.cache/key.bin": _BINARY_INPUT,
    "basis/.nested-hidden/inside.txt": _HIDDEN_NESTED_INPUT,
    "basis/nested/result2.dat": _NESTED_RESULT,
    "basis/nested/empty.txt": b"",
    "result1.dat": _ROOT_RESULT,
    "other/result3.dat": _OTHER_RESULT,
}
_EXPECTED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".hidden",
        ".hidden/empty_dir",
        "basis",
        "basis/.cache",
        "basis/.nested-hidden",
        "basis/empty_dir",
        "basis/nested",
        "basis/nested/deep",
        "basis/nested/deep/empty_dir2",
        "other",
    }
)
_EXPECTED_TREE: dict[str, bytes | None] = {
    **dict.fromkeys(_EXPECTED_DIRECTORIES, None),
    **_EXPECTED_FILES,
}
_EXTRA_METADATA: dict[str, object] = {
    "priority": 7,
    "tags": ["snapshot", "e2e"],
    "nested": {"depth": 2},
}
_INITIAL_INPUT_DECLARATIONS = "INPUT basis **/result?.dat .hidden"
_CHANGED_INPUT_DECLARATIONS = "missing/**/*.orb"
_REMOTE_ROOT_PREFIX = "/tmp/yascheduler-input-snapshot-"
_RUN_SH = "#!/bin/sh\nsleep 3\nprintf 'snapshot-output-ok\\n' > result.out\n"


# region FUNC__snapshot_mapping
# PURPOSE: Project a hydrated task snapshot into path -> bytes-for-file/None-for-directory so exact paths, kinds, bytes, and empty files/directories compare in one assertion.
def _snapshot_mapping(entries: Sequence[Any]) -> dict[str, bytes | None]:
    return {
        entry.path: entry.content if isinstance(entry, TaskInputFile) else None
        for entry in entries
    }


# endregion FUNC__snapshot_mapping


# region FUNC__write_isolated_config
# PURPOSE: Write a fresh, test-owned INI config using the shared PostgreSQL/SSH fixtures but its own local engine/data and remote task directories, never mutating the session config.
def _write_isolated_config(
    path: Path,
    *,
    db_config: Any,
    ssh: dict[str, Any],
    local_data_dir: Path,
    remote_root: str,
    input_declarations: str,
) -> None:
    path.write_text(
        "[db]\n"
        f"host = {db_config.host}\n"
        f"port = {db_config.port}\n"
        f"user = {db_config.user}\n"
        f"password = {db_config.password}\n"
        f"database = {db_config.database}\n"
        "\n"
        "[local]\n"
        f"data_dir = {local_data_dir}\n"
        "\n"
        "[remote]\n"
        f"user = {ssh['username']}\n"
        f"data_dir = {remote_root}\n"
        f"engines_dir = {remote_root}/engines\n"
        f"tasks_dir = {remote_root}/tasks\n"
        "\n"
        "[engine.test_shell]\n"
        "spawn = {engine_path}/run.sh\n"
        "check_pname = sleep\n"
        f"input_files = {input_declarations}\n"
        "output_files = result.out\n"
        "deploy_local_files = run.sh\n"
        "sleep_interval = 1\n"
        "platforms = linux\n"
    )


# endregion FUNC__write_isolated_config


# region FUNC__read_remote_tree
# PURPOSE: Read the complete remote task tree through real SFTP using lstat kinds and binary reads, so symlinks, extra/missing entries, wrong kinds, wrong bytes, and empty files/directories all make the comparison fail.
async def _read_remote_tree(
    session: MachineSession,
    cwd: str,
) -> dict[str, object]:
    root = PurePosixPath(cwd)
    tree: dict[str, object] = {}

    async def walk(sftp: Any, directory: PurePosixPath, prefix: str) -> None:
        for entry in await sftp.readdir(directory):
            name = str(entry.filename)
            if name in (".", ".."):
                continue
            relative = f"{prefix}/{name}" if prefix else name
            path = directory / name
            attrs = await sftp.lstat(path)
            if attrs.type == FILEXFER_TYPE_DIRECTORY:
                tree[relative] = None
                await walk(sftp, path, relative)
            elif attrs.type == FILEXFER_TYPE_REGULAR:
                async with sftp.open(path, "rb") as remote_file:
                    tree[relative] = await remote_file.read()
            else:
                tree[relative] = f"other:{attrs.type}"

    async with session.open_sftp() as sftp:
        await walk(sftp, root, "")
    return tree


# endregion FUNC__read_remote_tree


# region FUNC__wait_for_done
# PURPOSE: Poll a fresh UoW until the task is DONE, failing immediately if the pre-spawn observer recorded an assertion/spawn failure so daemon error swallowing cannot degrade into a generic timeout.
async def _wait_for_done(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: int,
    spawn_failures: list[str],
    timeout_s: float = 45.0,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_status: TaskStatus | None = None
    while asyncio.get_running_loop().time() < deadline:
        if spawn_failures:
            pytest.fail(f"pre-spawn observation failed: {spawn_failures[0]}")
        async with uow_factory() as uow:
            task = await uow.tasks.get(TaskId(task_id))
        if task is not None:
            last_status = task.status
            if task.status == TaskStatus.DONE:
                return task
        await asyncio.sleep(0.25)
    pytest.fail(
        f"task {task_id} did not reach DONE within {timeout_s}s; "
        f"last_status={last_status}; spawn_failures={spawn_failures}"
    )


# endregion FUNC__wait_for_done


# region FUNC_test_task_input_snapshot_survives_source_removal_and_deployment
# PURPOSE: Accept a mixed input snapshot through the public client, reload it through fresh dependencies after changing engine declarations and deleting the source, then prove the daemon's real SFTP upload reconstructs it exactly before a harmless real spawn and completes through output retrieval.
async def test_task_input_snapshot_survives_source_removal_and_deployment(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_pool: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ssh = ssh_pool[0]
    remote_root = f"{_REMOTE_ROOT_PREFIX}{uuid4().hex}"
    source_dir = tmp_path / "submission"
    local_data_dir = tmp_path / "local-data"
    config_a_path = tmp_path / "yascheduler-a.conf"
    config_b_path = tmp_path / "yascheduler-b.conf"

    # Isolated local engine setup and SSH key material. The session-scoped
    # e2e_config and its files are never modified.
    engine_dir = local_data_dir / "engines" / "test_shell"
    engine_dir.mkdir(parents=True)
    run_sh = engine_dir / "run.sh"
    run_sh.write_text(_RUN_SH)
    run_sh.chmod(0o755)
    keys_dir = local_data_dir / "keys"
    keys_dir.mkdir()
    key_path = Path(str(ssh["key_path"]))
    (keys_dir / key_path.name).symlink_to(key_path)

    for relative, content in _EXPECTED_FILES.items():
        target = source_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for relative in _EXPECTED_DIRECTORIES:
        (source_dir / relative).mkdir(parents=True, exist_ok=True)

    _write_isolated_config(
        config_a_path,
        db_config=e2e_config.db,
        ssh=ssh,
        local_data_dir=local_data_dir,
        remote_root=remote_root,
        input_declarations=_INITIAL_INPUT_DECLARATIONS,
    )
    submit_config = parse_config(config_a_path)
    assert submit_config.engines["test_shell"].input_files == (
        "INPUT",
        "basis",
        "**/result?.dat",
        ".hidden",
    )

    captured = capture_input_snapshot(
        source_dir,
        submit_config.engines["test_shell"].input_files,
    )
    assert _snapshot_mapping(captured) == _EXPECTED_TREE

    submit_client = Yascheduler(config_a_path)
    task_id = await submit_client.queue_submit_task_async(
        "snapshot e2e",
        {
            "local_folder": str(source_dir),
            "priority": _EXTRA_METADATA["priority"],
            "tags": _EXTRA_METADATA["tags"],
            "nested": _EXTRA_METADATA["nested"],
        },
        "test_shell",
        input_entries=captured,
    )
    del submit_client

    # Mutate and delete the source after acceptance. The accepted task must
    # remain owned by the persisted snapshot alone.
    (source_dir / "INPUT").write_bytes(b"edited-after-submission")
    (source_dir / "basis" / "Si.orb").write_bytes(b"edited-after-submission")
    (source_dir / "new-unowned.txt").write_bytes(b"new-after-submission")
    shutil.rmtree(source_dir)
    assert not source_dir.exists()

    # Change the live engine declarations to paths that never existed in the
    # accepted snapshot, then reload through fresh config/client/UoW seams.
    _write_isolated_config(
        config_b_path,
        db_config=e2e_config.db,
        ssh=ssh,
        local_data_dir=local_data_dir,
        remote_root=remote_root,
        input_declarations=_CHANGED_INPUT_DECLARATIONS,
    )
    config_b = parse_config(config_b_path)
    assert config_b.engines["test_shell"].input_files == (_CHANGED_INPUT_DECLARATIONS,)

    reload_client = Yascheduler(config_b_path)
    projection = await reload_client.queue_get_task_async(task_id)
    assert projection is not None
    public_entries = projection["input_entries"]
    assert {
        entry["path"]: base64.b64decode(entry["content_base64"])
        for entry in public_entries
        if entry["kind"] == "file"
    } == _EXPECTED_FILES
    assert {
        entry["path"] for entry in public_entries if entry["kind"] == "directory"
    } == set(_EXPECTED_DIRECTORIES)
    assert projection["metadata"]["priority"] == _EXTRA_METADATA["priority"]
    assert projection["metadata"]["tags"] == _EXTRA_METADATA["tags"]
    assert projection["metadata"]["nested"] == _EXTRA_METADATA["nested"]
    assert projection["metadata"]["local_folder"] == str(source_dir)

    async with uow_factory() as uow:
        persisted = await uow.tasks.get(TaskId(task_id))
    assert persisted is not None
    assert _snapshot_mapping(persisted.input_entries) == _EXPECTED_TREE
    assert persisted.extra == _EXTRA_METADATA

    spawn_calls: list[tuple[str, str | None]] = []
    spawn_failures: list[str] = []
    real_run_bg = SSHMachineSession.run_bg

    async def observed_run_bg(
        self: SSHMachineSession,
        cmd: str,
        *,
        cwd: str | None = None,
    ) -> None:
        if cwd is None:
            spawn_failures.append("run_bg called without cwd")
            raise AssertionError("run_bg called without cwd")
        try:
            actual_tree = await _read_remote_tree(self, cwd)
            assert actual_tree == _EXPECTED_TREE
        except BaseException as err:
            spawn_failures.append(
                f"pre-spawn check failed: {type(err).__name__}: {err}"
            )
            raise
        spawn_calls.append((cmd, cwd))
        try:
            await real_run_bg(self, cmd, cwd=cwd)
        except BaseException as err:
            spawn_failures.append(f"real run_bg failed: {type(err).__name__}: {err}")
            raise

    monkeypatch.setattr(SSHMachineSession, "run_bg", observed_run_bg)

    orchestrator = None
    orch_task = None
    try:
        # Normal operator path: add/setup the SSH node with the changed config,
        # then start the real daemon; deployment itself must use stored entries.
        await _manage_node_async(
            [f"{ssh['host']}:{ssh['port']}", "--config", str(config_b_path)]
        )
        orchestrator = await make_daemon(config_b)
        orch_task = asyncio.create_task(orchestrator.start())
        task = await _wait_for_done(uow_factory, task_id, spawn_failures)

        assert not spawn_failures
        assert len(spawn_calls) == 1
        assert "run.sh" in spawn_calls[0][0]
        assert task.status == TaskStatus.DONE
        assert error_of(task) is None
        assert _snapshot_mapping(task.input_entries) == _EXPECTED_TREE
        assert task.extra == _EXTRA_METADATA

        output_file = source_dir / "result.out"
        assert output_file.read_bytes() == _OUTPUT_BYTES
    finally:
        if orchestrator is not None:
            await orchestrator.stop()
        if orch_task is not None:
            try:
                await asyncio.wait_for(orch_task, timeout=10)
            except asyncio.TimeoutError:
                orch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await orch_task
            except asyncio.CancelledError:
                pass


# endregion FUNC_test_task_input_snapshot_survives_source_removal_and_deployment
