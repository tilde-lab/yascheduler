"""Domain port interfaces: abstract contracts for persistence, machine collection/sessions, and cloud provisioning."""
# region MODULE_CONTRACT
# PURPOSE: Decouple use cases from infrastructure — abstract persistence, SSH, and cloud-provisioning concerns behind Protocol contracts so the application layer never imports adapters directly.
# SCOPE:
# - Protocol ports — TaskRepository, NodeRepository, MachineRepository, MachineSession, CloudConfig, CloudProvisioner.
# - NOT: concrete implementations (infra.*) or use-case orchestration (application.*).
# INVARIANTS: All ports are typing.Protocol; methods are async unless noted; decorated with @runtime_checkable.
# KEYWORDS: port, protocol, repository, persistence, machine session, cloud provisioner, CloudConfig, MachineRepository
# endregion MODULE_CONTRACT

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Awaitable,
        Callable,
        Coroutine,
        Mapping,
        Sequence,
    )
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path, PurePath
    from re import Pattern

    from asyncssh.sftp import SFTPClient

    from .engine import EngineRepository
    from .model import (
        AnyTask,
        ConnectedMachine,
        NewNode,
        NewTask,
        Node,
        NodeId,
        ProcessResult,
        RunningTask,
        TaskId,
        TaskStatus,
        TodoTask,
    )
    from .task_inputs import TaskInputEntry, TaskInputSnapshot

__all__ = [
    "CloudConfig",
    "CloudProvisioner",
    "MachineRepository",
    "MachineSession",
    "NodeRepository",
    "TaskRepository",
]


# region CLASS_TaskRepository
# PURPOSE: Enable use cases to persist and query tasks without depending on SQL — the repository interface defines end-to-end identity management from NewTask (pre-identity) to Task (DB-generated identity).
# INVARIANTS: All lookups and mutators keyed on TaskId; @runtime_checkable.
@runtime_checkable
class TaskRepository(Protocol):
    """Async port for task persistence."""

    async def get(self, task_id: TaskId) -> AnyTask | None:
        """Return a task by ``task_id``, or ``None``."""
        ...

    async def get_running(self, task_id: TaskId) -> RunningTask | None:
        """Return a task by ``task_id`` if RUNNING, else ``None`` (absent or other status)."""
        ...

    async def get_todo(self, task_id: TaskId) -> TodoTask | None:
        """Return a task by ``task_id`` if TO_DO, else ``None`` (absent or other status)."""
        ...

    async def save(
        self, task: AnyTask, *, expected_status: TaskStatus | None = None
    ) -> None:
        """Persist changes to an existing task aggregate."""
        ...

    async def list_by_status(
        self,
        statuses: set[TaskStatus],
        *,
        limit: int | None = None,
    ) -> list[AnyTask]:
        """Return tasks matching the given statuses with optional limit."""
        ...

    async def list_running(self, *, limit: int | None = None) -> list[RunningTask]:
        """Return tasks whose state is RUNNING (every element's state is Running)."""
        ...

    async def list_todo(self, *, limit: int | None = None) -> list[TodoTask]:
        """Return tasks whose state is TO_DO (every element's state is Todo)."""
        ...

    async def insert(self, new_task: NewTask) -> TodoTask:
        """Insert a new task and return it with generated identity (state is Todo)."""
        ...

    async def list_by_jobs(self, job_ids: list[TaskId]) -> list[AnyTask]:
        """Return tasks matching the given job IDs."""
        ...

    # region METHOD_iter_input_entries
    # PURPOSE: Yield one task's input snapshot entries one at a time in path order so deployment streams the tree without holding its bytes in memory.
    # ENSURES: Entries arrive ordered so a directory precedes everything beneath it; iteration ends after the last entry; the record itself never carries these bytes.
    def iter_input_entries(self, task_id: TaskId) -> AsyncIterator[TaskInputEntry]:
        """Return an async iterator over one task's snapshot entries in path order."""
        ...

    # endregion METHOD_iter_input_entries

    # region METHOD_list_input_snapshots
    # PURPOSE: Return complete input snapshots for a batch of tasks in one read so the client query projects exact bytes without loading them on every task read.
    # ENSURES: Every returned snapshot is complete — every entry, its kind, its exact file bytes, its empty directories; tasks with no entries are absent from the mapping.
    async def list_input_snapshots(
        self, task_ids: Sequence[TaskId]
    ) -> Mapping[TaskId, TaskInputSnapshot]:
        """Return complete input snapshots keyed by task id for the given ids."""
        ...

    # endregion METHOD_list_input_snapshots

    async def list_ids_by_node_id_and_status(
        self,
        node_id: NodeId,
        status: TaskStatus,
    ) -> list[TaskId]:
        """Return task IDs for a given node and status."""
        ...

    async def count_by_status(self) -> Mapping[TaskStatus, int]:
        """Return task counts grouped by status."""
        ...


# endregion CLASS_TaskRepository


