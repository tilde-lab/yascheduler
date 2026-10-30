"""Submission-boundary normalization of legacy and explicit task inputs."""
# region MODULE_CONTRACT
# PURPOSE: Convert a submission's input sources — legacy metadata file values or explicit typed entries — into the one domain snapshot exactly once, at the public API boundary, so persistence and deployment never see legacy encodings.
# SCOPE:
# - normalize_task_inputs(metadata, engine, input_entries): classify metadata keys against typed fields and engine input declarations, delegate claimed legacy values to the shared conversion, normalize explicit entries, and separate unrelated payload.
# - NOT: filesystem capture (infra local_inputs), legacy payload claiming and value encoding (domain legacy_inputs, shared with migration 014), snapshot persistence, engine resolution, or submit use-case validation.
# INVARIANTS:
# - Pure: no filesystem, SQL, SSH, or config imports — normalization never reads the caller's filesystem to fill missing values.
# - Boundary records distinguish start/completion/failure using source, engine, counts and path context, never metadata values, file bytes, encoded payloads or arbitrary exception text.
# - Every engine input declaration is required: a declaration that claims no metadata key raises MissingInputFileError naming it (domain legacy_inputs).
# - Explicit entries together with metadata keys the engine declarations would claim are ambiguous and rejected (an explicit empty sequence is a valid empty snapshot, not a fallback to metadata).
# - Typed metadata fields (TYPED_METADATA_KEYS) are never consumed as input payload by any declaration.
# - Legality of a legacy value is the shared conversion's: a flat payload key is one name, never a directory, and values keep their recorded UTF-8/`fort.9` meanings.
# - TRANSITION: legacy mode strips the claimed keys from the returned metadata — they live in the typed snapshot, and keeping them in `extra` would store a second input representation (design: single storage source). Deployment reads only the snapshot (task 4.1 flipped it), and queries derive the legacy filename/value metadata view from the snapshot, so legacy consumers are unaffected. Pre-migration rows may still carry legacy extra copies; the query projection keeps their precedence until the forward migration (task 3.3) converts and strips them. Explicit mode never writes input keys into metadata.
# RATIONALE:
# - Q: Why does normalization live in entrypoints instead of the application use case?
#   A: Legacy payload interpretation is a public-boundary encoding concern; the use case receives typed entries only, so one boundary seam converts and every downstream layer stays legacy-free.
# - Q: Why does claiming delegate to the domain instead of staying here?
#   A: Migration 014 converts stored payloads with the same rule, and a copy here drifted from it: the migration read a literal declaration as a directory and aborted on a payload this boundary accepted. One pure function in the domain is the only place both callers may share.
# - Q: Why does the ambiguity check reject instead of merging?
#   A: A key claimed by declarations while explicit entries are supplied has two competing owners; rejecting with the offending key named is the only non-lossy answer, and the empty explicit sequence keeps a deliberate meaning (empty snapshot).
# - Q: Why are claimed legacy keys stripped from the metadata instead of kept as a transitional copy?
#   A: Deployment already reads only the typed snapshot (task 4.1), and the query projection derives the legacy filename/value view from the snapshot, so stored copies serve no consumer — they would only be a second input representation that can drift. The forward migration strips converted keys from stored rows the same way.
# KEYWORDS: task inputs, submission, normalization, legacy metadata, base64, fort.9, boundary
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from yascheduler.application import legacy_input_snapshot, legacy_selection_selects
from yascheduler.domain import (
    TYPED_METADATA_KEYS,
    InvalidTaskSnapshotError,
    TaskInputEntry,
    TaskInputSnapshot,
    normalize_input_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from yascheduler.domain import Engine

__all__ = ["normalize_task_inputs"]

logger = logging.getLogger(__name__)


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
        for key in sorted(payload):
            if legacy_selection_selects(declaration, key):
                message = (
                    "explicit input_entries supplied together with legacy "
                    f"input payload key {key!r}; remove one input source"
                )
                raise InvalidTaskSnapshotError(key, message)


# endregion FUNC__reject_ambiguous_payload
