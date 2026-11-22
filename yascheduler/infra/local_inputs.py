"""Local capture of a task input snapshot from a submission directory."""
# region MODULE_CONTRACT
# PURPOSE: Turn engine input selections plus a local submission root into the one immutable task-owned snapshot (exact bytes, explicit directories) so submission accepts work independent of the source filesystem.
# SCOPE:
# - capture_input_snapshot(root, selections): validate selections, scan only the branches they need, expand matched directories to complete subtrees, read file bytes, and return the normalized snapshot.
# - Non-following directory scans, symlink and unsupported-type rejection, required-selection enforcement.
# - NOT: task writes, submission-boundary legacy normalization, SQL storage, SFTP deployment, or defence against a source tree rewritten while capture runs.
# INVARIANTS:
# - Never follows or preserves symbolic links: a selected, matched, or in-subtree link fails with TaskInputCaptureError; unsupported entry types fail the same way when selected.
# - Files are read as raw bytes with no decoding or newline normalization; file contents never enter logs or exceptions.
# - Capture start/completion/failure records carry counts and selection/path context, never source bytes or arbitrary exception text.
# - Traversal only joins validated components onto the root.
# - Every selection is required: a missing explicit path or a glob with no matches fails the whole capture, naming the selection.
# RATIONALE:
# - Q: Why does traversal ask the domain instead of keeping its own component matcher?
#   A: The domain pins the glob semantics; capture must agree exactly. Capture owns listing, classification, and error policy, and asks the domain the three questions a walk needs — does this entry match, can anything still match below this directory, does a component name this entry — so pruning stays exact and no part of the language lives here.
# - Q: Why are unselected symlinks under a `**` branch skipped, but selected ones rejected?
#   A: `**` consumes directory levels only; a link is not a directory, so it is not consumable and not selected — rejecting every unrelated link would invalidate otherwise valid selections. A link matched by a name pattern, or living inside a matched directory's subtree, is selected and therefore rejected explicitly.
# - Q: Why is capture not defended against the source changing mid-read?
#   A: Callers keep the source tree stable until capture returns, and a writer racing the capture is outside the model: whoever can rewrite the submission directory can submit their own task instead, so no check would move the trust boundary — it would only build the appearance of one. O_NOFOLLOW plus a regular-file check keep the open itself honest (a replaced entry fails cleanly instead of blocking), and nothing beyond that is claimed.
# KEYWORDS: task inputs, capture, snapshot, local filesystem, submission root, glob, symbolic links
# endregion MODULE_CONTRACT

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from yascheduler.domain import (
    TaskInputCaptureError,
    TaskInputDirectory,
    TaskInputFile,
    TaskInputKind,
    TaskInputSnapshot,
    is_glob_pattern,
    normalize_input_snapshot,
    selection_matches,
    selection_names_entry,
    selection_reaches,
    validate_selection,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = ["capture_input_snapshot"]

logger = logging.getLogger(__name__)

# O_NONBLOCK keeps an open on a non-regular entry, a FIFO above all, from
# blocking the capture forever; the regular-file check after the open rejects
# it instead of waiting for a writer.
_OPEN_FLAGS: int = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)
# Diagnostic path for the implicit task root itself (it has no stored entry).
_IMPLICIT_ROOT_PATH: str = "."