# region CLASS_NodeRepository
# PURPOSE: Enable use cases to manage node lifecycle without coupling to the SQL layer — the protocol abstracts all keying on NodeId.
# INVARIANTS: Lookups and mutators keyed on NodeId; list_all ordered by node_id ascending; @runtime_checkable.
@runtime_checkable
class NodeRepository(Protocol):
    """Async port for node persistence."""

    async def get_by_id(self, node_id: NodeId) -> Node | None:
        """Return a node by ``node_id``, or ``None``."""
        ...

    async def get_by_ids(self, node_ids: list[NodeId]) -> dict[NodeId, Node]:
        """Return a dict of nodes keyed by ``node_id`` for the given IDs."""
        ...

    async def list_enabled(self) -> list[Node]:
        """Return all enabled nodes."""
        ...

    async def list_disabled(self) -> list[Node]:
        """Return all disabled nodes."""
        ...

    async def insert(self, new_node: NewNode) -> Node:
        """Insert a new node and return it with generated identity."""
        ...

    async def update(self, node: Node) -> None:
        """Replace the underlying machine state."""
        ...

    async def enable(self, node_id: NodeId) -> None:
        """Mark a node as enabled."""
        ...

    async def disable(self, node_id: NodeId) -> None:
        """Mark a node as disabled."""
        ...

    async def remove(self, node_id: NodeId) -> None:
        """Remove a node by its ID."""
        ...

    async def list_all(self) -> list[Node]:
        """Return all nodes."""
        ...

    async def count_by_status(self) -> Mapping[bool, int]:
        """Return task counts grouped by status."""
        ...


# endregion CLASS_NodeRepository


# region CLASS_CloudConfig
# PURPOSE: Enable cloud-provider substitutability via structural typing — any DTO exposing the required field set satisfies the contract without needing a shared base class or inheritance hierarchy.
# INVARIANTS: Satisfied structurally (PEP 544) by every ConfigCloud* DTO in infra.cloud.cloud_configs.
@runtime_checkable
class CloudConfig(Protocol):
    """Cloud provider config contract — minimal surface application consumers read.

    Satisfied by every `ConfigCloud*` DTO in `infra/cloud/cloud_configs.py` —
    the DTOs inherit this Protocol explicitly (typing aid); a DTO outside the
    inheritance tree still satisfies it structurally (PEP 544).
    """

    prefix: str
    max_nodes: int
    idle_tolerance: int
    connect_grace: int
    username: str
    jump_username: str | None
    jump_host: str | None
    jump_port: int
    label: str


# endregion CLASS_CloudConfig


# region CLASS_MachineSession
# PURPOSE: Give callers a stable per-connection handle for SSH operations, state transitions, and monitor lifecycle — decoupled from collection pooling and teardown so a session can be passed as a single argument through the entire call chain.
# INVARIANTS: Methods are async unless noted; implementations own connection teardown.
# RATIONALE:
# - Q: Why does the Protocol split collection lifecycle (MachineRepository) from the per-session handle (MachineSession)?
#   A: So callers operate on a stable per-call handle while the repository owns connection pooling and teardown; the former facade MachineOperations was removed in favor of invoking collaborators directly on the session every caller already holds.
@runtime_checkable
class MachineSession(Protocol):
    """Connected-machine entity handle — identity, state transitions.

    connect-time config, adapter-derived accessors, base SSH primitives,
    and the per-session monitor mechanism.

    Does NOT cover collection lifecycle, queries, or repository keying —
    those are MachineRepository. Does NOT declare _close (private to the
    concrete class; invoked only by SSHMachineRepository.disconnect).

    The Protocol is Engine-agnostic: install_monitor is generic over
    Callable[[], Awaitable[bool]] and Callable[[], None].
    """

    # ---- Domain face ----
    @property
    def hostname(self) -> str:
        """Remote machine hostname (immutable after construction)."""
        ...

    @property
    def machine(self) -> ConnectedMachine:
        """Connected machine runtime state."""
        ...

    @property
    def is_closed(self) -> bool:
        """``True`` when the underlying connection is closed."""
        ...

    def occupy(self) -> None:
        """Transition the underlying machine to ``BUSY``."""
        ...

    def release(self) -> None:
        """Transition the underlying machine to ``FREE``."""
        ...

    def update(self, machine: ConnectedMachine) -> None:
        """Replace the underlying machine state."""
        ...

    # ---- Connect-time config (read-only) ----
    @property
    def adapter(self) -> Any:  # noqa: ANN401
        """Platform-specific remote machine adapter (resolved at connect time)."""
        ...

    @property
    def platforms(self) -> Sequence[str]:
        """Platform tags resolved at connect time."""
        ...

    @property
    def data_dir(self) -> PurePath:
        """Remote data directory path configured at connect time."""
        ...

    @property
    def engines_dir(self) -> PurePath:
        """Remote engines directory path configured at connect time."""
        ...

    @property
    def tasks_dir(self) -> PurePath:
        """Remote tasks directory path configured at connect time."""
        ...

    # ---- Adapter-derived accessors (read-only) ----
    @property
    def path(self) -> type[PurePath]:
        """``PurePath`` subclass matching the remote OS path semantics."""
        ...

    @property
    def quote(self) -> Callable[[str], str]:
        """Shell-quoting callable matching the remote OS syntax."""
        ...

    # ---- Base primitives ----
    async def run(self, cmd: str) -> ProcessResult:
        """Run a command on the remote machine and wait for exit."""
        ...

    def run_full(self, cmd: str) -> Coroutine[Any, Any, Any]:
        """Run a command and return the full ``SSHCompletedProcess``."""
        ...

    async def run_bg(self, cmd: str, *, cwd: str | None = None) -> None:
        """Create a background process on the remote machine."""
        ...

    async def upload(self, local: Path, remote: str) -> None:
        """Upload a local file to a remote path."""
        ...

    def open_sftp(self) -> AbstractAsyncContextManager[SFTPClient]:
        """Open an SFTP client session."""
        ...

    def get_cpu_cores(self) -> Coroutine[Any, Any, int]:
        """Read the number of CPU cores from the remote machine."""
        ...

    async def setup_node(self, engines: EngineRepository) -> None:
        """Install engines and dependencies on the remote machine."""
        ...

    def pgrep(
        self,
        pattern: str | Pattern[str],
        *,
        full: bool = True,
    ) -> AsyncGenerator[Any, None]:
        """Yield remote processes matching a name or command pattern."""
        ...

    def list_processes(self) -> AsyncGenerator[Any, None]:
        """Yield all remote processes with PID, name, and command line."""
        ...

    # ---- Monitor mechanism (generic, Engine-agnostic) ----
    def install_monitor(
        self,
        *,
        interval: float,
        check_factory: Callable[[], Awaitable[bool]],
        on_free: Callable[[], None],
    ) -> None:
        """Install a periodic check on the remote machine to detect task completion."""
        ...

    def cancel_monitor(self) -> None:
        """Cancel the periodic occupancy monitor."""
        ...


