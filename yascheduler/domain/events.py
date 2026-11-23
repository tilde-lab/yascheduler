"""Domain events for task lifecycle transitions."""
# region MODULE_CONTRACT
# PURPOSE: Record task lifecycle transitions as immutable values so the UoW can dispatch webhooks/handlers without re-querying aggregates.
# SCOPE:
# - DomainEvent base + TaskCreated, TaskAllocated, TaskCompleted, TaskFailed, TaskAbandoned, and the Event union alias.
# - NOT: event dispatch (application.message_bus) or webhook wire format (infra.notifier).
# INVARIANTS: Every event is frozen; task_id is always present. Events are constructed only inside Task transition methods and inside materialize_task (for TaskCreated).
# KEYWORDS: domain event, lifecycle, webhook, TaskCreated, TaskAllocated, TaskCompleted, TaskFailed, TaskAbandoned
# endregion MODULE_CONTRACT

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import NodeId, TaskId

__all__ = [
    "DomainEvent",
    "Event",
    "TaskAbandoned",
    "TaskAllocated",
    "TaskCompleted",
    "TaskCreated",
    "TaskFailed",
]


# region CLASS_DomainEvent
# PURPOSE: Anchor the per-transition payload the UoW dispatches so handlers receive an immutable record carrying everything they need without re-querying aggregates.
# INVARIANTS: frozen; webhook_url is None when no outbound delivery is configured for the originating task.
@dataclass(frozen=True)
class DomainEvent:
    """Domainevent."""

    task_id: TaskId
    webhook_url: str | None
    webhook_custom_params: dict[str, object]


# endregion CLASS_DomainEvent


# region CLASS_TaskCreated
# PURPOSE: Record that a task entered the system so the webhook handler can notify external systems of a new TO_DO task.
@dataclass(frozen=True)
class TaskCreated(DomainEvent):
    """Taskcreated."""

    engine_name: str


# endregion CLASS_TaskCreated


# region CLASS_TaskAllocated
# PURPOSE: Record that a TO_DO task was bound to a node and entered RUNNING so the webhook handler can notify external systems of the allocation.
@dataclass(frozen=True)
class TaskAllocated(DomainEvent):
    """Taskallocated."""

    node_id: NodeId
    engine_name: str


# endregion CLASS_TaskAllocated


# region CLASS_TaskCompleted
# PURPOSE: Record that a RUNNING task completed successfully so the webhook handler can notify external systems of the DONE outcome.
@dataclass(frozen=True)
class TaskCompleted(DomainEvent):
    """Taskcompleted."""

    local_folder: str


# endregion CLASS_TaskCompleted


# region CLASS_TaskFailed
# PURPOSE: Record that a task ended in failure (rejected before running OR failed during run) so the webhook handler can notify external systems of the loss.
@dataclass(frozen=True)
class TaskFailed(DomainEvent):
    """Taskfailed."""

    reason: str


# endregion CLASS_TaskFailed


# region CLASS_TaskAbandoned
# PURPOSE: Record that a RUNNING task was abandoned because its node disappeared so the webhook handler can notify external systems.
@dataclass(frozen=True)
class TaskAbandoned(DomainEvent):
    """Taskabandoned."""

    node_id: NodeId


# endregion CLASS_TaskAbandoned


Event = TaskCreated | TaskAllocated | TaskCompleted | TaskFailed | TaskAbandoned
