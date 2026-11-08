"""Allocate task use case — match a TO_DO task to a free machine or request cloud provisioning."""
# region MODULE_CONTRACT
# PURPOSE: Keep tasks moving from TO_DO to RUNNING by finding any eligible compute — free machine first, cloud fallback — so the backlog does not stall.
# SCOPE: Task-to-machine allocation with cloud fallback — free machine search, cloud provisioning, tmp-node lifecycle, and persistence under a critical section.
# KEYWORDS: allocate, task, machine, cloud, provisioning, tmp-node, critical section
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from yascheduler.domain import (
    MachineSession,
    NewNode,
    Node,
    NodeId,
    RunningTask,
    TaskId,
    TaskStatus,
    TodoTask,
)
from yascheduler.domain.exceptions import NodeRowNotFoundError

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import PurePath

    from yascheduler.domain import (
        CloudProvisioner,
        Engine,
        EngineRepository,
        MachineRepository,
    )
    from yascheduler.infra import OccupancyChecker

    from .allocation_tracker import AllocationTracker
    from .uow import AbstractUnitOfWork

logger = logging.getLogger(__name__)

__all__ = ["allocate_task"]


class _TmpSelection(NamedTuple):
    name: str
    node: Node


# region FUNC__validate_engine
# PURPOSE: Reject tasks with unknown engines early so they do not consume resources on an allocation attempt that can never succeed.
# ENSURES: Returns the resolved Engine on success; on unsupported engine, transitions task via reject(), saves/commits via UoW, returns None.
async def _validate_engine(
    task: TodoTask,
    engines: EngineRepository,
    uow_factory: Callable[[], AbstractUnitOfWork],
) -> Engine | None:
    # region BLOCK_validate_engine
    engine_name: str | None = task.engine
    engine = engines.get(engine_name) if engine_name else None
    if engine is None:
        logger.warning(
            "Unsupported engine '%s' for task_id=%s",
            engine_name,
            task.task_id,
        )
        rejected = task.reject("unsupported engine")
        async with uow_factory() as uow:
            await uow.tasks.save(rejected)
            await uow.commit()
        return None
    # endregion BLOCK_validate_engine
    return engine


# endregion FUNC__validate_engine


# region FUNC__try_start_on_machine
# PURPOSE: Complete the full RUNNING transition for a task on a specific machine — occupy the slot, upload inputs, spawn the job, persist the state change, and release the tracker guard.
# ENSURES: On success: task is RUNNING in DB, occupancy check started, tracker slot discarded. On failure returns False without side effects.
async def _try_start_on_machine(
    session: MachineSession,
    node: Node,
    engine: Engine,
    task: TodoTask,
    occupancy_checker: OccupancyChecker,
    uow_factory: Callable[[], AbstractUnitOfWork],
    start_task_on_machine: Callable[
        [MachineSession, Engine, RunningTask], Awaitable[bool]
    ],
    tracker: AllocationTracker,
    remote_tasks_dir: PurePath,
) -> bool:
    dt_str = task.created_at.strftime("%Y%m%d_%H%M%S")
    remote_folder = str(remote_tasks_dir / f"{dt_str}_{task.task_id}")
    running = task.run(node.node_id, remote_folder)
    logger.debug(
        "TRY_ALLOCATE",
        extra={
            "task_id": running.task_id,
            "hostname": session.hostname,
            "node_id": node.node_id,
        },
    )
    # Claim before spawn: the status-guarded save takes the row lock and
    # holds it across the spawn inside one unit of work. A lost cross-host
    # claim raises before the spawn is reached, so no orphan process is
    # created on the losing host.
    async with uow_factory() as uow:
        await uow.tasks.save(running, expected_status=TaskStatus.TO_DO)
        if not await start_task_on_machine(session, engine, running):
            await uow.rollback()
            return False
        await uow.commit()
    logger.debug(
        "ALLOCATED",
        extra={
            "task_id": running.task_id,
            "hostname": session.hostname,
            "node_id": node.node_id,
        },
    )
    occupancy_checker.start_occupancy_check(session, engine)
    tracker.discard(running.task_id)
    return True


# endregion FUNC__try_start_on_machine