# endregion CLASS_MachineSession


# region CLASS_MachineRepository
# PURPOSE: Decouple use cases from the SSH session collection — the repository owns connect/disconnect lifecycle and queries while callers receive a stable MachineSession handle per connection.
@runtime_checkable
class MachineRepository(Protocol):
    """Connected-machine collection — lifecycle and queries."""

    # ---- Collection lifecycle ----
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
        """Open a session to ``node``."""
        ...

    async def disconnect(self, node_id: NodeId) -> None:
        """Close and unregister the session for ``node_id``."""
        ...

    async def disconnect_all(self) -> None:
        """Close and unregister all sessions."""
        ...

    def list_free(self, platforms: list[str] | None) -> list[MachineSession]:
        """Return free sessions, optionally filtering by platform."""
        ...

    def list_connected(self) -> list[MachineSession]:
        """Return all currently connected sessions."""
        ...

    def get_session(self, node_id: NodeId) -> MachineSession | None:
        """Return the session for ``node_id``, or ``None``."""
        ...

    def contains(self, node_id: NodeId) -> bool:
        """Return ``True`` if ``node_id`` has an active session."""
        ...

    def __len__(self) -> int: ...

    def __contains__(self, node_id: NodeId) -> bool: ...


# endregion CLASS_MachineRepository


# region CLASS_CloudProvisioner
# PURPOSE: Decouple the orchestrator from provider-specific cloud SDKs — the port abstracts allocate/deallocate/select_provider so capacity management stays adapter-agnostic.
# INVARIANTS: stop shuts down all provider sessions.
# RATIONALE:
# - Q: Why does select_provider return a bare str instead of a ProviderSelection value object?
#   A: The application treats it as an opaque identity and passes it back unchanged to allocate — a dedicated wrapper would add ceremony without behavioral benefit.
# - Q: Why is capacity() not on the port?
#   A: Capacity counting is an orchestrator/use-case concern, not an adapter concern — the port only needs select_provider's binary yes/no answer.
@runtime_checkable
class CloudProvisioner(Protocol):
    """Cloud VM provisioning port. ``allocate``/``deallocate`` are async.

    ``select_provider`` is sync (returns ``None`` when no capacity or throttled).
    ``deallocate`` reads ``node.cloud``/``node.hostname`` and no-ops on ``cloud is None`` or ``external_id is None``.
    """

    async def allocate(self, provider: str, node: Node) -> Node:
        """Provision a cloud node for the given provider and tmp-node."""
        ...

    async def deallocate(self, node: Node) -> None:
        """Delete a cloud VM identified by ``node.cloud``."""
        ...

    def select_provider(
        self,
        platforms: list[str],
        current_counts: dict[str, int],
    ) -> str | None:
        """Return a provider name with available capacity, or ``None``."""
        ...

    async def stop(self) -> None:
        """Shut down all cloud provider sessions."""
        ...


# endregion CLASS_CloudProvisioner
