"""Task input snapshot value objects and pure selection rules."""
# region MODULE_CONTRACT
# PURPOSE: Give task-owned inputs one typed, engine-independent model — validated task-relative paths, component-aware glob selection, and immutable snapshot entries — so submission capture, normalization, persistence, and deployment share a single contract (ADR-0019 part 1).
# SCOPE:
# - Value objects TaskInputFile/TaskInputDirectory, the TaskInputEntry union, the TaskInputSnapshot alias, and the TaskInputKind discriminator.
# - Pure selection rules: explicit path validation, selection validation, and per-component matching over relative paths and entry kinds.
# - Snapshot normalization: materializing missing parents, rejecting duplicates and kind conflicts, copying to an immutable tuple.
# - NOT: filesystem traversal and byte reads (infra local-input module), engine input declarations, SQL storage, or SFTP deployment.
# INVARIANTS: No filesystem, SQL, SSH, or config imports — pure data and rules only; every exported rule is total over validated input and raises a domain ValidationError subclass otherwise; entry contents are bytes and are never decoded here.
# RATIONALE:
# - Q: Why in the domain layer instead of the entrypoints normalizer or infra capture?
#   A: The selection language and snapshot invariants are scheduler business rules consumed by several layers (capture, boundary normalization, submit validation, deployment). Keeping them pure in the domain prevents each consumer from re-implementing the ADR-0019 semantics.
# - Q: Why does a kind property exist when the entry type already encodes it?
#   A: Selection matching and later persistence need a uniform discriminator without isinstance dispatch at every consumer.
# KEYWORDS: task inputs, snapshot, selection, glob, path validation, ADR-0019, entry kinds
# endregion MODULE_CONTRACT

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Union

from yascheduler.domain.exceptions import (
    InvalidTaskSnapshotError,
    MissingInputFileError,
    UnsafeTaskPathError,
)
from yascheduler.shared import StrEnum, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = [
    "TaskInputDirectory",
    "TaskInputEntry",
    "TaskInputFile",
    "TaskInputKind",
    "TaskInputSnapshot",
    "is_glob_pattern",
    "normalize_input_snapshot",
    "selection_matches",
    "validate_selection",
    "validate_snapshot_selections",
    "validate_task_path",
]


# region CLASS_TaskInputKind
# PURPOSE: Discriminate snapshot entries as files or directories for selection matching and persistence without isinstance dispatch.
# INVARIANTS: Members mirror the storage `kind` values (`file`, `directory`) and the public query projection (ADR-0019).
class TaskInputKind(StrEnum):
    """Kind of one task input snapshot entry."""

    FILE = "file"
    DIRECTORY = "directory"


# endregion CLASS_TaskInputKind


# region CLASS_TaskInputFile
# PURPOSE: Carry one captured input file's task-relative path and exact bytes so deployment reproduces submission-time content without the source filesystem.
# INVARIANTS: Frozen; ``path`` is a str and ``content`` is bytes — no text decoding, newline normalization, or encoding guess happens here; path safety and runtime type enforcement live at the snapshot normalization boundary (ADR-0019), not per entry, so the value object stays a pure carrier.
@dataclass(frozen=True)
class TaskInputFile:
    """One input file entry of a task's input snapshot."""

    path: str
    content: bytes

    @property
    def kind(self) -> TaskInputKind:
        """Discriminate this entry as a file."""
        return TaskInputKind.FILE


# endregion CLASS_TaskInputFile


# region CLASS_TaskInputDirectory
# PURPOSE: Represent one directory of the captured input tree explicitly so empty directories survive storage and deployment, and so matched directories can expand to their subtree.
# INVARIANTS: Frozen; ``path`` is a str; carries no content by design — a directory is structure, not bytes; runtime type enforcement lives at the snapshot normalization boundary.
@dataclass(frozen=True)
class TaskInputDirectory:
    """One directory entry of a task's input snapshot."""

    path: str

    @property
    def kind(self) -> TaskInputKind:
        """Discriminate this entry as a directory."""
        return TaskInputKind.DIRECTORY


