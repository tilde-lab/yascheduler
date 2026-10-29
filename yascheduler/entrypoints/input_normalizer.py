"""Submission-boundary normalization of legacy and explicit task inputs."""
# region MODULE_CONTRACT
# PURPOSE: Convert a submission's input sources — legacy metadata file values or explicit typed entries — into the one domain snapshot exactly once, at the public API boundary, so persistence and deployment never see legacy encodings.
# SCOPE:
# - normalize_task_inputs(metadata, engine, input_entries): classify metadata keys against typed fields and engine input declarations, encode claimed legacy values, normalize explicit entries, and separate unrelated payload.
# - The exact `fort.9` lenient base64 compatibility decode (the one legacy convention that stored binary as base64 text); deployment's copy stays until task 4.1 removes it.
# - NOT: filesystem capture (infra local_inputs), snapshot persistence, engine resolution, or submit use-case validation.
# INVARIANTS:
# - Pure: no filesystem, SQL, SSH, or config imports — normalization never reads the caller's filesystem to fill missing values.
# - Boundary records distinguish start/completion/failure using source, engine, counts and path context, never metadata values, file bytes, encoded payloads or arbitrary exception text.
# - Every engine input declaration is required: a missing literal key or a glob with no claimable metadata key raises MissingInputFileError naming the declaration.
# - Explicit entries together with metadata keys the engine declarations would claim are ambiguous and rejected (an explicit empty sequence is a valid empty snapshot, not a fallback to metadata).
# - Typed metadata fields (TYPED_METADATA_KEYS) are never consumed as input payload by any declaration.
# - Legacy values must be str: ordinary strings encode as UTF-8; `fort.9` uses the lenient base64 convention; base64 is never guessed for other filenames; non-string values fail with an actionable diagnostic.
# - TRANSITION: legacy mode strips the claimed keys from the returned metadata — they live in the typed snapshot, and keeping them in `extra` would store a second input representation (design: single storage source). Deployment reads only the snapshot (task 4.1 flipped it), and queries derive the legacy filename/value metadata view from the snapshot, so legacy consumers are unaffected. Pre-migration rows may still carry legacy extra copies; the query projection keeps their precedence until the forward migration (task 3.3) converts and strips them. Explicit mode never writes input keys into metadata.
# RATIONALE:
# - Q: Why does normalization live in entrypoints instead of the application use case?
#   A: Legacy payload interpretation is a public-boundary encoding concern; the use case receives typed entries only, so one boundary seam converts and every downstream layer stays legacy-free.
# - Q: Why classify glob declarations against metadata keys with the shared selection language?
#   A: Engine input declarations use the same task-relative selection contract as capture; matching payload keys with selection_matches keeps legacy directory/glob declarations and filesystem capture one language, and deriving parent entries through normalize_input_snapshot lets legacy submitters express trees without new syntax.
# - Q: Why does the ambiguity check reject instead of merging?
#   A: A key claimed by declarations while explicit entries are supplied has two competing owners; rejecting with the offending key named is the only non-lossy answer, and the empty explicit sequence keeps a deliberate meaning (empty snapshot).
# - Q: Why are claimed legacy keys stripped from the metadata instead of kept as a transitional copy?
#   A: Deployment already reads only the typed snapshot (task 4.1), and the query projection derives the legacy filename/value view from the snapshot, so stored copies serve no consumer — they would only be a second input representation that can drift. The forward migration strips converted keys from stored rows the same way.
# KEYWORDS: task inputs, submission, normalization, legacy metadata, base64, fort.9, boundary
# endregion MODULE_CONTRACT

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING

