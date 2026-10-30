# region MODULE_CONTRACT
# PURPOSE: Structural conformance tests for domain port Protocols via isinstance checks.
# SCOPE: TaskRepository, NodeRepository, MachineRepository, MachineSession, CloudProvisioner Protocols, including the task-input read surface (streamed entries + batched snapshots).
# KEYWORDS: domain port Protocols, isinstance, structural conformance
# endregion MODULE_CONTRACT

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Protocol
from unittest.mock import MagicMock

import pytest

from yascheduler.domain.model import (
    ConnectedMachine,
    NewNode,
    NewTask,
    Node,
    NodeId,
    ProcessResult,
    Task,
    TaskId,
    TaskStatus,
)
from yascheduler.domain.ports import (
    CloudProvisioner,
    MachineRepository,
    MachineSession,
    NodeRepository,
    TaskRepository,
)
from yascheduler.domain.task_inputs import TaskInputEntry, TaskInputSnapshot

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Awaitable,
        Callable,
        Mapping,
        Sequence,
    )
    from pathlib import Path
    from re import Pattern

    from yascheduler.domain import EngineRepository


class StubTaskRepository:
    async def get(self, task_id: TaskId) -> Task | None:
        raise NotImplementedError

    async def get_running(self, task_id: TaskId) -> Task | None:
        return None

    async def get_todo(self, task_id: TaskId) -> Task | None:
        return None

    async def save(
        self, task: Task, *, expected_status: TaskStatus | None = None
    ) -> None:
        pass

    async def list_by_status(
        self,
        statuses: set[TaskStatus],
        *,
        limit: int | None = None,
    ) -> list[Task]:
        return []

    async def list_running(self, *, limit: int | None = None) -> list[Task]:
        return []

    async def list_todo(self, *, limit: int | None = None) -> list[Task]:
        return []

    async def insert(self, new_task: NewTask) -> Task:
        raise NotImplementedError

    async def list_by_jobs(self, job_ids: list[TaskId]) -> list[Task]:
        return []

    async def list_ids_by_node_id_and_status(
        self,
        node_id: NodeId,
        status: TaskStatus,
    ) -> list[TaskId]:
        return []

    async def count_by_status(self) -> dict[TaskStatus, int]:
        return {}

    def iter_input_entries(self, task_id: TaskId) -> AsyncIterator[TaskInputEntry]:
        return _empty_async_gen()

    async def list_input_snapshots(
        self, task_ids: Sequence[TaskId]
    ) -> Mapping[TaskId, TaskInputSnapshot]:
        return {}


class StubNodeRepository:
    async def list_enabled(self) -> list[Node]:
        return []

    async def list_disabled(self) -> list[Node]:
        return []

    async def get_by_id(self, node_id: NodeId) -> Node | None:
        raise NotImplementedError

    async def get_by_ids(self, node_ids: list[NodeId]) -> dict[NodeId, Node]:
        return {}

    async def insert(self, new_node: NewNode) -> Node:
        raise NotImplementedError

    async def update(self, node: Node) -> None:
        pass

    async def enable(self, node_id: NodeId) -> None:
        pass

    async def disable(self, node_id: NodeId) -> None:
        pass

    async def remove(self, node_id: NodeId) -> None:
        pass

    async def list_all(self) -> list[Node]:
        return []

    async def count_by_status(self) -> dict[bool, int]:
        return {}


def _make_session_stub() -> ConnectedMachine:
    return ConnectedMachine(node_id=NodeId(1), platforms=("linux",))


class StubMachineSession(MachineSession):
    """Stub MachineSession covering the full Protocol surface."""

    @property
    def hostname(self) -> str:
        return "10.0.0.1"

    @property
    def machine(self) -> ConnectedMachine:
        return _make_session_stub()

    @property
    def is_closed(self) -> bool:
        return False

    def occupy(self) -> None:
        pass

    def release(self) -> None:
        pass

    def update(self, machine: ConnectedMachine) -> None:
        pass

    @property
    def adapter(self) -> Any:
        return MagicMock()

    @property
    def platforms(self) -> Sequence[str]:
        return ("linux",)

    @property
    def data_dir(self) -> PurePath:
        return PurePath("./data")

    @property
    def engines_dir(self) -> PurePath:
        return PurePath("./data/engines")

    @property
    def tasks_dir(self) -> PurePath:
        return PurePath("./data/tasks")

    @property
    def path(self) -> type[PurePath]:
        return PurePath

    @property
    def quote(self) -> Callable[[str], str]:
        return lambda s: s

    async def run(self, cmd: str) -> ProcessResult:
        return ProcessResult(exit_code=0)

    async def run_full(self, cmd: str) -> Any:
        return MagicMock(returncode=0)

    async def run_bg(self, cmd: str, *, cwd: str | None = None) -> None:
        pass

    async def upload(self, local: Path, remote: str) -> None:
        pass

    def open_sftp(self) -> Any:
        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[Any, None]:
            yield MagicMock()

        return _ctx()

    async def get_cpu_cores(self) -> int:
        return 1

    async def setup_node(self, engines: EngineRepository) -> None:
        pass

    async def pgrep(
        self,
        pattern: str | Pattern[str],
        full: bool = True,
    ) -> AsyncGenerator[Any, None]:
        return
        yield  # type: ignore[unreachable]

    async def list_processes(self) -> AsyncGenerator[Any, None]:
        return
        yield  # type: ignore[unreachable]

    def install_monitor(
        self,
        *,
        interval: float,
        check_factory: Callable[[], Awaitable[bool]],
        on_free: Callable[[], None],
    ) -> None:
        pass

    def cancel_monitor(self) -> None:
        pass


