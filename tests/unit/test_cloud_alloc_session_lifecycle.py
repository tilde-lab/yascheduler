# region MODULE_CONTRACT
# PURPOSE: Regression-guard the four fixes in fix-cloud-alloc-session-lifecycle (DB-enabled free-machine gate, setup-failure disconnect, per-session loop isolation, stdout in cloud-init error).
# SCOPE: Fix A (setup-in-flight / disabled-but-connected / enabled / concurrent pile-on via timing-aware fakes through allocate_task), Fix B (CloudSetupError + generic exception + never-connected + success-no-disconnect via real CloudProvisionerImpl.allocate), Fix C (stale session isolated, cloud branch reachable via allocate_task), Fix D (cloud-init message contains stdout; timeout message unchanged via real CloudProvisionerImpl._setup_vm).
# KEYWORDS: cloud alloc session, setup-in-flight, disconnect, isolation
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yascheduler.application.allocate_task import allocate_task
from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.domain.exceptions import CloudSetupError
from yascheduler.domain.model import (
    ConnectedMachine,
    MachineState,
    NewNode,
    NewTask,
    Node,
    NodeId,
    Task,
    TaskId,
    TaskStatus,
    Todo,
    allocated_node_id_of,
)
from yascheduler.infra.cloud.dto import CloudCreateNodeDTO
from yascheduler.infra.cloud.manager import CloudProvisionerImpl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from yascheduler.domain import Engine, TaskInputEntry, TaskInputSnapshot


# =============================================================================
# Fakes — allocator-level (Fix A / Fix C)
# =============================================================================


class FakeMachineSession:
    """Minimal MachineSession carrying ip + a mutable ConnectedMachine snapshot.

    Exposes ``run``/``setup_node``/``get_cpu_cores`` as configurable AsyncMock
    attributes so tests can set up return values / side effects (the real
    ``CloudProvisionerImpl._setup_vm`` calls these directly on the session).
    """

    def __init__(self, hostname: str, platform: str = "linux") -> None:
        self._hostname = hostname
        # Derive node_id from hostname to ensure uniqueness; the allocator pairs
        # sessions with nodes by node_id so this must match the DB-side ID.
        last_octet = int(hostname.rsplit(".", 1)[-1]) if "." in hostname else 1
        self._machine = ConnectedMachine(
            platforms=(platform,),
            state=MachineState.FREE,
            free_since=0.0,
            node_id=NodeId(last_octet),
        )
        # Session methods called directly by CloudProvisionerImpl._setup_vm.
        self.run: AsyncMock = AsyncMock()
        self.setup_node: AsyncMock = AsyncMock()
        self.get_cpu_cores: AsyncMock = AsyncMock(return_value=4)

    @property
    def hostname(self) -> str:
        return self._hostname

    @property
    def machine(self) -> ConnectedMachine:
        return self._machine


