# region MODULE_CONTRACT
# PURPOSE: Prove through real PostgreSQL + SSH that output selections accepted with a task remain authoritative after the engine configuration changes and survive persistence, allocation, and retrieval.
# SCOPE: Isolated test_shell engine with an output set changed between submission and daemon start; parameterized added and removed selections; exact local files and bytes; persisted record equality; task DONE outcome; and a task-sourced resolution log.
# INVARIANTS: Expected output bytes are independent literals in this module; each case uses its own remote/local roots and leaves live cloud providers untouched; the daemon reads config B while the task keeps config A's selection record.
# DEPENDENCIES: USES API: testcontainers (PostgreSQL + SSH), Yascheduler client, CLI node management, make_daemon.
# KEYWORDS: e2e, output selections, acceptance record, config change, postgres, ssh
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from tests.log_assertions import extra_fields
from yascheduler.domain.model import TaskId, TaskStatus
from yascheduler.entrypoints import Config, Yascheduler
from yascheduler.entrypoints.cli.manage_node import _manage_node_async
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.entrypoints.di import make_daemon

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.application.orchestrator import Orchestrator
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

pytestmark = pytest.mark.e2e

__all__ = ["test_recorded_output_selections_survive_engine_config_change"]

_ENGINE = "test_shell"
_INPUT_NAME = "1.input"
_INPUT_TEXT = "hello output selections e2e"
_REMOTE_ROOT_PREFIX = "/tmp/yascheduler-output-selections-"
_RUN_SH = (
    "#!/bin/sh\n"
    "sleep 3\n"
    "printf 'present-output\\n' > present.out\n"
    "printf 'kept-output\\n' > kept.out\n"
    "printf 'removed-output\\n' > removed.out\n"
)
_ADDED_ACCEPTED = ("present.out",)
_ADDED_CHANGED = ("present.out", "added-missing.out")
_ADDED_EXPECTED = {"present.out": b"present-output\n"}
_REMOVED_ACCEPTED = ("kept.out", "removed.out")
_REMOVED_CHANGED = ("kept.out",)
_REMOVED_EXPECTED = {
    "kept.out": b"kept-output\n",
    "removed.out": b"removed-output\n",
}


# region FUNC__write_isolated_config
# PURPOSE: Write a test-owned INI config using shared DB/SSH fixtures but isolated local/remote roots and a caller-supplied engine output set.
def _write_isolated_config(
    path: Path,
    *,
    db_config: Any,
    ssh: dict[str, Any],
    local_data_dir: Path,
    remote_root: str,
    output_files: tuple[str, ...],
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
        f"[engine.{_ENGINE}]\n"
        "spawn = {engine_path}/run.sh\n"
        "check_pname = sleep\n"
        f"input_files = {_INPUT_NAME}\n"
        f"output_files = {' '.join(output_files)}\n"
        "deploy_local_files = run.sh\n"
        "sleep_interval = 1\n"
        "platforms = linux\n"
    )


# endregion FUNC__write_isolated_config


