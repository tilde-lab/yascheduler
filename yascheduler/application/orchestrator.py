"""Daemon orchestrator — manages producer-consumer loops calling use cases."""
# region MODULE_CONTRACT
# PURPOSE: Keep the daemon running continuously by driving the four scheduling phases — connect machines, allocate tasks, consume outputs, deallocate idle nodes — as resilient async loops that never block on a single failure.
# SCOPE: Orchestrator class with async producer-consumer loops for machine connection, task allocation, task consumption, and node deallocation; resilience wrappers around use cases; config-driven sleep intervals and concurrency limits.
# DEPENDENCIES: USES API: aiohttp.ClientSession
# KEYWORDS: daemon, orchestrator, producer-consumer, loop, connect, allocate, consume, deallocate
# INVARIANTS: Orchestrator-level dependencies typed against domain Protocols (MachineRepository, CloudProvisioner) and concrete collaborators (TaskDeployer, OutputDownloader, OccupancyChecker)
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from asyncio.locks import Event
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING

from yascheduler.domain import (
    CloudProvisioner,
    MachineConnectionError,
    MachineRepository,
    MachineSession,
    MachineState,
    Node,
    NodeId,
    RunningTask,
    TaskId,
    TaskStatus,
    TodoTask,
)

from .abandon_node import abandon_node
from .allocate_task import _count_nodes_by_cloud, allocate_task
from .consume_task import consume_task
from .deallocate_nodes import deallocate_node, deallocate_nodes
from .queue import UMessage, UniqueQueue

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence
    from pathlib import Path, PurePath

    import aiohttp

    from yascheduler.domain import (
        CloudConfig,
        Engine,
        EngineRepository,
        LocalSettings,
        RemoteDefaults,
    )
    from yascheduler.infra import (
        OccupancyChecker,
        OutputDownloader,
        TaskDeployer,
    )

    from .allocation_tracker import AllocationTracker
    from .uow import AbstractUnitOfWork

__all__ = ["Orchestrator"]

# Upper bound on the graceful-drain join at shutdown. stop() cancels every
# task in self._bg_jobs; if workers are cancelled
# before their coordinator reaches queue.join(), no consumer remains and the
# join would hang until SIGKILL. Bounded so shutdown always completes.
_DRAIN_TIMEOUT: float = 5.0


async def _asleep_until(end: datetime) -> None:
    """Sleep until :end:."""
    now = datetime.now(timezone.utc)
    if now >= end:
        return
    await asyncio.sleep((end - now).total_seconds())


