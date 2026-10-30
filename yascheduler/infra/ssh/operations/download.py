"""OutputDownloader — tree-preserving SFTP output retrieval with retry, error classification, post-finalisation rmtree. Stateless: takes (log) at construction, (session, ...) per call."""
# region MODULE_CONTRACT
# PURPOSE: Resolve the engine's output selections against the remote task root, recreate their task-relative layout under the local result root without clearing it, transfer files with per-file retry isolation and error classification, and remove the remote tree only after finalisation.
# SCOPE: OutputDownloader class; my_retry partial (canonical location); module-private local destination checks and remote path joining.
# INVARIANTS: Downloads never remove the remote tree — cleanup_remote is the only deletion path and runs after the terminal transition. Local destination traversal never follows symbolic links and never writes through a link or a file/directory type conflict. Resolved entries are transferred to their exact local paths; result-root entries no selection covers are retained.
# DEPENDENCIES: USES API: asyncssh (SFTPClient, SFTP errors)
# KEYWORDS: download, sftp, output, tree, selection, retry, merge, rmtree, cleanup, OutputDownloader
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
import os
import stat
from functools import partial
from typing import TYPE_CHECKING

from asyncssh.sftp import SFTPError, SFTPNoSuchFile

from yascheduler.domain import TaskInputKind, UnsafeTaskPathError
from yascheduler.infra.ssh.platform.types import SFTPRetryExc
from yascheduler.shared import retry

from .remote_selection import resolve_output_selections

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from yascheduler.domain import MachineSession, TaskId

    from .remote_selection import ResolvedEntry

__all__ = ["OutputDownloader", "my_retry"]
logger = logging.getLogger(__name__)
my_retry = partial(retry, on=SFTPRetryExc, max_time=60)


# region FUNC__lstat_mode
# PURPOSE: Read one local destination path's mode without following a symbolic link, mapping absence to None so callers can distinguish "create" from "inspect".
# INVARIANTS: Only a genuinely absent path returns None; permission and other filesystem errors propagate to the caller's per-entry failure classification.
def _lstat_mode(path: Path) -> int | None:
    """Return the non-following mode of ``path``, or None when it does not exist."""
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError:
        return None


# endregion FUNC__lstat_mode


# region FUNC__reject_destination
# PURPOSE: Reject one local destination path whose existing mode is a symbolic link or conflicts with the required entry kind, so retrieval never writes through an unsafe path.
# INVARIANTS: The mode is always read with lstat; symbols and type conflicts raise UnsafeTaskPathError carrying the task-relative path and a concrete reason.
def _reject_destination(mode: int, path: str, *, need_directory: bool) -> None:
    """Raise when an existing destination entry is unsafe for the required kind."""
    if stat.S_ISLNK(mode):
        raise UnsafeTaskPathError(path, "destination traversal met a symbolic link")
    if need_directory and not stat.S_ISDIR(mode):
        raise UnsafeTaskPathError(
            path, "destination traversal met a non-directory entry"
        )
    if not need_directory and not stat.S_ISREG(mode):
        raise UnsafeTaskPathError(
            path, "destination traversal met a non-ordinary-file entry"
        )


# endregion FUNC__reject_destination


# region FUNC__check_local_destination
# PURPOSE: Check every traversed local component and the target itself without following links, so a selected output is only ever written to a real directory path.
# INVARIANTS: The target must stay under the result root; a missing component ends traversal because nothing below it exists to check; a symlink or file/directory conflict raises UnsafeTaskPathError naming the offending task-relative component.
def _check_local_destination(
    local_root: Path, path: str, *, need_directory: bool
) -> Path:
    """Validate the local destination path and return the absolute-enough target."""
    parts = path.split("/")
    target = local_root.joinpath(*parts)
    if not target.is_relative_to(local_root):
        raise UnsafeTaskPathError(path, "destination escapes the result root")
    current = local_root
    for index, part in enumerate(parts[:-1]):
        current = current / part
        mode = _lstat_mode(current)
        if mode is None:
            break
        _reject_destination(mode, "/".join(parts[: index + 1]), need_directory=True)
    mode = _lstat_mode(target)
    if mode is not None:
        _reject_destination(mode, path, need_directory=need_directory)
    return target


# endregion FUNC__check_local_destination


# region FUNC__prepare_local_entry
# PURPOSE: Validate one resolved entry's destination and create the directory structure it needs, returning the exact local file path for a transfer.
# INVARIANTS: A directory entry is created with parents; a file entry's parent directories are created but the file itself is only replaced, never pre-created as a directory; every write goes through the non-following checks.
def _prepare_local_entry(local_root: Path, entry: ResolvedEntry) -> Path:
    """Create the local directory structure for one resolved entry."""
    target = _check_local_destination(
        local_root, entry.path, need_directory=entry.kind is TaskInputKind.DIRECTORY
    )
    if entry.kind is TaskInputKind.DIRECTORY:
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


# endregion FUNC__prepare_local_entry


# region FUNC__record_retrieval_error
# PURPOSE: Classify one failed resolved entry or resolution path into the pass's transient/permanent lists using the existing SFTPRetryExc rule.
# INVARIANTS: SFTPRetryExc is transient, every other exception is permanent; the task-relative path or selection is preserved for the final error text; no automatic retry happens here.
def _record_retrieval_error(
    path: str | None,
    err: Exception,
    transient_errors: list[tuple[str | None, Exception]],
    permanent_errors: list[tuple[str | None, Exception]],
) -> None:
    """Append one retrieval failure to its classification list."""
    if isinstance(err, SFTPRetryExc):
        transient_errors.append((path, err))
    else:
        permanent_errors.append((path, err))


# endregion FUNC__record_retrieval_error


