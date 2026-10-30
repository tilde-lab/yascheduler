"""Unit tests for Orchestrator._connect_machine_consumer grace timer + abandon dispatch.

Covers the in-memory per-IP connect-failure timer introduced by the
fix-never-connected-node-leak change:

- Within grace → log + retry (no abandon call, IP stays in timer)
- Past grace → abandon_node called, IP popped from timer
- Successful connect pops the IP from the timer
- Unknown cloud falls back to 120s grace
- Abandon failure is caught (worker survives)
- Fresh Orchestrator starts with an empty timer (daemon-restart reset)
- Producer yields static (cloud=None) nodes; static node failures retry
  without abandon (consumer-side guard before the grace-check)
"""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for Orchestrator._connect_machine_consumer never-connected-node grace timer + abandon dispatch.
# SCOPE: Failure-within-grace retries, failure-past-grace abandons, success resets timer, unknown-cloud fallback, abandon-failed isolation, daemon-restart reset, _connect_grace_for pure helper, producer yields static nodes (cloud is None); static nodes retried without abandon.
# KEYWORDS: _connect_machine_consumer, grace timer, abandon dispatch
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.log_assertions import extra_fields
from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.orchestrator import Orchestrator
from yascheduler.application.queue import UMessage
from yascheduler.domain import Engine, EngineRepository, LocalSettings, RemoteDefaults
from yascheduler.domain.exceptions import MachineConnectionError
from yascheduler.domain.model import Node, NodeId
from yascheduler.entrypoints import Config
from yascheduler.infra.cloud.cloud_configs import (
    ConfigCloudAzure,
    ConfigCloudHetzner,
)

if TYPE_CHECKING:
    from yascheduler.application.uow import AbstractUnitOfWork


def _make_node(hostname: str = "10.0.0.5", cloud: str | None = "hetzner") -> Node:
    return Node(
        node_id=NodeId(1),
        hostname=hostname,
        ncpus=2,
        cloud=cloud,
        username="root",
        port=22,
        enabled=True,
    )


def make_orchestrator(
    config_clouds: list | None = None,
    allocation_tracker: AllocationTracker | None = None,
) -> Orchestrator:
    """Build a real Orchestrator with mocked externals (no I/O)."""
    local = MagicMock(spec=LocalSettings)
    local.conn_machine_pending = 10
    local.allocate_pending = 5
    local.consume_pending = 3
    local.deallocate_pending = 2
    local.conn_machine_limit = 1
    local.allocate_limit = 1
    local.consume_limit = 1
    local.deallocate_limit = 1
    local.keys_dir = Path("/tmp/keys")

    remote = MagicMock(spec=RemoteDefaults)
    remote.tasks_dir = PurePosixPath("/tmp/tasks")
    remote.data_dir = PurePosixPath("/tmp/data")
    remote.engines_dir = PurePosixPath("/tmp/engines")
    remote.username = "root"
    remote.jump_host = None
    remote.jump_username = None

    cfg = MagicMock(spec=Config)
    cfg.local = local
    cfg.remote = remote

    mock_uow = AsyncMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

    def uow_factory() -> AbstractUnitOfWork:
        return mock_uow

    repository = MagicMock()
    repository.__len__ = MagicMock(return_value=0)
    task_deployer = MagicMock()
    output_downloader = MagicMock()
    occupancy_checker = MagicMock()

    engine = MagicMock(spec=Engine, sleep_interval=0)
    engine.name = "test_engine"
    engines = MagicMock(spec=EngineRepository)
    engines.values.return_value = [engine]

    clouds = AsyncMock()

    if allocation_tracker is None:
        allocation_tracker = AllocationTracker()
    if config_clouds is None:
        config_clouds = []

    return Orchestrator(
        local_settings=local,
        remote_defaults=remote,
        uow_factory=uow_factory,
        clouds=clouds,
        repository=repository,
        task_deployer=task_deployer,
        output_downloader=output_downloader,
        occupancy_checker=occupancy_checker,
        engines=engines,
        config_clouds=config_clouds,
        local_tasks_dir=Path("/tmp"),
        allocation_tracker=allocation_tracker,
        active_clouds=[],
        allocation_lock=asyncio.Lock(),
        list_private_keys_fn=lambda _keys_dir: [],
    )


