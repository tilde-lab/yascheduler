# region MODULE_CONTRACT
# PURPOSE: Real-VastAI cloud-provider E2E test — autoscale -> allocate -> download -> idle-deallocate happy path against a live VastAI account.
# SCOPE: opt-in env-gated test (YASCHEDULER_TEST_VASTAI=1 + VAST_API_KEY); drives make_daemon + _submit_async; asserts VM creation, both jobs DONE with matching outputs, idle deallocation, strong deletion via GET /instances/{id}/ → 404; guaranteed instance-ID based cleanup with loud-fail-on-leak in finally.
# DEPENDENCIES: USES API: vastai
# KEYWORDS: VastAI, autoscale, deallocate, e2e
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
import os
import stat
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
from yascheduler.entrypoints.cli.submit import _submit_async
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.entrypoints.di import make_daemon

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.entrypoints import Config
    from yascheduler.infra.cloud.cloud_configs import ConfigCloudVastAI
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

log = logging.getLogger("e2e.test_vastai_live")

# Timeouts sized to observed real-VastAI cold-start: GPU image pull + boot +
# cloud-init + setup_node takes ~3-5 minutes; the two test_shell jobs (3s each)
# finish seconds after the node is enabled; idle deallocate fires
# ~idle_tolerance(5)s later. Margins are generous for GPU cold-start variance.
_AUTOSCALE_TIMEOUT_S = 300.0
_COMPLETION_TIMEOUT_S = 120.0
_DEALLOC_TIMEOUT_S = 120.0
_VM_DELETE_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 1.0


def _cloud_env_or_skip() -> str:
    if os.environ.get("YASCHEDULER_TEST_VASTAI") != "1":
        pytest.skip(
            "YASCHEDULER_TEST_VASTAI != 1; set YASCHEDULER_TEST_VASTAI=1 to enable",
        )
    api_key = os.environ.get("VAST_API_KEY", "")
    if not api_key:
        pytest.skip(
            "VAST_API_KEY unset/empty; set it to a real VastAI API key",
        )
    return api_key


@pytest.fixture(scope="session")
def vastai_config(
    tmp_path_factory: Any,
    _db_config: Any,
    _init_schema: None,
) -> Config:
    api_key = _cloud_env_or_skip()

    tmp = tmp_path_factory.mktemp("vastai_config")
    data_dir = tmp / "data"
    ini_path = tmp / "yascheduler.conf"
    db_cfg = _db_config
    # vastai_package_upgrade=false skips the slow package upgrade in the
    # onstart script so the default ConfigCloudVastAI.connect_grace (300s,
    # NOT INI-parsed) is ample.
    onstart_path = tmp / "onstart.sh"
    onstart_path.write_text("#!/bin/sh\ntouch /tmp/yascheduler-onstart-ok\n")
    onstart_path.chmod(onstart_path.stat().st_mode | stat.S_IEXEC)
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
        f"check_cmd = test -f /tmp/yascheduler-test-job-running\n"
        f"input_files = 1.input\n"
        f"output_files = 1.input.out\n"
        f"deploy_local_files = run.sh\n"
        f"sleep_interval = 1\n"
        f"platforms = linux\n"
        f"\n"
        f"[clouds]\n"
        f"vastai_api_key = {api_key}\n"
        f"vastai_max_nodes = 1\n"
        f"vastai_idle_tolerance = 20\n"
        f"vastai_package_upgrade = false\n"
        f"vastai_label = yascheduler-e2e-test\n"
        f"vastai_max_price_per_hr = 1.0\n"
        f"vastai_onstart_script = {onstart_path}\n"
    )
    ini_path.write_text(ini_content)

    engines_dir = tmp / "data" / "engines" / "test_shell"
    engines_dir.mkdir(parents=True)
    run_sh = engines_dir / "run.sh"
    run_sh.write_text(
        "#!/bin/sh\ntouch /tmp/yascheduler-test-job-running\n"
        "if [ -f /tmp/yascheduler-onstart-ok ]; then echo ONSTART_OK > 1.input.out; else echo ONSTART_MISSING > 1.input.out; exit 1; fi\n"
        "sleep 3\nrm -f /tmp/yascheduler-test-job-running\n"
        "cat 1.input >> 1.input.out\n",
    )
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IEXEC)

    # Fresh empty keys_dir: the daemon generates its own key via
    # get_or_create_ssh_key, registered into the VastAI account by
    # ensure_ssh_key.
    keys_dir = tmp / "data" / "keys"
    keys_dir.mkdir(parents=True)

    os.environ["YASCHEDULER_CONF_PATH"] = str(ini_path)
    return parse_config(str(ini_path))