# region FUNC__find_free_machines
# PURPOSE: Enumerate all viable machine-task pairs so the allocator can iterate them without redundant lookups.
# REQUIRES: The UoW opened at block entry reads uow.nodes.list_enabled() in the same transaction as uow.tasks.list_by_status({RUNNING}); MachineRepository.list_free(platforms) is intersected with the enabled-node set MINUS the busy-node set (allocated_node_id of RUNNING tasks).
# ENSURES: Returns list of (session, node) pairs for machines that are free, compatible, not already running a task, not listed in the engine's exclude_nodes, and present in the enabled-nodes DB view.
# RATIONALE:
# - Q: Why does the enabled=True gate live in the use case instead of MachineRepository?
#   A: MachineRepository is an infrastructure-layer SSH-collection port that SHALL NOT be coupled to NodeRepository (a persistence port); joining these two data sources is the use case's responsibility. The gate restores the invariant that a machine is allocatable only after its DB row is enabled=TRUE — flipped from enabled=FALSE only after cloud-init, engine setup, and CPU detection complete.
async def _find_free_machines(
    engine: Engine,
    uow_factory: Callable[[], AbstractUnitOfWork],
    repository: MachineRepository,
) -> list[tuple[MachineSession, Node]]:
    # region BLOCK_find_free_machines
    async with uow_factory() as uow:
        running_tasks = await uow.tasks.list_running()
        enabled_nodes = await uow.nodes.list_enabled()
    busy_node_ids = {t.state.allocated_node_id for t in running_tasks}
    nodes_by_id = {n.node_id: n for n in enabled_nodes}
    return [
        (s, nodes_by_id[s.machine.node_id])
        for s in repository.list_free(platforms=list(engine.platforms))
        if s.machine.node_id in nodes_by_id
        and s.machine.node_id not in busy_node_ids
        and s.hostname not in engine.exclude_nodes
    ]
    # endregion BLOCK_find_free_machines


# endregion FUNC__find_free_machines


# region FUNC__allocate_free_machine
# PURPOSE: Try every eligible free machine until one succeeds or all are exhausted, so a single stale session does not block the cloud-provisioning path.
# ENSURES: Returns True if allocated to a machine; False if no candidate succeeded. Per-pair failures are logged but do not abort the loop.
async def _allocate_free_machine(
    task: TodoTask,
    engine: Engine,
    uow_factory: Callable[[], AbstractUnitOfWork],
    repository: MachineRepository,
    occupancy_checker: OccupancyChecker,
    start_task_on_machine: Callable[
        [MachineSession, Engine, RunningTask], Awaitable[bool]
    ],
    tracker: AllocationTracker,
    remote_tasks_dir: PurePath,
) -> bool:
    free_sessions = await _find_free_machines(engine, uow_factory, repository)

    # region BLOCK_allocate_machine
    for session, node in free_sessions:
        # region BLOCK_try_start_isolated
        # Defense-in-depth: a single stale or transiently-unreachable session
        # must not abort the loop and starve the cloud-provisioning branch.
        # No repository.disconnect here — a transient SSH failure does not
        # imply a dead session; the monitor task manages its lifecycle.
        # Stale sessions left by failed setup are prevented at the source by
        # the setup-failure disconnect in CloudProvisionerImpl.allocate.
        try:
            if await _try_start_on_machine(
                session,
                node,
                engine,
                task,
                occupancy_checker,
                uow_factory,
                start_task_on_machine,
                tracker,
                remote_tasks_dir,
            ):
                return True
        except Exception as err:  # noqa: PERF203
            logger.debug(
                "SESSION_FAILED",
                extra={
                    "task_id": task.task_id,
                    "hostname": session.hostname,
                    "err": err,
                },
            )
            continue
        # endregion BLOCK_try_start_isolated
    # endregion BLOCK_allocate_machine

    return False


# endregion FUNC__allocate_free_machine


