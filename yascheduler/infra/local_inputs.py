"""Local capture of a task input snapshot from a submission directory."""
# region MODULE_CONTRACT
# PURPOSE: Turn engine input selections plus a local submission root into the one immutable task-owned snapshot (exact bytes, explicit directories) so submission accepts work independent of the source filesystem (ADR-0019 part 1, task 1.3).
# SCOPE:
# - capture_input_snapshot(root, selections): validate selections, scan only the branches they need, expand matched directories to complete subtrees, read file bytes, and return the normalized snapshot.
# - Non-following directory scans, symlink/unsupported-type rejection, required-selection enforcement, and read-time source rechecks.
# - NOT: task writes, submission-boundary legacy normalization, SQL storage, or SFTP deployment.
# INVARIANTS:
# - Never follows or preserves symbolic links: a selected, matched, or in-subtree link fails with TaskInputCaptureError; unsupported entry types fail the same way when selected.
# - Files are read as raw bytes with no decoding or newline normalization; file contents never enter logs or exceptions.
# - Traversal only joins validated components onto the root, so enumeration cannot escape the task root.
# - Every selection is required: a missing explicit path or a glob with no matches fails the whole capture, naming the selection.
# RATIONALE:
# - Q: Why does traversal mirror the pure selection language instead of reusing a generic walker?
#   A: The domain matcher (task 1.2) pins the glob semantics; capture must agree exactly, so descent applies the same component rules (fnmatchcase per component, `**` as directory levels, trailing `**` directories-only) while pruning branches no pattern can reach.
# - Q: Why are unselected symlinks under a `**` branch skipped, but selected ones rejected?
#   A: `**` consumes directory levels only; a link is not a directory, so it is not consumable and not selected — rejecting every unrelated link would invalidate otherwise valid selections. A link matched by a name pattern, or living inside a matched directory's subtree, is selected and therefore rejected explicitly.
# - Q: Why lstat-then-O_NOFOLLOW-open-then-fstat identity checks?
#   A: Capture is not a filesystem transaction; callers keep sources stable (documented contract). These checks abort capture on the changes that are detectable without one: disappearance, replacement, and type swaps.
# KEYWORDS: task inputs, capture, snapshot, local filesystem, submission root, glob, symbolic links, ADR-0019
# endregion MODULE_CONTRACT

from __future__ import annotations

import errno
import logging
import os
import stat
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING

