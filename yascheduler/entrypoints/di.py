"""Dependency injection composition root — factories per entry point (daemon, CLI)."""
# region MODULE_CONTRACT
# PURPOSE: Hand each entry point only the collaborators its use cases need, so the daemon wire-up stays separate from the CLI wire-up and the entry point owns lifecycle alone.
# RATIONALE:
# - Q: why does the composition root live in the entrypoints layer rather than in its own dedicated top-level package or in the application layer?
#   A: hexagonal architecture places the composition root in the outermost layer (the driving-adapter layer) alongside the other entry points; the entrypoints layer is layer 1 in the import-linter layers contract, so its imports flow entrypoints → infra → application → domain — which is layer-legal. Placing the root here keeps wiring next to the entry points that consume it and avoids inventing a sixth architectural layer outside the contract.
# SCOPE: Daemon orchestrator factory (make_daemon), CLI dependency container (CLIDeps + make_cli_deps), and domain event bus setup with webhook registration.
# KEYWORDS: di, composition-root, factories, daemon, cli, dependency-injection
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

import aiohttp

from yascheduler.application import (
    AbstractUnitOfWork,
    AllocationTracker,
    MessageBus,
    Orchestrator,
    normalize_task_inputs,
    submit_task,
)
from yascheduler.domain import (
    TaskAbandoned,
    TaskAllocated,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
    TaskId,
    TaskInputEntry,
)
from yascheduler.infra import (
    CloudAdapter,
    CloudProvisionerImpl,
    OccupancyChecker,
    OutputDownloader,
    PostgresUnitOfWork,
    SSHMachineRepository,
    TaskDeployer,
    list_private_keys,
    resolve_adapter,
    webhook_handler,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from yascheduler.domain import EngineRepository
    from yascheduler.infra.cloud import ConfigCloud

    from .config import Config

__all__ = ["CLIDeps", "make_cli_deps", "make_daemon"]
logger = logging.getLogger(__name__)


# region CLASS_CLIDeps
# PURPOSE: Carry the minimum collaborators a CLI command needs (engine registry + UoW factory) so a CLI invocation can submit or query tasks without paying the daemon's SSH/cloud/event-bus wire-up cost.
# RATIONALE:
#   Q: Why does CLIDeps bundle a uow_factory callable instead of a ready AbstractUnitOfWork instance?
#   A: Each CLI query needs a fresh UoW so its transaction boundary is its own; a callable lets the facade open + close the UoW per call without leaking a handle across calls.
@dataclass
class CLIDeps:
    """Lightweight dependency container for CLI submit operations."""

    engines: EngineRepository
    uow_factory: Callable[[], AbstractUnitOfWork]

    # region METHOD_submit
    # PURPOSE: Normalize the submission's input source (legacy metadata or explicit entries) exactly once, forward the typed snapshot and optional output selections to submit_task, so the use case never sees legacy encodings and unknown engines still fail inside the use case before any write.
    # RATIONALE:
    #   Q: Why does the DI container own normalization instead of each entry point?
    #   A: Every public submission path (CLI command, sync/async client) funnels through CLIDeps.submit — placing the seam here normalizes once per submission regardless of caller, which the design requires ("normalize legacy values once before the application use case").
    async def submit(
        self,
        label: str,
        metadata: dict[str, object],
        engine_name: str,
        *,
        input_entries: Sequence[TaskInputEntry] | None = None,
        output_selections: list[str] | None = None,
    ) -> TaskId:
        """Submit a new task via the submit_task use case."""
        engine = self.engines.get(engine_name)
        # An unknown engine needs no declarations, so it skips normalization;
        # the use case raises UnsupportedEngineError before any write.
        input_snapshot, remaining_metadata = (
            ((), metadata)
            if engine is None
            else normalize_task_inputs(metadata, engine, input_entries)
        )
        return await submit_task(
            label,
            remaining_metadata,
            engine_name,
            self.engines,
            self.uow_factory,
            input_snapshot=input_snapshot,
            output_selections=output_selections,
        )

    # endregion METHOD_submit


# endregion CLASS_CLIDeps


# region FUNC__setup_domain_events
# PURPOSE: Stand up the daemon's event-bus backbone so domain events emitted by the use-case layer are delivered to operator-configured webhooks without each use case re-wiring its own bus.
# RATIONALE:
#   Q: Why is the webhook handler bound to one shared aiohttp.ClientSession instead of one session per webhook delivery?
#   A: An aiohttp.ClientSession owns a connection pool; reusing one across all webhook deliveries keeps the daemon's outbound connection count bounded and lets the pool keep-alive across events.
def _setup_domain_events() -> tuple[MessageBus, aiohttp.ClientSession]:
    bus = MessageBus()
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    for event_type in (
        TaskCreated,
        TaskAllocated,
        TaskCompleted,
        TaskFailed,
        TaskAbandoned,
    ):
        bus.register(event_type, partial(webhook_handler, http=http))
    return bus, http


# endregion FUNC__setup_domain_events


# region FUNC_make_daemon
# PURPOSE: Hand the daemon entry point one ready Orchestrator wired with UoW + SSH + cloud + event bus + collaborators so run_daemon owns only the start/stop lifecycle and the wire-up boundary stays in one place.
async def make_daemon(
    config: Config,
    *,
    clouds: CloudProvisionerImpl | None = None,
) -> Orchestrator:
    """Async factory creating Orchestrator with all daemon dependencies."""
    bus, http = _setup_domain_events()
    try:

        def uow_factory() -> AbstractUnitOfWork:
            return PostgresUnitOfWork(config.db, bus)

        allocation_tracker = AllocationTracker()
        allocation_lock = asyncio.Lock()

        # Single SSHMachineRepository for the production path (clouds is None):
        # shared between CloudProvisionerImpl (machine_repository only — setup
        # calls session pass-throughs directly) and Orchestrator (repository)
        # so _setup_vm connections are visible to the orchestrator (no
        # double-connect) and reaped at shutdown. The three stateless
        # collaborators (TaskDeployer/OutputDownloader/OccupancyChecker) are
        # constructed once each and passed to the orchestrator only.
        # The pre-built-clouds branch still wires a fresh repository to the
        # orchestrator; caller-supplied clouds keep their own.
        repository = SSHMachineRepository()
        task_deployer = TaskDeployer()
        output_downloader = OutputDownloader()
        occupancy_checker = OccupancyChecker()

        if clouds is None:
            active_clouds: list[ConfigCloud] = []
            _adapters: dict[str, CloudAdapter] = {}
            _configs: dict[str, ConfigCloud] = {}
            for cfg in config.clouds:
                if cfg.max_nodes <= 0:
                    logger.warning(
                        "Cloud %s skipped: max_nodes=%d <= 0",
                        cfg.prefix,
                        cfg.max_nodes,
                    )
                    continue
                adapter = resolve_adapter(cfg)
                if adapter is None:
                    continue
                _adapters[adapter.name] = adapter
                _configs[adapter.name] = cfg
                active_clouds.append(cfg)

            logger.info("Active cloud APIs: %s", ", ".join(_adapters.keys()) or "-")
            clouds = CloudProvisionerImpl(
                adapters=_adapters,
                configs=_configs,
                machine_repository=repository,
                local_config=config.local,
                remote_config=config.remote,
                engines=config.engines,
            )
        else:
            # Caller-supplied clouds: filter by max_nodes > 0 AND adapter
            # actually resolved. configs is keyed by adapter.name == cfg.prefix for
            # every successfully resolved cloud, so its keys are the resolved
            # set. Without this, a pre-built-clouds caller would over-count
            # max_nodes in _clouds_get_capacity for any provider whose
            # optional deps are missing.
            resolved_prefixes = set(clouds.configs.keys())
            active_clouds = [
                cfg
                for cfg in config.clouds
                if cfg.max_nodes > 0 and cfg.prefix in resolved_prefixes
            ]

        # The concrete ConfigCloud* DTOs explicitly inherit the domain
        # CloudConfig Protocol (D1), so list[ConfigCloud] is assignable to
        # Sequence[CloudConfig] (covariance + inheritance) without a cast.
        return Orchestrator(
            local_settings=config.local,
            remote_defaults=config.remote,
            uow_factory=uow_factory,
            clouds=clouds,
            repository=repository,
            task_deployer=task_deployer,
            output_downloader=output_downloader,
            occupancy_checker=occupancy_checker,
            engines=config.engines,
            config_clouds=config.clouds,
            local_tasks_dir=config.local.tasks_dir,
            http_session=http,
            allocation_tracker=allocation_tracker,
            active_clouds=active_clouds,
            allocation_lock=allocation_lock,
            list_private_keys_fn=list_private_keys,
        )
    except Exception:
        await http.close()
        raise


# endregion FUNC_make_daemon


# region FUNC_make_cli_deps
# PURPOSE: Build a CLIDeps with only the engine registry and a UoW factory so a CLI invocation pays neither the SSH-pool nor the cloud-adapter nor the event-bus wire-up cost.
def make_cli_deps(config: Config) -> CLIDeps:
    """Sync factory creating lightweight CLIDeps for CLI commands (no SSH/cloud)."""
    bus = MessageBus()

    def _uow_factory() -> AbstractUnitOfWork:
        return PostgresUnitOfWork(config.db, bus)

    return CLIDeps(
        engines=config.engines,
        uow_factory=_uow_factory,
    )


# endregion FUNC_make_cli_deps