class FakeMachineRepository:
    """In-memory MachineRepository mirroring SSHMachineRepository semantics.

    connect() registers a FREE session in _sessions BEFORE returning (this is
    the connect-before-enable timing that Fix A gates against). disconnect()
    pops the session (safe no-op if absent, like SSHMachineRepository.disconnect).
    Set connect_raises to make connect() fail before registering (mirrors a
    _connect_to_vm SSH failure — Fix B never-connected scenario).

    ``session_run_side_effect``, when set, is applied as the ``side_effect`` of
    the session's ``run`` AsyncMock (``CloudProvisionerImpl._setup_vm`` calls
    ``session.run(...)`` directly). Similarly ``session_get_cpu_cores_return``
    sets the return value of ``session.get_cpu_cores``.
    """

    def __init__(
        self,
        connect_raises: BaseException | None = None,
        disconnect_raises: BaseException | None = None,
        *,
        session_run_side_effect: Any = None,
        session_get_cpu_cores_return: int = 4,
    ) -> None:
        self._sessions: dict[str, FakeMachineSession] = {}
        self._node_id_to_ip: dict[NodeId, str] = {}
        self.connect_calls: list[Node] = []
        self.disconnect_calls: list[NodeId] = []
        self._connect_raises = connect_raises
        self._disconnect_raises = disconnect_raises
        self._session_run_side_effect = session_run_side_effect
        self._session_get_cpu_cores_return = session_get_cpu_cores_return

    async def connect(
        self,
        node: Node,
        client_keys: Sequence[Any] | None = None,
        **kwargs: Any,
    ) -> FakeMachineSession:
        if self._connect_raises is not None:
            raise self._connect_raises
        self.connect_calls.append(node)
        platform = kwargs.get("platform", "linux")
        session = FakeMachineSession(hostname=node.hostname, platform=platform)
        if self._session_run_side_effect is not None:
            # A MagicMock configured with exit_code/stdout/stderr attributes is
            # a plain return value — use return_value so `await session.run(cmd)`
            # resolves to it directly. side_effect would call the MagicMock and
            # surface a fresh child mock, dropping the configured attributes.
            # A real async function (e.g. _slow_run blocking 60s) is a callable
            # side effect — use side_effect so AsyncMock calls it with the cmd.
            if isinstance(self._session_run_side_effect, MagicMock):
                session.run = AsyncMock(return_value=self._session_run_side_effect)
            else:
                session.run = AsyncMock(side_effect=self._session_run_side_effect)
        session.get_cpu_cores = AsyncMock(
            return_value=self._session_get_cpu_cores_return,
        )
        self._sessions[node.hostname] = session
        self._node_id_to_ip[node.node_id] = node.hostname
        return session

    async def disconnect(self, node_id: NodeId) -> None:
        self.disconnect_calls.append(node_id)
        if self._disconnect_raises is not None:
            raise self._disconnect_raises
        ip = self._node_id_to_ip.pop(node_id, None)
        if ip is not None:
            self._sessions.pop(ip, None)

    async def disconnect_all(self) -> None:
        for ip in list(self._sessions):
            self._sessions.pop(ip, None)

    def list_free(self, platforms: list[str] | None = None) -> list[FakeMachineSession]:
        wanted = set(platforms) if platforms is not None else None
        result = [
            s
            for s in self._sessions.values()
            if s.machine.state == MachineState.FREE
            and (wanted is None or set(s.machine.platforms) & wanted)
        ]
        result.sort(key=lambda s: s.machine.free_since or 0.0)
        return result

    def list_connected(self) -> list[FakeMachineSession]:
        return list(self._sessions.values())

    def get_session(self, node_id: NodeId) -> FakeMachineSession | None:
        ip = self._node_id_to_ip.get(node_id)
        return self._sessions.get(ip) if ip is not None else None

    def contains(self, node_id: NodeId) -> bool:
        return node_id in self._node_id_to_ip

    def __contains__(self, node_id: NodeId) -> bool:
        return node_id in self._node_id_to_ip

    def __len__(self) -> int:
        return len(self._sessions)


# ---- Shared-store in-memory UoW ----


class _FakeNodeRepo:
    """In-memory NodeRepository backed by a shared store dict."""

    def __init__(self, store: dict[str, Node]) -> None:
        self._store = store
        self._id_counter = 0

    async def get(self, ip: str) -> Node | None:
        return self._store.get(ip)

    async def list_enabled(self) -> list[Node]:
        return [n for n in self._store.values() if n.enabled]

    async def list_disabled(self) -> list[Node]:
        return [n for n in self._store.values() if not n.enabled]

    async def insert(self, new_node: NewNode) -> Node:
        self._id_counter += 1
        node = Node(
            node_id=NodeId(self._id_counter),
            hostname=new_node.hostname,
            ncpus=new_node.ncpus,
            enabled=new_node.enabled,
            cloud=new_node.cloud,
            username=new_node.username,
            port=new_node.port,
        )
        self._store[node.hostname] = node
        return node

    async def update(self, node: Node) -> None:
        self._store[node.hostname] = node

    async def enable(self, node_id: NodeId) -> None:
        ip = self._ip_for(node_id)
        node = self._store.get(ip) if ip is not None else None
        if node is not None:
            self._store[node.hostname] = Node(
                node_id=node.node_id,
                hostname=node.hostname,
                ncpus=node.ncpus,
                enabled=True,
                cloud=node.cloud,
                username=node.username,
                port=node.port,
            )

    async def disable(self, node_id: NodeId) -> None:
        ip = self._ip_for(node_id)
        node = self._store.get(ip) if ip is not None else None
        if node is not None:
            self._store[node.hostname] = Node(
                node_id=node.node_id,
                hostname=node.hostname,
                ncpus=node.ncpus,
                enabled=False,
                cloud=node.cloud,
                username=node.username,
                port=node.port,
            )

    async def remove(self, node_id: NodeId) -> None:
        ip = self._ip_for(node_id)
        if ip is not None:
            self._store.pop(ip, None)

    def _ip_for(self, node_id: NodeId) -> str | None:
        for ip, node in self._store.items():
            if node.node_id == node_id:
                return ip
        return None

    async def list_all(self) -> list[Node]:
        return list(self._store.values())

    async def get_by_ips(self, ips: list[str]) -> dict[str, Node]:
        return {ip: self._store[ip] for ip in ips if ip in self._store}

    async def count_by_status(self) -> dict[bool, int]:
        counts: dict[bool, int] = {True: 0, False: 0}
        for n in self._store.values():
            counts[n.enabled] = counts.get(n.enabled, 0) + 1
        return counts


