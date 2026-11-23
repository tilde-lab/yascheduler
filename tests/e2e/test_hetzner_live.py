# region MODULE_CONTRACT
# PURPOSE: Real-Hetzner cloud-provider E2E test — autoscale -> allocate -> download -> idle-deallocate happy path against a live Hetzner Cloud account.
# SCOPE: opt-in env-gated test (YASCHEDULER_TEST_HETZNER=1 + token); drives make_daemon + _submit_async; asserts VM creation, both jobs DONE with matching outputs, idle deallocation, strong deletion via GET /servers/{id} 404; guaranteed server-ID based cleanup with loud-fail-on-leak in finally.
# DEPENDENCIES: USES API: aiohttp (opt-in, YASCHEDULER_TEST_HETZNER=1)
# KEYWORDS: Hetzner Cloud, autoscale, deallocate, e2e
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest

from tests.log_assertions import extra_fields
from yascheduler.domain.model import (
    TaskId,
    allocated_node_id_of,
    error_of,
)
from yascheduler.domain.model import TaskStatus as DomainTaskStatus
from yascheduler.entrypoints.cli.submit import _submit_async
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.entrypoints.di import make_daemon

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.entrypoints import Config
    from yascheduler.infra.cloud.cloud_configs import ConfigCloudHetzner
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

log = logging.getLogger("e2e.test_hetzner_live")

# Timeouts sized to observed real-Hetzner cold-start: VM create + boot +
# cloud-init + setup_node takes ~80-90s with package_upgrade=false; the two
# test_shell jobs (3s each) finish seconds after the node is enabled; idle
# deallocate fires ~idle_tolerance(5)s later. Margins are ~1.4x the observed
# values — tight enough to fail fast on a real regression, generous enough to
# absorb Hetzner variance.
_AUTOSCALE_TIMEOUT_S = 120.0
_COMPLETION_TIMEOUT_S = 60.0
_DEALLOC_TIMEOUT_S = 60.0
_VM_DELETE_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 1.0
_HETZNER_API_BASE = "https://api.hetzner.cloud/v1"


def _cloud_env_or_skip() -> tuple[str, str, str, str]:
    if os.environ.get("YASCHEDULER_TEST_HETZNER") != "1":
        pytest.skip(
            "YASCHEDULER_TEST_HETZNER != 1; set YASCHEDULER_TEST_HETZNER=1 to enable",
        )
    token = os.environ.get("YASCHEDULER_CLOUDS_HETZNER_TOKEN", "")
    if not token:
        pytest.skip(
            "YASCHEDULER_CLOUDS_HETZNER_TOKEN unset/empty; set it to a real Hetzner API token",
        )
    server_type = os.environ.get("YASCHEDULER_CLOUDS_HETZNER_SERVER_TYPE", "cx23")
    location = os.environ.get("YASCHEDULER_CLOUDS_HETZNER_LOCATION", "hel1")
    image = os.environ.get("YASCHEDULER_CLOUDS_HETZNER_IMAGE_NAME", "debian-13")
    return token, server_type, location, image


@pytest.fixture(scope="session")
def hetzner_config(
    tmp_path_factory: Any,
    _db_config: Any,
    _init_schema: None,
) -> Config:
    token, server_type, location, image = _cloud_env_or_skip()

    tmp = tmp_path_factory.mktemp("hetzner_config")
    data_dir = tmp / "data"
    ini_path = tmp / "yascheduler.conf"
    db_cfg = _db_config
    # hetzner_package_upgrade=false skips the slow cloud-init apt-get upgrade so the
    # default ConfigCloudHetzner.connect_grace (60s, NOT INI-parsed) is ample.
    # connect_grace is deliberately NOT set here (it is not an INI-parsed key).
    ini_content = (
        f"[db]\n"
        f"host = {db_cfg.host}\n"
        f"port = {db_cfg.port}\n"
        f"user = {db_cfg.user}\n"
        f"password = {db_cfg.password}\n"
        f"database = {db_cfg.database}\n"
        f"\n"
        f"[local]\n"
        f"data_dir = {data_dir}\n"
        f"\n"
        f"[remote]\n"
        f"user = root\n"
        f"\n"
        f"[engine.test_shell]\n"
        f"spawn = {{engine_path}}/run.sh\n"
        f"check_pname = sleep\n"
        f"input_files = 1.input\n"
        f"output_files = 1.input.out\n"
        f"deploy_local_files = run.sh\n"
        f"sleep_interval = 1\n"
        f"platforms = linux\n"
        f"\n"
        f"[clouds]\n"
        f"hetzner_token = {token}\n"
        f"hetzner_max_nodes = 1\n"
        f"hetzner_server_type = {server_type}\n"
        f"hetzner_location = {location}\n"
        f"hetzner_image_name = {image}\n"
        f"hetzner_idle_tolerance = 5\n"
        f"hetzner_package_upgrade = false\n"
    )
    ini_path.write_text(ini_content)

    engines_dir = tmp / "data" / "engines" / "test_shell"
    engines_dir.mkdir(parents=True)
    run_sh = engines_dir / "run.sh"
    run_sh.write_text("#!/bin/sh\nsleep 3\ncat 1.input > 1.input.out\n")
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IEXEC)

    # Fresh empty keys_dir: the daemon generates its own key via get_or_create_ssh_key,
    # registered into the Hetzner project by get_ssh_key_id. NOT the static-node ssh_pool key.
    keys_dir = tmp / "data" / "keys"
    keys_dir.mkdir(parents=True)

    os.environ["YASCHEDULER_CONF_PATH"] = str(ini_path)
    return parse_config(str(ini_path))


