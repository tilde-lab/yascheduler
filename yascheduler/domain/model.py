"""Domain entities."""
# region MODULE_CONTRACT
# PURPOSE: Encode the scheduler's core vocabulary — task and node entities, lifecycle state, and value objects — as immutable data with atomic transitions, so business rules are enforced in one place and shared safely across async components.
# SCOPE:
# - Lifecycle enums (TaskStatus, MachineState, NodeStatus) and ProcessResult; identity value objects TaskId/NodeId; pre/post-persistence records NewTask/Task, NewNode/Node; the ConnectedMachine runtime entity.
# - NOT: persistence ports (domain.ports), events (domain.events), or exceptions (domain.exceptions).
# INVARIANTS: Entities are frozen; every state change happens through transition methods that validate the source state, mutate via replace, and append the matching DomainEvent to events.
# RATIONALE:
# - Q: Why dedicated value objects (TaskId/NodeId) instead of bare int?
#   A: Type safety — callers must unwrap .value at external boundaries (pg8000, JSON, argparse), preventing accidental mixing of ids with plain ints.
# - Q: Why frozen dataclasses with replace-based transitions?
#   A: Async-safe sharing — a task aggregate can be handed to concurrent coroutines without locks, since every change produces a new instance.
# KEYWORDS: task, node, machine, lifecycle, value object, entity, allocation, ConnectedMachine
# endregion MODULE_CONTRACT

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum, IntEnum, unique
from typing import TYPE_CHECKING, ClassVar, Generic, TypeVar, Union, cast

from yascheduler.shared import StrEnum, TypeAlias, TypeIs

if TYPE_CHECKING:
    from .task_inputs import TaskInputSnapshot

from .engine import (
    Deploy,
    Engine,
    EngineRepository,
    LocalArchiveDeploy,
    LocalFilesDeploy,
    RemoteArchiveDeploy,
)
from .events import (
    DomainEvent,
    TaskAbandoned,
    TaskAllocated,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
)
from .exceptions import (
    MachineBusyError,
    TaskNotRunningError,
    TaskNotTodoError,
)

# Module public API: own entities/value objects + engine re-exports for the canonical import path.
__all__ = [
    "AnyTask",
    "ConnectedMachine",
    "Deploy",
    "Done",
    "DoneTask",
    "Engine",
    "EngineRepository",
    "LocalArchiveDeploy",
    "LocalFilesDeploy",
    "MachineState",
    "NewNode",
    "NewTask",
    "Node",
    "NodeId",
    "NodeStatus",
    "ProcessResult",
    "RemoteArchiveDeploy",
    "Running",
    "RunningTask",
    "Task",
    "TaskId",
    "TaskState",
    "TaskStatus",
    "Todo",
    "TodoTask",
    "allocated_node_id_of",
    "error_of",
    "is_done",
    "is_running",
    "is_todo",
    "materialize_task",
    "remote_folder_of",
]


@unique
class TaskStatus(IntEnum):
    """Task lifecycle states: TO_DO, RUNNING, DONE."""

    TO_DO = 0
    RUNNING = 1
    DONE = 2


@unique
class MachineState(Enum):
    """Machine occupancy states: FREE, BUSY."""

    FREE = 1
    BUSY = 2


@unique
class NodeStatus(StrEnum):
    """Node lifecycle states: OTHER (placeholder for future states)."""

    OTHER = "OTHER"