async def _aiter(entries: Sequence[TaskInputEntry]) -> AsyncIterator[TaskInputEntry]:
    """Yield snapshot entries the way the real repository streams them."""
    for entry in entries:
        yield entry


class _FakeTaskRepo:
    """In-memory TaskRepository backed by a shared store dict."""

    def __init__(self, store: dict[TaskId, Task]) -> None:
        self._store = store

    async def get(self, task_id: TaskId) -> Task | None:
        return self._store.get(task_id)

    async def get_running(self, task_id: TaskId) -> Task | None:
        t = self._store.get(task_id)
        return t if t is not None and t.status is TaskStatus.RUNNING else None

    async def get_todo(self, task_id: TaskId) -> Task | None:
        t = self._store.get(task_id)
        return t if t is not None and t.status is TaskStatus.TO_DO else None

    async def save(
        self, task: Task, *, expected_status: TaskStatus | None = None
    ) -> None:
        self._store[task.task_id] = task

    async def list_by_status(
        self,
        statuses: set[TaskStatus],
        *,
        limit: int | None = None,
    ) -> list[Task]:
        result = [t for t in self._store.values() if t.status in statuses]
        return result[:limit] if limit is not None else result

    async def list_running(self, *, limit: int | None = None) -> list[Task]:
        return await self.list_by_status({TaskStatus.RUNNING}, limit=limit)

    async def list_todo(self, *, limit: int | None = None) -> list[Task]:
        return await self.list_by_status({TaskStatus.TO_DO}, limit=limit)

    async def insert(self, new_task: NewTask) -> Task:
        # Assign a fresh TaskId (one past the current max, min 1).
        next_id = TaskId(max((tid.value for tid in self._store), default=0) + 1)
        from datetime import datetime

        task = Task(
            task_id=next_id,
            label=new_task.label,
            engine=new_task.engine,
            state=Todo(),
            webhook_url=new_task.webhook_url,
            webhook_custom_params=new_task.webhook_custom_params,
            extra=new_task.extra,
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        )
        self._store[next_id] = task
        return task

    async def list_by_jobs(self, job_ids: list[TaskId]) -> list[Task]:
        return [t for t in self._store.values() if t.task_id in job_ids]

    async def list_ids_by_node_id_and_status(
        self,
        node_id: NodeId,
        status: TaskStatus,
    ) -> list[TaskId]:
        return [
            t.task_id
            for t in self._store.values()
            if allocated_node_id_of(t) == node_id and t.status == status
        ]

    async def count_by_status(self) -> dict[TaskStatus, int]:
        counts: dict[TaskStatus, int] = {}
        for t in self._store.values():
            counts[t.status] = counts.get(t.status, 0) + 1
        return counts

    def iter_input_entries(self, task_id: TaskId) -> AsyncIterator[TaskInputEntry]:
        # These tests allocate from TO_DO only; no snapshot bytes are involved.
        return _aiter(())

    async def list_input_snapshots(
        self, task_ids: Sequence[TaskId]
    ) -> dict[TaskId, TaskInputSnapshot]:
        return {}


class FakeUnitOfWork:
    """Async-context-manager UoW backed by shared task/node stores.

    Multiple uow_factory() calls observe consistent state because every UoW
    wraps the same store dicts (mirrors the real Postgres UoW sharing a pool).
    """

    def __init__(
        self,
        tasks_store: dict[TaskId, Task] | None = None,
        nodes_store: dict[str, Node] | None = None,
    ) -> None:
        self._tasks_store = tasks_store if tasks_store is not None else {}
        self._nodes_store = nodes_store if nodes_store is not None else {}
        self.tasks = _FakeTaskRepo(self._tasks_store)
        self.nodes = _FakeNodeRepo(self._nodes_store)
        self.commit_calls = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        pass

    async def collect_events(self) -> list[Any]:
        return []

    async def publish_events(self) -> None:
        pass