# region FUNC__log_entry
# PURPOSE: Emit one structured per-entry retrieval outcome so a large tree's transfer cost and failures are visible without parsing prose.
# INVARIANTS: Carries the task-relative path, kind, outcome, and failure class only — never file contents or arbitrary exception text.
def _log_entry(
    task_id: TaskId | None,
    entry: ResolvedEntry,
    outcome: str,
    err: Exception | None = None,
) -> None:
    """Emit the per-entry outcome record."""
    logger.debug(
        "OUTPUT_ENTRY",
        extra={
            "task_id": task_id,
            "path": entry.path,
            "kind": entry.kind.value,
            "outcome": outcome,
            "failure": type(err).__name__ if err is not None else "",
        },
    )


# endregion FUNC__log_entry


# region CLASS_OutputDownloader
# PURPOSE: Tree-preserving SFTP output retrieval with retry and error classification; best-effort remote cleanup after finalisation.
class OutputDownloader:
    """Resolve output selections and download their task-relative tree.

    Opens one SFTP client for the resolution pass and a FRESH SFTP client per
    file transfer (dead-connection blast radius bounded to one file).
    Classifies per-entry exceptions into transient (SFTPRetryExc) or permanent
    (everything else). The local result root is merged, never cleared: a
    destination symlink or file/directory conflict is a permanent error for
    that path and nothing is written through it. Downloads never remove the
    remote tree — `cleanup_remote` is the only deletion path and must be
    called only after the task outcome is finalised. Stateless: takes (log)
    at construction, (session, ...) per call. Returns (transient_errors,
    permanent_errors).
    """

    # region METHOD_download_outputs
    # PURPOSE: Resolve the configured selections once, then recreate their exact local layout and transfer each selected file, classifying every failure without aborting independent entries.
    # INVARIANTS: A resolution-channel failure is one session-level transient error; per-selection resolution errors are classified by their exception; local directory creation failures and file-transfer failures use the same transient/permanent lists; no remote deletion occurs.
    async def download_outputs(
        self,
        session: MachineSession,
        remote_dir: str,
        local_dir: Path,
        selections: Sequence[str],
        task_id: TaskId | None = None,
    ) -> tuple[
        list[tuple[str | None, Exception]],
        list[tuple[str | None, Exception]],
    ]:
        """Resolve output selections and download their tree into ``local_dir``."""
        # region BLOCK_download_outputs
        transient_errors: list[tuple[str | None, Exception]] = []
        permanent_errors: list[tuple[str | None, Exception]] = []
        remote_root = session.path(remote_dir)
        resolved: list[ResolvedEntry] = []

        try:
            # region BLOCK_resolve_outputs
            # One channel for the whole resolution pass: listing and metadata
            # reads are idempotent and share the retry budget.
            async with session.open_sftp() as sftp:
                read_dir = my_retry()(sftp.readdir)
                resolved, resolution_errors = await resolve_output_selections(
                    read_dir,
                    remote_root,
                    selections,
                    task_id=task_id,
                )
            # endregion BLOCK_resolve_outputs
        except Exception as err:
            # A channel-open failure means nothing can be resolved; one
            # session-level transient error defers the pass.
            logger.warning(
                "Cannot resolve outputs for task_id=%s from %s: %s",
                task_id,
                remote_dir,
                err,
            )
            transient_errors.append((remote_dir, err))
            return transient_errors, permanent_errors

        for path, err in resolution_errors:
            _record_retrieval_error(path, err, transient_errors, permanent_errors)
            logger.warning(
                "Cannot resolve output for task_id=%s at %s: %s",
                task_id,
                path,
                err,
            )

        try:
            # region BLOCK_per_entry_download
            # Fresh SFTP client per file: a dropped channel on file N invalidates
            # only N's retries. Directory entries need local preparation only.
            get_file = my_retry()
            for entry in resolved:
                try:
                    target = await asyncio.to_thread(
                        _prepare_local_entry, local_dir, entry
                    )
                except Exception as err:
                    _record_retrieval_error(
                        entry.path, err, transient_errors, permanent_errors
                    )
                    _log_entry(task_id, entry, "failed", err)
                    logger.warning(
                        "Cannot prepare local path for task_id=%s at %s: %s",
                        task_id,
                        entry.path,
                        err,
                    )
                    continue
                if entry.kind is TaskInputKind.DIRECTORY:
                    _log_entry(task_id, entry, "directory")
                    continue
                remote_path = remote_root.joinpath(*entry.path.split("/"))
                async with session.open_sftp() as sftp:
                    try:
                        await get_file(sftp.get)(
                            remote_path,
                            target,
                            preserve=True,
                        )
                    except (OSError, SFTPError) as err:
                        # region BLOCK_classify
                        _record_retrieval_error(
                            entry.path, err, transient_errors, permanent_errors
                        )
                        # endregion BLOCK_classify
                        _log_entry(task_id, entry, "failed", err)
                        logger.warning(
                            "Cannot download file for task_id=%s from %s: %s",
                            task_id,
                            entry.path,
                            err,
                        )
                    else:
                        _log_entry(task_id, entry, "downloaded")
            # endregion BLOCK_per_entry_download
        except Exception as err:
            # Catch-all: whole-session failure (open_sftp raising, or a
            # non-(OSError|SFTPError) escape) is transient — the remote dir is
            # preserved and the next pass re-derives every selection.
            logger.warning("Cannot scp from %s: %s", remote_dir, err)
            transient_errors.append((remote_dir, err))

        logger.debug(
            "OUTPUT_DOWNLOAD",
            extra={
                "task_id": task_id,
                "entries": len(resolved),
                "transient": len(transient_errors),
                "permanent": len(permanent_errors),
            },
        )
        return transient_errors, permanent_errors
        # endregion BLOCK_download_outputs

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