def _ini_path_from_env() -> str:
    path = os.environ.get("YASCHEDULER_CONF_PATH")
    if not path:
        raise RuntimeError(
            "YASCHEDULER_CONF_PATH unset; hetzner_config fixture must run first",
        )
    return path


async def _assert_vm_deleted(token: str, server_id: int) -> None:
    deadline = asyncio.get_running_loop().time() + _VM_DELETE_TIMEOUT_S
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"},
    ) as session:
        while asyncio.get_running_loop().time() < deadline:
            async with session.get(f"{_HETZNER_API_BASE}/servers/{server_id}") as resp:
                if resp.status == 404:
                    return
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning(
                        "[hetzner_live][VERIFY] unexpected status %s: %s",
                        resp.status,
                        body,
                    )
            await asyncio.sleep(_POLL_INTERVAL_S)
    log.error(
        "[hetzner_live][CLEANUP] server %s was NOT deleted — manual cleanup required",
        server_id,
    )
    pytest.fail(f"Hetzner server {server_id} was NOT deleted — manual cleanup required")


async def _delete_one_best_effort(
    cfg: ConfigCloudHetzner,
    server_id: str,
    log: logging.Logger,
) -> None:
    from yascheduler.infra.cloud.providers.hetzner import hetzner_delete_node

    try:
        await hetzner_delete_node(cfg, external_id=server_id)
    except Exception as err:
        log.error(
            "[hetzner_live][CLEANUP] hetzner_delete_node raised for server_id=%s err=%s "
            "— proceeding to deletion verification",
            server_id,
            err,
        )


async def _cleanup_observed(
    token: str,
    observed_server_ids: list[str],
    log: logging.Logger,
) -> None:
    if not observed_server_ids:
        return
    from yascheduler.infra.cloud.cloud_configs import ConfigCloudHetzner

    cfg: ConfigCloudHetzner = ConfigCloudHetzner(token=token, max_nodes=1)

    for sid in observed_server_ids:
        await _delete_one_best_effort(cfg, sid, log)

    # Strong per-server deletion assertion: a survivor fails the test loudly.
    for sid in observed_server_ids:
        await _assert_vm_deleted(token, int(sid))


async def _submit_two_jobs(
    ini_path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> list[int]:
    task_ids: list[int] = []
    for n in range(1, 3):
        job_dir = tmp_path / f"job_{n}"
        job_dir.mkdir(parents=True)
        (job_dir / "1.input").write_text(f"hello cloud {n}")

        script = tmp_path / f"submit_{n}.script"
        script.write_text(f"ENGINE=test_shell\nLABEL=cloud_job_{n}\n")

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


async def _poll_for_hetzner_node(
    uow_factory: Callable[[], PostgresUnitOfWork],
    observed_server_ids: list[str],
    timeout_s: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with uow_factory() as uow:
            nodes = await uow.nodes.list_all()
        # Require enabled: _select_and_insert_tmp inserts a tmp node with
        # cloud="hetzner"/enabled=False (placeholder IP); the real provisioned
        # node is committed enabled=True in _persist_node_with_cleanup (which
        # atomically removes the tmp row in the same commit).
        hetzner_nodes = [n for n in nodes if n.cloud == "hetzner" and n.enabled]
        if hetzner_nodes:
            node = hetzner_nodes[0]
            sid = node.external_id
            if sid and sid not in observed_server_ids:
                observed_server_ids.append(sid)
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"no enabled cloud==hetzner node appeared within {timeout_s}s")


async def _wait_both_done(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_ids: list[int],
    timeout_s: float,
) -> dict[int, str]:
    deadline = asyncio.get_running_loop().time() + timeout_s
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
        f"tasks not all DONE within {timeout_s}s; "
        f"task_ids={task_ids} last statuses={statuses}",
    )