class FakeCloudProvisioner:
    """CloudProvisioner fake reproducing connect-before-enable timing.

    allocate() calls machine_repository.connect(ip) registering a FREE session,
    then either flips the DB row to enabled=TRUE on success (via uow_factory)
    or raises CloudSetupError WITHOUT flipping the DB row (mirrors _setup_vm
    failure). The connect-before-enable window is the registry-vs-DB desync
    that Fix A gates against at the allocator level. select_provider_result
    controls the sync port method (None => no provider => clean cloud exit).
    """

    def __init__(
        self,
        machine_repository: FakeMachineRepository,
        uow_factory: Callable[[], FakeUnitOfWork],
        *,
        provider: str = "aws",
        new_ip: str = "10.0.0.99",
        fail: bool = False,
        new_platform: str = "linux",
        select_provider_result: str | None = "aws",
    ) -> None:
        self._repo = machine_repository
        self._uow_factory = uow_factory
        self._provider = provider
        self._new_ip = new_ip
        self._fail = fail
        self._new_platform = new_platform
        self._select_result = select_provider_result
        self.allocate_calls: list[str] = []

    async def allocate(self, provider: str, node: Node) -> Node:
        self.allocate_calls.append(provider)
        session = await self._repo.connect(
            Node(
                node_id=node.node_id,
                hostname=self._new_ip,
                ncpus=None,
                enabled=True,
                cloud=provider,
                username="root",
                port=22,
            ),
            platform=self._new_platform,
        )
        if self._fail:
            raise CloudSetupError(f"setup failed on {session.hostname}")
        async with self._uow_factory() as uow:
            await uow.nodes.insert(
                NewNode(
                    hostname=session.hostname,
                    ncpus=None,
                    enabled=True,
                    cloud=provider,
                ),
            )
            await uow.commit()
        return Node(
            node_id=node.node_id,
            hostname=self._new_ip,
            ncpus=None,
            enabled=True,
            cloud=provider,
            username="root",
            port=22,
        )

    async def deallocate(self, node: Node) -> None:
        pass

    def select_provider(
        self,
        platforms: list[str],
        current_counts: dict[str, int],
    ) -> str | None:
        return self._select_result

    async def stop(self) -> None:
        pass


# =============================================================================
# Fakes — real CloudProvisionerImpl tests (Fix B / Fix D)
# =============================================================================


def _make_real_adapter_config(
    name: str = "test",
    ip: str = "[IP]",
    create_node_timeout: float = 300,
) -> tuple[MagicMock, MagicMock]:
    """Build a mock CloudAdapter + ConfigCloud for real CloudProvisionerImpl tests."""
    adapter = MagicMock()
    adapter.name = name
    adapter.create_node_conn_timeout = 30
    adapter.create_node_timeout = create_node_timeout
    adapter.needs_cloud_init = True

    async def _create_node(**kw: Any) -> CloudCreateNodeDTO:
        return CloudCreateNodeDTO(
            external_id=ip,
            hostname=ip,
            username="root",
            jump_host=None,
            jump_port=22,
            jump_username="root",
        )

    adapter.create_node = _create_node
    adapter.delete_node = AsyncMock()
    adapter.supported_platform_checks = (lambda p: p == "linux",)

    sem = MagicMock()
    sem.locked.return_value = False
    adapter.get_op_semaphore.return_value = sem

    config = MagicMock()
    config.username = "root"
    config.jump_host = None
    config.jump_username = None
    config.prefix = name
    return adapter, config


def _make_real_local_config() -> MagicMock:
    """LocalSettings mock whose keys_dir.iterdir() returns [] (no private keys)."""
    cfg = MagicMock()
    keys_dir = MagicMock(spec=Path)
    keys_dir.iterdir.return_value = []
    cfg.keys_dir = keys_dir
    return cfg


def _make_real_remote_config() -> MagicMock:
    cfg = MagicMock()
    cfg.jump_host = None
    cfg.jump_username = None
    cfg.data_dir = PurePath("./data")
    cfg.engines_dir = PurePath("./data/engines")
    cfg.tasks_dir = PurePath("./data/tasks")
    return cfg