def _ini_path_from_env() -> str:
    path = os.environ.get("YASCHEDULER_CONF_PATH")
    if not path:
        raise RuntimeError(
            "YASCHEDULER_CONF_PATH unset; vastai_config fixture must run first",
        )
    return path


async def _assert_vm_deleted(api_key: str, instance_id: str) -> None:
    from yascheduler.infra.cloud.providers.vastai import VastAIClient, VastAIError

    deadline = asyncio.get_running_loop().time() + _VM_DELETE_TIMEOUT_S
    async with VastAIClient(api_key) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                await client.show_instance(int(instance_id))
            except VastAIError as err:
                if err.status == 404:
                    return
                if err.status == 429:
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    continue
            await asyncio.sleep(_POLL_INTERVAL_S)
    log.error(
        "[vastai_live][CLEANUP] instance %s was NOT deleted — manual cleanup required",
        instance_id,
    )
    pytest.fail(
        f"VastAI instance {instance_id} was NOT deleted — manual cleanup required",
    )


async def _delete_one_best_effort(
    cfg: ConfigCloudVastAI,
    instance_id: str,
    log: logging.Logger,
) -> None:
    from yascheduler.infra.cloud.providers.vastai import vastai_delete_node

    try:
        await vastai_delete_node(cfg, external_id=instance_id)
    except Exception as err:
        log.error(
            "[vastai_live][CLEANUP] vastai_delete_node raised for instance_id=%s err=%s "
            "— proceeding to deletion verification",
            instance_id,
            err,
        )


async def _cleanup_observed(
    api_key: str,
    observed_instance_ids: list[str],
    log: logging.Logger,
    label: str = "yascheduler",
) -> None:
    from yascheduler.infra.cloud.cloud_configs import ConfigCloudVastAI
    from yascheduler.infra.cloud.providers.vastai import VastAIClient

    cfg: ConfigCloudVastAI = ConfigCloudVastAI(
        api_key=api_key,
        max_nodes=1,
        label=label,
    )

    # Step 1: delete all known instance IDs (from observed_instance_ids).
    for iid in observed_instance_ids:
        await _delete_one_best_effort(cfg, iid, log)

    # Step 2: scan for orphaned instances matching cfg.label that were created
    # but never captured in observed_instance_ids (e.g. allocation failed after
    # VM creation but before the node was enabled in DB). Delete any found.
    async with VastAIClient(api_key) as client:
        try:
            async for inst in client.show_instances():
                inst_label = inst.get("label")
                if not isinstance(inst_label, str) or not inst_label.startswith(label):
                    continue
                iid = str(inst["id"])
                if iid in observed_instance_ids:
                    continue
                log.warning(
                    "[vastai_live][CLEANUP] orphan instance %s not in observed set — deleting",
                    iid,
                )
                observed_instance_ids.append(iid)
                await _delete_one_best_effort(cfg, iid, log)
        except Exception as err:
            log.warning(
                "[vastai_live][CLEANUP] orphan scan failed: %s — "
                "observed-instance cleanup still runs",
                err,
            )

    # Strong per-instance deletion assertion: a survivor fails the test loudly.
    for iid in observed_instance_ids:
        await _assert_vm_deleted(api_key, iid)


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