@dataclass(frozen=True)
class ProcessResult:
    """Exit code and captured output from a remote process execution."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


# region CLASS_TaskId
# PURPOSE: Wrap the database task id in a dedicated value object so transport/serialization boundaries must unwrap it explicitly, preventing accidental mixing with bare ints.
# INVARIANTS: value > 0; frozen and hashable; not equal to a bare int.
# RATIONALE:
# - Q: Why a dedicated type instead of bare int, NewType('TaskId', int), or int subclass?
#   A: A frozen dataclass wrapper enforces explicit .value unwrapping at every external boundary, preventing accidental id/int mixing that NewType wouldn't catch at runtime and an int subclass wouldn't prevent.
@dataclass(frozen=True)
class TaskId:
    """Task primary-key value object.

    Wraps a single ``value: int`` (the DB-generated ``task_id``). ``__post_init__``
    enforces ``value > 0`` (SERIAL starts at 1, so a non-positive value indicates a
    bug). ``__str__`` returns the bare integer string so CLI rendering and logging
    produce ``5``, not ``TaskId(value=5)``. Frozen → hashable → usable as a dict key.
    Intentionally NOT equal to a bare ``int`` (``TaskId(5) == 5`` is ``False``) — the
    type-safety point of a dedicated value object; callers must unwrap ``.value``
    explicitly at external boundaries (pg8000 params, JSON, argparse).
    """

    value: int

    def __post_init__(self) -> None:
        if self.value <= 0:
            msg = f"TaskId must be > 0, got {self.value}"
            raise ValueError(msg)

    def __str__(self) -> str:
        return str(self.value)


# endregion CLASS_TaskId


# region CONST_TYPED_METADATA_KEYS
# PURPOSE: Name the metadata keys that map onto typed task fields rather than arbitrary user payload, so every layer separating "unrelated extra" from task fields applies one classification (input files stay out of extra entirely).
# INVARIANTS: Exact key strings shared by the submission boundary normalization and the submit use case; engine input declarations never consume these keys as legacy input values.
TYPED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "engine",
        "remote_folder",
        "local_folder",
        "webhook_url",
        "webhook_custom_params",
        "error",
    }
)

# endregion CONST_TYPED_METADATA_KEYS


# region CLASS_NewTask
# PURPOSE: Carry the fields needed to insert a task before it has a database id, with no lifecycle methods — a pure data carrier.
# INVARIANTS: No identity (task_id absent); no events, status, allocated_node_id, remote_folder, error, created_at/updated_at — these are all supplied post-insert by the DB or by Task transition methods; input_entries is the task's immutable input snapshot, empty until the task-input-snapshots change wires capture through submission; output_selections is the task-owned acceptance-time output contract, with the empty tuple meaning no record.
@dataclass(frozen=True)
class NewTask:
    """Pre-persistence task record — no identity yet.

    A caller builds a ``NewTask`` to prepare a task for insertion.
    It is a pure data carrier with no lifecycle methods.
    ``input_entries`` carries the task-owned input snapshot; the empty tuple
    is the not-yet-captured placeholder, replaced by the submission boundary.
    ``output_selections`` carries the task-owned output contract recorded at
    acceptance; the empty tuple means no record and persists as SQL NULL.
    """

    engine: str
    label: str = ""
    local_folder: str | None = None
    webhook_url: str | None = None
    webhook_custom_params: dict[str, object] = field(default_factory=dict)
    extra: dict[str, object] = field(default_factory=dict)
    input_entries: TaskInputSnapshot = ()
    output_selections: tuple[str, ...] = ()


# endregion CLASS_NewTask


# region CLASS_TaskState
# PURPOSE: Carry the per-status fields a task holds at each lifecycle state, so the presence of a state object IS the carrier of status and each state declares exactly the fields its DB CHECK branch permits.
# INVARIANTS: ``status`` ClassVar binds the state type to its TaskStatus.
# RATIONALE:
# - Q: Why one closed union instead of flat Optionals on Task?
#   A: The DB CHECK correlates fields per status; the closed union mirrors that correlation structurally, so a single ``isinstance(task.state, Running)`` narrows all correlated fields at once instead of re-asserting what the DB guarantees.
@dataclass(frozen=True)
class Todo:
    """TO_DO state: only the CHECK-unconstrained ``remote_folder``.

    ``allocated_node_id`` and ``error`` are always NULL under the CHECK, so they
    are absent from the type.
    """

    status: ClassVar[TaskStatus] = TaskStatus.TO_DO
    remote_folder: str | None = None


@dataclass(frozen=True)
class Running:
    """RUNNING state: both allocation fields non-Optional by construction.

    Encodes the CHECK guarantee (allocated_node_id and remote_folder required,
    error forbidden) structurally instead of via runtime asserts at every read.
    """

    status: ClassVar[TaskStatus] = TaskStatus.RUNNING
    allocated_node_id: NodeId
    remote_folder: str


@dataclass(frozen=True)
class Done:
    """DONE state: error/allocated_node_id/remote_folder independent Optionals.

    DONE is CHECK-unconstrained; the three CHECK fields vary independently.
    """

    status: ClassVar[TaskStatus] = TaskStatus.DONE
    error: str | None = None
    allocated_node_id: NodeId | None = None
    remote_folder: str | None = None


TaskState = Union[Todo, Running, Done]

# Covariant TypeVar: sound because Task is frozen (like tuple[T_co, ...]); a
# wider-typed reference cannot mutate the state through the container.
S_co = TypeVar("S_co", bound=TaskState, covariant=True)
# Invariant TypeVar for the _advance retag target.
S2 = TypeVar("S2", bound=TaskState)

# endregion CLASS_TaskState


# region CLASS_Task
# PURPOSE: Represent a persisted task as an immutable aggregate whose status only changes through atomic transition methods, so invariants and event emission stay centralized.
# INVARIANTS: Every transition validates the source state via isinstance(self.state, ...), returns a new Task via the _advance retag-cast helper, and appends the matching DomainEvent to events; the state value object carries the CHECK-correlated fields; input_entries is the task-owned immutable input snapshot and output_selections is the acceptance-time output contract, both surviving every transition untouched via replace.
@dataclass(frozen=True)
class Task(Generic[S_co]):
    """Post-persistence task entity, parameterized by its lifecycle state.

    ``Task[S_co]`` carries its state on its type, not only on its ``state``
    field. Each transition declares the state it is legal from as its receiver
    type, so a transition whose declared source state does not match the task's
    state is a static error under the project's type checker.

    Every state change happens through one of the transition methods, which
    validate the source state, build the result state object, and route through
    :meth:`_advance` to append the matching :class:`DomainEvent` and return the
    retagged task.

    ``created_at``/``updated_at`` default to ``datetime.now()`` mirroring the
    DB schema (``DEFAULT NOW()``).
    The DB always overrides them via RETURNING on insert and on every read.
    """

    task_id: TaskId
    engine: str
    state: S_co
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    label: str = ""
    local_folder: str | None = None
    webhook_url: str | None = None
    webhook_custom_params: dict[str, object] = field(default_factory=dict)
    extra: dict[str, object] = field(default_factory=dict)
    input_entries: TaskInputSnapshot = ()
    events: tuple[DomainEvent, ...] = field(default=(), repr=True)
    output_selections: tuple[str, ...] = ()

    @property
    def status(self) -> TaskStatus:
        """Derive TaskStatus from the state object's ClassVar binding."""
        return self.state.status

    # region METHOD_advance
    # PURPOSE: Hold the single retag-cast in the domain so every transition routes through it, keeping the unchecked cast localized and documented.
    # RATIONALE:
    # - Q: Why cast on self (input), not on the result?
    #   A: The dataclasses.replace plugin validates kwargs against the pre-cast type; casting the result would leave ``state=Running(...)`` rejected on a ``Task[Todo]``. The retag-cast is the price of typestate on a frozen dataclass; a wrong retag surfaces as a static self-type error at the call site.
    def _advance(
        self: Task[S_co],
        state: S2,
        event: DomainEvent,
        *,
        local_folder: str | None = None,
    ) -> Task[S2]:
        if local_folder is None:
            return replace(
                cast("Task[S2]", self), state=state, events=(*self.events, event)
            )
        return replace(
            cast("Task[S2]", self),
            state=state,
            events=(*self.events, event),
            local_folder=local_folder,
        )

    # endregion METHOD_advance

    # region METHOD_run
    # PURPOSE: Bind the task to a node and begin remote execution, recording the allocation as an event.
    # REQUIRES: state is Todo.
    def run(self: Task[Todo], node_id: NodeId, remote_folder: str) -> Task[Running]:
        """Transition TO_DO→RUNNING, binding the node and setting remote_folder."""
        if not isinstance(self.state, Todo):
            raise TaskNotTodoError(self.task_id)
        event = TaskAllocated(
            task_id=self.task_id,
            webhook_url=self.webhook_url,
            webhook_custom_params=self.webhook_custom_params,
            node_id=node_id,
            engine_name=self.engine,
        )
        return self._advance(
            Running(allocated_node_id=node_id, remote_folder=remote_folder), event
        )

    # endregion METHOD_run

    # region METHOD_reject
    # PURPOSE: Terminate a not-yet-started task with a reason (e.g. unsupported engine) without ever running it.
    # REQUIRES: state is Todo.
    def reject(self: Task[Todo], reason: str) -> Task[Done]:
        """Transition TO_DO→DONE with an error reason (e.g. unsupported engine)."""
        if not isinstance(self.state, Todo):
            raise TaskNotTodoError(self.task_id)
        event = TaskFailed(
            task_id=self.task_id,
            webhook_url=self.webhook_url,
            webhook_custom_params=self.webhook_custom_params,
            reason=reason,
        )
        state = Done(
            error=reason, allocated_node_id=None, remote_folder=self.state.remote_folder
        )
        return self._advance(state, event)

    # endregion METHOD_reject

    # region METHOD_complete
    # PURPOSE: Finalize a running task on success, capturing output folders and emitting TaskCompleted.
    # REQUIRES: state is Running.
    def complete(
        self: Task[Running], *, local_folder: str, remote_folder: str
    ) -> Task[Done]:
        """Transition RUNNING→DONE on successful completion, setting folders."""
        if not isinstance(self.state, Running):
            raise TaskNotRunningError(self.task_id)
        event = TaskCompleted(
            task_id=self.task_id,
            webhook_url=self.webhook_url,
            webhook_custom_params=self.webhook_custom_params,
            local_folder=local_folder,
        )
        state = Done(
            error=None,
            allocated_node_id=self.state.allocated_node_id,
            remote_folder=remote_folder,
        )
        return self._advance(state, event, local_folder=local_folder)

    # endregion METHOD_complete

    # region METHOD_fail
    # PURPOSE: End a running task on failure, recording the reason and whatever partial output was downloaded.
    # REQUIRES: state is Running.
    def fail(
        self: Task[Running], reason: str, *, local_folder: str, remote_folder: str
    ) -> Task[Done]:
        """Transition RUNNING→DONE on failure, setting error and partial folders."""
        if not isinstance(self.state, Running):
            raise TaskNotRunningError(self.task_id)
        event = TaskFailed(
            task_id=self.task_id,
            webhook_url=self.webhook_url,
            webhook_custom_params=self.webhook_custom_params,
            reason=reason,
        )
        state = Done(
            error=reason,
            allocated_node_id=self.state.allocated_node_id,
            remote_folder=remote_folder,
        )
        return self._advance(state, event, local_folder=local_folder)

    # endregion METHOD_fail

    # region METHOD_abandon
    # PURPOSE: Stop a running task whose node disappeared, emitting TaskAbandoned. A RUNNING task always carries an allocation, so the event is unconditional and the node id is read from state.
    # REQUIRES: state is Running.
    def abandon(self: Task[Running], error: str = "node is gone") -> Task[Done]:
        """Transition RUNNING→DONE when the node disappeared."""
        if not isinstance(self.state, Running):
            raise TaskNotRunningError(self.task_id)
        event = TaskAbandoned(
            task_id=self.task_id,
            webhook_url=self.webhook_url,
            webhook_custom_params=self.webhook_custom_params,
            node_id=self.state.allocated_node_id,
        )
        state = Done(
            error=error,
            allocated_node_id=self.state.allocated_node_id,
            remote_folder=self.state.remote_folder,
        )
        return self._advance(state, event)

    # endregion METHOD_abandon