def _make_real_provisioner(
    machine_repository: Any,
    *,
    adapters: dict[str, Any] | None = None,
    configs: dict[str, Any] | None = None,
) -> tuple[CloudProvisionerImpl, MagicMock]:
    """Construct a real CloudProvisionerImpl wired to a FakeMachineRepository.

    Returns (provisioner, adapter) so callers can assert on adapter.delete_node
    (the adapter is a MagicMock whose delete_node is an AsyncMock). Params are
    Any-typed so the infra fakes satisfy CloudProvisionerImpl's concrete types.
    """
    adapter, config = _make_real_adapter_config()
    a = adapters or {"test": adapter}
    c = configs or {"test": config}
    engines = MagicMock()
    engines.filter.return_value = engines
    engines.get_platform_packages.return_value = []
    prov = CloudProvisionerImpl(
        adapters=a,
        configs=c,
        machine_repository=machine_repository,
        local_config=_make_real_local_config(),
        remote_config=_make_real_remote_config(),
        engines=engines,
    )
    return prov, adapter


def _make_engine() -> Engine:
    from yascheduler.domain import Engine

    return Engine(
        name="test_engine",
        spawn="echo {task_path}",
        check_cmd="echo ok",
        check_pname=None,
        input_files=("inp",),
        output_files=("OUTPUT",),
        platforms=("linux",),
    )


def _make_todo_task(task_id: int = 1) -> Task:
    from datetime import datetime

    return Task(
        task_id=TaskId(task_id),
        label="test",
        engine="test_engine",
        state=Todo(),
        webhook_url=None,
        webhook_custom_params={},
        extra={},
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )


def _node(n: int, *, enabled: bool = True) -> Node:
    return Node(
        node_id=NodeId(n),
        hostname=f"10.0.0.{n}",
        ncpus=None,
        enabled=enabled,
        username="root",
        port=22,
    )


def _tmp_node(n: int, *, cloud: str = "test") -> Node:
    """Build a tmp-node Node as allocate_task inserts it (pre-allocate).

    ``allocate`` receives this and overlays hostname/cloud/username via ``replace``
    after ``create_node`` returns the VM hostname. ``ncpus`` is ``None`` (cloud
    nodes discover at spawn via the session cache).
    """
    return Node(
        node_id=NodeId(n),
        hostname="",
        ncpus=None,
        enabled=False,
        cloud=cloud,
        username="root",
        port=22,
    )


def _make_engine_repo(engine: Engine) -> Any:
    repo = MagicMock()
    repo.get.return_value = engine
    repo.__contains__.return_value = True
    return repo


def _patch_ssh_key() -> Any:
    """Patch CloudProvisionerImpl._get_ssh_key so create_node gets a mock key
    without touching the filesystem (get_or_create_ssh_key does real IO).
    """
    return patch(
        "yascheduler.infra.cloud.manager.CloudProvisionerImpl._get_ssh_key",
        new=AsyncMock(return_value=MagicMock()),
    )


async def _allocate(
    task_id: TaskId,
    repo: Any,
    uow: Any,
    clouds: Any,
    start_cb: Any,
    *,
    engines: Any = None,
) -> Any:
    """Thin allocate_task wrapper. Fakes are Any-typed so the Protocol-typed
    parameters (uow_factory/repository/clouds) accept the concrete fakes without
    per-call type: ignore. Returns the allocate_task bool result.
    """
    return await allocate_task(
        task_id=task_id,
        engines=engines or _make_engine_repo(_make_engine()),
        uow_factory=lambda: uow,
        repository=repo,
        occupancy_checker=MagicMock(),
        clouds=clouds,
        start_task_on_machine=start_cb,
        tracker=AllocationTracker(),
        allocation_lock=asyncio.Lock(),
        remote_tasks_dir=PurePath("/remote/tasks"),
    )


# =============================================================================
# Fix A — DB-enabled gate in _find_free_machines
# =============================================================================