from yascheduler.domain import (
    TaskInputCaptureError,
    TaskInputDirectory,
    TaskInputFile,
    TaskInputKind,
    TaskInputSnapshot,
    normalize_input_snapshot,
    validate_selection,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from yascheduler.shared import TypeAlias

__all__ = ["capture_input_snapshot"]

logger = logging.getLogger(__name__)

_OPEN_FLAGS: int = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)
_READ_CHUNK_BYTES: int = 1 << 20
_GLOB_METACHARACTERS: str = "*?["
# Diagnostic path for the implicit task root itself (it has no stored entry).
_IMPLICIT_ROOT_PATH: str = "."
# File identity: (st_dev, st_ino) captured at scan time and rechecked at read time.
_Identity: TypeAlias = "tuple[int, int]"


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
    stable until submission returns. Detectable changes (disappearance,
    replacement, type swaps, unreadable sources) abort capture; simultaneous
    modification by another process is not observable and is not promised.
    """
    root = Path(root)
    validated = [validate_selection(selection) for selection in selections]
    _require_root_directory(root)
    logger.debug(
        "CAPTURE",
        extra={"root": str(root), "selections": len(validated)},
    )
    matches: dict[str, TaskInputKind] = {}
    identities: dict[str, _Identity] = {}
    for raw, selection in zip(selections, validated):
        parts = tuple(selection.split("/"))
        if not _select_matches(
            root, parts, (), at_root=True, matches=matches, identities=identities
        ):
            reason = (
                "glob selection matched no entries"
                if _is_glob_pattern(selection)
                else "required selection not found"
            )
            raise TaskInputCaptureError(raw, reason)
    _expand_matched_directories(root, matches, identities)
    contents = _read_files(root, identities)
    entries = [
        (
            TaskInputFile(path=path, content=contents[path])
            if kind is TaskInputKind.FILE
            else TaskInputDirectory(path=path)
        )
        for path, kind in sorted(matches.items())
    ]
    snapshot = normalize_input_snapshot(entries)
    logger.debug(
        "CAPTURE_DONE",
        extra={
            "root": str(root),
            "files": len(contents),
            "directories": len(snapshot) - len(contents),
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
# PURPOSE: Apply one validated selection's components to the filesystem, recording matched entries and returning whether anything matched, mirroring the domain matcher's semantics exactly while scanning only branches the pattern can reach.
# REQUIRES: parts come from a validated selection; rel is the scanned directory's canonical components (empty tuple at the root); matches/identities accumulate across selections (overlaps deduplicate by path).
# ENSURES: A name component matches entries with fnmatchcase (hidden names included); a matched symlink or unsupported type fails explicitly; a matched directory with pattern remainder is descended into; `**` applies its remainder at the current directory (zero levels) and descends into real subdirectories (more levels); a trailing `**` records the scanned directory itself but never the implicit root.
def _select_matches(
    root: Path,
    parts: tuple[str, ...],
    rel: tuple[str, ...],
    *,
    at_root: bool,
    matches: dict[str, TaskInputKind],
    identities: dict[str, _Identity],
) -> bool:
    """Scan one directory level for pattern ``parts``; True when anything matched."""
    matched = False
    head, rest = parts[0], parts[1:]
    if head == "**":
        if not rest:
            if not at_root:
                _record_match(
                    rel, TaskInputKind.DIRECTORY, matches=matches, identities=identities
                )
                matched = True
        else:
            matched = _select_matches(
                root, rest, rel, at_root=at_root, matches=matches, identities=identities
            )
        for name, kind, _what, _ in _scan_directory(root, rel):
            # `**` consumes directory levels only: files, links, and special
            # entries under the branch are not consumable and stay unselected.
            if kind is TaskInputKind.DIRECTORY:
                matched = (
                    _select_matches(
                        root,
                        parts,
                        (*rel, name),
                        at_root=False,
                        matches=matches,
                        identities=identities,
                    )
                    or matched
                )
    else:
        for name, kind, what, identity in _scan_directory(root, rel):
            if not fnmatchcase(name, head):
                continue
            if kind is None:
                raise TaskInputCaptureError(
                    "/".join((*rel, name)),
                    f"selected path is a {what}; only ordinary files and directories are supported",
                )
            if not rest:
                _record_match(
                    (*rel, name),
                    kind,
                    identity=identity,
                    matches=matches,
                    identities=identities,
                )
                matched = True
            elif kind is TaskInputKind.DIRECTORY:
                matched = (
                    _select_matches(
                        root,
                        rest,
                        (*rel, name),
                        at_root=False,
                        matches=matches,
                        identities=identities,
                    )
                    or matched
                )
            # A matched file with pattern remainder cannot complete the pattern.
    return matched


# endregion FUNC__select_matches


# region FUNC__expand_matched_directories
# PURPOSE: Expand every matched directory to its complete subtree — hidden entries and empty directories included — so selecting a directory transfers everything below it (ADR-0019 section 1).
# INVARIANTS: Any symlink or unsupported entry inside a matched subtree fails explicitly; an `expanded` guard keeps overlapping selections and nested matches single-visit.
def _expand_matched_directories(
    root: Path,
    matches: dict[str, TaskInputKind],
    identities: dict[str, _Identity],
) -> None:
    """Add every descendant of each matched directory to ``matches``."""
    expanded: set[str] = set()
    for path, kind in list(matches.items()):
        if kind is TaskInputKind.DIRECTORY and path not in expanded:
            _expand_directory(
                root,
                tuple(path.split("/")),
                matches=matches,
                identities=identities,
                expanded=expanded,
            )


def _expand_directory(
    root: Path,
    rel: tuple[str, ...],
    *,
    matches: dict[str, TaskInputKind],
    identities: dict[str, _Identity],
    expanded: set[str],
) -> None:
    """Recursively record the complete subtree of one matched directory."""
    expanded.add("/".join(rel))
    for name, kind, what, identity in _scan_directory(root, rel):
        child = (*rel, name)
        child_path = "/".join(child)
        if kind is None:
            raise TaskInputCaptureError(
                child_path,
                f"path inside a selected directory is a {what}; "
                "only ordinary files and directories are supported",
            )
        if kind is TaskInputKind.FILE:
            identities.setdefault(child_path, identity)
            matches.setdefault(child_path, TaskInputKind.FILE)
        else:
            matches.setdefault(child_path, TaskInputKind.DIRECTORY)
            _expand_directory(
                root, child, matches=matches, identities=identities, expanded=expanded
            )


# endregion FUNC__expand_matched_directories


# region FUNC__read_files
# PURPOSE: Read every captured file exactly once as raw bytes, rechecking each source against its scan-time identity so detectable mid-capture changes abort the capture.
# INVARIANTS: lstat → O_NOFOLLOW open → fstat; identity (dev, ino) must match both the scan-time value and the read-time lstat; non-regular or swapped sources fail with the path; contents never appear in exceptions or logs.
def _read_files(root: Path, identities: dict[str, _Identity]) -> dict[str, bytes]:
    """Return ``{path: bytes}`` for every captured file."""
    return {
        path: _read_file(root, tuple(path.split("/")), identity)
        for path, identity in identities.items()
    }


def _read_file(root: Path, rel: tuple[str, ...], expected: _Identity) -> bytes:
    """Read one captured file as bytes, rejecting detectable source changes."""
    target = root.joinpath(*rel)
    path = "/".join(rel)
    try:
        before = os.lstat(target)
        fd = os.open(target, _OPEN_FLAGS)
    except OSError as err:
        if err.errno == errno.ENOENT:
            reason = "source disappeared during capture"
        elif err.errno == errno.ELOOP:
            reason = "source became a symbolic link during capture"
        else:
            reason = f"cannot open source: {err}"
        raise TaskInputCaptureError(path, reason) from err
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode):
            raise TaskInputCaptureError(path, "source changed type during capture")
        if (after.st_dev, after.st_ino) != expected:
            raise TaskInputCaptureError(path, "source was replaced during capture")
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise TaskInputCaptureError(path, "source was replaced during capture")
        return _read_all_bytes(fd, path)
    finally:
        os.close(fd)


def _read_all_bytes(fd: int, path: str) -> bytes:
    """Drain one regular-file descriptor to exact bytes without decoding."""
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
        except OSError as err:
            raise TaskInputCaptureError(path, f"cannot read source: {err}") from err
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


# endregion FUNC__read_files


# region FUNC__scan_directory
# PURPOSE: Enumerate one real directory non-following and deterministically (sorted by name), classifying each entry for selection and expansion.
# INVARIANTS: Classification uses DirEntry symlink/`follow_symlinks=False` checks, so links are never followed and hidden entries are yielded like any other; files carry their scan-time identity for the read-time recheck.
def _scan_directory(
    root: Path, rel: tuple[str, ...]
) -> Iterator[tuple[str, TaskInputKind | None, str, _Identity]]:
    """Yield ``(name, kind, what, identity)`` for each entry of one directory.

    ``kind`` is None for entries that cannot be transferred; ``what`` names
    what they are ("symbolic link" or "unsupported filesystem entry type").
    File identity is ``(0, 0)`` for non-file entries.
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
        identity: _Identity = (0, 0)
        if kind is TaskInputKind.FILE:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as err:
                raise TaskInputCaptureError(
                    "/".join((*rel, entry.name)), f"cannot inspect source: {err}"
                ) from err
            identity = entry_stat.st_dev, entry_stat.st_ino
        yield entry.name, kind, what, identity


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