class TestConnectMachineConsumerGraceTimer:
    """Grace-timer behavior in _connect_machine_consumer."""

    @pytest.mark.asyncio
    async def test_connect_failure_within_grace_retries_without_abandoning(
        self,
    ) -> None:
        """Within grace: gateway.connect raises, age < grace → no abandon, IP stays in timer."""
        cfg_cloud = MagicMock(prefix="hetzner", connect_grace=60)
        orch = make_orchestrator(config_clouds=[cfg_cloud])
        orch._repository.connect = AsyncMock(  # type: ignore[method-assign]
            side_effect=MachineConnectionError(NodeId(1), "10.0.0.5", "refused"),
        )

        # Constant return_value (never exhausts): on first failure line 309
        # records first_seen=110.0 and line 310 computes age=0 < grace=60s →
        # retry path. A list side_effect would drain under Python 3.12, whose
        # asyncio calls time.monotonic during the await chain before the
        # consumer reaches the grace-check (StopIteration → RuntimeError).
        with (
            patch(
                "yascheduler.application.orchestrator.time.monotonic",
                return_value=110.0,
            ),
            patch(
                "yascheduler.application.orchestrator.abandon_node",
                new=AsyncMock(),
            ) as mock_abandon,
        ):
            await orch._connect_machine_consumer(UMessage(NodeId(1), _make_node()))

        mock_abandon.assert_not_called()
        # IP stays in the timer (retry path)
        assert NodeId(1) in orch._connect_failures

    @pytest.mark.asyncio
    async def test_connect_failure_past_grace_triggers_abandon(self) -> None:
        """Past grace: gateway.connect raises, age >= grace → abandon_node called, IP popped."""
        cfg_cloud = MagicMock(prefix="hetzner", connect_grace=60)
        orch = make_orchestrator(config_clouds=[cfg_cloud])
        orch._repository.connect = AsyncMock(  # type: ignore[method-assign]
            side_effect=MachineConnectionError(NodeId(1), "10.0.0.5", "refused"),
        )

        # Pre-seed first_seen so age = 165 - 100 = 65s >= 60s grace → abandon.
        orch._connect_failures[NodeId(1)] = 100.0
        with (
            patch(
                "yascheduler.application.orchestrator.time.monotonic",
                return_value=165.0,
            ),
            patch(
                "yascheduler.application.orchestrator.abandon_node",
                new=AsyncMock(),
            ) as mock_abandon,
        ):
            await orch._connect_machine_consumer(UMessage(NodeId(1), _make_node()))

        mock_abandon.assert_awaited_once()
        # abandon_node called with (node, clouds, uow_factory, tracker) — gateway dropped
        args = mock_abandon.call_args.args
        assert args[1] is orch._clouds
        assert args[3] is orch._tracker
        # IP popped after abandon
        assert NodeId(1) not in orch._connect_failures

    @pytest.mark.asyncio
    async def test_successful_connect_resets_failure_timer(self) -> None:
        """First call records first_seen; second call (success) pops the IP."""
        cfg_cloud = MagicMock(prefix="hetzner", connect_grace=60)
        orch = make_orchestrator(config_clouds=[cfg_cloud])
        orch._repository.connect = AsyncMock(  # type: ignore[method-assign]
            side_effect=MachineConnectionError(NodeId(1), "10.0.0.5", "x"),
        )

        # First call: failure within grace → IP recorded (constant monotonic →
        # age=0 < 60s → retry path, IP entered into the timer).
        with (
            patch(
                "yascheduler.application.orchestrator.time.monotonic",
                return_value=105.0,
            ),
            patch("yascheduler.application.orchestrator.abandon_node", new=AsyncMock()),
        ):
            await orch._connect_machine_consumer(UMessage(NodeId(1), _make_node()))
        assert NodeId(1) in orch._connect_failures

        # Second call: success → IP popped.
        orch._repository.connect = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]  # success
        await orch._connect_machine_consumer(UMessage(NodeId(1), _make_node()))
        assert NodeId(1) not in orch._connect_failures

    @pytest.mark.asyncio
    async def test_abandon_failed_does_not_kill_worker(self) -> None:
        """When abandon_node raises, the consumer catches it and returns without propagating."""
        cfg_cloud = MagicMock(prefix="hetzner", connect_grace=60)
        orch = make_orchestrator(config_clouds=[cfg_cloud])
        orch._repository.connect = AsyncMock(  # type: ignore[method-assign]
            side_effect=MachineConnectionError(NodeId(1), "10.0.0.5", "x"),
        )

        # Pre-seed first_seen so age = 200 - 100 = 100s >= 60s → abandon path
        # (abandon_node raises and is caught by the consumer).
        orch._connect_failures[NodeId(1)] = 100.0
        with (
            patch(
                "yascheduler.application.orchestrator.time.monotonic",
                return_value=200.0,
            ),
            patch(
                "yascheduler.application.orchestrator.abandon_node",
                new=AsyncMock(side_effect=RuntimeError("abandon failed")),
            ) as mock_abandon,
        ):
            # Must NOT raise — consumer swallows to keep the worker alive.
            await orch._connect_machine_consumer(UMessage(NodeId(1), _make_node()))

        mock_abandon.assert_awaited_once()
        # IP is still popped after a failed abandon (no infinite loop on next cycle)
        assert NodeId(1) not in orch._connect_failures