class TestFixA:
    """Fix A: a session is allocatable ONLY if its IP is DB-enabled."""

    async def test_setup_in_flight_session_invisible_to_allocator(self) -> None:
        """A connected session whose DB row is still enabled=FALSE (setup in flight) is excluded from free_sessions."""
        repo = FakeMachineRepository()
        await repo.connect(_node(1))  # setup-in-flight, DB not yet enabled
        nodes_store: dict[str, Node] = {}  # no enabled node for 10.0.0.1
        tasks_store: dict[TaskId, Task] = {TaskId(1): _make_todo_task()}
        uow = FakeUnitOfWork(tasks_store, nodes_store)

        start_cb: AsyncMock = AsyncMock(return_value=True)
        clouds = FakeCloudProvisioner(
            repo,
            lambda: uow,
            provider="aws",
            fail=False,
            select_provider_result=None,
        )

        result = await _allocate(TaskId(1), repo, uow, clouds, start_cb)

        assert result is False
        start_cb.assert_not_called()

    async def test_multiple_workers_do_not_pile_on(self) -> None:
        """Two concurrent allocate_task calls both exclude a setup-in-flight session."""
        repo = FakeMachineRepository()
        await repo.connect(_node(1))
        nodes_store: dict[str, Node] = {}
        tasks_store: dict[TaskId, Task] = {
            TaskId(1): _make_todo_task(),
            TaskId(2): _make_todo_task(2),
        }
        uow = FakeUnitOfWork(tasks_store, nodes_store)

        start_cb: AsyncMock = AsyncMock(return_value=True)

        def _make_clouds(tid: int) -> FakeCloudProvisioner:
            return FakeCloudProvisioner(
                repo,
                lambda: uow,
                provider="aws",
                new_ip=f"10.0.0.{tid + 50}",
                select_provider_result=None,
            )

        results = await asyncio.gather(
            _allocate(TaskId(1), repo, uow, _make_clouds(1), start_cb),
            _allocate(TaskId(2), repo, uow, _make_clouds(2), start_cb),
        )

        assert list(results) == [False, False]
        start_cb.assert_not_called()

    async def test_enabled_node_is_allocatable_after_setup(self) -> None:
        """A connected session whose DB row is enabled=TRUE IS allocated."""
        repo = FakeMachineRepository()
        await repo.connect(_node(1))
        nodes_store: dict[str, Node] = {"10.0.0.1": _node(1)}
        tasks_store: dict[TaskId, Task] = {TaskId(1): _make_todo_task()}
        uow = FakeUnitOfWork(tasks_store, nodes_store)

        captured_ip: list[str] = []

        async def _start(session: Any, engine: Any, task: Any, entries: Any) -> bool:
            captured_ip.append(session.hostname)
            return True

        result = await _allocate(TaskId(1), repo, uow, MagicMock(), _start)

        assert result is True
        assert captured_ip == ["10.0.0.1"]

    async def test_disabled_but_not_disconnected_excluded(self) -> None:
        """A connected session whose DB row was flipped to enabled=FALSE is excluded."""
        repo = FakeMachineRepository()
        await repo.connect(_node(1))
        nodes_store: dict[str, Node] = {"10.0.0.1": _node(1, enabled=False)}
        tasks_store: dict[TaskId, Task] = {TaskId(1): _make_todo_task()}
        uow = FakeUnitOfWork(tasks_store, nodes_store)

        start_cb: AsyncMock = AsyncMock(return_value=True)
        clouds = FakeCloudProvisioner(
            repo,
            lambda: uow,
            provider="aws",
            select_provider_result=None,
        )

        result = await _allocate(TaskId(1), repo, uow, clouds, start_cb)

        assert result is False
        start_cb.assert_not_called()


# =============================================================================
# Fix B — Setup-failure disconnects machine_repository session
# =============================================================================