# region FUNC__wait_for_done
# PURPOSE: Poll a fresh UoW until the task reaches DONE, reporting the last observed status on timeout.
async def _wait_for_done(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_id: int,
    timeout_s: float = 45.0,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_status = None
    while asyncio.get_running_loop().time() < deadline:
        async with uow_factory() as uow:
            task = await uow.tasks.get(TaskId(task_id))
        if task is not None:
            last_status = task.status
            if task.status == TaskStatus.DONE:
                return task
        await asyncio.sleep(0.25)
    pytest.fail(
        f"task {task_id} did not reach DONE within {timeout_s}s; "
        f"last_status={last_status}"
    )


# endregion FUNC__wait_for_done


# region FUNC__stop_orchestrator
# PURPOSE: Stop the real daemon and collect its task without leaking a hanging task on failure.
async def _stop_orchestrator(
    orchestrator: Orchestrator,
    orch_task: asyncio.Task[None],
) -> None:
    await orchestrator.stop()
    try:
        await asyncio.wait_for(orch_task, timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        orch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await orch_task


# endregion FUNC__stop_orchestrator


# region FUNC_test_recorded_output_selections_survive_engine_config_change
# PURPOSE: Submit under config A, reload the persisted record, run a real daemon under config B with an added or removed selection, and prove retrieval follows the record and reaches DONE.
@pytest.mark.parametrize(
    ("accepted", "changed", "expected"),
    [
        pytest.param(
            _ADDED_ACCEPTED,
            _ADDED_CHANGED,
            _ADDED_EXPECTED,
            id="added",
        ),
        pytest.param(
            _REMOVED_ACCEPTED,
            _REMOVED_CHANGED,
            _REMOVED_EXPECTED,
            id="removed",
        ),
    ],
)
async def test_recorded_output_selections_survive_engine_config_change(
    accepted: tuple[str, ...],
    changed: tuple[str, ...],
    expected: dict[str, bytes],
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_pool: list[dict[str, Any]],
    log_records: list[Any],
    tmp_path: Path,
) -> None:
    ssh = ssh_pool[0]
    remote_root = f"{_REMOTE_ROOT_PREFIX}{uuid4().hex}"
    local_data_dir = tmp_path / "local-data"
    result_dir = tmp_path / "results"
    config_a_path = tmp_path / "yascheduler-a.conf"
    config_b_path = tmp_path / "yascheduler-b.conf"

    engine_dir = local_data_dir / "engines" / _ENGINE
    engine_dir.mkdir(parents=True)
    run_sh = engine_dir / "run.sh"
    run_sh.write_text(_RUN_SH)
    run_sh.chmod(0o755)
    keys_dir = local_data_dir / "keys"
    keys_dir.mkdir()
    key_path = Path(str(ssh["key_path"]))
    (keys_dir / key_path.name).symlink_to(key_path)

    _write_isolated_config(
        config_a_path,
        db_config=e2e_config.db,
        ssh=ssh,
        local_data_dir=local_data_dir,
        remote_root=remote_root,
        output_files=accepted,
    )
    submit_config = parse_config(config_a_path)
    assert submit_config.engines[_ENGINE].output_files == accepted

    submit_client = Yascheduler(config_a_path)
    task_id = await submit_client.queue_submit_task_async(
        "output selections e2e",
        {_INPUT_NAME: _INPUT_TEXT, "local_folder": str(result_dir)},
        _ENGINE,
    )
    del submit_client

    # Reload through the DB before allocation: acceptance already owns the record.
    async with uow_factory() as uow:
        accepted_task = await uow.tasks.get(TaskId(task_id))
    assert accepted_task is not None
    assert accepted_task.output_selections == accepted

    # Config B represents a daemon restart after the engine section changed.
    _write_isolated_config(
        config_b_path,
        db_config=e2e_config.db,
        ssh=ssh,
        local_data_dir=local_data_dir,
        remote_root=remote_root,
        output_files=changed,
    )
    retrieval_config = parse_config(config_b_path)
    assert retrieval_config.engines[_ENGINE].output_files == changed
    assert retrieval_config.engines[_ENGINE].output_files != accepted

    orchestrator: Orchestrator | None = None
    orch_task: asyncio.Task[None] | None = None
    try:
        await _manage_node_async(
            [f"{ssh['host']}:{ssh['port']}", "--config", str(config_b_path)]
        )
        orchestrator = await make_daemon(retrieval_config)
        orch_task = asyncio.create_task(orchestrator.start())

        task = await _wait_for_done(uow_factory, task_id)
        assert task.status == TaskStatus.DONE
        assert task.state.error is None
        assert task.output_selections == accepted

        local_files = {
            str(path.relative_to(result_dir)): path.read_bytes()
            for path in result_dir.rglob("*")
            if path.is_file()
        }
        assert local_files == expected

        resolved = [
            extra_fields(record)
            for record in log_records
            if record.getMessage() == "OUTPUT_SELECTIONS_RESOLVED"
            and extra_fields(record).get("task_id") == task_id
        ]
        assert resolved, "retrieval must emit its resolved output selections"
        assert {tuple(fields["output_selections"]) for fields in resolved} == {accepted}
        assert {fields["source"] for fields in resolved} == {"task"}
        assert not any(
            "has no recorded output selections" in record.getMessage()
            and extra_fields(record).get("task_id") == task_id
            for record in log_records
        )
    finally:
        if orchestrator is not None and orch_task is not None:
            await _stop_orchestrator(orchestrator, orch_task)


# endregion FUNC_test_recorded_output_selections_survive_engine_config_change
