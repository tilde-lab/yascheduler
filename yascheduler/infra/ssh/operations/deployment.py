"""TaskDeployer — upload task inputs and spawn the calculation process on a remote machine via MachineSession. Stateless: takes (log) at construction, (session, ...) per call."""
# region MODULE_CONTRACT
# PURPOSE: Reconstruct the task's stored input tree on a remote machine via MachineSession SFTP and spawn the calculation process; rolls back BUSY on failure.
# SCOPE: TaskDeployer class; module-private helpers _write_remote_file, _make_remote_directory, _lstat_type, _check_destination.
# INVARIANTS: Upload boundary records identify task/count/path and failure class, including cancellation; rollback records describe whether release occurred. Input-transfer diagnostics never attach exception text or tracebacks, which may contain file bodies or credentials.
# DEPENDENCIES: USES API: asyncssh (SFTPClient, SFTP errors, SFTP file-type constants)
# KEYWORDS: deploy, task, upload, spawn, TaskDeployer, sftp, input snapshot
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import asyncssh
from asyncssh.sftp import (
    FILEXFER_TYPE_DIRECTORY,
    FILEXFER_TYPE_REGULAR,
    FILEXFER_TYPE_SYMLINK,
    FX_NO_SUCH_FILE,
    FX_NO_SUCH_PATH,
)