# region CLASS_Orchestrator
# PURPOSE: Keep the daemon running continuously by driving the four scheduling phases — connect, allocate, consume, deallocate — as resilient async loops that never block on a single failure.
class Orchestrator:
    """Manage the daemon's 4 producer-consumer loops, delegating business logic to use cases."""

    def __init__(
        self,
        local_settings: LocalSettings,
        remote_defaults: RemoteDefaults,
        uow_factory: Callable[[], AbstractUnitOfWork],
        clouds: CloudProvisioner,
        repository: MachineRepository,
        task_deployer: TaskDeployer,
        output_downloader: OutputDownloader,
        occupancy_checker: OccupancyChecker,
        engines: EngineRepository,
        config_clouds: Sequence[CloudConfig],
        local_tasks_dir: Path,
        allocation_tracker: AllocationTracker,
        active_clouds: Sequence[CloudConfig],
        allocation_lock: asyncio.Lock,
        list_private_keys_fn: Callable[[Path], Sequence[PurePath]],
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialise orchestrator with all daemon dependencies."""
        self._local_settings = local_settings
        self._remote_defaults = remote_defaults
        self._uow_factory = uow_factory
        self._clouds = clouds
        self._repository = repository
        self._task_deployer = task_deployer
        self._output_downloader = output_downloader
        self._occupancy_checker = occupancy_checker
        self._engines = engines
        self._config_clouds = config_clouds
        self._local_tasks_dir = local_tasks_dir
        self._http_session = http_session
        self._tracker = allocation_tracker
        self._active_clouds = active_clouds
        self._allocation_lock = allocation_lock
        self._list_private_keys_fn = list_private_keys_fn

        self._bg_jobs: set[asyncio.Task[None]] = set()
        self._cancellation_event = Event()
        self._machine_connected_event = Event()
        self._sleep_interval: int = min(e.sleep_interval for e in engines.values())
        self._occupancy_started: set[NodeId] = set()
        # In-flight consume task ids — prevents two workers from concurrently
        # consuming the same RUNNING task across overlapping producer cycles.
        # Same-event-loop check/add/remove are atomic (no await between check
        # and add). In-memory only; daemon restart resets the guard.
        self._consuming: set[TaskId] = set()
        # Per-node first-seen monotonic timestamp of a consecutive connect
        # failure (keyed by NodeId); in-memory only (daemon restart resets
        # grace windows).
        self._connect_failures: dict[NodeId, float] = {}
        # Single-execution guard for stop(): set synchronously at the top of
        # stop() with no `await` between check and set — atomic in
        # single-threaded asyncio. In-memory only; a fresh Orchestrator
        # starts with _stopped = False.
        self._stopped: bool = False

        lcfg = local_settings
        self._conn_machine_q: UniqueQueue[NodeId, Node] = UniqueQueue(
            "conn_machine",
            maxsize=lcfg.conn_machine_pending,
        )
        self._allocate_q: UniqueQueue[TaskId, TodoTask] = UniqueQueue(
            "allocate",
            maxsize=lcfg.allocate_pending,
        )
        self._consume_q: UniqueQueue[TaskId, RunningTask] = UniqueQueue(
            "consume",
            maxsize=lcfg.consume_pending,
        )
        self._deallocate_q: UniqueQueue[NodeId, Node] = UniqueQueue(
            "deallocate",
            maxsize=lcfg.deallocate_pending,
        )

    # ---- Task deployment wrapper ----

    # region METHOD_start_task_on_machine
    # PURPOSE: Bridge the UoW boundary — resolve ncpus from the DB for the task's allocated node before delegating to the infra deployer so the deployer sees the accurate CPU count.
    async def _start_task_on_machine(
        self,
        session: MachineSession,
        engine: Engine,
        task: RunningTask,
    ) -> bool:
        # region BLOCK_resolve_ncpus
        async with self._uow_factory() as uow:
            node = await uow.nodes.get_by_id(task.state.allocated_node_id)
        ncpus = (
            node.ncpus
            if node is not None and node.ncpus is not None
            else await session.get_cpu_cores()
        )
        # endregion BLOCK_resolve_ncpus
        return await self._task_deployer.start_task_on_machine(
            session,
            engine,
            task,
            ncpus,
            self._remote_defaults.engines_dir,
        )

    # endregion METHOD_start_task_on_machine

    # ---- Stats ----

    # region METHOD_print_stats
    # PURPOSE: Surface daemon health at-a-glance — queue backlogs, node busy/idle ratios, and task throughput — so operators can detect stalls without external monitoring.
    async def _print_stats(self) -> None:
        last_msg: str | None = None
        last_qmsg: str | None = None
        while not self._cancellation_event.is_set():
            end_time = datetime.now(timezone.utc) + timedelta(seconds=10)
            # region BLOCK_stats_resilience
            try:
                async with self._uow_factory() as uow:
                    ncounters = await uow.nodes.count_by_status()
                    tcounters = await uow.tasks.count_by_status()
                n_busy = sum(
                    1
                    for s in self._repository.list_connected()
                    if s.machine.state == MachineState.BUSY
                )
                tmpl = (
                    "THREADS: {tasks} "
                    "NODES: busy:{n_busy}/enabled:{n_enabled}/total:{n_total} "
                    "TASKS: run:{t_run}/todo:{t_todo}/done:{t_done}"
                )
                msg = tmpl.format(
                    tasks=len(asyncio.all_tasks()),
                    n_busy=n_busy,
                    n_enabled=ncounters.get(True, 0),
                    n_total=sum(ncounters.values()),
                    t_run=tcounters.get(TaskStatus.RUNNING, 0),
                    t_todo=tcounters.get(TaskStatus.TO_DO, 0),
                    t_done=tcounters.get(TaskStatus.DONE, 0),
                )
                if msg != last_msg:
                    last_msg = msg
                    logger.info(msg)

                queues: list[
                    UniqueQueue[NodeId, Node]
                    | UniqueQueue[TaskId, TodoTask]
                    | UniqueQueue[TaskId, RunningTask]
                ] = [
                    self._conn_machine_q,
                    self._allocate_q,
                    self._deallocate_q,
                    self._consume_q,
                ]
                qmsg = " ".join([f"{q.name}: {q.psize()}/{q.qsize()}" for q in queues])
                if qmsg != last_qmsg:
                    last_qmsg = qmsg
                    logger.info("QUEUES: %s", qmsg)

            # CancelledError is a BaseException — do NOT broaden to
            # `except BaseException` or shutdown will be swallowed.
            except Exception as err:
                logger.debug("ERROR", extra={"context": "stats", "err": err})
                logger.exception("stats print failed")
            finally:
                await _asleep_until(end_time)
            # endregion BLOCK_stats_resilience

    # endregion METHOD_print_stats

    # ---- Producers / Consumers ----

    # region METHOD_connect_machine_producer
    # PURPOSE: Yield enabled nodes and disabled nodes that still carry a RUNNING task, all not currently registered in the gateway, so the connect consumer can establish an SSH session — the static-node never-abandon guarantee is enforced downstream in the consumer.
    # REQUIRES: _connect_machine_consumer enforces the indefinite-retry path for cloud=None nodes before the grace-check; self._uow_factory yields a UnitOfWork whose nodes.list_enabled() returns the operator-defined enabled set from DB, nodes.list_disabled() returns disabled nodes with a real hostname, and tasks.list_by_status({RUNNING}) returns tasks carrying allocated_node_id.
    # ENSURES: Every yielded UMessage<NodeId, Node> corresponds to a node that is either enabled in yascheduler_nodes or disabled with at least one RUNNING task, and whose node_id is absent from self._repository (not already connected).
    # INVARIANTS: The producer does not filter on cloud type; the consumer separates their fate.
    # RATIONALE:
    # - Q: Why do static and cloud nodes share the same producer filter instead of splitting into two producers?
    #   A: The separation happens in the consumer where node.cloud is already available on the message payload — a single producer avoids duplicating the enabled+not-connected filter logic.
    # - Q: Why does the producer also yield disabled nodes with a RUNNING task?
    #   A: After a daemon restart, the in-memory session dict is empty. A disabled node (operator-initiated drain or idle-tolerance deallocate) may still carry a RUNNING task whose results have not been downloaded. Reconnecting lets the consume loop finish the task; the deallocator picks up the node for teardown once the task completes and the node is no longer busy.
    async def _connect_machine_producer(
        self,
    ) -> AsyncGenerator[UMessage[NodeId, Node], None]:
        async with self._uow_factory() as uow:
            enabled_nodes = await uow.nodes.list_enabled()
            # region BLOCK_draining_nodes
            # A disabled node may still carry a RUNNING task (operator-initiated
            # drain via _remove_node_soft, or idle-tolerance disable that raced
            # with a long-running task). Reconnect so the consume loop can
            # finish the task and download outputs; the deallocator tears the
            # node down once it is no longer busy.
            running_tasks = await uow.tasks.list_running()
            busy_node_ids = {t.state.allocated_node_id for t in running_tasks}
            draining_nodes = [
                n for n in await uow.nodes.list_disabled() if n.node_id in busy_node_ids
            ]
            # endregion BLOCK_draining_nodes
        # region BLOCK_filter_not_connected
        # Yield every candidate not currently registered in the gateway,
        # regardless of cloud. Static (cloud is None) and cloud-provisioned
        # nodes are both connected here; the never-abandon guarantee for
        # static nodes is enforced in _connect_machine_consumer before the
        # grace-check (see BLOCK_static_node_retry).
        new_nodes = [
            n
            for n in (*enabled_nodes, *draining_nodes)
            if not self._repository.contains(n.node_id)
        ]
        # endregion BLOCK_filter_not_connected
        for node in new_nodes:
            yield UMessage(node.node_id, node)

    # endregion METHOD_connect_machine_producer
    # region METHOD_connect_machine_consumer
    # PURPOSE: Establish SSH connections for enabled nodes and handle failures — resetting the failure timer on success, retrying indefinitely for static nodes (cloud is None), retrying cloud nodes within their connect_grace window, and abandoning cloud nodes whose grace window has been exceeded.
    # REQUIRES: self._connect_failures is an in-memory dict (not persisted — daemon restart resets grace windows); self._repository.connect reads jump_host/jump_port/jump_username from the Node itself (no config.remote lookup); self._connect_grace_for resolves grace by matching node.cloud against self._config_clouds prefixes (default 120s on mismatch).
    # ENSURES: On successful connect, the node_id entry is popped from _connect_failures and self._machine_connected_event is set; for static nodes (cloud is None), a trace DEBUG + warning(...) record is emitted and the method returns before the grace-check (NEVER reaches abandon_node); for cloud nodes with age < grace, a trace DEBUG + warning(...) record is emitted and the method returns; for cloud nodes with age >= grace, abandon_node() is called and the failure-timer entry is released.
    # INVARIANTS: _connect_failures is mutated exclusively in this method (setdefault on first failure, pop on success or after abandon); static nodes never write to _connect_failures because the early return before BLOCK_connect_grace_check avoids the setdefault call; no config.remote lookup or CloudConfig prefix-match loop for jump identity runs in the orchestrator — connection identity comes exclusively from the Node; a missing keys_dir is translated to MachineConnectionError in BLOCK_load_private_keys before the connect try-block, so it follows the same retry path as SSH-level failures rather than escaping to the worker wrapper as a contextless CONSUMER_ERROR.
    # RATIONALE:
    # - Q: Why do static nodes retry indefinitely instead of entering the grace/abandon path?
    #   A: A transient SSH outage after a daemon restart must not silently delete an operator's static-node row — static nodes retry indefinitely rather than entering the grace window or abandon path.
    # - Q: Why is the default grace 120 seconds for unrecognised cloud prefixes?
    #   A: The conservative fallback ensures the abandon path still fires for misconfigured or renamed cloud prefixes rather than silently retrying forever.
    # - Q: Why translate FileNotFoundError from list_private_keys into MachineConnectionError instead of letting it bubble to the worker wrapper?
    async def _connect_machine_consumer(self, msg: UMessage[NodeId, Node]) -> None:
        node = msg.payload
        try:
            # region BLOCK_load_private_keys
            # list_private_keys() now creates keys_dir lazily (issue #100), so
            # FileNotFoundError here is unexpected; the translation is kept as
            # a defensive fallback so any stray None/PermissionError-classed
            # failure still routes through the designed retry path rather than
            # escaping as a contextless CONSUMER_ERROR.
            try:
                keys = await asyncio.get_running_loop().run_in_executor(
                    None,
                    self._list_private_keys_fn,
                    self._local_settings.keys_dir,
                )
            except FileNotFoundError as err:
                raise MachineConnectionError(
                    node.node_id,
                    node.hostname,
                    f"keys_dir does not exist: {self._local_settings.keys_dir}",
                ) from err
            # endregion BLOCK_load_private_keys
            await self._repository.connect(
                node=node,
                client_keys=keys,
                connect_timeout=10,
                data_dir=self._remote_defaults.data_dir,
                engines_dir=self._remote_defaults.engines_dir,
                tasks_dir=self._remote_defaults.tasks_dir,
            )
            # region BLOCK_connect_reset_failure_timer
            self._connect_failures.pop(node.node_id, None)
            # endregion BLOCK_connect_reset_failure_timer
            self._machine_connected_event.set()
        except MachineConnectionError as err:
            # region BLOCK_static_node_retry
            # Static operator-managed nodes (cloud is None) are retried
            # indefinitely on every producer cycle and never enter the
            # abandon path. The guard sits before the grace-check so
            # _connect_failures is never populated and _connect_grace_for
            # is never called on the production path for static nodes.
            if node.cloud is None:
                logger.debug(
                    "CONNECT_RETRY_STATIC",
                    extra={
                        "node_id": node.node_id,
                        "hostname": node.hostname,
                        "err": err,
                    },
                )
                logger.warning("static node %s connect failed: %s", node.hostname, err)
                return
            # endregion BLOCK_static_node_retry
            # region BLOCK_connect_grace_check
            first_seen = self._connect_failures.setdefault(
                node.node_id,
                time.monotonic(),
            )
            age = time.monotonic() - first_seen
            grace = self._connect_grace_for(node.cloud)
            if age < grace:
                logger.debug(
                    "CONNECT_RETRY",
                    extra={
                        "node_id": node.node_id,
                        "hostname": node.hostname,
                        "age": age,
                        "grace": grace,
                        "err": err,
                    },
                )
                logger.warning("cloud node %s connect failed: %s", node.hostname, err)
                return
            # endregion BLOCK_connect_grace_check

            # region BLOCK_connect_abandon
            logger.debug(
                "CONNECT_ABANDON",
                extra={
                    "node_id": node.node_id,
                    "hostname": node.hostname,
                    "age": age,
                    "grace": grace,
                },
            )
            logger.exception(
                "abandoning cloud node %s after grace exceeded",
                node.hostname,
            )
            try:
                await abandon_node(
                    node,
                    self._clouds,
                    self._uow_factory,
                    self._tracker,
                )
            except Exception as abandon_err:
                logger.debug(
                    "ABANDON_FAILED",
                    extra={
                        "node_id": node.node_id,
                        "hostname": node.hostname,
                        "err": abandon_err,
                    },
                )
                logger.exception(
                    "abandon_node failed for node %s",
                    node.hostname,
                )
            self._connect_failures.pop(node.node_id, None)
            # endregion BLOCK_connect_abandon
        except Exception:
            logger.exception("An error occurred on remote machine creation")

    # endregion METHOD_connect_machine_consumer

    # region METHOD__connect_grace_for
    # PURPOSE: Resolve the per-cloud connect_grace seconds for a node so the connect consumer can decide retry-vs-abandon without each call site re-deriving the prefix lookup and default.
    def _connect_grace_for(self, cloud: str | None) -> int:
        if cloud is None:
            return 120
        for cfg in self._config_clouds:
            if cfg.prefix == cloud:
                return cfg.connect_grace
        return 120

    # endregion METHOD__connect_grace_for

    # region METHOD__allocator_producer
    # PURPOSE: Bound the next allocation wave to the larger of remaining cloud capacity, current free-machine count, or the configurable allocate_wave_min floor (LocalSettings, default 10) so the allocate queue never starves when capacity exists and never floods when it does not.
    # INVARIANTS: The wave is a fixed id-ordered window over TO_DO tasks. Operators must keep allocate_wave_min above the longest run of *unallocatable* tasks that can accumulate at the TO_DO head (unsupported engine, engine platform with no matching machine, per-engine exclude_nodes): such tasks never leave TO_DO, so an all-unallocatable window permanently blocks every allocatable task behind it — the queue starves while compute idles.
    async def _allocator_producer(
        self,
    ) -> AsyncGenerator[UMessage[TaskId, TodoTask], None]:
        ccap = await self._clouds_get_capacity()
        tlim = max(
            ccap,
            len(self._repository.list_free(None)),
            self._local_settings.allocate_wave_min,
        )
        async with self._uow_factory() as uow:
            tasks = await uow.tasks.list_todo(limit=tlim)
        if tasks:
            ids = [str(t.task_id) for t in tasks]
            logger.debug("ALLOCATOR_PRODUCER", extra={"task_ids": ", ".join(ids)})
        for task in tasks:
            yield UMessage(task.task_id, task)

    # endregion METHOD__allocator_producer

    # region METHOD_allocator_consumer
    # PURPOSE: Fire-and-forget task allocation — keep the worker alive even if one task fails so remaining tasks in the queue still get their allocation attempt.
    async def _allocator_consumer(self, msg: UMessage[TaskId, TodoTask]) -> None:
        # region BLOCK_allocate
        logger.debug("ALLOCATE", extra={"task_id": msg.id})
        try:
            await allocate_task(
                task_id=msg.id,
                engines=self._engines,
                uow_factory=self._uow_factory,
                repository=self._repository,
                occupancy_checker=self._occupancy_checker,
                clouds=self._clouds,
                start_task_on_machine=self._start_task_on_machine,
                tracker=self._tracker,
                allocation_lock=self._allocation_lock,
                remote_tasks_dir=self._remote_defaults.tasks_dir,
            )
        except Exception:
            logger.exception("Allocator error for task %s", msg.id)
        # endregion BLOCK_allocate

    # endregion METHOD_allocator_consumer

    # region METHOD_task_consumer_producer
    # PURPOSE: Yield RUNNING tasks whose id is not already in-flight so no task is concurrently consumed by two workers across overlapping producer cycles.
    # REQUIRES: self._consuming reflects the current set of in-flight consume task ids (same-event-loop access, no await between read and skip decision); self._uow_factory yields a UoW that lists RUNNING tasks from the DB.
    # ENSURES: Every yielded msg has msg.id NOT in self._consuming.
    # INVARIANTS: self._consuming is mutated only in _task_consumer_consumer (add before await consume_task, remove in finally).
    async def _task_consumer_producer(
        self,
    ) -> AsyncGenerator[UMessage[TaskId, RunningTask], None]:
        async with self._uow_factory() as uow:
            tasks = await uow.tasks.list_running()
        # region BLOCK_skip_in_flight
        # Skip tasks currently being consumed by another worker to prevent
        # two workers from concurrently consuming the same RUNNING task across
        # overlapping producer cycles (same event loop → atomic check).
        for task in tasks:
            if task.task_id in self._consuming:
                continue
            yield UMessage(task.task_id, task)
        # endregion BLOCK_skip_in_flight

    # endregion METHOD_task_consumer_producer

    # region METHOD_task_consumer_consumer
    # PURPOSE: Detect when a RUNNING task's machine is gone (evict after N cycles) or download outputs from a free machine, preventing phantom allocations and keeping the pipeline moving.
    # REQUIRES: msg.id NOT in self._consuming at entry (enforced by producer skip); self._consuming and self._occupancy_started are same-event-loop sets (atomic add/remove without await); msg.payload.allocated_node_id resolved via repository.get_session once at the top.
    # ENSURES: Guards the task id in self._consuming around the consume_task await; node_id from _occupancy_started discarded only when consume_task returns True (finalised).
    # INVARIANTS: self._consuming add/remove follows try/finally — added before await consume_task, removed in finally so a failed consume does not permanently block re-yield; self._occupancy_started entry for node_id added on first occupancy check tick, discarded only when consume_task returns True; machine-gone path (session is None) does not touch _consuming or _occupancy_started.
    async def _task_consumer_consumer(
        self,
        msg: UMessage[TaskId, RunningTask],
        machine_not_found: Counter,
    ) -> None:
        broken_tasks_passes = 20
        task_id, task = msg.id, msg.payload
        node_id = task.state.allocated_node_id
        session = self._repository.get_session(node_id)
        if session is None:
            # region BLOCK_machine_gone
            logger.warning("machine gone for task_id=%s node_id=%s", task_id, node_id)
            machine_not_found.update([task_id])
            if machine_not_found[task_id] > broken_tasks_passes:
                # Single atomic transition: RUNNING→DONE with error; emits
                # TaskAbandoned unconditionally (a RUNNING task always carries
                # an allocation).
                task = task.abandon()
                # region BLOCK_abandon_persist
                # Persist the TaskAbandoned transition. The row may have been
                # concurrently deleted between the producer's list_by_status
                # read and this save — in that case save() raises
                # TaskRowNotFoundError, which propagates to the consumer-worker
                # `except Exception` wrap and is logged. The tracker slot is
                # discarded in a finally so a 0-row raise cannot leak an
                # in-flight allocation slot for the daemon's lifetime.
                try:
                    async with self._uow_factory() as uow:
                        await uow.tasks.save(task)
                        await uow.commit()
                finally:
                    # Drop the in-flight allocation slot so the abandoned
                    # task_id can't falsely dedup a future task if task IDs
                    # ever recycle. Runs even if save() raised.
                    self._tracker.discard(task_id)
                # endregion BLOCK_abandon_persist
            # endregion BLOCK_machine_gone
            return

        machine = session.machine

        # session.machine.node_id is the authoritative non-optional NodeId key
        # (matches task.allocated_node_id for a correctly-bound RUNNING task).
        key = machine.node_id
        if key not in self._occupancy_started:
            engine = self._engines.get(task.engine)
            if engine:
                self._occupancy_checker.start_occupancy_check(session, engine)
                self._occupancy_started.add(key)
                session = self._repository.get_session(key)
                if session is None:
                    return
                machine = session.machine

        # region BLOCK_consume
        if machine.state == MachineState.FREE and key in self._occupancy_started:
            logger.debug("CONSUME", extra={"node_id": key, "task_id": task_id})
            # Guard the task id so the next producer cycle does not re-yield it
            # to another worker while this consume is in flight.
            self._consuming.add(task_id)
            try:
                finalised = await consume_task(
                    task_id=task_id,
                    session=session,
                    output_downloader=self._output_downloader,
                    engines=self._engines,
                    uow_factory=self._uow_factory,
                    local_tasks_dir=self._local_tasks_dir,
                    tracker=self._tracker,
                )
            finally:
                self._consuming.discard(task_id)
            # Discard the node_id from _occupancy_started only when finalised
            # so a deferred (transient-only) task is re-consumed on the next
            # cycle.
            if finalised:
                self._occupancy_started.discard(key)
        # endregion BLOCK_consume

    # endregion METHOD_task_consumer_consumer

    # ---- Deallocator producer-consumer ----

    # region METHOD_deallocator_producer
    # PURPOSE: Recover cloud capacity by marking idle nodes as disabled so the deallocation consumer can delete their VMs and stop billing.
    async def _deallocator_producer(
        self,
    ) -> AsyncGenerator[UMessage[NodeId, Node], None]:
        # region BLOCK_collect_idle
        # free_since is monotonic; pass it through unchanged so deallocate_nodes
        # compares against time.monotonic() and stays immune to wall-clock jumps.
        # Keyed by NodeId (was ip) — dup-IP nodes have distinct node_id keys.
        idle_machines: dict[NodeId, float] = {}
        for s in self._repository.list_connected():
            if (
                s.machine.state == MachineState.FREE
                and s.machine.free_since is not None
            ):
                idle_machines[s.machine.node_id] = s.machine.free_since
        # endregion BLOCK_collect_idle

        # region BLOCK_deallocate_use_case
        disabled_nodes = await deallocate_nodes(
            self._uow_factory,
            self._config_clouds,
            idle_machines,
        )
        # endregion BLOCK_deallocate_use_case

        # region BLOCK_yield_disabled
        for node in disabled_nodes:
            yield UMessage(node.node_id, node)
        # endregion BLOCK_yield_disabled

    # endregion METHOD_deallocator_producer

    # region METHOD_deallocator_consumer
    # PURPOSE: Fire-and-forget VM deletion — keep the worker alive if one VM delete fails so remaining disabled nodes still get deallocated.
    async def _deallocator_consumer(self, msg: UMessage[NodeId, Node]) -> None:
        node = msg.payload
        try:
            await deallocate_node(
                node,
                self._repository,
                self._clouds,
                self._uow_factory,
            )
        except Exception:
            logger.exception(
                "Deallocator error for node_id=%s hostname=%s",
                node.node_id,
                node.hostname,
            )

    # endregion METHOD_deallocator_consumer

    # ---- Infrastructure ----

    # region METHOD_clouds_get_capacity
    # PURPOSE: Limit cloud provisioning to the configured max_nodes ceiling so the daemon does not over-provision beyond operator-defined limits.
    async def _clouds_get_capacity(self) -> int:
        # region BLOCK_read_counts
        async with self._uow_factory() as uow:
            nodes = await uow.nodes.list_all()
        counts = _count_nodes_by_cloud(nodes)
        # endregion BLOCK_read_counts

        # region BLOCK_compute_capacity
        max_nodes = sum(c.max_nodes for c in self._active_clouds)
        current = sum(counts.get(c.prefix, 0) for c in self._active_clouds)
        diff = max_nodes - current
        return max(0, diff)
        # endregion BLOCK_compute_capacity

    # endregion METHOD_clouds_get_capacity

    # region METHOD_create_producer_consumers
    # PURPOSE: Keep scheduling phases alive despite transient errors — self-heal on producer/consumer failures and gracefully drain work on shutdown.
    async def _create_producer_consumers(
        self,
        queue: UniqueQueue,
        producer: Callable,
        consumer: Callable,
        workers_num: int = 1,
    ) -> None:
        async def worker() -> None:
            while not self._cancellation_event.is_set():
                msg = await queue.get()
                # region BLOCK_consumer_resilience
                # Mirror the producer-error wrap: a raise out of consumer(msg)
                # would otherwise kill the worker task silently. Catch Exception
                # (not BaseException) so CancelledError still propagates to the
                # graceful-drain path. The finally keeps the queue item dequeued.
                try:
                    await consumer(msg)
                except Exception as err:
                    logger.debug(
                        "CONSUMER_ERROR",
                        extra={"queue": queue.name, "err": err},
                    )
                    logger.exception("consumer error on queue %s", queue.name)
                finally:
                    queue.item_done(msg)
                # endregion BLOCK_consumer_resilience

        workers: set[asyncio.Task] = set()
        # region BLOCK_register_workers
        # Workers are background jobs for the daemon's lifetime — register them
        # in self._bg_jobs so stop()'s cancel cascade reaches them even if the
        # parent exits via a BaseException (SystemExit/KeyboardInterrupt) that
        # `except Exception` does not catch. The local `workers` set is kept
        # for the `except CancelledError` drain path; double-cancel is idempotent.
        for _ in range(workers_num):
            t = asyncio.create_task(worker())
            workers.add(t)
            self._bg_jobs.add(t)
        # endregion BLOCK_register_workers

        try:
            while not self._cancellation_event.is_set():
                end_time = datetime.now(timezone.utc) + timedelta(
                    seconds=self._sleep_interval,
                )
                # region BLOCK_producer_resilience
                try:
                    async for msg in producer():
                        await queue.put(msg)
                # CancelledError is a BaseException (not Exception) since Python
                # 3.8 — do NOT broaden this to `except BaseException` or graceful
                # shutdown will be swallowed and the drain below bypassed.
                except Exception as err:
                    logger.debug(
                        "PRODUCER_ERROR",
                        extra={"queue": queue.name, "err": err},
                    )
                    logger.exception("producer error on queue %s", queue.name)
                finally:
                    await _asleep_until(end_time)
                # endregion BLOCK_producer_resilience
        except asyncio.CancelledError:
            if not queue.empty():
                logger.info(
                    "Queue %s has %s items - waiting",
                    queue.name,
                    queue.qsize(),
                )
                # Workers may already be cancelled by stop()'s cascade through
                # self._bg_jobs (unordered set); without a consumer the unbounded
                # join hangs until SIGKILL. Bound it and drop residue on timeout.
                try:
                    await asyncio.wait_for(queue.join(), _DRAIN_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning(
                        "queue %s drain timed out after %ss, dropped %s items",
                        queue.name,
                        _DRAIN_TIMEOUT,
                        queue.qsize(),
                    )
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    # endregion METHOD_create_producer_consumers

    # ---- Lifecycle ----

    # region METHOD_await_first_machine
    # PURPOSE: Prevent the allocation loop from spinning on an empty machine pool before the first connect cycle completes.
    async def _await_first_machine(self) -> None:
        # region BLOCK_wait_machines
        if len(self._repository) > 0:
            return

        async def _wait() -> None:
            await self._machine_connected_event.wait()

        wait_task = asyncio.create_task(_wait())
        timeout_task = asyncio.create_task(asyncio.sleep(30))
        _done, pending = await asyncio.wait(
            [wait_task, timeout_task],
            return_when="FIRST_COMPLETED",
        )
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        # endregion BLOCK_wait_machines

    # endregion METHOD_await_first_machine

    async def _shutdown_barrier(self) -> None:
        await asyncio.gather(*self._bg_jobs, return_exceptions=True)

    # region METHOD_start
    # PURPOSE: Launch the daemon's four scheduling loops and block until shutdown, so the process lives as long as the scheduler needs to run.
    async def start(self) -> None:
        """Start all producer-consumer loops for the daemon."""
        logger.debug(
            "START",
            extra={"engines": [e.name for e in self._engines.values()]},
        )

        self._bg_jobs.add(asyncio.create_task(self._print_stats()))

        conn_machine_co = self._create_producer_consumers(
            queue=self._conn_machine_q,
            producer=self._connect_machine_producer,
            consumer=self._connect_machine_consumer,
            workers_num=self._local_settings.conn_machine_limit,
        )
        self._bg_jobs.add(asyncio.create_task(conn_machine_co))

        await self._await_first_machine()

        allocate_co = self._create_producer_consumers(
            queue=self._allocate_q,
            producer=self._allocator_producer,
            consumer=self._allocator_consumer,
            workers_num=self._local_settings.allocate_limit,
        )
        self._bg_jobs.add(asyncio.create_task(allocate_co))

        machine_not_found: Counter[TaskId] = Counter()
        consume_co = self._create_producer_consumers(
            queue=self._consume_q,
            producer=self._task_consumer_producer,
            consumer=partial(
                self._task_consumer_consumer,
                machine_not_found=machine_not_found,
            ),
            workers_num=self._local_settings.consume_limit,
        )
        self._bg_jobs.add(asyncio.create_task(consume_co))

        deallocate_co = self._create_producer_consumers(
            queue=self._deallocate_q,
            producer=self._deallocator_producer,
            consumer=self._deallocator_consumer,
            workers_num=self._local_settings.deallocate_limit,
        )
        self._bg_jobs.add(asyncio.create_task(deallocate_co))

        await self._shutdown_barrier()

    # endregion METHOD_start

    # region METHOD_stop
    # PURPOSE: Shut down gracefully — drain in-flight work, tear down cloud resources, and close connections — so no tasks are orphaned and no cloud VMs leak.
    async def stop(self) -> None:
        """Signal the daemon to stop and clean up non-session resources."""
        # region BLOCK_stop_guard
        if self._stopped:
            return
        self._stopped = True
        # endregion BLOCK_stop_guard
        logger.info("Stopping...")
        self._cancellation_event.set()

        # region BLOCK_stop_await_jobs
        for task in self._bg_jobs:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # CancelledError is BaseException (not Exception) since Py3.8, so
            # the two except clauses are distinct and non-overlapping. Catches
            # a bg job that already died with a non-CancelledError before
            # shutdown so the pre-existing exception does not abort cleanup.
            except Exception as e:
                logger.debug("BG_JOB_ENDED", extra={"err": e})
        # endregion BLOCK_stop_await_jobs

        # region BLOCK_stop_clouds
        try:
            await self._clouds.stop()
        except Exception as e:
            logger.warning("clouds stop failed: %s", e)
        # endregion BLOCK_stop_clouds

        # region BLOCK_stop_gateway
        try:
            await self._repository.disconnect_all()
        except Exception as e:
            logger.warning("disconnect all failed: %s", e)
        # endregion BLOCK_stop_gateway

        # region BLOCK_stop_http
        if self._http_session is not None:
            try:
                await self._http_session.close()
            except Exception as e:
                logger.warning("http close failed: %s", e)
        self._http_session = None
        # endregion BLOCK_stop_http

    # endregion METHOD_stop


# endregion CLASS_Orchestrator