def _count_nodes_by_cloud(nodes: Sequence[Node]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for n in nodes:
        if n.cloud:
            counts[n.cloud] = counts.get(n.cloud, 0) + 1
    return counts


# region FUNC__select_and_insert_tmp
# PURPOSE: Reserve a cloud capacity slot atomically so concurrent allocators see the committed tmp-node and do not overshoot max_nodes.
# ENSURES: Returns _TmpSelection (provider name + tmp-node) on success; None if no provider is available. The tmp-node row is committed before the lock is released.
async def _select_and_insert_tmp(
    clouds: CloudProvisioner,
    engine: Engine,
    uow_factory: Callable[[], AbstractUnitOfWork],
    allocation_lock: asyncio.Lock,
) -> _TmpSelection | None:
    # region BLOCK_capacity_and_select
    async with allocation_lock, uow_factory() as uow:
        nodes = await uow.nodes.list_all()
        counts = _count_nodes_by_cloud(nodes)
        selected_name = clouds.select_provider(list(engine.platforms), counts)
        if selected_name is None:
            return None
        tmp_node = await uow.nodes.insert(
            NewNode(cloud=selected_name, enabled=False),
        )
        await uow.commit()
        return _TmpSelection(name=selected_name, node=tmp_node)
    # endregion BLOCK_capacity_and_select


# endregion FUNC__select_and_insert_tmp


# region FUNC__cleanup_tmp_node_best_effort
# PURPOSE: Best-effort tmp-node removal by NodeId; logs failures, never raises.
# ENSURES: Always completes without raising; failures are logged at exception level.
async def _cleanup_tmp_node_best_effort(
    uow_factory: Callable[[], AbstractUnitOfWork],
    tmp_node_id: NodeId,
    task_id: TaskId,
    context: str,
) -> None:
    # region BLOCK_best_effort_tmp_cleanup
    try:
        async with uow_factory() as uow:
            await uow.nodes.remove(tmp_node_id)
            await uow.commit()
    except NodeRowNotFoundError:
        # Tmp-node already gone — the
        # desired end state is achieved; nothing to clean up.
        logger.debug(
            "tmp-node already removed: task_id=%s ctx=%s tmp_node_id=%s",
            task_id,
            context,
            tmp_node_id,
        )
    except Exception:
        logger.exception(
            "tmp-node cleanup failed: task_id=%s ctx=%s tmp_node_id=%s",
            task_id,
            context,
            tmp_node_id,
        )
    # endregion BLOCK_best_effort_tmp_cleanup


# endregion FUNC__cleanup_tmp_node_best_effort


# region FUNC__allocate_cloud_node
# PURPOSE: Provision the cloud VM and on failure clean up the tmp-node so the capacity slot is released and the error propagates for proper tracking.
# ENSURES: On success returns the provisioned Node; on failure cleans up tmp-node best-effort and re-raises (never masks with cleanup exception).
async def _allocate_cloud_node(
    clouds: CloudProvisioner,
    uow_factory: Callable[[], AbstractUnitOfWork],
    selected_name: str,
    tmp_node: Node,
    task_id: TaskId,
) -> Node:
    # region BLOCK_cloud_allocate
    try:
        node = await clouds.allocate(selected_name, tmp_node)
    except Exception:
        logger.exception("cloud allocation failed: task_id=%s", task_id)
        # Best-effort tmp-node cleanup; failure here is logged but does
        # not mask the original cloud-allocation exception.
        await _cleanup_tmp_node_best_effort(
            uow_factory,
            tmp_node.node_id,
            task_id,
            "cloud-alloc-failed",
        )
        raise
    # endregion BLOCK_cloud_allocate
    return node


# endregion FUNC__allocate_cloud_node


# region FUNC__persist_node_with_cleanup
# PURPOSE: Save the provisioned node's real details (IP, ncpus) and on failure delete the billable orphan VM immediately, so cloud costs are not incurred for nodes the scheduler cannot use.
# ENSURES: On success the node is committed (enabled=True, real ip, ncpus); on failure deallocates the VM (best-effort), cleans up tmp-node, and re-raises.
async def _persist_node_with_cleanup(
    node: Node,
    clouds: CloudProvisioner,
    uow_factory: Callable[[], AbstractUnitOfWork],
    tmp_node_id: NodeId,
    task_id: TaskId,
) -> None:
    # region BLOCK_final_persist
    try:
        async with uow_factory() as uow:
            await uow.nodes.update(node)
            await uow.commit()
    except BaseException:
        # `except BaseException` (not Exception) because of asyncio.CancelledError
        logger.exception(
            "persist node failed: task_id=%s hostname=%s",
            task_id,
            node.hostname,
        )
        # VM is up and billing but the DB row was not flipped; best-effort
        # delete so we don't leak a billable orphan. Failure here is logged
        # not raised. The returned node always carries cloud=adapter.name
        # (allocate is authoritative over the cloud field), so deallocate
        # resolves the provider from node.cloud directly.
        try:
            await clouds.deallocate(node)
        except Exception:
            logger.exception(
                "deallocate node failed: task_id=%s hostname=%s",
                task_id,
                node.hostname,
            )
        await _cleanup_tmp_node_best_effort(
            uow_factory,
            tmp_node_id,
            task_id,
            "persist-failed",
        )
        raise
    # endregion BLOCK_final_persist

    logger.debug(
        "CLOUD_DONE",
        extra={
            "task_id": task_id,
            "external_id": node.external_id,
            "cloud": node.cloud,
        },
    )


# endregion FUNC__persist_node_with_cleanup


# region FUNC_allocate_task
# PURPOSE: Match a TO_DO task to a free compatible machine or request cloud allocation with critical-section dedup.
# REQUIRES: Delegates to _find_free_machines which opens a UoW that reads uow.nodes.list_enabled() alongside uow.tasks.list_by_status({RUNNING}), intersecting MachineRepository.list_free(platforms) with the enabled-node set minus the busy-node set (allocated_node_id of RUNNING tasks).
# ENSURES: Returns True if allocated to a machine; False if cloud-provisioning was initiated or no allocation was possible. Tracker slot is discarded on failure paths.
async def allocate_task(
    task_id: TaskId,
    engines: EngineRepository,
    uow_factory: Callable[[], AbstractUnitOfWork],
    repository: MachineRepository,
    occupancy_checker: OccupancyChecker,
    clouds: CloudProvisioner,
    start_task_on_machine: Callable[
        [MachineSession, Engine, RunningTask], Awaitable[bool]
    ],
    tracker: AllocationTracker,
    allocation_lock: asyncio.Lock,
    remote_tasks_dir: PurePath,
) -> bool:
    """Match a TO_DO task to a free compatible machine or request cloud allocation."""
    async with uow_factory() as uow:
        task = await uow.tasks.get_todo(task_id)
    if task is None:
        return False

    logger.debug("ALLOCATE_TASK", extra={"task_id": task.task_id})

    engine = await _validate_engine(task, engines, uow_factory)
    if engine is None:
        return False

    if await _allocate_free_machine(
        task,
        engine,
        uow_factory,
        repository,
        occupancy_checker,
        start_task_on_machine,
        tracker,
        remote_tasks_dir,
    ):
        return True

    # Empty-platform engines are eligible for free machines (matched above) but
    # never for cloud provisioning — select_provider would silently return None
    # and the task would spin in TO_DO forever. Short-circuit with a warning
    # before entering the cloud critical section.
    if not engine.platforms:
        logger.warning(
            "engine %s has no platforms; cannot cloud-provision: task_id=%s",
            engine.name,
            task.task_id,
        )
        return False

    # region BLOCK_allocate_cloud_critical_section
    logger.debug("CLOUD", extra={"task_id": task.task_id})

    # Step 0: tracker dedup
    if not tracker.add(task.task_id):
        logger.debug("DEDUP", extra={"task_id": task.task_id})
        return False

    # success-flag guarantees tracker.discard on any exception escaping the
    # cloud-fallback (step1 commit/insert-tmp fail, step2 cloud-alloc fail,
    # step3 final persist fail) while preserving the entry on the success path
    # (VM provisioned, awaiting consume_task / next-cycle free-machine start).
    cloud_allocated = False
    tmp_node_id: NodeId | None = None
    try:
        selected = await _select_and_insert_tmp(
            clouds,
            engine,
            uow_factory,
            allocation_lock,
        )
        if selected is None:
            logger.debug("NO_PROVIDER", extra={"task_id": task.task_id})
            return False
        selected_name = selected.name
        tmp_node_id = selected.node.node_id
        tracker.set_node(task.task_id, tmp_node_id)
        node = await _allocate_cloud_node(
            clouds,
            uow_factory,
            selected_name,
            selected.node,
            task_id,
        )
        await _persist_node_with_cleanup(
            node,
            clouds,
            uow_factory,
            tmp_node_id,
            task_id,
        )
        cloud_allocated = True
        return False
    finally:
        if not cloud_allocated:
            tracker.discard(task.task_id)
            # Safety net for cancellation at the await between the tmp-node
            # commit and helper entry: step2/step3 clean up the tmp-node on
            # their own failure paths, so a redundant call here is a silent
            # 0-row DELETE no-op; only the cancellation gap would otherwise
            # leak the capacity slot.
            if tmp_node_id is not None:
                await _cleanup_tmp_node_best_effort(
                    uow_factory,
                    tmp_node_id,
                    task.task_id,
                    "allocator-unexpected",
                )
    # endregion BLOCK_allocate_cloud_critical_section


# endregion FUNC_allocate_task