# endregion CLASS_Task


# State-parameterized task aliases. ``AnyTask`` is the explicit union (not
# ``Task[TaskState]``) so the ``TypeIs`` narrowing helpers stay sound under
# the covariant ``S_co``.
TodoTask: TypeAlias = Task[Todo]
RunningTask: TypeAlias = Task[Running]
DoneTask: TypeAlias = Task[Done]
AnyTask: TypeAlias = Union[Task[Todo], Task[Running], Task[Done]]


# region FUNC_any_status_helpers
# PURPOSE: Give any-status readers (client facade, mixed-status CLI) an honest Optional-returning API that does not widen the narrow path baked into Task[S_co].
def allocated_node_id_of(t: AnyTask) -> NodeId | None:
    """Return the node binding if the task's state carries one, else None."""
    if isinstance(t.state, Running):
        return t.state.allocated_node_id
    if isinstance(t.state, Done):
        return t.state.allocated_node_id
    return None


def remote_folder_of(t: AnyTask) -> str | None:
    """Return the remote work folder carried on the task's state, else None."""
    return t.state.remote_folder


def error_of(t: AnyTask) -> str | None:
    """Return the error string if the task's state carries one, else None."""
    if isinstance(t.state, Done):
        return t.state.error
    return None


# endregion FUNC_any_status_helpers


