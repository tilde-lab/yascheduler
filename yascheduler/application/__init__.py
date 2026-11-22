"""Application layer facade — sole public surface for cross-layer consumers and composition root."""
# region MODULE_CONTRACT
# PURPOSE: Expose the application layer's public surface from one import path so consumers depend on yascheduler.application, not internal modules.
# KEYWORDS: application facade, public api, re-export, composition root
# endregion MODULE_CONTRACT

from .abandon_node import abandon_node
from .allocation_tracker import AllocationTracker
from .legacy_inputs import (
    legacy_input_snapshot,
    legacy_input_value,
    legacy_selection_selects,
    normalize_task_inputs,
)
from .message_bus import MessageBus
from .orchestrator import Orchestrator
from .query_tasks import query_tasks
from .submit_task import submit_task
from .uow import AbstractUnitOfWork

__all__ = [
    "AbstractUnitOfWork",
    "AllocationTracker",
    "MessageBus",
    "Orchestrator",
    "abandon_node",
    "legacy_input_snapshot",
    "legacy_input_value",
    "legacy_selection_selects",
    "normalize_task_inputs",
    "query_tasks",
    "submit_task",
]
