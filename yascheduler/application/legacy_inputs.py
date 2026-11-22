"""Legacy input conversion plus the one submission-boundary input normalizer."""
# region MODULE_CONTRACT
# PURPOSE: Retire the pre-snapshot input format in one place: give the submission boundary and the forward migration one implementation of "which keys of a stored metadata payload are engine inputs", so a payload cannot be read two ways and a row the boundary accepted cannot stop the upgrade. Transitional — when no stored payload remains, this module and its callers go.
# SCOPE:
# - legacy_input_snapshot(engine_name, declarations, payload): select the claimed keys, encode their recorded value meanings, normalize the snapshot, and return the payload left unrelated.
# - legacy_selection_selects(declaration, key): the single selection rule, also used by the submission ambiguity check.
# - normalize_task_inputs(metadata, engine, input_entries): the submission-boundary entry point — classify metadata keys, delegate claimed values to the conversion above, validate explicit entries, reject ambiguity, and separate unrelated payload.
# - NOT: filesystem traversal, SQL, configuration parsing, persistence, engine resolution, or any runtime rule for current submissions.
# INVARIANTS:
# - A flat payload has no directories: a key is one name that may contain `/`, never a file inside a directory, so no key brings another key's value along. A literal declaration selects exactly its own key; a glob selects keys as file paths, where `*` never crosses `/` and a trailing `**` component selects every key below its prefix.
# - Typed metadata keys are never claimed, unsafe keys claim nothing, and an unsafe declaration is a configuration error.
# - Every declaration selects at least one key (MissingInputFileError), and every claimed value is a str (InvalidTaskSnapshotError): str(object) coercion is not a file encoding contract.
# - Values keep their recorded meanings: UTF-8 text, except the exact `fort.9` lenient base64 convention; base64 is never guessed for other names.
# - Normalization never reads the caller's filesystem; claimed keys are removed from the returned metadata because the snapshot is their one storage representation; explicit entries together with claimed payload keys are ambiguous and rejected.
# RATIONALE:
# - Q: Why does a claimed key never expand into a directory?
#   A: The pre-snapshot scheduler stored a flat name-to-text map and wrote one file per declared name, so `basis/one.orb` was a name, not a file in a directory `basis`. Deriving directories from `/` invented a structure no stored payload had, and made one key both a file and its own directory, which aborted the upgrade on rows submission had accepted.
# - Q: Why the application layer, not the domain?
#   A: The domain describes what the scheduler is, and a retired input format is not part of it; only the flat reading of a selection is a rule the domain still needs shape for. The application layer is the highest one both callers reach downward (entrypoints and infra), and it holds the submission use case this format is retired by.
# - Q: Why not infra, which both callers also reach?
#   A: Importing yascheduler.infra executes its package facade, which loads the cloud and SSH adapters; the submission boundary would pay for transports it never uses.
# - Q: Why is the ambiguity check given a predicate instead of the claim?
#   A: It reports an overlap without a missing declaration becoming an error, so it asks whether a key would be claimed and never reads a value.
# - Q: Why does the submission normalizer live here instead of the entrypoints?
#   A: Legacy payload interpretation is a boundary encoding concern, but the claim rule is shared with the forward migration, and infra may only import downwards. One module holds the rule and the boundary entry point, so the rule cannot be read two ways. The use case still receives typed entries only.
# KEYWORDS: legacy inputs, flat payload, claim, snapshot, fort.9, base64, migration, submission, boundary normalization, transitional
# endregion MODULE_CONTRACT

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING

from yascheduler.domain.exceptions import (
    InvalidTaskSnapshotError,
    MissingInputFileError,
    UnsafeTaskPathError,
)
from yascheduler.domain.model import TYPED_METADATA_KEYS
from yascheduler.domain.task_inputs import (
    TaskInputEntry,
    TaskInputFile,
    TaskInputKind,
    is_glob_pattern,
    normalize_input_snapshot,
    selection_matches,
    validate_selection,
    validate_task_path,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from yascheduler.domain.model import Engine
    from yascheduler.domain.task_inputs import TaskInputSnapshot

__all__ = [
    "legacy_input_snapshot",
    "legacy_input_value",
    "legacy_selection_selects",
    "normalize_task_inputs",
]

logger = logging.getLogger(__name__)

# The one legacy filename whose value was stored as lenient base64 text.
_LEGACY_BASE64_KEY: str = "fort.9"


# region FUNC_legacy_selection_selects
# PURPOSE: Answer whether one engine declaration claims one flat legacy key, so claiming, the ambiguity check, and the migration cannot disagree.
# REQUIRES: declaration is an engine input selection; key is any raw payload key.
# INVARIANTS: A literal declaration claims only the key of that exact name; a glob claims keys as file paths with `*` never crossing `/`, and a trailing `**` component claims every key below its prefix; an unsafe key claims nothing; an unsafe declaration raises UnsafeTaskPathError.
def legacy_selection_selects(declaration: str, key: str) -> bool:
    """Return True when the declaration claims the flat payload key."""
    selection = validate_selection(declaration)
    try:
        path = validate_task_path(key)
    except UnsafeTaskPathError:
        return False
    if not is_glob_pattern(selection):
        return path == selection
    if selection == "**" or selection.endswith("/**"):
        return path.startswith(selection[:-2])
    return selection_matches(selection, path, TaskInputKind.FILE)


# endregion FUNC_legacy_selection_selects


# region FUNC__claim_legacy_inputs
# PURPOSE: Move the values each declaration claims out of the payload into one ``key -> value`` map, requiring one value per declaration.
# INVARIANTS: Typed metadata keys are never claimable; overlapping declarations claim a key once; the first unsatisfied declaration raises MissingInputFileError and the first non-str value raises InvalidTaskSnapshotError.
def _claim_legacy_inputs(
    declarations: Sequence[str], engine_name: str, payload: Mapping[str, object]
) -> dict[str, str]:
    """Return ``{key: value}`` claimed by the declarations."""
    claimable = {
        key: value for key, value in payload.items() if key not in TYPED_METADATA_KEYS
    }
    claimed: dict[str, str] = {}
    for declaration in declarations:
        selected = [
            key
            for key in sorted(claimable)
            if legacy_selection_selects(declaration, key)
        ]
        if not selected:
            raise MissingInputFileError(engine_name, declaration)
        for key in selected:
            value = claimable[key]
            if not isinstance(value, str):
                message = (
                    "legacy input value must be a str, got "
                    f"{type(value).__name__}; str(object) coercion is not a "
                    "file encoding contract"
                )
                raise InvalidTaskSnapshotError(key, message)
            claimed[key] = value
    return claimed


# endregion FUNC__claim_legacy_inputs


# region FUNC__legacy_input_bytes
# PURPOSE: Encode one claimed value with its recorded deployment meaning, so a migrated row and a submitted row carry the same bytes.
# INVARIANTS: Only the exact `fort.9` name decodes lenient base64; every other value is UTF-8 text and never guessed; both failures name the key without echoing the value.
def _legacy_input_bytes(path: str, value: str) -> bytes:
    """Return the file bytes a claimed legacy string encodes."""
    if path != _LEGACY_BASE64_KEY:
        try:
            return value.encode("utf-8")
        except UnicodeEncodeError as err:
            message = "legacy input value cannot be encoded as UTF-8"
            raise InvalidTaskSnapshotError(path, message) from err
    compact = value.strip().replace("\n", "").replace(" ", "")
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact)
    except (binascii.Error, ValueError) as err:
        message = f"legacy {_LEGACY_BASE64_KEY} value is not valid base64"
        raise InvalidTaskSnapshotError(path, message) from err


# endregion FUNC__legacy_input_bytes


# region FUNC_legacy_input_value
# PURPOSE: Project one stored file back to the legacy metadata string its payload would have carried, so a query keeps answering in the shape the submission accepted instead of a second reading of the same bytes.
# INVARIANTS: The exact `fort.9` name always answers canonical padded base64, matching what its submission decodes; every other name answers UTF-8 text and falls back to canonical base64 only when the bytes are not valid UTF-8.
def legacy_input_value(path: str, content: bytes) -> str:
    """Return the legacy metadata string of one stored input file."""
    if path == _LEGACY_BASE64_KEY:
        return base64.b64encode(content).decode("ascii")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(content).decode("ascii")


# endregion FUNC_legacy_input_value


# region FUNC_legacy_input_snapshot
# PURPOSE: Convert one flat legacy payload into the snapshot the task owns plus the metadata that is not an input, identically at submission and during the upgrade.
# REQUIRES: declarations are the engine's input declarations; payload is the flat name-to-value metadata; the caller keeps the values stable.
# ENSURES: The snapshot holds one entry per claimed key, sorted, with parents materialized; the returned mapping is the caller's payload without the claimed keys, so a caller's own non-input keys survive untouched.
def legacy_input_snapshot(
    engine_name: str, declarations: Sequence[str], payload: Mapping[str, object]
) -> tuple[TaskInputSnapshot, dict[str, object]]:
    """Return the input snapshot and the unrelated metadata of one legacy payload."""
    claimed = _claim_legacy_inputs(declarations, engine_name, payload)
    snapshot = normalize_input_snapshot(
        [
            TaskInputFile(path=key, content=_legacy_input_bytes(key, value))
            for key, value in sorted(claimed.items())
        ]
    )
    return snapshot, {
        key: value for key, value in payload.items() if key not in claimed
    }