class TestConnectGraceFor:
    """_connect_grace_for: per-cloud lookup with conservative fallback."""

    def test_known_cloud_returns_dto_default(self) -> None:
        """Hetzner config → connect_grace=60."""
        orch = make_orchestrator(config_clouds=[ConfigCloudHetzner(token="test-token")])
        assert orch._connect_grace_for("hetzner") == 60

    def test_azure_returns_120(self) -> None:
        """Azure (prefix 'az') → connect_grace=120."""
        orch = make_orchestrator(
            config_clouds=[
                ConfigCloudHetzner(token="test-token"),
                ConfigCloudAzure(
                    tenant_id="test-tid",
                    client_id="test-cid",
                    client_secret="test-secret",
                    subscription_id="test-sub",
                ),
            ],
        )
        assert orch._connect_grace_for("az") == 120

    def test_unknown_cloud_falls_back_to_120s_grace(self) -> None:
        """cloud='unknown' (no matching prefix) → 120."""
        orch = make_orchestrator(config_clouds=[ConfigCloudHetzner(token="test-token")])
        assert orch._connect_grace_for("unknown") == 120

    def test_none_cloud_falls_back_to_120s_grace(self) -> None:
        """cloud=None → 120."""
        orch = make_orchestrator(config_clouds=[ConfigCloudHetzner(token="test-token")])
        assert orch._connect_grace_for(None) == 120

    def test_empty_config_clouds_falls_back_to_120(self) -> None:
        """No clouds configured → 120."""
        orch = make_orchestrator(config_clouds=[])
        assert orch._connect_grace_for("hetzner") == 120


class TestDaemonRestartResetsFailureTimers:
    """Fresh Orchestrator starts with an empty _connect_failures dict."""

    def test_daemon_restart_resets_failure_timers(self) -> None:
        """A newly-constructed Orchestrator has _connect_failures == {}.

        Enforces the in-memory-only contract from the orchestrator spec's
        "Daemon restart resets failure timers" scenario.
        """
        orch = make_orchestrator()
        assert orch._connect_failures == {}