class TestFixB:
    """Fix B: CloudProvisionerImpl.allocate disconnects the session before
    deleting the VM on the setup-failure path. Uses the real
    CloudProvisionerImpl so the disconnect call is genuinely exercised.
    """

    async def test_cloud_setup_error_disconnects_before_deleting_vm(self) -> None:
        """CloudSetupError from _setup_vm (cloud-init failure) triggers disconnect(ip) before delete_node, leaving _sessions empty."""
        repo = FakeMachineRepository(
            session_run_side_effect=MagicMock(
                exit_code=1,
                stdout="status: error",
                stderr="",
            ),
        )
        prov, adapter = _make_real_provisioner(repo)

        with (
            _patch_ssh_key(),
            pytest.raises(CloudSetupError, match="cloud-init failed"),
        ):
            await prov.allocate("test", _tmp_node(50))

        # Session was registered by _connect_to_vm, then disconnected on failure.
        assert repo.disconnect_calls == [NodeId(50)]
        assert len(repo._sessions) == 0
        assert repo.list_free() == []
        adapter.delete_node.assert_awaited_once()

    async def test_generic_exception_disconnects_before_deleting_vm(self) -> None:
        """A non-CloudSetupError escaping _setup_vm triggers the generic except block: disconnect(ip) before delete_node, then re-raise as CloudSetupError.

        _setup_vm normally wraps every failure into CloudSetupError, so this
        patches _setup_vm to raise a raw RuntimeError — exercising allocate's
        generic ``except Exception`` handler (defense-in-depth) and confirming
        it too disconnects before deleting the VM. Patching _setup_vm skips
        _connect_to_vm, so no session is registered (this shares the
        never-connected safe-no-op property); the real-``_setup_vm`` code
        path cannot produce a non-CloudSetupError after a session is
        registered because every failure there is wrapped.
        """
        repo = FakeMachineRepository()
        prov, adapter = _make_real_provisioner(repo)

        with (
            _patch_ssh_key(),
            patch.object(
                CloudProvisionerImpl,
                "_setup_vm",
                new=AsyncMock(side_effect=RuntimeError("unexpected boom")),
            ),
            pytest.raises(CloudSetupError, match="Setup node error"),
        ):
            await prov.allocate("test", _tmp_node(50))

        assert repo.disconnect_calls == [NodeId(50)]
        assert len(repo._sessions) == 0
        assert repo.list_free() == []
        adapter.delete_node.assert_awaited_once()

    async def test_disconnect_on_never_connected_ip_is_safe_noop(self) -> None:
        """When _connect_to_vm itself fails (session never registered), allocate's except still calls disconnect(ip) safely and the VM is deleted."""
        # connect_raises makes _connect_to_vm raise before registering a session.
        repo = FakeMachineRepository(connect_raises=OSError("ssh connect refused"))
        prov, adapter = _make_real_provisioner(repo)

        with (
            _patch_ssh_key(),
            pytest.raises(CloudSetupError, match="SSH connect to"),
        ):
            await prov.allocate("test", _tmp_node(50))

        # disconnect was called on the never-registered IP without raising.
        assert repo.disconnect_calls == [NodeId(50)]
        assert len(repo._sessions) == 0
        adapter.delete_node.assert_awaited_once()

    async def test_success_path_does_not_disconnect(self) -> None:
        """On a successful _setup_vm, the session stays registered — the orchestrator reuses the connection."""
        repo = FakeMachineRepository(
            session_run_side_effect=MagicMock(exit_code=0, stdout="", stderr=""),
            session_get_cpu_cores_return=8,
        )
        prov, adapter = _make_real_provisioner(repo)

        with _patch_ssh_key():
            node = await prov.allocate("test", _tmp_node(1))

        assert node.hostname == "[IP]"
        assert node.ncpus is None  # No ncpus write-back — cloud nodes discover at spawn
        assert repo.disconnect_calls == []
        assert "[IP]" in repo._sessions
        adapter.delete_node.assert_not_awaited()

    async def test_disconnect_failure_does_not_skip_vm_deletion(self) -> None:
        """If machine_repository.disconnect raises during teardown (e.g. wait_closed on a broken transport), allocate swallows it, logs, and STILL deletes the VM — no billable orphan.

        SSHMachineSession._close can raise (await conn.wait_closed()); without the
        best-effort wrap that raise would skip delete_node and orphan the VM.
        """
        repo = FakeMachineRepository(
            disconnect_raises=RuntimeError("wait_closed failed"),
            session_run_side_effect=MagicMock(
                exit_code=1,
                stdout="status: error",
                stderr="",
            ),
        )
        prov, adapter = _make_real_provisioner(repo)

        with (
            _patch_ssh_key(),
            pytest.raises(CloudSetupError, match="cloud-init failed"),
        ):
            await prov.allocate("test", _tmp_node(1))

        # delete_node STILL ran despite disconnect raising — no VM orphan.
        adapter.delete_node.assert_awaited_once()


# =============================================================================
# Fix C — Per-session loop isolation in _allocate_free_machine
# =============================================================================


