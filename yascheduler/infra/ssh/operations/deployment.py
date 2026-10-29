"""TaskDeployer — upload task inputs and spawn the calculation process on a remote machine via MachineSession. Stateless: takes (log) at construction, (session, ...) per call."""
# region MODULE_CONTRACT
# PURPOSE: Reconstruct the task's stored input tree on a remote machine via MachineSession SFTP and spawn the calculation process; rolls back BUSY on failure.
# SCOPE: TaskDeployer class, _write_remote_file and _make_remote_directory module-private helpers.
# DEPENDENCIES: USES API: asyncssh (SFTPClient, SFTP errors)
# KEYWORDS: deploy, task, upload, spawn, TaskDeployer, sftp, input snapshot
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import asyncssh

from yascheduler.domain import MachineState, TaskId, TaskInputFile

if TYPE_CHECKING:
    from pathlib import PurePath

    from asyncssh.sftp import SFTPClient

    from yascheduler.domain import Engine, MachineSession, RunningTask

__all__ = ["TaskDeployer"]

logger = logging.getLogger(__name__)


# region FUNC__write_remote_file
# PURPOSE: Write one stored input file's exact bytes to a remote path via SFTP.
# INVARIANTS: Binary mode only — content is the stored snapshot bytes, never text-decoded; asyncssh.misc.Error is logged with path/reason/code and re-raised; any other exception propagates unlogged (the caller's handler reports task context).
async def _write_remote_file(sftp: SFTPClient, path: str, data: bytes) -> None:
    # region BLOCK_write_file
    try:
        async with sftp.open(path, "wb") as f:
            await f.write(data)  # type: ignore[type-var]
    except asyncssh.misc.Error as err:
        logger.exception(
            "Write %s - SFTPError: %s (%s)",
            path,
            err.reason,
            err.code,
        )
        raise
    # endregion BLOCK_write_file


# endregion FUNC__write_remote_file


# region FUNC__make_remote_directory
# PURPOSE: Create one stored input directory entry (with parents) on the remote host so empty directories survive deployment.
# INVARIANTS: Idempotent — exist_ok tolerates a directory that already exists (e.g. partial earlier attempt); asyncssh.misc.Error is logged with path/reason/code/task_id and re-raised; any other exception propagates unlogged.
async def _make_remote_directory(
    sftp: SFTPClient,
    path: PurePath,
    task_id: TaskId,
) -> None:
    # region BLOCK_mkdir
    try:
        await sftp.makedirs(PurePosixPath(path), exist_ok=True)
    except asyncssh.misc.Error as err:
        logger.exception(
            "Mkdir %s - SFTPError: %s (%s) (task_id=%s)",
            path,
            err.reason,
            err.code,
            task_id,
        )
        raise
    # endregion BLOCK_mkdir


# endregion FUNC__make_remote_directory


# region CLASS_TaskDeployer
# PURPOSE: Upload task inputs and spawn calculation process on a remote machine via MachineSession; rolls back BUSY on failure.
class TaskDeployer:
    """Upload task inputs and spawn the calculation process on a remote machine.

    Stateless: takes (log) at construction, (session, ...) per call. Rolls back
    the session BUSY marking under `except BaseException` on any deploy/spawn
    failure.
    """

    def __init__(self) -> None:
        """Stateless deployer — no initialisation needed."""

    # region METHOD__upload_task_data
    # PURPOSE: Reconstruct the task's stored input tree under the remote task root — directories parent-first, then exact file bytes — from the immutable snapshot, without consulting live engine input declarations or task metadata.
    # INVARIANTS: Entries iterate parent-first (sorted by canonical path, so every ancestor precedes its descendants); files are written in binary mode from stored bytes; an empty directory stays an explicit directory and a zero-length file stays a file; the whole tree is uploaded before this returns.
    async def _upload_task_data(
        self,
        session: MachineSession,
        task: RunningTask,
        remote_dir: PurePath,
    ) -> bool:

        # region BLOCK_upload
        async with session.open_sftp() as sftp:
            try:
                await sftp.makedirs(PurePosixPath(remote_dir), exist_ok=True)
            except asyncssh.misc.Error as err:
                logger.exception(
                    "Create %s - SFTPError: %s (%s) (task_id=%s)",
                    remote_dir,
                    err.reason,
                    err.code,
                    task.task_id,
                )
                raise

            for entry in sorted(task.input_entries, key=lambda item: item.path):
                target = remote_dir / entry.path
                if isinstance(entry, TaskInputFile):
                    await _write_remote_file(sftp, target.as_posix(), entry.content)
                else:
                    await _make_remote_directory(sftp, target, task.task_id)
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
                    )
                except Exception:
                    logger.exception(
                        "Can't upload task_id=%s files",
                        task.task_id,
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
                    err,
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
                    err,
                )
            session.update(session.machine.release())
            raise
            # endregion BLOCK_rollback_busy
        # endregion BLOCK_deploy_spawn

        return True
        # endregion BLOCK_start_task

    # endregion METHOD_start_task_on_machine


# endregion CLASS_TaskDeployer