async def _poll_for_vastai_node(
    uow_factory: Callable[[], PostgresUnitOfWork],
    observed_instance_ids: list[str],
    timeout_s: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with uow_factory() as uow:
            nodes = await uow.nodes.list_all()
        # Require enabled: _select_and_insert_tmp inserts a tmp node with
        # cloud="vastai"/enabled=False (placeholder IP); the real provisioned
        # node is committed enabled=True in _persist_node_with_cleanup (which
        # atomically removes the tmp row in the same commit).
        vastai_nodes = [n for n in nodes if n.cloud == "vastai" and n.enabled]
        if vastai_nodes:
            node = vastai_nodes[0]
            iid = node.external_id
            if iid and iid not in observed_instance_ids:
                observed_instance_ids.append(iid)
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"no enabled cloud==vastai node appeared within {timeout_s}s")


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
    observed_instance_ids: list[str],
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
        expected = f"ONSTART_OK\n{payloads[idx]}"
        assert actual == expected, (
            f"task {tid} output={actual!r}, expected {expected!r}"
        )
        node_id = allocated_node_id_of(task)
        task_iid = (
            nodes_by_id[node_id].external_id
            if node_id and node_id in nodes_by_id
            else ""
        )
        assert task_iid in observed_instance_ids, (
            f"task {tid} node.external_id={task_iid} "
            f"not among observed instance IDs={observed_instance_ids}"
        )
        assert isinstance(task.created_at, datetime), (
            f"task {tid} created_at={task.created_at!r} is not datetime"
        )
        assert isinstance(task.updated_at, datetime), (
            f"task {tid} updated_at={task.updated_at!r} is not datetime"
        )


def _assert_cloud_done_log(
    records: list[logging.LogRecord],
    observed_instance_ids: list[str],
) -> None:
    cloud_done_records = [r for r in records if r.getMessage() == "CLOUD_DONE"]
    done_match = any(
        (fields := extra_fields(rec)).get("cloud") == "vastai"
        and fields.get("external_id") in observed_instance_ids
        for rec in cloud_done_records
    )
    assert done_match, (
        f"no CLOUD_DONE trace record with cloud=vastai and external_id in "
        f"{observed_instance_ids}; "
        f"captured CLOUD_DONE records={cloud_done_records}"
    )


def _assert_cloud_delete_log(records: list[logging.LogRecord]) -> None:
    cloud_delete_records = [r for r in records if r.getMessage() == "CLOUD_DELETE"]
    delete_match = any(
        extra_fields(rec).get("cloud") == "vastai" for rec in cloud_delete_records
    )
    assert delete_match, (
        f"no CLOUD_DELETE trace record with cloud=vastai; "
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
        if not any(n.cloud == "vastai" for n in nodes):
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"cloud==vastai node row not removed within {timeout_s}s")


async def test_vastai_live(
    vastai_config: Config,
    uow_factory: Callable[[], PostgresUnitOfWork],
    log_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    config = vastai_config
    # Redundant gate check (vastai_config already skipped if unset) + key for cleanup.
    api_key = _cloud_env_or_skip()
    ini_path = _ini_path_from_env()

    observed_instance_ids: list[str] = []
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

        # Poll until exactly one cloud==vastai node appears (cold-start 0 -> 1).
        # The node instance ID is recorded into observed_instance_ids by the helper.
        await _poll_for_vastai_node(
            uow_factory,
            observed_instance_ids,
            _AUTOSCALE_TIMEOUT_S,
        )

        await _wait_both_done(
            uow_factory,
            task_ids,
            _COMPLETION_TIMEOUT_S,
        )

        await _assert_outputs(uow_factory, task_ids, observed_instance_ids, payloads)

        # CLOUD_DONE is emitted by _persist_node_with_cleanup at provision time,
        # so it is already present once both tasks reached DONE.
        _assert_cloud_done_log(log_records, observed_instance_ids)

        # DB-row removal (idle deallocate), then strong GET /instances/{id}/ deletion assertion.
        await _poll_node_gone(uow_factory, _DEALLOC_TIMEOUT_S)
        # CLOUD_DELETE is emitted by deallocate_node during the idle-deallocate
        # that just removed the row — assert it only now, not at completion time.
        _assert_cloud_delete_log(log_records)

        for iid in observed_instance_ids:
            await _assert_vm_deleted(api_key, iid)

    finally:
        # (a) stop daemon + best-effort await of the background task.
        await orchestrator.stop()
        try:
            await asyncio.wait_for(orch_task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            orch_task.cancel()
        # (b) observed-instance cleanup with loud-fail-on-leak.
        # Use the same label as the test config so orphan scan only matches
        # instances created by this test run.
        vastai_label = next(
            (
                getattr(c, "label", "yascheduler")
                for c in config.clouds
                if c.prefix == "vastai"
            ),
            "yascheduler",
        )
        await _cleanup_observed(api_key, observed_instance_ids, log, label=vastai_label)