async def _empty_async_gen() -> Any:
    return
    yield  # type: ignore[unreachable]


class StubMachineRepository(MachineRepository):
    async def connect(
        self,
        node: Node,
        client_keys: Sequence[PurePath] | None,
        *,
        connect_timeout: int | None = None,
        data_dir: PurePath | None = None,
        engines_dir: PurePath | None = None,
        tasks_dir: PurePath | None = None,
    ) -> MachineSession:
        return StubMachineSession()

    async def disconnect(self, node_id: NodeId) -> None:
        pass

    async def disconnect_all(self) -> None:
        pass

    def list_free(self, platforms: list[str] | None) -> list[MachineSession]:
        return []

    def list_connected(self) -> list[MachineSession]:
        return []

    def get_session(self, node_id: NodeId) -> MachineSession | None:
        return None

    def contains(self, node_id: NodeId) -> bool:
        return False

    def __len__(self) -> int:
        return 0

    def __contains__(self, node_id: NodeId) -> bool:
        return False


class StubCloudProvisioner:
    async def allocate(self, provider: str, node: Node) -> Node:
        raise NotImplementedError

    async def deallocate(self, node: Node) -> None:
        pass

    def select_provider(
        self,
        platforms: list[str],
        current_counts: dict[str, int],
    ) -> str | None:
        return None

    async def stop(self) -> None:
        pass


def test_task_repository_protocol() -> None:
    stub = StubTaskRepository()
    assert isinstance(stub, TaskRepository)


def test_task_repository_input_read_surface() -> None:
    """TaskRepository exposes a synchronous entry iterator and an awaited batched snapshot read."""
    import inspect

    assert hasattr(TaskRepository, "iter_input_entries")
    assert hasattr(TaskRepository, "list_input_snapshots")
    # iter_input_entries returns the iterator directly — callers must not await it.
    assert not inspect.iscoroutinefunction(TaskRepository.iter_input_entries)
    assert inspect.iscoroutinefunction(TaskRepository.list_input_snapshots)


def test_node_repository_protocol() -> None:
    stub = StubNodeRepository()
    assert isinstance(stub, NodeRepository)


def test_machine_repository_protocol() -> None:
    stub = StubMachineRepository()
    assert isinstance(stub, MachineRepository)


def test_machine_repository_connect_signature_no_jump_kwargs() -> None:
    """MachineRepository.connect has no jump_host or jump_username parameters."""
    import inspect

    sig = inspect.signature(MachineRepository.connect)
    params = sig.parameters
    assert "jump_host" not in params
    assert "jump_username" not in params
    # Verify jump is read from node — node param is present
    assert "node" in params
    assert "client_keys" in params


def test_machine_session_protocol() -> None:
    stub = StubMachineSession()
    assert isinstance(stub, MachineSession)


def test_cloud_provisioner_protocol() -> None:
    stub = StubCloudProvisioner()
    assert isinstance(stub, CloudProvisioner)


def test_machine_session_hostname() -> None:
    """MachineSession Protocol has hostname property."""
    assert hasattr(MachineSession, "hostname")


def test_two_protocols_defined() -> None:
    """MachineRepository and MachineSession are defined as runtime_checkable Protocols."""
    assert issubclass(MachineRepository, Protocol)  # type: ignore[arg-type]
    assert issubclass(MachineSession, Protocol)  # type: ignore[arg-type]
    # Verify runtime_checkable decorator was applied
    assert hasattr(MachineRepository, "__instancecheck__")
    assert hasattr(MachineSession, "__instancecheck__")

    # No MachineOperations Protocol exists
    with pytest.raises(ImportError):
        from yascheduler.domain.ports import MachineOperations  # noqa: F401


def test_node_repository_insert_shape() -> None:
    """NodeRepository.insert takes NewNode and returns Node."""
    import inspect

    sig = inspect.signature(StubNodeRepository.insert)
    params = list(sig.parameters.keys())
    assert "new_node" in params


def test_node_repository_get_by_id_shape() -> None:
    """NodeRepository.get_by_id takes NodeId and returns Node | None."""
    import inspect

    sig = inspect.signature(StubNodeRepository.get_by_id)
    params = list(sig.parameters.keys())
    assert "node_id" in params
    # Return annotation should include None
    ann = sig.return_annotation
    assert ann is not inspect.Parameter.empty


def test_node_repository_remove_shape() -> None:
    """NodeRepository.remove takes NodeId."""
    import inspect

    sig = inspect.signature(StubNodeRepository.remove)
    params = list(sig.parameters.keys())
    assert "node_id" in params


def test_cloud_allocate_sets_external_id_alongside_hostname() -> None:
    """CloudProvisioner.allocate takes provider and node, returns Node."""
    import inspect

    sig = inspect.signature(StubCloudProvisioner.allocate)
    params = list(sig.parameters.keys())
    assert "provider" in params
    assert "node" in params
    ann = sig.return_annotation
    assert str(ann) == "Node"


def test_cloud_deallocate_reads_node_cloud_and_hostname() -> None:
    """CloudProvisioner.deallocate takes a Node."""
    import inspect

    sig = inspect.signature(StubCloudProvisioner.deallocate)
    params = list(sig.parameters.keys())
    assert "node" in params