# endregion CLASS_TaskInputDirectory


TaskInputEntry: TypeAlias = Union[TaskInputFile, TaskInputDirectory]
TaskInputSnapshot: TypeAlias = "tuple[TaskInputEntry, ...]"


# region FUNC__validated_components
# PURPOSE: Apply the shared task-root safety rules to one raw path or selection and return its canonical components, so validate_task_path and validate_selection cannot diverge.
# INVARIANTS: Pure string rules; rejects Windows drive roots and drive-shaped components, backslashes, leading separators, `..` components, and the task root itself (UnsafeTaskPathError); drops empty and `.` components without touching glob metacharacters.
_DRIVE_ROOT = re.compile(r"[A-Za-z]:")


def _validated_components(raw: object, what: str) -> tuple[str, ...]:
    """Return the canonical safe components of one path or selection."""
    if not isinstance(raw, str):
        placeholder = f"<{what}>"
        reason = f"{what} must be a str"
        raise UnsafeTaskPathError(placeholder, reason)
    if _DRIVE_ROOT.match(raw):
        raise UnsafeTaskPathError(raw, "Windows drive root")
    if "\\" in raw:
        raise UnsafeTaskPathError(raw, "UNC root or backslash separator")
    if raw.startswith("/"):
        raise UnsafeTaskPathError(raw, "absolute path")
    components: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafeTaskPathError(raw, "'..' component escapes the task root")
        if _DRIVE_ROOT.match(part):
            # A drive-shaped component (`C:`, `C:file`) collapses or redirects
            # the joined path on Windows remotes, aliasing distinct entries.
            raise UnsafeTaskPathError(raw, "Windows drive component")
        components.append(part)
    if not components:
        raise UnsafeTaskPathError(raw, "names the task root itself")
    return tuple(components)


# endregion FUNC__validated_components


# region FUNC_validate_task_path
# PURPOSE: Canonicalize one explicit task-relative entry path so only safe, task-root-confined paths enter a snapshot or destination traversal.
# REQUIRES: raw is a str; callers pass unchecked boundary input.
# ENSURES: The returned path is canonical — ``/``-separated, no leading or trailing separators, no empty or ``.`` components, no ``..`` components, and no backslashes; rejection raises UnsafeTaskPathError naming the offending path.
def validate_task_path(raw: str) -> str:
    """Validate and canonicalize one explicit task-relative path.

    Rejects absolute POSIX paths, Windows drive and UNC roots, drive-shaped
    components anywhere in the path (they alias or redirect joined paths on
    Windows remotes), ``..`` components, backslashes, and the task root itself
    (the root is implicit and has no stored entry). Redundant separators and
    ``.`` components are normalized away. Returns the canonical ``/``-separated
    relative path.
    """
    return "/".join(_validated_components(raw, "task path"))


# endregion FUNC_validate_task_path


# region FUNC_validate_selection
# PURPOSE: Apply the same task-root safety rules to a selection pattern while preserving its glob metacharacters, so matching can never reach outside the task root.
# REQUIRES: selection is a str; callers pass unchecked boundary input.
# ENSURES: The returned selection keeps ``*``, ``?``, and ``[...]`` class components intact, is ``/``-separated with no unsafe components; rejection raises UnsafeTaskPathError naming the offending selection.
def validate_selection(selection: str) -> str:
    """Validate one selection pattern and normalize its safe components.

    Rejections match :func:`validate_task_path`: absolute paths, Windows
    drive/UNC roots, ``..`` components, backslashes, and the task root.
    Glob metacharacters stay literal to the caller; shell variable, home, and
    brace expansion do not exist in this language.
    """
    return "/".join(_validated_components(selection, "selection"))


# endregion FUNC_validate_selection


# region FUNC_is_glob_pattern
# PURPOSE: Distinguish glob selections and engine input declarations (metacharacter present) from explicit paths, so every consumer — capture diagnostics, legacy payload classification, migration — applies one fnmatch metacharacter definition.
# REQUIRES: selection is any raw component string; validation is not required.
# ENSURES: True iff any component contains `*`, `?`, or `[` — exactly the characters the component matcher treats as wildcards; character classes expressing literal metacharacters therefore still count as patterns.
def is_glob_pattern(selection: str) -> bool:
    """Return True when the selection or declaration contains glob metacharacters."""
    return any(
        character in part for part in selection.split("/") for character in "*?["
    )


