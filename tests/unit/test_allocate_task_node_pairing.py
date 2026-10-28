# region MODULE_CONTRACT
# PURPOSE: Unit tests for the task-allocated-node-id application-layer changes: _find_free_machines session↔Node pairing and _try_start_on_machine node_id logging.
# SCOPE: _find_free_machines returns list[tuple[MachineSession, Node]] paired by ip (dup-IP collapses to last-wins); _try_start_on_machine takes (session, node), calls task.run(node_id, remote_folder), logs node_id=%s.
# KEYWORDS: _find_free_machines, session-node pairing, node_id logging
# endregion MODULE_CONTRACT

import logging
import time
from pathlib import PurePath
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.log_assertions import extra_fields
from yascheduler.application.allocate_task import (
    _find_free_machines,
    _try_start_on_machine,
)
from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.application.uow import AbstractUnitOfWork
from yascheduler.domain.model import (
    Engine,
    Node,
    NodeId,
    Task,
    TaskId,
    TaskStatus,
    Todo,
)


class TestFindFreeMachinesNodePairing:
    """_find_free_machines returns list[tuple[MachineSession, Node]] paired by ip."""

    async def test_pairs_session_with_node_by_ip(self, engine: Engine) -> None:
        """Two enabled nodes (distinct ips) + two matching sessions → paired correctly."""
        from yascheduler.domain.model import ConnectedMachine, MachineState

        node_a = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True)
        node_b = Node(node_id=NodeId(2), hostname="10.0.0.2", ncpus=4, enabled=True)

        m_a = MagicMock(spec=ConnectedMachine)
        m_a.node_id = NodeId(1)
        m_a.hostname = "10.0.0.1"
        m_a.state = MachineState.FREE
        m_a.free_since = time.monotonic()
        m_b = MagicMock(spec=ConnectedMachine)
        m_b.node_id = NodeId(2)
        m_b.hostname = "10.0.0.2"
        m_b.state = MachineState.FREE
        m_b.free_since = time.monotonic()
        session_a = SimpleNamespace(machine=m_a, hostname="10.0.0.1")
        session_b = SimpleNamespace(machine=m_b, hostname="10.0.0.2")

        repository = MagicMock()
        repository.list_free = MagicMock(return_value=[session_a, session_b])

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.list_by_status = AsyncMock(return_value=[])  # nothing busy
        uow.nodes = AsyncMock()
        uow.nodes.list_enabled = AsyncMock(return_value=[node_a, node_b])
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        pairs = await _find_free_machines(engine, uow_factory, repository)

        assert len(pairs) == 2
        # Each session paired with the matching Node by ip.
        ips_to_nodes = {s.hostname: n for s, n in pairs}
        assert ips_to_nodes == {"10.0.0.1": node_a, "10.0.0.2": node_b}
        # Each Node carries node_id.
        for _s, n in pairs:
            assert isinstance(n.node_id, NodeId)

    async def test_dup_ip_collapses_to_one_node(self, engine: Engine) -> None:
        """Dup-IP enabled nodes collapse to one Node in nodes_by_ip (last wins).

        Documents the same-ambiguity-as-today behavior: the prior
        ``enabled_ips = {n.hostname}`` set membership collapsed duplicates too; this
        change records allocated_node_id from one of the duplicates (arbitrary).
        Full disambiguation lands with Surface A.
        """
        from yascheduler.domain.model import ConnectedMachine, MachineState

        node_first = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True)
        node_last = Node(node_id=NodeId(2), hostname="10.0.0.1", ncpus=4, enabled=True)

        m = MagicMock(spec=ConnectedMachine)
        m.node_id = NodeId(2)
        m.hostname = "10.0.0.1"
        m.state = MachineState.FREE
        m.free_since = time.monotonic()
        session = SimpleNamespace(machine=m, hostname="10.0.0.1")

        repository = MagicMock()
        repository.list_free = MagicMock(return_value=[session])

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        uow.nodes = AsyncMock()
        # list_enabled returns both dup-IP nodes; dict comprehension last-wins.
        uow.nodes.list_enabled = AsyncMock(return_value=[node_first, node_last])
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        pairs = await _find_free_machines(engine, uow_factory, repository)

        # One pair (one session, one ip); the Node is the last-wins one.
        assert len(pairs) == 1
        _s, paired_node = pairs[0]
        assert paired_node.hostname == "10.0.0.1"
        assert paired_node.node_id in (NodeId(1), NodeId(2))


