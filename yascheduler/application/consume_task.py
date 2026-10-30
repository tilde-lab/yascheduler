"""Consume task use case — download outputs from a remote machine and finalise or defer the task."""
# region MODULE_CONTRACT
# PURPOSE: Reliably retrieve remote computation results and classify errors to either close the task lifecycle or retry, so transient infra failures do not prematurely terminate valid work.
# SCOPE: Task consumption lifecycle — task-owned output selections (configured fallback for legacy tasks), output download via SFTP, transient/permanent error classification, finalise with domain transitions (complete/fail), persistence, tracker slot discard, best-effort remote cleanup after finalisation.
# KEYWORDS: consume, finalise, download, defer, retry, output, cleanup, sftp
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from yascheduler.domain import (
        DoneTask,
        EngineRepository,
        MachineSession,
        RunningTask,
        TaskId,
    )
    from yascheduler.infra import OutputDownloader

    from .allocation_tracker import AllocationTracker
    from .uow import AbstractUnitOfWork

logger = logging.getLogger(__name__)

__all__ = ["consume_task"]


# region FUNC__prepare_store_folder
# PURPOSE: Ensure the local filesystem is ready to receive remote outputs and return the remote task root, so the download step can resolve the task's selections against it without pre-joining paths.
async def _prepare_store_folder(
    task: RunningTask, local_tasks_dir: Path
) -> tuple[Path, str]:
    # region BLOCK_create_dir
    remote_folder: str = task.state.remote_folder
    if task.local_folder:
        store_folder = Path(task.local_folder)
    else:
        store_folder = local_tasks_dir / Path(remote_folder).name
    await asyncio.get_running_loop().run_in_executor(
        None,
        partial(store_folder.mkdir, 0o777, parents=True, exist_ok=True),
    )
    # endregion BLOCK_create_dir
    return store_folder, remote_folder


# endregion FUNC__prepare_store_folder


# region FUNC__format_download_error
# PURPOSE: Produce a single actionable error message from per-file download failures so operators or clients can diagnose what went wrong.
def _format_download_error(
    errors: list[tuple[str | None, Exception]],
) -> str:
    parts: list[str] = []
    for path, err in errors:
        msg = str(err)
        parts.append(f"{path}: {msg}" if path is not None else msg)
    return "Download error: " + ", ".join(parts)


# endregion FUNC__format_download_error


# region FUNC__finalize_and_clean
# PURPOSE: Persist the terminal transition first, then remove the remote directory — the ordering guarantees a crash can never make a later pass re-download from a partly deleted tree.
# ENSURES: Returns True (finalised): outcome saved and committed, tracker slot discarded, cleanup attempted exactly once.
async def _finalize_and_clean(
    task: RunningTask,
    finalised: DoneTask,
    store_folder: Path | None,
    session: MachineSession,
    output_downloader: OutputDownloader,
    uow_factory: Callable[[], AbstractUnitOfWork],
    tracker: AllocationTracker,
) -> bool:
    # region BLOCK_set_status
    # The outcome is committed BEFORE anything remote is deleted. Once
    # committed, the task is terminal and is never consumed again, so a
    # crash between here and the cleanup call leaves the remote tree
    # intact for node teardown instead of corrupting a later download.
    async with uow_factory() as uow:
        await uow.tasks.save(finalised)
        await uow.commit()
    # endregion BLOCK_set_status

    if finalised.state.error:
        logger.warning(
            "task_id=%s failed: %s", finalised.task_id, finalised.state.error
        )
        logger.debug(
            "FINALISE_FAIL",
            extra={
                "task_id": finalised.task_id.value,
                "label": finalised.label,
                "local_folder": str(store_folder) if store_folder else "",
                "error": finalised.state.error,
            },
        )
    else:
        logger.info(
            "task_id=%s %s done and saved in %s",
            finalised.task_id,
            finalised.label,
            store_folder,
        )
        logger.debug(
            "FINALISE_OK",
            extra={
                "task_id": finalised.task_id.value,
                "label": finalised.label,
                "local_folder": str(store_folder) if store_folder else "",
            },
        )

    tracker.discard(finalised.task_id)

    # region BLOCK_cleanup_after_finalisation
    # Best-effort remote cleanup, strictly after the terminal commit. The
    # guard is absolute: the task is already terminal and its slot released,
    # so no failure here may escape consume_task — an escaped exception would
    # surface as a generic consumer error and leave stale in-memory
    # occupancy state in the orchestrator. A failure is logged and the
    # directory is left for node teardown (cloud nodes are deleted with the
    # VM).
    try:
        await output_downloader.cleanup_remote(
            session=session,
            remote_dir=task.state.remote_folder,
            task_id=finalised.task_id,
        )
    except Exception:
        logger.exception(
            "cleanup failed after finalisation for task_id=%s at %s "
            "(left for node teardown)",
            finalised.task_id,
            task.state.remote_folder,
        )
    # endregion BLOCK_cleanup_after_finalisation
    return True


