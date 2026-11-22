"""Remote resolution of task output selections over SFTP directory listings."""
# region MODULE_CONTRACT
# PURPOSE: Resolve validated task-relative output selections under a remote task root into the exact file and directory entries retrieval must transfer, reusing the domain selection language so outputs and inputs share one glob contract.
# SCOPE:
# - resolve_output_selections(): per-selection resolution against one listing reader, complete subtree expansion of matched directories, overlap deduplication by path, and per-path/per-selection errors.
# - Non-following classification of remote entries: symbolic links and unsupported types are reported, never followed.
# - NOT: opening SFTP sessions, retrying transport errors, reading file contents, HTTP/config handling, or local destination writes.
# INVARIANTS:
# - Resolution never follows or records a symbolic link; a selected link or a link inside a selected directory is a permanent per-path error.
# - Every configured selection is required: an explicit path or glob that resolves to nothing is reported as that selection's permanent error, while other selections still resolve.
# - Each task-relative path appears at most once regardless of selection order or overlap; matched directories always expand to their complete subtree, hidden entries and empty directories included.
# - Root confinement holds: selections are validated before use and only listing names are joined component-wise under the supplied remote root; "."/".." listing names are never traversed.
# - Exception text and records carry paths and failure classes, never remote file contents.
# RATIONALE:
# - Q: Why does resolution take a listing reader instead of an SFTP client?
#   A: The downloader owns the retried transport callable and the pass-level channel; resolution stays free of asyncssh session lifecycles and is unit-testable against a fake listing.
# - Q: Why walk with the domain matcher and reachability rule instead of a local depth bound?
#   A: The domain pins the one glob language; resolution asks it whether an entry matches and whether anything can still match below a directory, so the remote and the local capture prune identically and neither can drift.
# KEYWORDS: output retrieval, selection, glob, sftp, symlinks, task-relative paths
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from asyncssh.sftp import (
    FILEXFER_TYPE_DIRECTORY,
    FILEXFER_TYPE_REGULAR,
    FILEXFER_TYPE_SYMLINK,
    SFTPNoSuchFile,
    SFTPNoSuchPath,
)

