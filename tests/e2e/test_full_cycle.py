# region MODULE_CONTRACT
# PURPOSE: E2E test exercising full scheduler lifecycle via real entrypoint code paths across two SSH nodes.
# SCOPE: Start daemon → submit 4 jobs via _submit_async → assert TO_DO → add 2 nodes via _manage_node_async → poll until DONE → assert outputs, distribution, logs → soft-remove both nodes.
# KEYWORDS: e2e, full lifecycle, SSH nodes, scheduler daemon
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.log_assertions import extra_fields
from yascheduler.domain.model import (
    TaskId,
    allocated_node_id_of,
    error_of,
)
from yascheduler.domain.model import TaskStatus as DomainTaskStatus
from yascheduler.entrypoints.cli.manage_node import _manage_node_async
from yascheduler.entrypoints.cli.submit import _submit_async
from yascheduler.entrypoints.di import make_daemon

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.entrypoints import Config
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

log = logging.getLogger("e2e.test_full_cycle")

_POLL_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.5


async def test_full_cycle(
    e2e_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    ssh_pool: list[dict[str, Any]],
    log_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    config = e2e_config
    ini_path = _ini_path_from_env()

    ip_a = ssh_pool[0]["host"]
    ip_b = ssh_pool[1]["host"]

    orchestrator = await make_daemon(config)
    orch_task = asyncio.create_task(orchestrator.start())

    task_ids: list[int] = []
    try:
        task_ids = await _submit_four_jobs(
            config,
            ini_path,
            monkeypatch,
            tmp_path,
            capfd,
        )

        await _assert_all_status(
            uow_factory,
            task_ids,
            DomainTaskStatus.TO_DO,
            "after submit, before nodes",
        )

        await _add_nodes(ssh_pool, ini_path)
        await _assert_nodes_present(uow_factory, {ip_a, ip_b})

        seen_running = await _wait_all_done(uow_factory, task_ids)
        # Assert every task passed through RUNNING (lifecycle: TO_DO -> RUNNING -> DONE).
        # The test_shell engine sleeps 3s, so each task is RUNNING for >=3s;
        # the 0.5s poll interval guarantees every RUNNING window is observed.
        missing_running = set(task_ids) - set(seen_running)
        assert not missing_running, (
            f"tasks {missing_running} were never observed RUNNING; "
            f"seen_running={seen_running}"
        )
        # Every observed RUNNING task must have been allocated to one of the pool IPs.
        assert set(seen_running.values()).issubset({ip_a, ip_b}), (
            f"RUNNING allocated_ips={set(seen_running.values())} "
            f"not subset of {{{ip_a}, {ip_b}}}"
        )

        tasks = await _read_tasks(uow_factory, task_ids)
        assert len(task_ids) == len(tasks), (
            f"length mismatch: task_ids={len(task_ids)} tasks={len(tasks)}"
        )
        for tid, task in zip(task_ids, tasks, strict=True):
            assert task is not None, f"task {tid} vanished from DB"
            assert task.status == DomainTaskStatus.DONE, (
                f"task {tid} status={task.status}, expected DONE"
            )
            assert error_of(task) is None, f"task {tid} error={error_of(task)!r}"
            local_folder = task.local_folder
            assert local_folder, f"task {tid} missing local_folder"
            n = task_ids.index(tid) + 1
            expected = f"hello e2e {n}"
            out_file = Path(str(local_folder)) / "1.input.out"
            assert out_file.exists(), f"task {tid} output missing: {out_file}"
            assert out_file.read_text() == expected, (
                f"task {tid} output={out_file.read_text()!r}, expected {expected!r}"
            )
            assert isinstance(task.created_at, datetime), (
                f"task {tid} created_at={task.created_at!r} is not datetime"
            )
            assert isinstance(task.updated_at, datetime), (
                f"task {tid} updated_at={task.updated_at!r} is not datetime"
            )

        async with uow_factory() as uow:
            node_ids = [
                nid
                for t in tasks
                if t is not None and (nid := allocated_node_id_of(t)) is not None
            ]
            nodes_by_id = await uow.nodes.get_by_ids(node_ids) if node_ids else {}
        # ponytail: build (task, node_id) pairs once to avoid repeated helper calls
        # in the comprehensions below; node_id is None for unallocated tasks.
        allocated = [
            (t, nid)
            for t in tasks
            if t is not None and (nid := allocated_node_id_of(t)) is not None
        ]
        ips = {nodes_by_id[nid].hostname for _, nid in allocated if nid in nodes_by_id}
        assert ips == {ip_a, ip_b}, (
            f"expected both nodes used, got allocated_ips={ips}; "
            f"expected {{{ip_a}, {ip_b}}}"
        )
        for ip in (ip_a, ip_b):
            count = sum(
                1
                for _, nid in allocated
                if nid in nodes_by_id and nodes_by_id[nid].hostname == ip
            )
            assert count < len(task_ids), (
                f"node {ip} received all {len(task_ids)} tasks — monopoly rejected"
            )

        _assert_allocation_logs(log_records, task_ids, {ip_a, ip_b})

        await _remove_nodes_soft(ssh_pool, ini_path)
        await _assert_nodes_present(uow_factory, set())

    finally:
        await orchestrator.stop()
        try:
            await asyncio.wait_for(orch_task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            orch_task.cancel()


def _ini_path_from_env() -> str:
    path = os.environ.get("YASCHEDULER_CONF_PATH")
    if not path:
        raise RuntimeError(
            "YASCHEDULER_CONF_PATH unset; e2e_config fixture must run first",
        )
    return path


async def _submit_four_jobs(
    config: Config,
    ini_path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> list[int]:
    del config  # config is validated via _submit_async's own parse_config
    task_ids: list[int] = []
    for n in range(1, 5):
        job_dir = tmp_path / f"job_{n}"
        job_dir.mkdir(parents=True)
        (job_dir / "1.input").write_text(f"hello e2e {n}")

        script = tmp_path / f"submit_{n}.script"
        script.write_text(f"ENGINE=test_shell\nLABEL=job_{n}\n")

        monkeypatch.chdir(job_dir)
        capfd.readouterr()
        await _submit_async([str(script), "--config", ini_path])
        out = capfd.readouterr().out.strip()
        monkeypatch.undo()

        assert out, f"job {n}: _submit_async printed nothing to stdout"
        task_id = int(out)
        assert task_id > 0, f"job {n}: task_id={task_id} not positive"
        task_ids.append(task_id)
    return task_ids


async def _assert_all_status(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_ids: list[int],
    expected: DomainTaskStatus,
    label: str,
) -> None:
    async with uow_factory() as uow:
        for tid in task_ids:
            task = await uow.tasks.get(TaskId(tid))
            assert task is not None, f"task {tid} missing ({label})"
            assert task.status == expected, (
                f"task {tid} status={task.status}, expected {expected} ({label})"
            )


async def _add_nodes(ssh_pool: list[dict[str, Any]], ini_path: str) -> None:
    for entry in ssh_pool:
        await _manage_node_async([_host_spec(entry), "--config", ini_path])


async def _assert_nodes_present(
    uow_factory: Callable[[], PostgresUnitOfWork],
    expected_ips: set[str],
) -> None:
    async with uow_factory() as uow:
        nodes = await uow.nodes.list_all()
    actual = {n.hostname for n in nodes}
    assert actual == expected_ips, f"node IPs={actual}, expected={expected_ips}"


async def _wait_all_done(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_ids: list[int],
) -> dict[int, str]:
    deadline = asyncio.get_running_loop().time() + _POLL_TIMEOUT_S
    statuses: list[Any] = []
    seen_running: dict[int, str] = {}
    while asyncio.get_running_loop().time() < deadline:
        async with uow_factory() as uow:
            statuses = []
            for tid in task_ids:
                t = await uow.tasks.get(TaskId(tid))
                statuses.append(t.status if t else None)
                if t and t.status == DomainTaskStatus.RUNNING:
                    node_id = allocated_node_id_of(t)
                    if node_id is not None:
                        node = await uow.nodes.get_by_id(node_id)
                        seen_running[tid] = node.hostname if node else ""
        if all(s == DomainTaskStatus.DONE for s in statuses):
            return seen_running
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"tasks not all DONE within {_POLL_TIMEOUT_S}s; "
        f"task_ids={task_ids} last statuses={statuses}",
    )


async def _read_tasks(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_ids: list[int],
) -> list[Any]:
    async with uow_factory() as uow:
        tasks: list[Any] = [await uow.tasks.get(TaskId(tid)) for tid in task_ids]
    return tasks


def _assert_allocation_logs(
    records: list[logging.LogRecord],
    task_ids: list[int],
    expected_ips: set[str],
) -> None:
    allocated_records = [r for r in records if r.getMessage() == "ALLOCATED"]
    seen_task_ids: set[int] = set()
    seen_ips: set[str] = set()
    for rec in allocated_records:
        fields = extra_fields(rec)
        tid = fields.get("task_id")
        if tid is not None:
            seen_task_ids.add(tid.value)
        hostname = fields.get("hostname")
        if hostname is not None:
            seen_ips.add(hostname)
    missing_task_ids = set(task_ids) - seen_task_ids
    assert not missing_task_ids, (
        f"no [ALLOCATED] log for task_ids={missing_task_ids}; "
        f"captured ALLOCATED records={allocated_records}"
    )
    missing_ips = expected_ips - seen_ips
    assert not missing_ips, (
        f"[ALLOCATED] logs never mention hostname={missing_ips}; "
        f"captured ALLOCATED records={allocated_records}"
    )


async def _remove_nodes_soft(
    ssh_pool: list[dict[str, Any]],
    ini_path: str,
) -> None:
    for entry in ssh_pool:
        await _manage_node_async(
            [_host_spec(entry), "--remove-soft", "--config", ini_path],
        )


def _host_spec(entry: dict[str, Any]) -> str:
    # Username is inherited from [remote] in the INI; omit it from the spec so
    # _add_node resolves spec.username from config.remote.username.
    return f"{entry['host']}:{entry['port']}"