# region FUNC__record_match
# PURPOSE: Record one matched entry, keeping the first classification per path so overlapping selections transfer each path once.
# INVARIANTS: Pure dict bookkeeping; a match always counts toward the selection's required-match outcome even when the path was already recorded.
def _record_match(
    rel: tuple[str, ...],
    kind: TaskInputKind,
    *,
    identity: _Identity = (0, 0),
    matches: dict[str, TaskInputKind],
    identities: dict[str, _Identity],
) -> None:
    """Add one matched entry path to the capture accumulators."""
    path = "/".join(rel)
    if kind is TaskInputKind.FILE:
        identities.setdefault(path, identity)
    matches.setdefault(path, kind)


# endregion FUNC__record_match


# region FUNC__is_glob_pattern
# PURPOSE: Distinguish glob selections (metacharacter present) from explicit paths so a no-match failure names its cause.
# INVARIANTS: Mirrors fnmatch's metacharacter set (`*`, `?`, `[`); an explicit-looking selection therefore reports "not found", a pattern reports "matched nothing".
def _is_glob_pattern(selection: str) -> bool:
    """Return True when the selection contains glob metacharacters."""
    return any(
        character in part
        for part in selection.split("/")
        for character in _GLOB_METACHARACTERS
    )


# endregion FUNC__is_glob_pattern