from yascheduler.domain import (
    MachineState,
    TaskId,
    TaskInputFile,
    UnsafeTaskPathError,
    validate_task_path,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import PurePath

    from asyncssh.sftp import SFTPClient

    from yascheduler.domain import Engine, MachineSession, RunningTask, TaskInputEntry

__all__ = ["TaskDeployer"]

logger = logging.getLogger(__name__)

_ABSENT_SFTP_CODES = frozenset({FX_NO_SUCH_FILE, FX_NO_SUCH_PATH})
_DESTINATION_REJECTION_MESSAGES = {
    "symbolic link": "destination traversal met a symbolic link",
    "not a directory": "destination traversal met a non-directory entry",
    "not an ordinary file": "destination traversal met a non-ordinary-file entry",
}


# region FUNC__write_remote_file
# PURPOSE: Write one stored input file's exact bytes to a remote path via SFTP.
# INVARIANTS: Binary mode only — content is the stored snapshot bytes, never text-decoded; asyncssh.misc.Error is logged with path/code/class and re-raised; exception text and traceback are not logged because transport errors may echo payloads or credentials; any other exception propagates unlogged (the caller's handler reports task context).
async def _write_remote_file(sftp: SFTPClient, path: str, data: bytes) -> None:
    # region BLOCK_write_file
    try:
        async with sftp.open(path, "wb") as f:
            await f.write(data)  # type: ignore[type-var]
    except asyncssh.misc.Error as err:
        # Traceback would leak arbitrary exception content.
        logger.error(  # noqa: TRY400
            "Write %s - SFTPError: %s (%s)",
            path,
            type(err).__name__,
            err.code,
        )
        raise
    # endregion BLOCK_write_file


# endregion FUNC__write_remote_file


# region FUNC__make_remote_directory
# PURPOSE: Create one stored input directory entry (with parents) on the remote host so empty directories survive deployment.
# INVARIANTS: The path is passed to SFTP in POSIX-separated string form regardless of the remote platform flavor, so backslash-flavored Windows paths are never sent as one opaque component; idempotent — exist_ok tolerates a directory that already exists (e.g. partial earlier attempt); asyncssh.misc.Error is logged with path/code/class/task_id and re-raised, never exception text or traceback; any other exception propagates unlogged.
async def _make_remote_directory(
    sftp: SFTPClient,
    path: PurePath,
    task_id: TaskId,
) -> None:
    # region BLOCK_mkdir
    try:
        await sftp.makedirs(path.as_posix(), exist_ok=True)
    except asyncssh.misc.Error as err:
        # Traceback would leak arbitrary exception content.
        logger.error(  # noqa: TRY400
            "Mkdir %s - SFTPError: %s (%s) (task_id=%s)",
            path,
            type(err).__name__,
            err.code,
            task_id,
        )
        raise
    # endregion BLOCK_mkdir


# endregion FUNC__make_remote_directory


# region FUNC__lstat_type
# PURPOSE: Return the lstat (non-following) SFTP file type of one destination path, or None when it does not exist, so symlink and conflict checks never follow links.
# INVARIANTS: Only SFTP "no such file/path" errors map to None; permission-denied and every other asyncssh.misc.Error propagate to the caller's failure path (an unverifiable destination is not written to); no exception is logged here — the caller classifies.
async def _lstat_type(sftp: SFTPClient, path: PurePath) -> int | None:
    try:
        return (await sftp.lstat(path.as_posix())).type
    except asyncssh.misc.Error as err:
        if err.code in _ABSENT_SFTP_CODES:
            return None
        raise


# endregion FUNC__lstat_type


# region FUNC__check_destination
# PURPOSE: Reject one destination path before writing when it is a symbolic link or conflicts with the required entry kind, per the destination-traversal contract.
# INVARIANTS: The check never follows links (lstat); a missing path is allowed and handled by the subsequent mkdir/write; a needed-directory path must be absent or a directory, a file path must be absent or an ordinary file (replaceable); every rejection raises UnsafeTaskPathError identifying the offending path and reason, and logs a structured boundary record without file contents.
async def _check_destination(
    sftp: SFTPClient,
    path: PurePath,
    *,
    need_directory: bool,
    task_id: TaskId,
) -> None:
    kind = await _lstat_type(sftp, path)
    wanted = FILEXFER_TYPE_DIRECTORY if need_directory else FILEXFER_TYPE_REGULAR
    if kind is None or kind == wanted:
        return
    reason = (
        "symbolic link"
        if kind == FILEXFER_TYPE_SYMLINK
        else "not a directory"
        if need_directory
        else "not an ordinary file"
    )
    posix = path.as_posix()
    logger.debug(
        "DESTINATION_REJECTED",
        extra={"path": posix, "reason": reason, "task_id": task_id},
    )
    raise UnsafeTaskPathError(posix, _DESTINATION_REJECTION_MESSAGES[reason])


# endregion FUNC__check_destination


# region CLASS_TaskDeployer
# PURPOSE: Upload task inputs and spawn calculation process on a remote machine via MachineSession; rolls back BUSY on failure.
class TaskDeployer:
    """Upload task inputs and spawn the calculation process on a remote machine.

    Stateless: takes (log) at construction, (session, ...) per call. Rolls back
    the session BUSY marking under `except BaseException` on any deploy/spawn
    failure.
    """

    # region METHOD__upload_task_data
    # PURPOSE: Reconstruct the task's stored input tree under the remote task root — directories parent-first, then exact file bytes — from the streamed snapshot entries, without consulting live engine input declarations or task metadata and without ever holding the whole snapshot in memory.
    # INVARIANTS: Entries arrive from the store's keyset stream in C-collation path order (parent before descendants); each entry path is re-validated at this trust boundary (validate_task_path) before the remote write — unsafe paths must not reach SFTP path joining even if storage were corrupted; each entry's destination is joined component-wise in the remote platform's path flavor and lstat-checked without following links — symbolic links and file/directory conflicts fail with the offending path, while an existing ordinary file at a file path is replaced; a failing entry fails the transfer after its predecessors were written (a retry overwrites the partial tree). The ancestor checks are leaf-per-entry by design: the normalized snapshot materializes every parent directory entry, so each in-tree ancestor is itself lstat-checked — symlinked ancestors are caught through their own materialized entries. Ancestors above the task root are scheduler-controlled (resolved through realpath at session level) and are deliberately not re-inspected per deploy, per the accepted-risk clause on non-atomic SFTP type checks.
    async def _upload_task_data(
        self,
        session: MachineSession,
        task: RunningTask,
        remote_dir: PurePath,
        entries: AsyncIterator[TaskInputEntry],
    ) -> bool:

        # region BLOCK_upload
        # Trust boundary: validate each entry's stored path before its remote
        # write — an unsafe path must not reach SFTP path joining even if
        # storage were corrupted. Per-entry validation replaces the former
        # whole-snapshot re-validation: a corrupt row now fails
        # after its predecessors reached the machine, and the retry overwrites.
        path = "."
        entry_count = 0
        logger.debug("UPLOAD_INPUTS", extra={"task_id": task.task_id.value})
        try:
            async with session.open_sftp() as sftp:
                # Check the task root before creating it so a pre-existing symlink
                # or a regular file at the root path fails with the same
                # UnsafeTaskPathError contract as entry-level destinations.
                await _check_destination(
                    sftp,
                    remote_dir,
                    need_directory=True,
                    task_id=task.task_id,
                )
                await _make_remote_directory(sftp, remote_dir, task.task_id)

                async for entry in entries:
                    path = validate_task_path(entry.path)
                    entry_count += 1
                    target = remote_dir.joinpath(*path.split("/"))
                    # Confinement guard: a component like ``a:b`` is drive-parsed
                    # by the Windows path flavor and would discard the task root
                    # (e.g. ``C:/tasks`` / ``sub/a:b`` -> ``a:b``). Any target
                    # that is not inside the remote task root is rejected before
                    # it can reach SFTP.
                    if not target.is_relative_to(remote_dir):
                        logger.debug(
                            "DESTINATION_REJECTED",
                            extra={
                                "path": path,
                                "reason": "escapes the task root",
                                "task_id": task.task_id,
                            },
                        )
                        # Boundary logging classifies this validation failure; keep it local.
                        raise UnsafeTaskPathError(  # noqa: TRY301
                            path, "destination escapes the task root"
                        )
                    await _check_destination(
                        sftp,
                        target,
                        need_directory=not isinstance(entry, TaskInputFile),
                        task_id=task.task_id,
                    )
                    if isinstance(entry, TaskInputFile):
                        await _write_remote_file(sftp, target.as_posix(), entry.content)
                    else:
                        await _make_remote_directory(sftp, target, task.task_id)
        except BaseException as err:
            logger.debug(
                "UPLOAD_INPUTS_FAILED",
                extra={
                    "task_id": task.task_id.value,
                    "path": getattr(err, "path", path),
                    "entry_count": entry_count,
                    "failure": type(err).__name__,
                },
            )
            raise
        logger.debug(
            "UPLOAD_INPUTS_DONE",
            extra={"task_id": task.task_id.value, "entry_count": entry_count},
        )
        return True
        # endregion BLOCK_upload

    # endregion METHOD__upload_task_data

    # region METHOD__exec_spawn_command
    # PURPOSE: Execute spawn command on remote machine via SSH.
    async def _exec_spawn_command(
        self,
        session: MachineSession,
        engine: Engine,
        task: RunningTask,
        task_dir: PurePath,
        eng_path: PurePath,
        ncpus: int,
    ) -> None:
        # region BLOCK_spawn
        try:
            run_cmd = engine.spawn.format(
                engine_path=session.quote(str(eng_path)),
                task_path=session.quote(str(task_dir)),
                ncpus=ncpus,
            )
            logger.debug(
                "SPAWN",
                extra={
                    "hostname": session.hostname,
                    "task_id": task.task_id,
                    "cmd": run_cmd,
                    "cwd": str(task_dir),
                },
            )
            await session.run_bg(run_cmd, cwd=str(task_dir))
        except Exception:
            logger.exception("SSH spawn cmd error")
            raise
        # endregion BLOCK_spawn

    # endregion METHOD__exec_spawn_command

    # region METHOD_start_task_on_machine
    # PURPOSE: Upload task inputs and spawn calculation process on remote machine.
    async def start_task_on_machine(
        self,
        session: MachineSession,
        engine: Engine,
        task: RunningTask,
        entries: AsyncIterator[TaskInputEntry],
        ncpus: int,
        engines_dir: PurePath,
    ) -> bool:
        """Upload task inputs and spawn calculation process on remote machine."""
        # region BLOCK_start_task
        session.occupy()

        logger.info(
            "Submitting task_id=%s %s with %s to %s",
            task.task_id,
            task.label,
            engine.name,
            session.hostname,
        )

        # region BLOCK_deploy_spawn
        try:
            path_type = session.path
            remote_folder = path_type(task.state.remote_folder)
            # region BLOCK_deploy
            async with session.open_sftp() as sftp:
                try:
                    root_dir = path_type(await sftp.realpath("."))
                    task_dir = (
                        remote_folder
                        if remote_folder.is_absolute()
                        else root_dir / remote_folder
                    )
                    if engines_dir.is_absolute():
                        engine_path = engines_dir / engine.name
                    else:
                        engine_path = root_dir / engines_dir / engine.name
                    await self._upload_task_data(
                        session,
                        task,
                        task_dir,
                        entries,
                    )
                except Exception as err:
                    # Traceback would leak arbitrary exception content.
                    logger.error(  # noqa: TRY400
                        "Can't upload task_id=%s files (%s)",
                        task.task_id,
                        type(err).__name__,
                    )
                    raise
            # endregion BLOCK_deploy

            await self._exec_spawn_command(
                session,
                engine,
                task,
                task_dir,
                engine_path,
                ncpus,
            )
        except BaseException as err:
            # region BLOCK_rollback_busy
            # Roll back the session BUSY marking on any deploy/spawn failure (incl.
            # CancelledError during daemon shutdown) so the machine is not left stuck.
            if session.is_closed:
                logger.warning(
                    "task_id=%s on %s: already disconnected, skipping rollback (%s)",
                    task.task_id,
                    session.hostname,
                    type(err).__name__,
                )
                logger.debug(
                    "DEPLOY_ROLLBACK",
                    extra={
                        "task_id": task.task_id.value,
                        "failure": type(err).__name__,
                        "released": False,
                    },
                )
                raise
            if session.machine.state != MachineState.BUSY:
                logger.warning(
                    "unexpected state %s, expected BUSY (task_id=%s on %s)",
                    session.machine.state,
                    task.task_id,
                    session.hostname,
                )
            else:
                logger.info(
                    "task_id=%s on %s (%s); rolling back BUSY",
                    task.task_id,
                    session.hostname,
                    type(err).__name__,
                )
            session.update(session.machine.release())
            logger.debug(
                "DEPLOY_ROLLBACK",
                extra={
                    "task_id": task.task_id.value,
                    "failure": type(err).__name__,
                    "released": True,
                },
            )
            raise
            # endregion BLOCK_rollback_busy
        # endregion BLOCK_deploy_spawn

        return True
        # endregion BLOCK_start_task

    # endregion METHOD_start_task_on_machine


# endregion CLASS_TaskDeployer
