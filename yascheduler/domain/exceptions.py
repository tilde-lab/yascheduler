"""Domain exception hierarchy for business-level error handling."""
# region MODULE_CONTRACT
# PURPOSE: Give the scheduler one typed error vocabulary so callers catch specific business failures instead of parsing messages.
# SCOPE:
# - DomainError root plus validation, task-lifecycle, machine-state, scheduling, and cloud sub-hierarchies.
# - NOT: error rendering, exit codes, or retry policy.
# INVARIANTS: Every domain error subclasses DomainError; messages are stable and human-readable.
# RATIONALE:
# - Q: Why is CloudCapacityExhaustedError under SchedulingError, not CloudError?
#   A: Capacity planning is a domain scheduling rule (no provider can serve the request), whereas CloudError covers operational provider failures (VM creation, SSH, setup). Keeping them apart lets the allocator retry/throttle differently from handling provider outages.
# KEYWORDS: domain error, exception, validation, task lifecycle, machine busy, scheduling, cloud error
# endregion MODULE_CONTRACT

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yascheduler.domain.model import NodeId, TaskId

__all__ = [
    "CloudAllocateError",
    "CloudCapacityExhaustedError",
    "CloudError",
    "CloudSetupError",
    "DomainError",
    "InvalidTaskSnapshotError",
    "MachineBusyError",
    "MachineConnectionError",
    "MissingInputFileError",
    "NoCompatibleNodeError",
    "NodeRowNotFoundError",
    "SchedulingError",
    "TaskError",
    "TaskNotRunningError",
    "TaskNotTodoError",
    "UnsafeTaskPathError",
    "UnsupportedEngineError",
    "ValidationError",
]


class DomainError(Exception):
    """Base class for all domain exceptions."""


class ValidationError(DomainError):
    """Input validation errors."""


class UnsupportedEngineError(ValidationError):
    """Unknown calculation engine requested."""

    def __init__(self, engine_name: str) -> None:
        self.engine_name = engine_name
        super().__init__(f"unsupported engine: {engine_name}")


# region CLASS_UnsafeTaskPathError
# PURPOSE: Reject task-relative paths and selections that could escape the task root before any traversal reads or writes through them.
# INVARIANTS: Carries the offending path/selection and a concrete reason so callers report actionable path context (ADR-0019 selection contract).
class UnsafeTaskPathError(ValidationError):
    """A task-relative path or selection violates the task-root contract.

    Raised when an input path or selection is absolute, has a Windows
    drive/UNC root, contains ``..`` components or backslashes, names the task
    root itself, or otherwise cannot stay inside the task root.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"unsafe task path {path!r}: {reason}")


# endregion CLASS_UnsafeTaskPathError


# region CLASS_InvalidTaskSnapshotError
# PURPOSE: Reject snapshot entry sets that violate the task-owned input invariants (duplicates, kind conflicts, non-entry objects) so only a consistent tree is ever accepted or deployed.
# INVARIANTS: Carries the offending entry path and a concrete reason; raised before any task write (ADR-0019 snapshot ownership).
class InvalidTaskSnapshotError(ValidationError):
    """A task input snapshot violates the owned-entry invariants.

    Raised for duplicate entry paths, file/directory ancestor conflicts, and
    entries that are not task input value objects.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"invalid task input snapshot at {path!r}: {reason}")


# endregion CLASS_InvalidTaskSnapshotError


# region CLASS_TaskInputCaptureError
# PURPOSE: Reject a submission whose input selections cannot be captured from the source filesystem, naming the offending selection or path (ADR-0019 capture contract).
# INVARIANTS: Carries the offending task-relative path or selection and a concrete reason; raised before any task write; never carries file contents or credentials.
class TaskInputCaptureError(ValidationError):
    """A task input snapshot could not be captured from the submission root.

    Raised when a required selection is missing, a source cannot be read, a
    selected entry is a symbolic link or an unsupported filesystem type, or a
    source changed detectably during capture.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"task input capture failed at {path!r}: {reason}")


# endregion CLASS_TaskInputCaptureError


class MissingInputFileError(ValidationError):
    """Required engine input file not provided."""

    def __init__(self, engine_name: str, filename: str) -> None:
        self.engine_name = engine_name
        self.filename = filename
        super().__init__(f"missing input file '{filename}' for engine: {engine_name}")


class TaskError(DomainError):
    """Task lifecycle errors."""


class TaskNotTodoError(TaskError):
    """Task not in TODO status."""

    def __init__(self, task_id: TaskId) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id} is not in TODO status")


class TaskNotRunningError(TaskError):
    """Task not running."""

    def __init__(self, task_id: TaskId) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id} is not in running status")


class MachineBusyError(DomainError):
    """Operation attempted on a busy machine."""

    def __init__(self, node_id: NodeId) -> None:
        self.node_id = node_id
        super().__init__(f"machine ({node_id}) is busy")


class NodeRowNotFoundError(DomainError):
    """A node UPDATE/DELETE matched zero rows.

    Raised by the node repository when an update/enable/disable/remove finds
    no row for the given node_id. A zero-row result means the node is gone
    (concurrent tmp-node cleanup, double deallocation, manual DB delete).
    """

    def __init__(self, node_id: NodeId) -> None:
        self.node_id = node_id
        super().__init__(f"node row not found for node_id={node_id}")


class MachineConnectionError(DomainError):
    """SSH connection failure when establishing a connection to a remote machine."""

    def __init__(self, node_id: NodeId, hostname: str, reason: str) -> None:
        self.node_id = node_id
        self.hostname = hostname
        self.reason = reason
        super().__init__(
            f"cannot connect to machine ({node_id}) at {hostname}: {reason}",
        )


class SchedulingError(DomainError):
    """Scheduling/allocation errors."""


class NoCompatibleNodeError(SchedulingError):
    """No matching node found for task."""

    def __init__(self, task_id: TaskId, platforms: list[str]) -> None:
        self.task_id = task_id
        self.platforms = platforms
        super().__init__(
            f"no compatible node found for task {task_id} on platforms: {platforms}",
        )


class CloudCapacityExhaustedError(SchedulingError):
    """Cloud provider at capacity."""

    def __init__(self, task_id: TaskId) -> None:
        self.task_id = task_id
        super().__init__(f"cloud capacity exhausted for task {task_id}")


class CloudError(DomainError):
    """Operational cloud-provider failures."""


class CloudAllocateError(CloudError):
    """Cloud node allocation error — provider selection or VM creation failed."""


class CloudSetupError(CloudError):
    """Cloud node setup error — SSH / cloud-init / engine installation failed."""