from yascheduler.domain import (
    TaskInputKind,
    UnsafeTaskPathError,
    selection_matches,
    selection_reaches,
    validate_selection,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence
    from pathlib import PurePath

    from asyncssh.sftp import SFTPName

    from yascheduler.domain import TaskId

    ListingReader = Callable[[str], Awaitable[Sequence["SFTPName"]]]

__all__ = ["ResolvedEntry", "resolve_output_selections"]

logger = logging.getLogger(__name__)

_UNMATCHED = "selection matched no entries"


# region CLASS_ResolvedEntry
# PURPOSE: Carry one resolved output path and its kind so the downloader can create directories and transfer files without re-deriving kinds.
# INVARIANTS: Frozen and task-relative — ``path`` is a canonical ``/``-separated relative path below the remote task root; ``kind`` is the true remote entry kind.
@dataclass(frozen=True)
class ResolvedEntry:
    """One selected path of the remote output tree."""

    path: str
    kind: TaskInputKind


# endregion CLASS_ResolvedEntry


# region FUNC_resolve_output_selections
# PURPOSE: Turn the engine's configured output selections into the exact remote entries one retrieval pass must handle, reporting per-selection and per-path failures instead of aborting the whole tree.
# REQUIRES: read_dir is the downloader's retried listing callable taking a POSIX-flavoured path string; remote_root is the task root in the session's path flavour; selections are raw boundary strings.
# ENSURES: Returns the deduplicated, path-sorted entries plus one (path-or-selection, exception) pair per failure; unsafe selections, non-following rejections, and unmatched selections are permanent-domain errors, while a listing failure keeps the original transport exception for the caller's transient/permanent classification.
async def resolve_output_selections(
    read_dir: ListingReader,
    remote_root: PurePath,
    selections: Sequence[str],
    *,
    task_id: TaskId | None = None,
) -> tuple[list[ResolvedEntry], list[tuple[str | None, Exception]]]:
    """Resolve validated output selections into task-relative entries and errors."""
    # region BLOCK_resolve
    entries: dict[str, TaskInputKind] = {}
    errors: list[tuple[str | None, Exception]] = []
    for raw in selections:
        try:
            selection = validate_selection(raw)
        except UnsafeTaskPathError as err:
            errors.append((raw, err))
            continue
        errors_before = len(errors)
        try:
            matched = await _walk_selection(
                read_dir, remote_root, selection, (), entries=entries, errors=errors
            )
        except Exception as err:
            # A listing or metadata failure is reported for this selection and
            # the remaining selections are still resolved. Classification is
            # the downloader's transient/permanent decision.
            errors.append((raw, err))
            matched = False
        if not matched and len(errors) == errors_before:
            errors.append((raw, FileNotFoundError(_UNMATCHED)))
    errors = _deduplicate_errors(errors)
    resolved = sorted(
        (ResolvedEntry(path=path, kind=kind) for path, kind in entries.items()),
        key=lambda entry: entry.path,
    )
    logger.debug(
        "OUTPUT_RESOLVE",
        extra={
            "task_id": task_id,
            "selections": len(selections),
            "entries": len(resolved),
            "files": sum(1 for entry in resolved if entry.kind is TaskInputKind.FILE),
            "directories": sum(
                1 for entry in resolved if entry.kind is TaskInputKind.DIRECTORY
            ),
            "errors": len(errors),
        },
    )
    return resolved, errors
    # endregion BLOCK_resolve


# endregion FUNC_resolve_output_selections


# region FUNC__deduplicate_errors
# PURPOSE: Collapse identical (path, failure) pairs produced when overlapping selections traverse the same rejected path, so one bad entry is reported once.
# INVARIANTS: Preserves first-seen order; distinguishes failures by path, exception type, and message; different selections keep their own unmatched records.
def _deduplicate_errors(
    errors: list[tuple[str | None, Exception]],
) -> list[tuple[str | None, Exception]]:
    """Return failures with identical path and failure collapsed once."""
    seen: set[tuple[str | None, type[Exception], str]] = set()
    unique: list[tuple[str | None, Exception]] = []
    for path, err in errors:
        key = (path, type(err), str(err))
        if key in seen:
            continue
        seen.add(key)
        unique.append((path, err))
    return unique


# endregion FUNC__deduplicate_errors


# region FUNC__walk_selection
# PURPOSE: Walk one directory level applying the domain matcher to each real entry, expanding matched directories to their subtree and descending only where the selection can still match.
# INVARIANTS: Only ordinary files and directories are candidates; a matched directory is expanded completely and not walked again as a branch; descent follows the domain reachability rule (selection_reaches), so a branch the selection cannot cover is never listed; a failed listing propagates for per-selection classification.
async def _walk_selection(
    read_dir: ListingReader,
    remote_root: PurePath,
    selection: str,
    rel: tuple[str, ...],
    *,
    entries: dict[str, TaskInputKind],
    errors: list[tuple[str | None, Exception]],
) -> bool:
    """Walk one remote directory for a selection; True when anything matched."""
    try:
        listing = await read_dir(_remote_path(remote_root, rel).as_posix())
    except (SFTPNoSuchFile, SFTPNoSuchPath):
        if not rel:
            # A missing remote task root means every selection matched nothing.
            return False
        raise
    matched = False
    for name, entry_type in _listing_entries(listing):
        child = (*rel, name)
        path = "/".join(child)
        kind = _entry_kind(entry_type)
        if kind is None:
            if selection_matches(selection, path, TaskInputKind.FILE):
                errors.append(
                    (
                        path,
                        UnsafeTaskPathError(path, _unsupported_reason(entry_type)),
                    )
                )
            continue
        if selection_matches(selection, path, kind):
            matched = True
            entries.setdefault(path, kind)
            if kind is TaskInputKind.DIRECTORY:
                await _expand_subtree(read_dir, remote_root, child, entries, errors)
        elif kind is TaskInputKind.DIRECTORY and selection_reaches(selection, path):
            matched = (
                await _walk_selection(
                    read_dir,
                    remote_root,
                    selection,
                    child,
                    entries=entries,
                    errors=errors,
                )
                or matched
            )
    return matched


# endregion FUNC__walk_selection


# region FUNC__expand_subtree
# PURPOSE: Record the complete subtree of one matched directory — hidden entries and empty directories included — so a selected directory transfers everything below it.
# INVARIANTS: Recursion only follows real directories; a symbolic link or unsupported entry inside the selected tree is a permanent per-path error and is never followed or recorded.
async def _expand_subtree(
    read_dir: ListingReader,
    remote_root: PurePath,
    rel: tuple[str, ...],
    entries: dict[str, TaskInputKind],
    errors: list[tuple[str | None, Exception]],
) -> None:
    """Record every descendant of one matched directory."""
    for name, entry_type in _listing_entries(
        await read_dir(_remote_path(remote_root, rel).as_posix())
    ):
        child = (*rel, name)
        path = "/".join(child)
        kind = _entry_kind(entry_type)
        if kind is None:
            errors.append(
                (
                    path,
                    UnsafeTaskPathError(path, _unsupported_reason(entry_type)),
                )
            )
            continue
        entries.setdefault(path, kind)
        if kind is TaskInputKind.DIRECTORY:
            await _expand_subtree(read_dir, remote_root, child, entries, errors)


# endregion FUNC__expand_subtree


# region FUNC__listing_entries
# PURPOSE: Yield ``(name, attrs.type)`` for each real listing entry, excluding the traversal shorthand names a server must not offer.
# INVARIANTS: Pure iteration over an SFTP readdir result; ``.`` and ``..`` are dropped so no joined path can escape the remote task root.
def _listing_entries(listing: Sequence[SFTPName]) -> Iterator[tuple[str, int]]:
    """Yield the usable name and file type of each listing entry."""
    for entry in listing:
        name = str(entry.filename)
        if name in (".", ".."):
            continue
        yield name, entry.attrs.type


# endregion FUNC__listing_entries


# region FUNC__entry_kind
# PURPOSE: Map an SFTP file type to the shared entry kind, returning None for anything that must not be transferred.
# INVARIANTS: Only regular files and directories map to a kind; symlinks and every other type (special, unknown) map to None so callers reject them explicitly.
def _entry_kind(entry_type: int) -> TaskInputKind | None:
    """Return the shared entry kind for an SFTP file type, or None."""
    if entry_type == FILEXFER_TYPE_REGULAR:
        return TaskInputKind.FILE
    if entry_type == FILEXFER_TYPE_DIRECTORY:
        return TaskInputKind.DIRECTORY
    return None


# endregion FUNC__entry_kind


# region FUNC__unsupported_reason
# PURPOSE: Name a non-transferable remote entry precisely so logs and task errors distinguish a symbolic link from other unsupported types.
# INVARIANTS: Pure mapping; never inspects a link target.
def _unsupported_reason(entry_type: int) -> str:
    """Return the diagnostic reason for an unsupported SFTP entry type."""
    if entry_type == FILEXFER_TYPE_SYMLINK:
        return "symbolic link"
    return "unsupported filesystem entry type"


# endregion FUNC__unsupported_reason


# region FUNC__remote_path
# PURPOSE: Join one canonical component tuple onto the remote root in the root's path flavour, never by splitting a name on the current platform, and keep every joined path confined to that root.
# INVARIANTS: Empty tuple returns the root itself; ``.``/``..`` names never reach this function because listing entries filter them; a Windows drive-shaped listing name that would discard the root raises UnsafeTaskPathError naming the relative path.
def _remote_path(root: PurePath, rel: tuple[str, ...]) -> PurePath:
    """Return the confined remote path of one relative component tuple."""
    target = root.joinpath(*rel) if rel else root
    if not target.is_relative_to(root):
        raise UnsafeTaskPathError("/".join(rel), "remote path escapes the task root")
    return target


# endregion FUNC__remote_path