# region FUNC_narrowing_helpers
# PURPOSE: Narrow a wide AnyTask to a specific Task[S] at sites that receive the union (get, list_by_jobs, webhook handler) so state-specific transitions become statically callable.
def is_todo(t: AnyTask) -> TypeIs[Task[Todo]]:
    """Narrow to Task[Todo] when the task's state is Todo."""
    return isinstance(t.state, Todo)


def is_running(t: AnyTask) -> TypeIs[Task[Running]]:
    """Narrow to Task[Running] when the task's state is Running."""
    return isinstance(t.state, Running)


def is_done(t: AnyTask) -> TypeIs[Task[Done]]:
    """Narrow to Task[Done] when the task's state is Done."""
    return isinstance(t.state, Done)


# endregion FUNC_narrowing_helpers


# region FUNC_materialize_task
# PURPOSE: Attach the TaskCreated event to a freshly-inserted Task so the UoW dispatches it on commit.
# RATIONALE:
# - Q: Why a dedicated function instead of calling TaskCreated inside insert?
#   A: Keeps the domain event construction in the domain layer and the SQL/ORM concern in the repository. The infrastructure layer never imports TaskCreated directly.
def materialize_task(task: Task) -> Task:
    """Return a Task with a TaskCreated event appended to events."""
    event = TaskCreated(
        task_id=task.task_id,
        webhook_url=task.webhook_url,
        webhook_custom_params=task.webhook_custom_params,
        engine_name=task.engine,
    )
    return replace(task, events=(event,))