# endregion FUNC_legacy_input_snapshot


# region FUNC_normalize_task_inputs
# PURPOSE: Turn one submission's metadata plus input source into the typed snapshot and the metadata that continues downstream, so legacy encoding and ambiguity handling end at this boundary.
# REQUIRES: engine is the resolved Engine for the submission (unknown engines are rejected by callers before normalization); input_entries is None (legacy mode) or a caller-supplied sequence of typed entries, possibly empty.
# ENSURES: Returns (snapshot, metadata_dict): snapshot is the normalized immutable tuple (claimed legacy values encoded per contract, or the validated explicit entries); the metadata dict is a fresh copy of the input mapping without the claimed legacy input keys — they are represented by the snapshot, not stored a second time in `extra`; explicit mode raises InvalidTaskSnapshotError on any key the engine declarations would claim; legacy mode raises MissingInputFileError for each unsatisfied declaration and InvalidTaskSnapshotError for non-string values or base64 decode failures.
def normalize_task_inputs(
    metadata: Mapping[str, object],
    engine: Engine,
    input_entries: Sequence[TaskInputEntry] | None,
) -> tuple[TaskInputSnapshot, dict[str, object]]:
    """Normalize legacy metadata or explicit entries into the task snapshot.

    ``input_entries=None`` selects legacy mode: engine-declared file keys are
    claimed from the metadata payload and their string values encoded (UTF-8,
    or the lenient base64 convention for the exact ``fort.9`` filename) into
    the snapshot. Claimed keys are removed from the returned metadata — they
    are represented by the snapshot, and public queries still expose their
    legacy filename/value view derived from it, so legacy consumers see the
    same query shape. An explicit sequence — including an empty one — is
    validated as the snapshot itself; metadata keys
    the declarations would claim then make the submission ambiguous and are
    rejected. Normalization never reads the filesystem.
    """
    source = "explicit" if input_entries is not None else "legacy"
    snapshot: TaskInputSnapshot = ()
    logger.debug(
        "NORMALIZE_INPUTS_START",
        extra={"engine": engine.name, "source": source},
    )
    try:
        payload = {
            key: value
            for key, value in metadata.items()
            if key not in TYPED_METADATA_KEYS
        }
        if input_entries is not None:
            snapshot = normalize_input_snapshot(input_entries)
            _reject_ambiguous_payload(payload, engine)
            remaining_metadata = dict(metadata)
        else:
            # Claimed keys live in the typed snapshot; storing them again in
            # `extra` would be a second input representation (single storage
            # source). Queries derive the legacy view from the snapshot.
            snapshot, remaining_metadata = legacy_input_snapshot(
                engine.name, engine.input_files, metadata
            )
    except Exception as err:
        logger.debug(
            "NORMALIZE_INPUTS_FAILED",
            extra={
                "engine": engine.name,
                "source": source,
                "entry_count": len(snapshot),
                "path": getattr(err, "path", getattr(err, "filename", None)),
                "failure": type(err).__name__,
            },
        )
        raise
    logger.debug(
        "NORMALIZE_INPUTS",
        extra={
            "engine": engine.name,
            "source": source,
            "entry_count": len(snapshot),
        },
    )
    return snapshot, remaining_metadata


# endregion FUNC_normalize_task_inputs


# region FUNC__reject_ambiguous_payload
# PURPOSE: Fail explicit-entry submissions that also carry metadata keys the engine declarations would claim, so one submission has exactly one input source.
# INVARIANTS: Reports the alphabetically first claimed key for determinism; claims are computed without consuming values.
def _reject_ambiguous_payload(payload: dict[str, object], engine: Engine) -> None:
    """Raise InvalidTaskSnapshotError when payload keys overlap declarations."""
    for declaration in engine.input_files:
        for key in sorted(payload):
            if legacy_selection_selects(declaration, key):
                message = (
                    "explicit input_entries supplied together with legacy "
                    f"input payload key {key!r}; remove one input source"
                )
                raise InvalidTaskSnapshotError(key, message)


# endregion FUNC__reject_ambiguous_payload
