# region MODULE_CONTRACT
# PURPOSE: Structural conformance guard for the domain port Protocols: each stays runtime-checkable, structurally satisfiable, and keeps the surface that callers rely on.
# SCOPE: TaskRepository, NodeRepository, MachineRepository, MachineSession, CloudProvisioner Protocol conformance, the split task-input read surface (streamed entries + batched snapshots), and the connect() jump-credentials rule.
# KEYWORDS: domain port Protocols, isinstance, structural conformance
# endregion MODULE_CONTRACT

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from yascheduler.domain.ports import (
    CloudProvisioner,
    MachineRepository,
    MachineSession,
    NodeRepository,
    TaskRepository,
)

PROTOCOLS: tuple[Any, ...] = (
    TaskRepository,
    NodeRepository,
    MachineRepository,
    MachineSession,
    CloudProvisioner,
)


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=lambda proto: proto.__name__)
def test_protocol_is_runtime_checkable_and_satisfiable(protocol: Any) -> None:
    """Every port is a runtime_checkable Protocol that a spec'd double satisfies."""
    assert hasattr(protocol, "__instancecheck__")
    assert isinstance(MagicMock(spec=protocol), protocol)


def test_task_repository_input_surface_streams_then_batches() -> None:
    """Entry iteration returns a sync iterator; the batched snapshot read is awaited."""
    assert not inspect.iscoroutinefunction(TaskRepository.iter_input_entries)
    assert inspect.iscoroutinefunction(TaskRepository.list_input_snapshots)


def test_machine_repository_connect_reads_jump_from_node() -> None:
    """Jump credentials travel with the Node, never as connect() kwargs."""
    params = inspect.signature(MachineRepository.connect).parameters
    assert {"node", "client_keys"} <= set(params)
    assert not {"jump_host", "jump_username"} & set(params)


def test_no_machine_operations_protocol() -> None:
    """Machine work stays on MachineSession; no third protocol splits it."""
    with pytest.raises(ImportError):
        from yascheduler.domain.ports import MachineOperations  # noqa: F401