# endregion FUNC__finalize_and_clean


# region FUNC_consume_task
# PURPOSE: Load a RUNNING task, retrieve its recorded output selections, and finalise (DONE) or defer (stay RUNNING), retaining a logged configuration fallback for legacy tasks.
# ENSURES: Returns True when finalised (or task gone — vacuously finalised); False when deferred; a removed engine still fails the task even when selections were recorded.
async def consume_task(
    task_id: TaskId,
    session: MachineSession,
    output_downloader: OutputDownloader,
    engines: EngineRepository,
    uow_factory: Callable[[], AbstractUnitOfWork],
    local_tasks_dir: Path,
    tracker: AllocationTracker,
) -> bool:
    """Load task by id via UoW, download outputs from remote machine, finalise or defer."""
    async with uow_factory() as uow:
        task = await uow.tasks.get_running(task_id)
    if task is None:
        logger.warning(
            "task_id=%s not found or no longer RUNNING, skipping consume", task_id
        )
        # Discard the tracker slot so a missing/wrong-status task row can't
        # leak an in-flight allocation slot forever (treat as vacuously
        # finalised).
        tracker.discard(task_id)
        return True

    # Engine may have been removed from config while the task was RUNNING.
    # Treat as a permanent consume error: finalise via fail() so the task does
    # not loop forever being re-yielded by the producer each cycle.
    engine = engines.get(task.engine)
    if engine is None:
        logger.warning(
            "task_id=%s engine '%s' no longer in config, failing task",
            task_id,
            task.engine,
        )
        finalised = task.fail(
            f"engine '{task.engine}' no longer in config",
            local_folder=task.local_folder or "",
            remote_folder=task.state.remote_folder,
        )
        return await _finalize_and_clean(
            task, finalised, None, session, output_downloader, uow_factory, tracker
        )

    store_folder, task_remote_folder = await _prepare_store_folder(
        task,
        local_tasks_dir,
    )
    # region BLOCK_resolve_output_selections
    selections = task.output_selections
    if not selections:
        selections = engine.output_files
        logger.warning(
            "task_id=%s has no recorded output selections; using engine '%s' configuration",
            task.task_id,
            task.engine,
            extra={"task_id": task.task_id.value, "engine": task.engine},
        )
    logger.debug(
        "OUTPUT_SELECTIONS_RESOLVED",
        extra={
            "task_id": task.task_id.value,
            "output_selections": list(selections),
            "source": "task" if task.output_selections else "engine",
        },
    )
    # endregion BLOCK_resolve_output_selections

    # region BLOCK_download
    transient_errors, permanent_errors = await output_downloader.download_outputs(
        session=session,
        remote_dir=task_remote_folder,
        local_dir=store_folder,
        selections=list(selections),
        task_id=task.task_id,
    )
    logger.debug(
        "RETRIEVAL_ENTRIES",
        extra={
            "task_id": task.task_id.value,
            "transient": [path or "" for path, _ in transient_errors],
            "permanent": [path or "" for path, _ in permanent_errors],
        },
    )
    # endregion BLOCK_download

    # region BLOCK_defer
    # Defer while ANY transient failure remains — including when permanent
    # failures also exist: the remote results stay untouched and the next
    # pass re-attempts every output, re-deriving the error lists from
    # scratch. A pass with zero transient failures decides the outcome.
    if transient_errors:
        logger.info(
            "task_id=%s download deferred (transient errors), staying RUNNING for retry",
            task.task_id,
        )
        logger.debug(
            "RETRIEVAL_DEFER",
            extra={
                "task_id": task.task_id.value,
                "transient": len(transient_errors),
                "permanent": len(permanent_errors),
            },
        )
        return False
    # endregion BLOCK_defer

    # region BLOCK_finalise
    # Zero transient failures: finalise on this pass's own errors. The
    # terminal transitions set the folders AND emit the matching event inline.
    if permanent_errors:
        finalised = task.fail(
            _format_download_error(permanent_errors),
            local_folder=str(store_folder),
            remote_folder=task_remote_folder,
        )
    else:
        finalised = task.complete(
            local_folder=str(store_folder),
            remote_folder=task_remote_folder,
        )
    logger.debug(
        "FINALISE_DECISION",
        extra={
            "will_fail": bool(permanent_errors),
            "permanent": len(permanent_errors),
            "transient": len(transient_errors),
        },
    )
    # endregion BLOCK_finalise
    return await _finalize_and_clean(
        task,
        finalised,
        store_folder,
        session,
        output_downloader,
        uow_factory,
        tracker,
    )


# endregion FUNC_consume_task