# endregion FUNC_is_glob_pattern


# region FUNC_validate_snapshot_selections
# PURPOSE: Enforce the one input-validation rule — every engine input declaration is satisfied by at least one snapshot entry — so submission rejects incomplete snapshots before any task write, replacing both the engine's key-presence check and the use case's metadata loop (ADR-0019, task-input-snapshots task 2.2).
# REQUIRES: declarations are the engine's raw input selections; snapshot is a normalized snapshot.
# ENSURES: Each declaration is validated and matched against the snapshot with the shared selection language; the first unsatisfied declaration raises MissingInputFileError naming the engine and the declaration; an empty declaration set is trivially satisfied, and an empty snapshot satisfies only an empty declaration set.
def validate_snapshot_selections(
    engine_name: str,
    declarations: Sequence[str],
    snapshot: TaskInputSnapshot,
) -> None:
    """Raise MissingInputFileError for each engine declaration the snapshot misses."""
    entries = tuple(snapshot)
    for declaration in declarations:
        selection = validate_selection(declaration)
        if not any(
            selection_matches(selection, entry.path, entry.kind) for entry in entries
        ):
            raise MissingInputFileError(engine_name, declaration)


# endregion FUNC_validate_snapshot_selections


# region FUNC_selection_matches
# PURPOSE: Answer whether one validated selection selects one validated entry, using the shared component-aware glob language so input capture and coverage checks agree (ADR-0019 section 1).
# REQUIRES: selection passed validate_selection; path is canonical (validate_task_path) and kind is the entry's true kind.
# ENSURES: Matching is case-sensitive and component-aware — ``*``, ``?``, and character classes match within one ``/``-separated component including hidden names; a ``**`` component matches zero or more directory levels, with a zero-level match applying to directories only; a literal selection matches exactly that path; no path outside the task root can match.
def selection_matches(selection: str, path: str, kind: TaskInputKind) -> bool:
    """Return True when the selection selects the given entry.

    A glob matching a directory selects that directory; expanding it to the
    complete subtree is the caller's tree responsibility. A selection that
    matched nothing is the caller's required-selection error, not a match
    outcome here.
    """
    parts = tuple(selection.split("/"))
    # A trailing ``**`` component selects directory levels; the matched entry
    # itself must therefore be a directory (files below it are covered by the
    # caller's subtree expansion of matched directories).
    if parts[-1] == "**" and kind is not TaskInputKind.DIRECTORY:
        return False
    return _components_match(parts, tuple(path.split("/")), kind)


# endregion FUNC_selection_matches


# region FUNC__components_match
# PURPOSE: Component-wise matcher implementing the shared glob language — fnmatchcase per component plus `**` as zero or more directory levels — so both transfer directions get identical semantics.
# INVARIANTS: Pure recursion over component tuples; `*`/`?`/classes never cross `/` (fnmatchcase is case-sensitive); `**` consumes only directory components — a final component is consumable only for a directory entry, so `**` never matches a file.
def _components_match(
    selection_parts: tuple[str, ...],
    path_parts: tuple[str, ...],
    kind: TaskInputKind,
) -> bool:
    """Match canonical path components against selection components."""
    if not selection_parts:
        return not path_parts
    head, rest = selection_parts[0], selection_parts[1:]
    if head == "**":
        if _components_match(rest, path_parts, kind):
            return True
        if not path_parts:
            return False
        if len(path_parts) == 1 and kind is not TaskInputKind.DIRECTORY:
            return False
        return _components_match(selection_parts, path_parts[1:], kind)
    if not path_parts or not fnmatchcase(path_parts[0], head):
        return False
    return _components_match(rest, path_parts[1:], kind)


# endregion FUNC__components_match