# endregion FUNC_materialize_task


# region CLASS_NodeId
# PURPOSE: Wrap the database node id in a dedicated value object so transport/serialization boundaries must unwrap it explicitly, preventing accidental mixing with bare ints.
# INVARIANTS: value > 0; frozen and hashable; not equal to a bare int.
# RATIONALE:
# - Q: Why a dedicated type instead of bare int, NewType('NodeId', int), or int subclass?
#   A: A frozen dataclass wrapper enforces explicit .value unwrapping at every external boundary, preventing accidental id/int mixing.
@dataclass(frozen=True)
class NodeId:
    """Node primary-key value object.

    Wraps a single ``value: int`` (the DB-generated ``node_id``). ``__post_init__``
    enforces ``value > 0`` (SERIAL starts at 1, so a non-positive value indicates a
    bug). ``__str__`` returns the bare integer string so CLI rendering and logging
    produce ``5``, not ``NodeId(value=5)``. Frozen → hashable → usable as a dict key.
    Intentionally NOT equal to a bare ``int`` (``NodeId(5) == 5`` is ``False``) — the
    type-safety point of a dedicated value object; callers must unwrap ``.value``
    explicitly at external boundaries (pg8000 params, JSON, argparse).
    """

    value: int

    def __post_init__(self) -> None:
        if self.value <= 0:
            msg = f"NodeId must be > 0, got {self.value}"
            raise ValueError(msg)

    def __str__(self) -> str:
        return str(self.value)