class TestConnectMachineProducerYieldsStaticNodes:
    """The connect-machine producer yields static (cloud=None) nodes; static
    node failures are retried without abandon.

    The v6.2.1 producer filter (`n.cloud is not None`) was over-broad — it
    excluded static nodes from the connect path entirely, breaking the
    yasetnode → daemon handoff (tasks stuck in TO_DO). The filter is removed;
    a consumer-side guard before the grace-check now retries static nodes
    indefinitely without ever calling abandon_node, preserving the intent
    (static nodes are never auto-removed) with a narrower mechanism.
    """

    @pytest.mark.asyncio
    async def test_static_node_yielded_to_consumer(self) -> None:
        """cloud=None enabled node not in gateway → producer YIELDS it.

        Drives the producer to completion and collects every yielded message's
        IP. A static node MUST appear (the daemon connects it), alongside the
        cloud node.
        """
        from yascheduler.domain.model import Node

        static_node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.9",
            ncpus=2,
            cloud=None,
            username="root",
            port=22,
            enabled=True,
        )
        cloud_node = Node(
            node_id=NodeId(2),
            hostname="10.0.0.10",
            ncpus=2,
            cloud="hetzner",
            username="root",
            port=22,
            enabled=True,
        )

        orch = make_orchestrator(config_clouds=[])
        # Gateway contains neither IP → both are yielded.
        orch._repository.contains = MagicMock(return_value=False)  # type: ignore[method-assign]
        orch._uow_factory = MagicMock(  # type: ignore[method-assign]
            return_value=_uow_with_nodes([static_node, cloud_node]),
        )

        yielded_ids = [msg.id async for msg in orch._connect_machine_producer()]

        assert NodeId(1) in yielded_ids, (
            "static node (cloud=None) MUST be yielded to the connect consumer — "
            "the daemon must connect operator-managed nodes"
        )
        assert NodeId(2) in yielded_ids, "cloud node must still be yielded normally"

    @pytest.mark.asyncio
    async def test_gateway_registered_static_node_not_yielded(self) -> None:
        """A static node already in the gateway is not re-yielded (pre-existing behavior)."""
        from yascheduler.domain.model import Node

        static_node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.9",
            ncpus=2,
            cloud=None,
            username="root",
            port=22,
            enabled=True,
        )

        orch = make_orchestrator(config_clouds=[])
        orch._repository.contains = MagicMock(return_value=True)  # type: ignore[method-assign]
        orch._uow_factory = MagicMock(  # type: ignore[method-assign]
            return_value=_uow_with_nodes([static_node]),
        )

        yielded_ips = [msg.id async for msg in orch._connect_machine_producer()]
        assert yielded_ips == []

    @pytest.mark.asyncio
    async def test_disabled_node_with_running_task_is_yielded(self) -> None:
        """A disabled node carrying a RUNNING task is yielded for reconnection.

        After a daemon restart the in-memory session dict is empty. A disabled
        node (operator drain or idle-tolerance disable) may still carry a
        RUNNING task whose outputs have not been downloaded. The producer MUST
        yield it so the consume loop can finish the task.
        """
        from yascheduler.domain.model import Node, Running, Task, TaskId

        disabled_node = Node(
            node_id=NodeId(3),
            hostname="10.0.0.3",
            ncpus=2,
            cloud="vultr",
            username="root",
            port=22,
            enabled=False,
        )
        running_task = Task(
            task_id=TaskId(7),
            engine="test_shell",
            state=Running(allocated_node_id=NodeId(3), remote_folder="/r"),
        )

        orch = make_orchestrator(config_clouds=[])
        orch._repository.contains = MagicMock(return_value=False)  # type: ignore[method-assign]
        orch._uow_factory = MagicMock(  # type: ignore[method-assign]
            return_value=_uow_with_nodes(
                [],
                disabled_nodes=[disabled_node],
                running_tasks=[running_task],
            ),
        )

        yielded_ids = [msg.id async for msg in orch._connect_machine_producer()]
        assert NodeId(3) in yielded_ids

    @pytest.mark.asyncio
    async def test_disabled_node_without_running_task_not_yielded(self) -> None:
        """A disabled node with no RUNNING task is NOT yielded (no work to drain)."""
        from yascheduler.domain.model import Node

        disabled_node = Node(
            node_id=NodeId(4),
            hostname="10.0.0.4",
            ncpus=2,
            cloud="vultr",
            username="root",
            port=22,
            enabled=False,
        )

        orch = make_orchestrator(config_clouds=[])
        orch._repository.contains = MagicMock(return_value=False)  # type: ignore[method-assign]
        orch._uow_factory = MagicMock(  # type: ignore[method-assign]
            return_value=_uow_with_nodes(
                [],
                disabled_nodes=[disabled_node],
                running_tasks=[],
            ),
        )

        yielded_ids = [msg.id async for msg in orch._connect_machine_producer()]
        assert yielded_ids == []

    @pytest.mark.asyncio
    async def test_disabled_node_with_running_task_skipped_if_connected(
        self,
    ) -> None:
        """A disabled node already connected is not re-yielded."""
        from yascheduler.domain.model import Node, Running, Task, TaskId

        disabled_node = Node(
            node_id=NodeId(5),
            hostname="10.0.0.5",
            ncpus=2,
            cloud="vultr",
            username="root",
            port=22,
            enabled=False,
        )
        running_task = Task(
            task_id=TaskId(9),
            engine="test_shell",
            state=Running(allocated_node_id=NodeId(5), remote_folder="/r"),
        )

        orch = make_orchestrator(config_clouds=[])
        orch._repository.contains = MagicMock(return_value=True)  # type: ignore[method-assign]
        orch._uow_factory = MagicMock(  # type: ignore[method-assign]
            return_value=_uow_with_nodes(
                [],
                disabled_nodes=[disabled_node],
                running_tasks=[running_task],
            ),
        )

        yielded_ids = [msg.id async for msg in orch._connect_machine_producer()]
        assert yielded_ids == []

    @pytest.mark.asyncio
    async def test_static_node_failure_retries_without_abandon(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A static node that fails SSH is retried without reaching abandon_node.

        Drives _connect_machine_consumer directly with a static node whose
        gateway.connect raises MachineConnectionError. The consumer-side guard
        (START_BLOCK_STATIC_NODE_RETRY) fires before the grace-check, so:
        - abandon_node is never called
        - _connect_failures is never populated for the static IP
        - a CONNECT_RETRY_STATIC warning is emitted
        """
        import logging

        from yascheduler.domain.exceptions import MachineConnectionError
        from yascheduler.domain.model import Node

        static_node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.9",
            ncpus=2,
            cloud=None,
            username="root",
            port=22,
            enabled=True,
        )

        orch = make_orchestrator(config_clouds=[])
        orch._repository.connect = AsyncMock(  # type: ignore[method-assign]
            side_effect=MachineConnectionError(NodeId(1), "10.0.0.9", "refused"),
        )

        with (
            patch(
                "yascheduler.application.orchestrator.abandon_node",
                new=AsyncMock(),
            ) as mock_abandon,
            caplog.at_level(logging.DEBUG),
        ):
            await orch._connect_machine_consumer(UMessage(NodeId(1), static_node))

        mock_abandon.assert_not_called()
        assert NodeId(1) not in orch._connect_failures, (
            "static node IP must never enter the failure timer "
            "(consumer-side guard bypasses the grace-check)"
        )
        trace_records = [
            r for r in caplog.records if r.getMessage() == "CONNECT_RETRY_STATIC"
        ]
        assert len(trace_records) == 1, "expected one CONNECT_RETRY_STATIC trace record"
        assert extra_fields(trace_records[0]).get("hostname") == "10.0.0.9"

    @pytest.mark.asyncio
    async def test_static_node_past_grace_does_not_abandon(self) -> None:
        """A static node that has been failing >120s is still NOT abandoned.

        Temporal guard: even if the failure timer hypothetically accumulated
        past the 120s conservative fallback, the consumer-side guard fires
        before the grace-check so abandon_node is never reached and the DB
        row is preserved. Mirrors the test_connect_failure_past_grace_triggers_abandon
        pattern in TestConnectMachineConsumerGraceTimer, but with cloud=None
        and patched monotonic timestamps >120s apart.
        """
        from yascheduler.domain.exceptions import MachineConnectionError
        from yascheduler.domain.model import Node

        static_node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.9",
            ncpus=2,
            cloud=None,
            username="root",
            port=22,
            enabled=True,
        )

        orch = make_orchestrator(config_clouds=[])
        orch._repository.connect = AsyncMock(  # type: ignore[method-assign]
            side_effect=MachineConnectionError(NodeId(1), "10.0.0.9", "refused"),
        )

        with (
            patch(
                "yascheduler.application.orchestrator.time.monotonic",
                return_value=250.0,
            ),
            patch(
                "yascheduler.application.orchestrator.abandon_node",
                new=AsyncMock(),
            ) as mock_abandon,
        ):
            await orch._connect_machine_consumer(UMessage(NodeId(1), static_node))

        mock_abandon.assert_not_called()
        # The DB row is preserved because abandon_node (the only path that
        # removes the yascheduler_nodes row) is structurally unreachable for
        # static nodes — the consumer-side guard returns before the
        # grace-check / abandon block.
        assert NodeId(1) not in orch._connect_failures


class TestConnectNewNode:
    """Gherkin: New node connected — connect call shape without jump kwargs."""

    @pytest.mark.asyncio
    async def test_connect_new_node_no_jump_kwargs(self) -> None:
        """WHEN new enabled node appears, THEN repository.connect(node, client_keys, ...)
        called with NO jump_host / jump_username arguments.
        """
        orch = make_orchestrator(config_clouds=[])
        connect_mock = AsyncMock(return_value=MagicMock())
        orch._repository.connect = connect_mock  # type: ignore[method-assign]

        node = _make_node()
        await orch._connect_machine_consumer(UMessage(node.node_id, node))

        connect_mock.assert_called_once_with(
            node=node,
            client_keys=[],
            connect_timeout=10,
            data_dir=orch._remote_defaults.data_dir,
            engines_dir=orch._remote_defaults.engines_dir,
            tasks_dir=orch._remote_defaults.tasks_dir,
        )

    @pytest.mark.asyncio
    async def test_connect_reads_jump_identity_from_node_no_inline_resolution(
        self,
    ) -> None:
        """WHEN orchestrator calls repository.connect for a node with
        jump_host set, THEN no inline resolution loop runs (no iteration over
        config.clouds, no read of config.remote.jump_host).
        """
        cfg_cloud = MagicMock(prefix="hetzner", connect_grace=60)
        orch = make_orchestrator(config_clouds=[cfg_cloud])
        connect_mock = AsyncMock(return_value=MagicMock())
        orch._repository.connect = connect_mock  # type: ignore[method-assign]

        node = _make_node(cloud="hetzner")
        await orch._connect_machine_consumer(UMessage(node.node_id, node))

        # The connect call has no jump_host/jump_username kwargs — proves
        # no resolution loop ran to produce them.
        connect_mock.assert_called_once_with(
            node=node,
            client_keys=[],
            connect_timeout=10,
            data_dir=orch._remote_defaults.data_dir,
            engines_dir=orch._remote_defaults.engines_dir,
            tasks_dir=orch._remote_defaults.tasks_dir,
        )


class TestMissingKeysDirTranslation:
    """BLOCK_load_private_keys translates FileNotFoundError from list_private_keys
    into MachineConnectionError so a missing keys_dir routes through the same
    retry path as SSH failures instead of escaping as a contextless
    CONSUMER_ERROR.

    Covers the regression where a missing data_dir/keys_dir produced a spammed
    `consumer error on queue conn_machine` ERROR with no node context, because
    list_private_keys was called outside the connect try-block. The translation
    sits before the try-block so both static and cloud nodes hit their designed
    retry/abandon handlers.
    """

    @pytest.mark.asyncio
    async def test_static_node_missing_keys_dir_translated_to_machine_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """list_private_keys FileNotFoundError on a static node → MachineConnectionError
        routed through BLOCK_static_node_retry (never abandon, contextual WARNING)."""
        import logging

        from yascheduler.domain.model import Node

        static_node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.9",
            ncpus=2,
            cloud=None,
            username="root",
            port=22,
            enabled=True,
        )

        orch = make_orchestrator(config_clouds=[])
        orch._list_private_keys_fn = MagicMock(  # type: ignore[method-assign]
            side_effect=FileNotFoundError(2, "No such file or directory"),
        )
        orch._repository.connect = AsyncMock()  # type: ignore[method-assign]

        with (
            patch(
                "yascheduler.application.orchestrator.abandon_node",
                new=AsyncMock(),
            ) as mock_abandon,
            caplog.at_level(logging.DEBUG),
        ):
            await orch._connect_machine_consumer(UMessage(NodeId(1), static_node))

        # repository.connect never reached: the translation raised before it.
        orch._repository.connect.assert_not_called()
        # Static node → never abandon, never enters the failure timer.
        mock_abandon.assert_not_called()
        assert NodeId(1) not in orch._connect_failures
        # Contextual trace + user-visible WARNING both fire.
        trace_records = [
            r for r in caplog.records if r.getMessage() == "CONNECT_RETRY_STATIC"
        ]
        assert len(trace_records) == 1
        extra = extra_fields(trace_records[0])
        assert extra.get("hostname") == "10.0.0.9"
        assert "keys_dir does not exist" in str(extra.get("err"))
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "static node 10.0.0.9 connect failed" in r.getMessage()
        ]
        assert len(warning_records) == 1
        assert "keys_dir does not exist" in warning_records[0].getMessage()

    @pytest.mark.asyncio
    async def test_cloud_node_missing_keys_dir_translated_to_machine_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """list_private_keys FileNotFoundError on a cloud node → MachineConnectionError
        routed through BLOCK_connect_grace_check (no abandon within grace)."""
        import logging

        from yascheduler.domain.model import Node

        cloud_node = Node(
            node_id=NodeId(2),
            hostname="10.0.0.10",
            ncpus=2,
            cloud="hetzner",
            username="root",
            port=22,
            enabled=True,
        )

        cfg_cloud = MagicMock(prefix="hetzner", connect_grace=60)
        orch = make_orchestrator(config_clouds=[cfg_cloud])
        orch._list_private_keys_fn = MagicMock(  # type: ignore[method-assign]
            side_effect=FileNotFoundError(2, "No such file or directory"),
        )
        orch._repository.connect = AsyncMock()  # type: ignore[method-assign]

        with (
            patch(
                "yascheduler.application.orchestrator.time.monotonic",
                return_value=110.0,
            ),
            patch(
                "yascheduler.application.orchestrator.abandon_node",
                new=AsyncMock(),
            ) as mock_abandon,
            caplog.at_level(logging.DEBUG),
        ):
            await orch._connect_machine_consumer(UMessage(NodeId(2), cloud_node))

        orch._repository.connect.assert_not_called()
        # Within grace → no abandon, IP stays in timer.
        mock_abandon.assert_not_called()
        assert NodeId(2) in orch._connect_failures
        trace_records = [r for r in caplog.records if r.getMessage() == "CONNECT_RETRY"]
        assert len(trace_records) == 1
        extra = extra_fields(trace_records[0])
        assert extra.get("hostname") == "10.0.0.10"
        assert "keys_dir does not exist" in str(extra.get("err"))


def _uow_with_nodes(
    nodes: list,
    *,
    disabled_nodes: list | None = None,
    running_tasks: list | None = None,
) -> AsyncMock:
    """Build a UoW mock whose nodes.list_enabled returns the given nodes.

    Optional ``disabled_nodes`` populates nodes.list_disabled (defaults to
    empty) and ``running_tasks`` populates tasks.list_by_status (defaults to
    empty) so the connect producer's drain path can be exercised.
    """
    uow = AsyncMock()
    uow.nodes = AsyncMock()
    uow.nodes.list_enabled = AsyncMock(return_value=nodes)
    uow.nodes.list_disabled = AsyncMock(return_value=disabled_nodes or [])
    uow.tasks = AsyncMock()
    uow.tasks.list_running = AsyncMock(return_value=running_tasks or [])
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    return uow