# region FUNC_capture_input_snapshot
# PURPOSE: Resolve every selection under the submission root and return the complete immutable snapshot, so the accepted task no longer depends on the source filesystem.
# REQUIRES: selections are raw boundary strings; root is the caller-supplied submission root (the CLI uses its working directory; the Python API never infers one).
# ENSURES: Returns the normalized snapshot — exact file bytes, explicit directory entries including empty ones, materialized parents, each path once; every selection matched at least one entry or TaskInputCaptureError names it; unsafe selections raise UnsafeTaskPathError before any filesystem access; callers must keep sources stable until submission returns.
def capture_input_snapshot(root: Path, selections: Sequence[str]) -> TaskInputSnapshot:
    """Capture the selected input tree under ``root`` as a task snapshot.

    Reads ordinary file contents as exact bytes (no decoding, no newline
    normalization) and records directory entries explicitly so empty
    directories survive. Every selection is required: an absent explicit path
    or a glob with no matches raises :class:`TaskInputCaptureError` naming the
    selection, and no partial snapshot is returned.

    Symbolic links are never followed: a selected or glob-matched link —
    including a dangling one — and any link inside a selected directory's
    subtree fail explicitly, as do selected entries of unsupported filesystem
    types.

    Capture is not a filesystem transaction: callers must keep the sources
    stable until capture returns. A missing or unreadable source fails the
    capture with its path; a source rewritten by another process underneath
    the capture is not detected and is not promised to be.
    """
    root = Path(root)
    logger.debug(
        "CAPTURE",
        extra={
            "root": str(root),
            "selections": len(selections),
            "entry_count": 0,
        },
    )
    matches: dict[str, TaskInputKind] = {}
    raw: str | None = None
    try:
        validated = [validate_selection(selection) for selection in selections]
        _require_root_directory(root)
        for raw, selection in zip(selections, validated):
            if not _select_matches(root, selection, (), matches=matches):
                reason = (
                    "glob selection matched no entries"
                    if is_glob_pattern(selection)
                    else "required selection not found"
                )
                # Boundary logging classifies this validation failure; keep the raise local.
                raise TaskInputCaptureError(raw, reason)  # noqa: TRY301
        # Expansion and reads operate on the merged tree, not the last selection.
        raw = None
        _expand_matched_directories(root, matches)
        contents = _read_files(root, matches)
        entries = [
            (
                TaskInputFile(path=path, content=contents[path])
                if kind is TaskInputKind.FILE
                else TaskInputDirectory(path=path)
            )
            for path, kind in sorted(matches.items())
        ]
        snapshot = normalize_input_snapshot(entries)
    except Exception as err:
        logger.debug(
            "CAPTURE_FAILED",
            extra={
                "root": str(root),
                "selection": raw,
                "path": getattr(err, "path", "."),
                "entry_count": len(matches),
                "failure": type(err).__name__,
            },
        )
        raise
    logger.debug(
        "CAPTURE_DONE",
        extra={
            "root": str(root),
            "files": len(contents),
            "directories": len(snapshot) - len(contents),
            "entry_count": len(snapshot),
        },
    )
    return snapshot


# endregion FUNC_capture_input_snapshot


# region FUNC__require_root_directory
# PURPOSE: Fail with root context before traversal when the submission root is missing or not a directory.
# INVARIANTS: Uses a following stat on the root itself — the root is the caller-supplied base, not a selected entry, so a root symlink (e.g. a linked working directory) stays usable.
def _require_root_directory(root: Path) -> None:
    """Raise TaskInputCaptureError unless ``root`` is an accessible directory."""
    try:
        root_stat = root.stat()
    except OSError as err:
        message = f"submission root {root} cannot be accessed: {err}"
        raise TaskInputCaptureError(_IMPLICIT_ROOT_PATH, message) from err
    if not stat.S_ISDIR(root_stat.st_mode):
        message = f"submission root {root} is not a directory"
        raise TaskInputCaptureError(_IMPLICIT_ROOT_PATH, message)


# endregion FUNC__require_root_directory


# region FUNC__select_matches
# PURPOSE: Apply one validated selection to the filesystem, recording the entries it selects and answering whether it selected anything, while scanning only the branches the selection can still cover.
# REQUIRES: selection passed validate_selection; rel is the scanned directory's canonical components (empty tuple at the root); matches accumulates across selections (overlaps deduplicate by path).
# ENSURES: The scanned directory itself is recorded when the selection selects it, never at the implicit root; an entry is recorded exactly when the selection selects it; an untransferable entry the selection names, or one inside a selected directory, fails explicitly; a directory is descended into only while the selection can still match below it.
def _select_matches(
    root: Path,
    selection: str,
    rel: tuple[str, ...],
    *,
    matches: dict[str, TaskInputKind],
) -> bool:
    """Scan one directory for ``selection``; True when it selected anything."""
    directory = "/".join(rel)
    matched = False
    # A selection covering this directory selects it; the implicit root is
    # never a task entry.
    if directory and selection_matches(selection, directory, TaskInputKind.DIRECTORY):
        matches.setdefault(directory, TaskInputKind.DIRECTORY)
        matched = True
    for name, kind, what in _scan_directory(root, rel):
        child = (*rel, name)
        path = "/".join(child)
        if kind is None:
            # An untransferable entry is rejected only when a component names
            # it; `**` passes over directory levels and names nothing.
            if selection_names_entry(selection, directory, name):
                raise TaskInputCaptureError(
                    path,
                    f"selected path is a {what}; "
                    "only ordinary files and directories are supported",
                )
            continue
        if selection_matches(selection, path, kind):
            matches.setdefault(path, kind)
            matched = True
        if kind is TaskInputKind.DIRECTORY and selection_reaches(selection, path):
            matched = (
                _select_matches(root, selection, child, matches=matches) or matched
            )
    return matched


# endregion FUNC__select_matches