# endregion CLASS_NodeId


# region CLASS_NewNode
# PURPOSE: Carry the fields needed to insert a node before it has a database id, mirroring Node minus identity.
# INVARIANTS: No node_id field (absent by design). hostname="" is the empty-string sentinel for tmp rows. ncpus=None means no operator-set limit (discovered at spawn).
@dataclass(frozen=True)
class NewNode:
    """Pre-persistence node record — no identity yet; mirrors :class:`Node`.

    minus ``node_id``.
    """

    enabled: bool = True
    status: NodeStatus = NodeStatus.OTHER
    hostname: str = ""
    username: str = "root"
    port: int = 22
    jump_host: str | None = None
    jump_port: int = 22
    jump_username: str = "root"
    ncpus: int | None = None
    cloud: str | None = None
    external_id: str | None = None


# endregion CLASS_NewNode


# region CLASS_Node
# PURPOSE: Represent a persisted node, carrying its database identity as the first field so a Node instance always proves it was read from (or returned by) the database.
# INVARIANTS: ncpus > 0. external_id is None for static nodes.
# RATIONALE:
# - Q: Why do created_at/updated_at default to datetime.now()?
#   A: Mirrors the DB schema convention; the DB always overrides via RETURNING on insert/read.
@dataclass(frozen=True)
class Node:
    """Post-persistence node record — always carries its identity.

    ``node_id`` is the FIRST field (identity first); a ``Node`` only ever comes from
    the database.
    """

    node_id: NodeId
    hostname: str
    ncpus: int | None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    username: str = "root"
    port: int = 22
    jump_host: str | None = None
    jump_port: int = 22
    jump_username: str = "root"
    enabled: bool = True
    status: NodeStatus = NodeStatus.OTHER
    cloud: str | None = None
    external_id: str | None = None


# endregion CLASS_Node


# region CLASS_ConnectedMachine
# PURPOSE: Track a runtime-connected machine's occupancy state with atomic FREE/BUSY transitions so allocation and release are concurrency-safe.
# INVARIANTS: Frozen; state changes return new instances via replace; free_since is set on every transition to FREE. Platform tags are runtime-discovered.
# RATIONALE:
# - Q: Why is platforms on ConnectedMachine instead of Node?
#   A: It is runtime-discovered at connect time via the platform-package detector, not a persistent attribute of the node record. It feeds the is_compatible(engine.platforms) check and is meaningless outside a live connection.
@dataclass(frozen=True)
class ConnectedMachine:
    """Runtime connected machine.

    ``node_id`` identifies which :class:`Node` this connected machine represents.
    """

    node_id: NodeId
    platforms: tuple[str, ...]
    state: MachineState = MachineState.FREE
    free_since: float | None = None

    # region METHOD_is_compatible
    # PURPOSE: Tell whether this machine can accept a task, i.e. is free and on a supported platform .
    def is_compatible(self, platforms: tuple[str, ...]) -> bool:
        """Check if machine is FREE and its platform tags intersect the given platforms."""
        return self.state == MachineState.FREE and bool(
            set(self.platforms) & set(platforms)
        )

    # endregion METHOD_is_compatible

    # region METHOD_occupy
    # PURPOSE: Claim the machine for a task, refusing double-assignment.
    # REQUIRES: state is FREE.
    def occupy(self) -> ConnectedMachine:
        """Transition machine state to BUSY if currently FREE."""
        if self.state == MachineState.BUSY:
            raise MachineBusyError(self.node_id)
        return replace(self, state=MachineState.BUSY)

    # endregion METHOD_occupy

    # region METHOD_release
    # PURPOSE: Return the machine to the free pool and stamp when it became available.
    def release(self) -> ConnectedMachine:
        """Transition machine state to FREE and record release timestamp."""
        return replace(self, state=MachineState.FREE, free_since=time.monotonic())

    # endregion METHOD_release


# endregion CLASS_ConnectedMachine