# region FUNC_normalize_input_snapshot
# PURPOSE: Turn a caller- or capture-supplied entry sequence into the one immutable snapshot a task owns, enforcing the ADR-0019 owned-entry invariants once for every consumer.
# REQUIRES: entries is a sequence of TaskInputFile/TaskInputDirectory values; the caller keeps sources stable — this function fetches no filesystem data.
# ENSURES: The returned snapshot is a tuple ordered by canonical path containing every valid entry plus materialized parent directory entries, each path once; duplicates, file/directory ancestor conflicts, non-entry objects, and unsafe paths raise InvalidTaskSnapshotError/UnsafeTaskPathError with the offending path; the empty sequence is a valid empty snapshot.
def normalize_input_snapshot(entries: Sequence[TaskInputEntry]) -> TaskInputSnapshot:
    """Validate entries and return the immutable task-owned input snapshot.

    Materializes missing parent directories so the tree is reconstructable,
    rejects duplicate explicit paths and file/ancestor kind conflicts, and
    copies the input sequence to an immutable tuple.
    """
    try:
        items = list(entries)
    except TypeError as err:
        placeholder = "<entries>"
        reason = "entries must be an iterable of task input entries"
        raise InvalidTaskSnapshotError(placeholder, reason) from err

    by_path: dict[str, TaskInputEntry] = {}
    kinds: dict[str, TaskInputKind] = {}
    for position, entry in enumerate(items):
        if not isinstance(entry, (TaskInputFile, TaskInputDirectory)):
            placeholder = f"<entry {position}>"
            reason = f"item {position} is not a task input entry"
            raise InvalidTaskSnapshotError(placeholder, reason)
        canonical = validate_task_path(entry.path)
        if isinstance(entry, TaskInputFile) and not isinstance(entry.content, bytes):
            raise InvalidTaskSnapshotError(canonical, "file content must be bytes")
        if canonical in kinds:
            existing = kinds[canonical]
            if existing is not entry.kind:
                raise InvalidTaskSnapshotError(
                    canonical,
                    f"path declared as both {existing.value} and {entry.kind.value}",
                )
            raise InvalidTaskSnapshotError(canonical, "duplicate entry path")
        by_path[canonical] = (
            entry if canonical == entry.path else _with_canonical_path(entry, canonical)
        )
        kinds[canonical] = entry.kind

    for path in by_path:
        for ancestor in _strict_ancestors(path):
            if kinds.get(ancestor) is TaskInputKind.FILE:
                raise InvalidTaskSnapshotError(
                    ancestor, f"file entry cannot contain child {path!r}"
                )

    merged = dict(by_path)
    for path in by_path:
        for ancestor in _strict_ancestors(path):
            if ancestor not in merged:
                merged[ancestor] = TaskInputDirectory(path=ancestor)

    return tuple(sorted(merged.values(), key=lambda entry: entry.path))


# endregion FUNC_normalize_input_snapshot


# region FUNC__strict_ancestors
# PURPOSE: Enumerate the canonical strict ancestor paths of one entry path so conflict checks and parent materialization share one traversal of the same set.
# INVARIANTS: Pure string split/join over a canonical path; the entry itself is never returned.
def _strict_ancestors(path: str) -> Iterator[str]:
    """Yield every strict ancestor path of a canonical ``/``-separated path."""
    parts = path.split("/")
    for depth in range(1, len(parts)):
        yield "/".join(parts[:depth])


# endregion FUNC__strict_ancestors


# region FUNC__with_canonical_path
# PURPOSE: Rebuild one entry under its canonical path when the caller supplied a non-canonical spelling, keeping kind and bytes untouched.
# INVARIANTS: Returns a new frozen entry of the same class; content is shared, never copied or decoded.
def _with_canonical_path(entry: TaskInputEntry, canonical: str) -> TaskInputEntry:
    """Return a copy of ``entry`` carrying the canonical path."""
    if isinstance(entry, TaskInputFile):
        return TaskInputFile(path=canonical, content=entry.content)
    return TaskInputDirectory(path=canonical)


# endregion FUNC__with_canonical_path
