"""Register a new task in TO_DO state after validation."""
# region MODULE_CONTRACT
# PURPOSE: Accept validated task requests from clients and persist them so the daemon's allocator can pick them up for scheduling.
# SCOPE: Task submission use case — engine validation, input snapshot coverage validation, NewTask construction, persistence via UoW.
# KEYWORDS: submit, create, task, validation, engine, input files, snapshot
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from yascheduler.domain import (
    TYPED_METADATA_KEYS,
    NewTask,
    TaskId,
    UnsupportedEngineError,
    normalize_input_snapshot,
    validate_snapshot_selections,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from yascheduler.domain import EngineRepository, TaskInputSnapshot, TodoTask

    from .uow import AbstractUnitOfWork

logger = logging.getLogger(__name__)

__all__ = ["submit_task"]


# region FUNC_submit_task
# PURPOSE: Validate engine and snapshot coverage, construct a NewTask carrying the immutable input snapshot, persist via UoW, and return the generated TaskId.
# REQUIRES: engine_name is configured in engines; input_snapshot is a task input snapshot (boundary-normalized for public submissions; the default empty snapshot suits engines without input declarations).
# ENSURES: Returns a valid TaskId > 0; the task row is committed with status TO_DO carrying a copied immutable snapshot; unknown engines and unsatisfied engine selections raise before any task write.
async def submit_task(
    label: str,
    metadata: Mapping[str, Any],
    engine_name: str,
    engines: EngineRepository,
    uow_factory: Callable[[], AbstractUnitOfWork],
    *,
    input_snapshot: TaskInputSnapshot = (),
) -> TaskId:
    """Create a new TO_DO task after validating engine and input snapshot."""
    # region BLOCK_validate
    if engine_name not in engines:
        raise UnsupportedEngineError(engine_name)
    engine = engines[engine_name]
    # Copy to an immutable tuple and enforce the one snapshot rule: every
    # engine input declaration must be satisfied by a snapshot entry.
    snapshot = normalize_input_snapshot(input_snapshot)
    validate_snapshot_selections(engine_name, engine.input_files, snapshot)
    # endregion BLOCK_validate

    # region BLOCK_create_task
    extra = {k: v for k, v in metadata.items() if k not in TYPED_METADATA_KEYS}
    new_task = NewTask(
        label=label,
        engine=engine_name,
        local_folder=metadata.get("local_folder"),
        webhook_url=metadata.get("webhook_url"),
        webhook_custom_params=metadata.get("webhook_custom_params", {}),
        extra=extra,
        input_entries=snapshot,
    )
    # endregion BLOCK_create_task

    # region BLOCK_persist
    async with uow_factory() as uow:
        task: TodoTask = await uow.tasks.insert(new_task)
        await uow.tasks.save(task)
        await uow.commit()
    # endregion BLOCK_persist

    logger.info("submitted: %s", label)
    return task.task_id


# endregion FUNC_submit_task