# region FUNC__expand_matched_directories
# PURPOSE: Expand every matched directory to its complete subtree — hidden entries and empty directories included — so selecting a directory transfers everything below it.
# INVARIANTS: Any symlink or unsupported entry inside a matched subtree fails explicitly; an `expanded` guard keeps overlapping selections and nested matches single-visit.
def _expand_matched_directories(root: Path, matches: dict[str, TaskInputKind]) -> None:
    """Add every descendant of each matched directory to ``matches``."""
    expanded: set[str] = set()
    for path, kind in list(matches.items()):
        if kind is TaskInputKind.DIRECTORY and path not in expanded:
            _expand_directory(
                root, tuple(path.split("/")), matches=matches, expanded=expanded
            )


def _expand_directory(
    root: Path,
    rel: tuple[str, ...],
    *,
    matches: dict[str, TaskInputKind],
    expanded: set[str],
) -> None:
    """Recursively record the complete subtree of one matched directory."""
    expanded.add("/".join(rel))
    for name, kind, what in _scan_directory(root, rel):
        child = (*rel, name)
        if kind is None:
            raise TaskInputCaptureError(
                "/".join(child),
                f"path inside a selected directory is a {what}; "
                "only ordinary files and directories are supported",
            )
        matches.setdefault("/".join(child), kind)
        if kind is TaskInputKind.DIRECTORY:
            _expand_directory(root, child, matches=matches, expanded=expanded)


# endregion FUNC__expand_matched_directories


# region FUNC__read_files
# PURPOSE: Read every captured file exactly once as raw bytes.
# INVARIANTS: One O_NOFOLLOW open per file, a regular-file check before reading; a missing, linked, non-regular, or unreadable source fails with its path; contents never appear in exceptions or logs.
def _read_files(root: Path, matches: dict[str, TaskInputKind]) -> dict[str, bytes]:
    """Return ``{path: bytes}`` for every captured file."""
    return {
        path: _read_file(root, tuple(path.split("/")))
        for path, kind in matches.items()
        if kind is TaskInputKind.FILE
    }


def _read_file(root: Path, rel: tuple[str, ...]) -> bytes:
    """Read one captured file as bytes without following a symbolic link."""
    target = root.joinpath(*rel)
    path = "/".join(rel)
    try:
        fd = os.open(target, _OPEN_FLAGS)
    except OSError as err:
        if err.errno == errno.ENOENT:
            reason = "source is missing"
        elif err.errno == errno.ELOOP:
            reason = "source is a symbolic link"
        else:
            reason = f"cannot open source: {err}"
        raise TaskInputCaptureError(path, reason) from err
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise TaskInputCaptureError(path, "source is not a regular file")
        return _read_all_bytes(fd, path)
    finally:
        os.close(fd)


def _read_all_bytes(fd: int, path: str) -> bytes:
    """Drain one regular-file descriptor to exact bytes without decoding."""
    try:
        return os.fdopen(fd, "rb", closefd=False).read()
    except OSError as err:
        raise TaskInputCaptureError(path, f"cannot read source: {err}") from err


# endregion FUNC__read_files


# region FUNC__scan_directory
# PURPOSE: Enumerate one directory non-following and deterministically (sorted by name), classifying each entry for selection and expansion.
# INVARIANTS: Classification uses DirEntry symlink/`follow_symlinks=False` checks, so links are never followed and hidden entries are yielded like any other.
def _scan_directory(
    root: Path, rel: tuple[str, ...]
) -> Iterator[tuple[str, TaskInputKind | None, str]]:
    """Yield ``(name, kind, what)`` for each entry of one directory.

    ``kind`` is None for entries that cannot be transferred; ``what`` names
    what they are ("symbolic link" or "special filesystem entry").
    """
    directory = root.joinpath(*rel)
    try:
        with os.scandir(directory) as iterator:
            dir_entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as err:
        raise TaskInputCaptureError(
            "/".join(rel) or ".", f"cannot list directory: {err}"
        ) from err
    for entry in dir_entries:
        kind, what = _classify_entry(entry)
        yield entry.name, kind, what


def _classify_entry(entry: os.DirEntry) -> tuple[TaskInputKind | None, str]:
    """Classify one scanned entry without following symbolic links."""
    if entry.is_symlink():
        return None, "symbolic link"
    if entry.is_file(follow_symlinks=False):
        return TaskInputKind.FILE, "ordinary file"
    if entry.is_dir(follow_symlinks=False):
        return TaskInputKind.DIRECTORY, "directory"
    return None, "special filesystem entry"


# endregion FUNC__scan_directory