async def _assert_outputs(
    uow_factory: Callable[[], PostgresUnitOfWork],
    task_ids: list[int],
    observed_server_ids: list[str],
    payloads: list[str],
) -> None:
    async with uow_factory() as uow:
        tasks = [await uow.tasks.get(TaskId(tid)) for tid in task_ids]
        node_ids = [
            nid for t in tasks if t and (nid := allocated_node_id_of(t)) is not None
        ]
        nodes_by_id = await uow.nodes.get_by_ids(node_ids) if node_ids else {}
    for idx, (tid, task) in enumerate(zip(task_ids, tasks, strict=True)):
        assert task is not None, f"task {tid} vanished from DB"
        assert task.status == DomainTaskStatus.DONE, (
            f"task {tid} status={task.status}, expected DONE"
        )
        assert error_of(task) is None, f"task {tid} error={error_of(task)!r}"
        local_folder = task.local_folder
        assert local_folder, f"task {tid} missing local_folder"
        out_file = Path(str(local_folder)) / "1.input.out"
        assert out_file.exists(), f"task {tid} output missing: {out_file}"
        actual = out_file.read_text()
        expected = payloads[idx]
        assert actual == expected, (
            f"task {tid} output={actual!r}, expected {expected!r}"
        )
        node_id = allocated_node_id_of(task)
        task_sid = (
            nodes_by_id[node_id].external_id
            if node_id and node_id in nodes_by_id
            else ""
        )
        assert task_sid in observed_server_ids, (
            f"task {tid} node.external_id={task_sid} "
            f"not among observed server IDs={observed_server_ids}"
        )
        assert isinstance(task.created_at, datetime), (
            f"task {tid} created_at={task.created_at!r} is not datetime"
        )
        assert isinstance(task.updated_at, datetime), (
            f"task {tid} updated_at={task.updated_at!r} is not datetime"
        )


def _assert_cloud_done_log(
    records: list[logging.LogRecord],
    observed_server_ids: list[str],
) -> None:
    cloud_done_records = [r for r in records if r.getMessage() == "CLOUD_DONE"]
    done_match = any(
        (fields := extra_fields(rec)).get("cloud") == "hetzner"
        and fields.get("external_id") in observed_server_ids
        for rec in cloud_done_records
    )
    assert done_match, (
        f"no CLOUD_DONE trace record with cloud=hetzner and external_id in {observed_server_ids}; "
        f"captured CLOUD_DONE records={cloud_done_records}"
    )


def _assert_cloud_delete_log(records: list[logging.LogRecord]) -> None:
    cloud_delete_records = [r for r in records if r.getMessage() == "CLOUD_DELETE"]
    delete_match = any(
        extra_fields(rec).get("cloud") == "hetzner" for rec in cloud_delete_records
    )
    assert delete_match, (
        f"no CLOUD_DELETE trace record with cloud=hetzner; "
        f"captured CLOUD_DELETE records={cloud_delete_records}"
    )


async def _poll_node_gone(
    uow_factory: Callable[[], PostgresUnitOfWork],
    timeout_s: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with uow_factory() as uow:
            nodes = await uow.nodes.list_all()
        if not any(n.cloud == "hetzner" for n in nodes):
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"cloud==hetzner node row not removed within {timeout_s}s")


async def test_hetzner_live(
    hetzner_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    log_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    config = hetzner_config
    # Redundant gate check (hetzner_config already skipped if unset) + token for cleanup.
    token, _server_type, _location, _image = _cloud_env_or_skip()
    ini_path = _ini_path_from_env()

    observed_server_ids: list[str] = []
    payloads = ["hello cloud 1", "hello cloud 2"]

    orchestrator = await make_daemon(config)
    orch_task = asyncio.create_task(orchestrator.start())

    try:
        task_ids = await _submit_two_jobs(ini_path, monkeypatch, tmp_path, capfd)
        # Assert both queued before any node exists.
        async with uow_factory() as uow:
            for tid in task_ids:
                t = await uow.tasks.get(TaskId(tid))
                assert t is not None, f"task {tid} missing after submit"
                assert t.status == DomainTaskStatus.TO_DO, (
                    f"task {tid} status={t.status}, expected TO_DO after submit"
                )

        # Poll until exactly one cloud==hetzner node appears (cold-start 0 -> 1).
        # The node server ID is recorded into observed_server_ids by the helper.
        await _poll_for_hetzner_node(
            uow_factory,
            observed_server_ids,
            _AUTOSCALE_TIMEOUT_S,
        )

        await _wait_both_done(
            uow_factory,
            task_ids,
            _COMPLETION_TIMEOUT_S,
        )

        await _assert_outputs(uow_factory, task_ids, observed_server_ids, payloads)

        # CLOUD_DONE is emitted by _persist_node_with_cleanup at provision time,
        # so it is already present once both tasks reached DONE.
        _assert_cloud_done_log(log_records, observed_server_ids)

        # DB-row removal (idle deallocate), then strong deletion assertion.
        await _poll_node_gone(uow_factory, _DEALLOC_TIMEOUT_S)
        # CLOUD_DELETE is emitted by deallocate_node during the idle-deallocate
        # that just removed the row — assert it only now, not at completion time.
        _assert_cloud_delete_log(log_records)
        for sid in observed_server_ids:
            await _assert_vm_deleted(token, int(sid))

    finally:
        # (a) stop daemon + best-effort await of the background task.
        await orchestrator.stop()
        try:
            await asyncio.wait_for(orch_task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            orch_task.cancel()
        # (b) observed-VM cleanup with loud-fail-on-leak.
        await _cleanup_observed(token, observed_server_ids, log)