from yascheduler.domain import (
    TYPED_METADATA_KEYS,
    InvalidTaskSnapshotError,
    MissingInputFileError,
    TaskInputEntry,
    TaskInputFile,
    TaskInputKind,
    TaskInputSnapshot,
    UnsafeTaskPathError,
    is_glob_pattern,
    normalize_input_snapshot,
    selection_matches,
    validate_selection,
    validate_task_path,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from yascheduler.domain import Engine

__all__ = ["normalize_task_inputs"]

logger = logging.getLogger(__name__)

# The one legacy filename whose value was stored as lenient base64 text.
_LEGACY_BASE64_KEY: str = "fort.9"


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
        extra={"engine": engine.name, "source": source, "entry_count": 0},
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
            claimed = _claim_legacy_payload(payload, engine)
            entries = [
                TaskInputFile(path=key, content=_encode_legacy_value(key, value))
                for key, value in sorted(claimed.items())
            ]
            snapshot = normalize_input_snapshot(entries)
            # Claimed keys live in the typed snapshot; storing them again in
            # `extra` would be a second input representation (single storage
            # source). Queries derive the legacy view from the snapshot.
            remaining_metadata = {
                key: value for key, value in metadata.items() if key not in claimed
            }
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
            "entries": len(snapshot),
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
        if is_glob_pattern(declaration):
            for key in sorted(payload):
                if _is_claimable_key(key, declaration):
                    message = (
                        "explicit input_entries supplied together with legacy "
                        f"input payload key {key!r}; remove one input source"
                    )
                    raise InvalidTaskSnapshotError(key, message)
        elif declaration in payload:
            message = (
                "explicit input_entries supplied together with legacy "
                f"input payload key {declaration!r}; remove one input source"
            )
            raise InvalidTaskSnapshotError(declaration, message)


# endregion FUNC__reject_ambiguous_payload


# region FUNC__claim_legacy_payload
# PURPOSE: Apply the engine input declarations to the metadata payload, claiming one value per key and enforcing that every declaration is satisfied.
# INVARIANTS: Literal declarations claim their exact key or raise MissingInputFileError; glob declarations claim every safe payload key matching the shared selection language and require at least one; claimed values must be str (InvalidTaskSnapshotError otherwise); keys are claimed once even under overlapping declarations.
def _claim_legacy_payload(payload: dict[str, object], engine: Engine) -> dict[str, str]:
    """Return ``{key: value}`` claimed by the engine input declarations."""
    claimed: dict[str, str] = {}
    for declaration in engine.input_files:
        if is_glob_pattern(declaration):
            # Overlapping declarations may claim the same keys: a key provided
            # once satisfies every declaration that selects it.
            matched = [
                key for key in sorted(payload) if _is_claimable_key(key, declaration)
            ]
            if not matched:
                raise MissingInputFileError(engine.name, declaration)
            for key in matched:
                _claim_key(claimed, payload, key)
        else:
            if declaration not in payload:
                raise MissingInputFileError(engine.name, declaration)
            _claim_key(claimed, payload, declaration)
    return claimed


# endregion FUNC__claim_legacy_payload


# region FUNC__is_claimable_key
# PURPOSE: Answer whether one metadata key is a safe task-relative file path selected by one declaration, using the shared selection language so legacy claims and filesystem capture agree.
# INVARIANTS: The declaration itself is validated (an unsafe engine declaration is a configuration error and raises UnsafeTaskPathError); unsafe keys (absolute, `..`, backslash, task root) are never claimable — they stay unrelated payload.
def _is_claimable_key(key: str, declaration: str) -> bool:
    """Return True when the glob declaration selects the key as a file path."""
    selection = validate_selection(declaration)
    try:
        canonical = validate_task_path(key)
    except UnsafeTaskPathError:
        return False
    return selection_matches(selection, canonical, TaskInputKind.FILE)


# endregion FUNC__is_claimable_key


# region FUNC__claim_key
# PURPOSE: Move one payload key into the claimed map, validating that its value is a str before any encoding is attempted.
# INVARIANTS: str(object) coercion is not a file encoding contract — non-string values fail with the key and the offending type.
def _claim_key(claimed: dict[str, str], payload: dict[str, object], key: str) -> None:
    """Claim ``payload[key]`` after checking it is a string value."""
    value = payload[key]
    if not isinstance(value, str):
        message = (
            f"legacy input value must be a str, got {type(value).__name__}; "
            "str(object) coercion is not a file encoding contract"
        )
        raise InvalidTaskSnapshotError(key, message)
    claimed[key] = value


# endregion FUNC__claim_key


# region FUNC__encode_legacy_value
# PURPOSE: Encode one claimed legacy string into file bytes per the legacy contract: UTF-8 everywhere except the exact `fort.9` lenient base64 convention.
# INVARIANTS: Base64 is never guessed for other filenames; decode failures surface as InvalidTaskSnapshotError naming the key.
def _encode_legacy_value(key: str, value: str) -> bytes:
    """Return the file bytes a claimed legacy string encodes."""
    if key != _LEGACY_BASE64_KEY:
        return value.encode("utf-8")
    try:
        return _lenient_base64_decode(value)
    except (binascii.Error, ValueError) as err:
        message = f"legacy {_LEGACY_BASE64_KEY} value is not valid base64: {err}"
        raise InvalidTaskSnapshotError(key, message) from err


def _lenient_base64_decode(value: str) -> bytes:
    """Decode base64 text leniently (whitespace stripped, padding repaired).

    Reproduces the deployment-side decoder the convention was written against:
    whitespace and newlines are removed, missing padding is appended, and the
    default (non-strict) alphabet check applies.
    """
    compact = value.strip().replace("\n", "").replace(" ", "")
    missing_padding = len(compact) % 4
    if missing_padding:
        compact += "=" * (4 - missing_padding)
    return base64.b64decode(compact)


# endregion FUNC__encode_legacy_value
