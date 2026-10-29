"""OutputDownloader — per-file SFTP-isolated download with retry, error classification, post-finalisation rmtree. Stateless: takes (log) at construction, (session, ...) per call."""
# region MODULE_CONTRACT
# PURPOSE: Per-file SFTP-isolated download with retry and error classification, plus one best-effort remote-tree removal that runs only after the task outcome is finalised. Stateless: session passed per call.
# SCOPE: OutputDownloader class and my_retry partial (canonical location).
# DEPENDENCIES: USES API: asyncssh (SFTPError)
# KEYWORDS: download, sftp, output, retry, rmtree, cleanup, OutputDownloader
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from asyncssh.sftp import SFTPError, SFTPNoSuchFile

from yascheduler.infra.ssh.platform.types import SFTPRetryExc
from yascheduler.shared import retry

if TYPE_CHECKING:
    from pathlib import Path

    from yascheduler.domain import MachineSession, TaskId

__all__ = ["OutputDownloader", "my_retry"]
logger = logging.getLogger(__name__)
my_retry = partial(retry, on=SFTPRetryExc, max_time=60)


# region CLASS_OutputDownloader
# PURPOSE: Per-file SFTP-isolated download with retry and error classification; best-effort remote cleanup.
class OutputDownloader:
    """Per-file SFTP-isolated download with retry and error classification.

    Opens a FRESH SFTP client per file (dead-connection blast radius bounded
    to one file). Classifies per-file exceptions into transient (SFTPRetryExc)
    or permanent (everything else). Downloads never remove the remote tree —
    `cleanup_remote` is the only deletion path and must be called only after
    the task outcome is finalised, so a crash can never make a later pass
    re-download from a partly deleted tree. Stateless: takes (log) at
    construction, (session, ...) per call. Returns a 4-tuple
    (local_folder, remote_folder, transient_errors, permanent_errors).
    """

    # region METHOD_download_outputs
    # PURPOSE: Per-file SFTP-isolated download with retry and error classification; no remote deletion.
    async def download_outputs(
        self,
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        files: list[str],
        task_id: TaskId | None = None,
    ) -> tuple[
        str,
        str,
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]:
        """Per-file SFTP-isolated download with retry and error classification."""
        # region BLOCK_download_outputs
        local_folder = str(local_dir)
        remote_folder = remote_dir
        transient_errors: list[tuple[str | None, Exception]] = []
        permanent_errors: list[tuple[str | None, Exception]] = []
        file_get_retry = my_retry()

        try:
            # region BLOCK_per_file_download
            # Fresh SFTP client per file: a dropped channel on file N invalidates only N's
            # retries. The inner try classifies ONLY sftp.get failures — a get_sftp OPEN
            # failure escapes to the outer handler as a session-level transient (not per-file).
            for out_file in files:
                async with session.open_sftp() as sftp:
                    try:
                        await file_get_retry(sftp.get)(
                            out_file,
                            local_dir,
                            preserve=True,
                        )
                    except (OSError, SFTPError) as err:
                        # region BLOCK_classify
                        if isinstance(err, SFTPRetryExc):
                            transient_errors.append((out_file, err))
                        else:
                            permanent_errors.append((out_file, err))
                        # endregion BLOCK_classify
                        logger.warning(
                            "Cannot download file for task_id=%s from %s: %s",
                            task_id,
                            out_file,
                            err,
                        )
            # endregion BLOCK_per_file_download
        except Exception as err:
            # Catch-all: whole-session failure (open_sftp raising, or a non-(OSError|SFTPError)
            # escape) is transient — the remote dir is preserved.
            logger.warning("Cannot scp from %s: %s", remote_dir, err)
            transient_errors.append((remote_dir, err))
        # endregion BLOCK_download_outputs
        return local_folder, remote_folder, transient_errors, permanent_errors

    # endregion METHOD_download_outputs

    # region METHOD_cleanup_remote
    # PURPOSE: Remove the remote task-directory tree in one best-effort attempt with its own SFTP channel and the shared retry budget; failures are logged and left to node teardown.
    # ENSURES: Never raises; an already absent directory is a no-op logged at debug level.
    async def cleanup_remote(
        self,
        session: MachineSession,
        remote_dir: str,
        task_id: TaskId | None = None,
    ) -> None:
        """Delete the remote directory tree, best effort after finalisation."""
        path_type = session.path
        rmtree_retry = my_retry()
        # region BLOCK_cleanup_rmtree
        try:
            async with session.open_sftp() as sftp:
                await rmtree_retry(sftp.rmtree)(path_type(remote_dir))
        except SFTPNoSuchFile:
            # Already cleaned (earlier attempt or operator removal).
            logger.debug(
                "remote dir already absent for task_id=%s at %s",
                task_id,
                remote_dir,
            )
        except Exception as err:
            logger.warning(
                "Cannot clean remote dir for task_id=%s at %s: %s "
                "(left for node teardown)",
                task_id,
                remote_dir,
                err,
            )
        else:
            logger.debug(
                "CLEANUP_REMOTE",
                extra={"task_id": task_id, "remote_dir": remote_dir},
            )
        # endregion BLOCK_cleanup_rmtree

    # endregion METHOD_cleanup_remote


# endregion CLASS_OutputDownloader