class TestFindFreeMachinesExcludeNodes:
    """_find_free_machines drops sessions whose hostname is in engine.exclude_nodes."""

    async def test_excluded_hostname_filtered_out(self, engine: Engine) -> None:
        """A session whose hostname is excluded is filtered out; a non-matching hostname stays eligible."""
        from dataclasses import replace

        from yascheduler.domain.model import ConnectedMachine, MachineState

        engine = replace(engine, exclude_nodes=frozenset({"10.0.0.2"}))

        node_a = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True)
        node_b = Node(node_id=NodeId(2), hostname="10.0.0.2", ncpus=4, enabled=True)

        m_a = MagicMock(spec=ConnectedMachine)
        m_a.node_id = NodeId(1)
        m_a.hostname = "10.0.0.1"
        m_a.state = MachineState.FREE
        m_a.free_since = time.monotonic()
        m_b = MagicMock(spec=ConnectedMachine)
        m_b.node_id = NodeId(2)
        m_b.hostname = "10.0.0.2"
        m_b.state = MachineState.FREE
        m_b.free_since = time.monotonic()
        session_a = SimpleNamespace(machine=m_a, hostname="10.0.0.1")
        session_b = SimpleNamespace(machine=m_b, hostname="10.0.0.2")

        repository = MagicMock()
        repository.list_free = MagicMock(return_value=[session_a, session_b])

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.list_by_status = AsyncMock(return_value=[])  # nothing busy
        uow.nodes = AsyncMock()
        uow.nodes.list_enabled = AsyncMock(return_value=[node_a, node_b])
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        pairs = await _find_free_machines(engine, uow_factory, repository)

        # The excluded hostname is filtered out; the non-matching one stays.
        assert len(pairs) == 1
        assert pairs[0][0] is session_a
        assert pairs[0][1] is node_a


class TestTryStartOnMachineNodeIdLogging:
    """_try_start_on_machine log lines include node_id=%s alongside ip=%s."""

    async def test_logs_node_id(
        self,
        engine: Engine,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from yascheduler.domain.model import ConnectedMachine, MachineState

        node = Node(node_id=NodeId(7), hostname="10.0.0.1", ncpus=4, enabled=True)
        m = MagicMock(spec=ConnectedMachine)
        m.hostname = "10.0.0.1"
        m.state = MachineState.FREE
        m.free_since = time.monotonic()
        session = SimpleNamespace(machine=m, hostname="10.0.0.1")

        from datetime import datetime

        task = Task(
            task_id=TaskId(1),
            label="t",
            engine="test_engine",
            state=Todo(),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

        operations = MagicMock()
        operations.start_occupancy_check = MagicMock()
        start_on_machine = AsyncMock(return_value=True)
        tracker = MagicMock(spec=AllocationTracker)

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        uow.tasks.save = AsyncMock()
        uow.commit = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        with caplog.at_level(
            logging.DEBUG,
            logger="yascheduler.application.allocate_task",
        ):
            result = await _try_start_on_machine(
                session,
                node,
                engine,
                task,
                operations,
                uow_factory,
                start_on_machine,
                tracker,
                PurePath("/remote/tasks"),
            )

        assert result is True
        # The allocation trace record carries node_id alongside hostname.
        # The former block marker is now the debug message; structured fields
        # are the record attributes beyond the native LogRecord key set.
        alloc_lines = [r for r in caplog.records if r.getMessage() == "ALLOCATED"]
        assert alloc_lines, "expected an ALLOCATED trace record"
        rec = alloc_lines[0]
        fields = extra_fields(rec)
        assert fields.get("hostname") == "10.0.0.1"
        assert fields.get("node_id") == NodeId(7)
        # save() called with the status guard (Level A double-allocation guard).
        uow.tasks.save.assert_awaited_once()
        assert uow.tasks.save.await_args is not None
        assert uow.tasks.save.await_args.kwargs["expected_status"] == TaskStatus.TO_DO
        # tracker.discard called with the task_id
        tracker.discard.assert_called_once_with(task.task_id)

    async def test_save_rejection_aborts_before_spawn(
        self,
        engine: Engine,
    ) -> None:
        """On save() rejection (lost cross-host claim), the spawn is never attempted — no orphan process can be created on the losing host."""
        node = Node(node_id=NodeId(7), hostname="10.0.0.1", ncpus=4, enabled=True)

        from datetime import datetime

        task = Task(
            task_id=TaskId(1),
            label="t",
            engine="test_engine",
            state=Todo(),
            webhook_url=None,
            webhook_custom_params={},
            extra={},
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )

        session = SimpleNamespace(hostname="10.0.0.1")
        occupancy_checker = MagicMock()
        start_on_machine = AsyncMock(return_value=True)
        tracker = MagicMock(spec=AllocationTracker)

        uow = AsyncMock()
        uow.tasks = AsyncMock()
        # save() raises — the status guard rejected the double-allocation.
        uow.tasks.save = AsyncMock(side_effect=RuntimeError("guard rejected"))
        uow.commit = AsyncMock()
        uow.rollback = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        def uow_factory() -> AbstractUnitOfWork:
            return uow

        with pytest.raises(RuntimeError, match="guard rejected"):
            await _try_start_on_machine(
                session,
                node,
                engine,
                task,
                occupancy_checker,
                uow_factory,
                start_on_machine,
                tracker,
                PurePath("/remote/tasks"),
            )

        # The spawn never ran — claim lost before any remote side effect.
        start_on_machine.assert_not_awaited()
        # Nothing committed; tracker not touched.
        uow.commit.assert_not_awaited()
        tracker.discard.assert_not_called()