class TestFixC:
    """Fix C: a single session failure does not abort the free-machine loop."""

    async def test_stale_session_failure_does_not_abort_loop(self) -> None:
        """One session that raises is skipped (logged), the next healthy enabled session gets the task."""
        repo = FakeMachineRepository()
        await repo.connect(_node(1))  # stale
        await repo.connect(_node(2))  # healthy
        nodes_store: dict[str, Node] = {"10.0.0.1": _node(1), "10.0.0.2": _node(2)}
        tasks_store: dict[TaskId, Task] = {TaskId(1): _make_todo_task()}
        uow = FakeUnitOfWork(tasks_store, nodes_store)

        call_log: list[str] = []

        async def _start(session: Any, engine: Any, task: Any, entries: Any) -> bool:
            call_log.append(session.hostname)
            if session.hostname == "10.0.0.1":
                raise RuntimeError("stale session channel closed")
            return True

        result = await _allocate(TaskId(1), repo, uow, MagicMock(), _start)

        assert result is True
        # Both sessions were attempted; the first failed but the loop continued.
        assert call_log == ["10.0.0.1", "10.0.0.2"]
        # Fix C's except block must NOT disconnect (per design D3) — only the
        # monitor owns session lifecycle. Regression-guards a stray disconnect.
        assert repo.disconnect_calls == []

    async def test_cloud_branch_reached_when_all_free_sessions_fail(self) -> None:
        """When every free session fails, the cloud branch is reached (fake CloudProvisioner.allocate is invoked)."""
        repo = FakeMachineRepository()
        await repo.connect(_node(1))  # stale
        nodes_store: dict[str, Node] = {"10.0.0.1": _node(1)}
        tasks_store: dict[TaskId, Task] = {TaskId(1): _make_todo_task()}
        uow = FakeUnitOfWork(tasks_store, nodes_store)

        async def _start_fails(
            session: Any, engine: Any, task: Any, entries: Any
        ) -> bool:
            raise RuntimeError("unreachable session")

        clouds = FakeCloudProvisioner(
            repo,
            lambda: uow,
            provider="aws",
            new_ip="10.0.0.99",
            fail=False,
        )

        result = await _allocate(TaskId(1), repo, uow, clouds, _start_fails)

        # Cloud branch was reached: the fake CloudProvisioner.allocate ran.
        assert clouds.allocate_calls == ["aws"]
        # allocate_task returns False (cloud node provisioned but not started
        # this tick — it becomes a free machine on the next tick).
        assert result is False


# =============================================================================
# Fix D — stdout in cloud-init error message
# =============================================================================


class TestFixD:
    """Fix D: cloud-init failure message includes stdout; timeout message unchanged."""

    async def test_cloud_init_error_contains_stdout(self) -> None:
        """cloud-init exit_code=2 with stdout='status: error' yields a CloudSetupError whose message contains stdout=status: error."""
        repo = FakeMachineRepository(
            session_run_side_effect=MagicMock(
                exit_code=1,
                stdout="status: error",
                stderr="",
            ),
        )
        prov, _adapter = _make_real_provisioner(repo)

        with _patch_ssh_key(), pytest.raises(CloudSetupError) as exc_info:
            await prov.allocate("test", _tmp_node(1))

        msg = str(exc_info.value)
        assert "stdout=status: error" in msg
        assert "stderr=" in msg

    async def test_cloud_init_timeout_message_unchanged(self) -> None:
        """asyncio.TimeoutError in the CLOUD_INIT block yields the unchanged 'timed out' message (does not read result.stdout/stderr)."""

        # AsyncMock side_effect treats a bare coroutine as an iterator, not a
        # callable — wrap in an async function so session.run(cmd) blocks for
        # 60s and asyncio.wait_for cancels at create_node_timeout.
        async def _slow_run(cmd: str) -> Any:
            await asyncio.sleep(60)

        repo = FakeMachineRepository(session_run_side_effect=_slow_run)
        prov, adapter = _make_real_provisioner(repo)
        # adapter is the same object as prov.adapters["test"] (adapters=None).
        adapter.create_node_timeout = 0.05

        with _patch_ssh_key(), pytest.raises(CloudSetupError) as exc_info:
            await prov.allocate("test", _tmp_node(1))

        msg = str(exc_info.value)
        assert "timed out" in msg
        assert "[IP]" in msg
        assert "0.05s" in msg
        assert "stdout=" not in msg
