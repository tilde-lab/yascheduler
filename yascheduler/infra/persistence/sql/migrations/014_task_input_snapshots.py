"""Convert legacy task input payloads to task-owned snapshots."""

# Dynamic migration modules live outside a package; the runner loads them by path.
# ruff: noqa: INP001
# region MODULE_CONTRACT
# PURPOSE: Preserve the deployment meaning of every stored task's legacy inputs while moving them out of arbitrary metadata into owned binary entries (ADR-0019).
# SCOPE: Forward migration 014; configured engine declarations select stored payload paths, never local files. No submission, deployment, or output behavior.
# INVARIANTS: DDL, conversion, metadata removal, and tracker recording share the runner's transaction; no intermediate commits. All task columns except converted extra keys remain unchanged, including timestamps. Diagnostics contain task/path context, never file bodies.
# DEPENDENCIES: Domain snapshot/path/selection rules; Migration engine context; SQL frozen in this module, not mutable runtime task queries.
# endregion MODULE_CONTRACT

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING, Any

from yascheduler.domain import (
    TYPED_METADATA_KEYS,
    InvalidTaskSnapshotError,
    MissingInputFileError,
    TaskInputFile,
    TaskInputKind,
    UnsafeTaskPathError,
    ValidationError,
    normalize_input_snapshot,
    selection_matches,
    validate_selection,
    validate_task_path,
)
from yascheduler.infra.persistence.migration_base import Migration

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from yascheduler.domain import TaskInputSnapshot

__all__ = ["TaskInputSnapshots"]

logger = logging.getLogger(__name__)

# Statements are inlined so this migration is one frozen artifact: nothing
# outside it can reference these queries or edit them as if they were runtime SQL.
#
# Migration-safe creation: schema bootstrap may already have created the empty
# table before running pending migrations.
_CREATE_INPUTS = """
CREATE TABLE IF NOT EXISTS yascheduler_task_inputs (
    task_id INTEGER NOT NULL REFERENCES yascheduler_tasks (
        task_id
    ) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    content BYTEA,
    CONSTRAINT task_input_pk PRIMARY KEY (task_id, path),
    CONSTRAINT task_input_kind_values CHECK (kind IN ('file', 'directory')),
    CONSTRAINT task_input_kind_content_invariants CHECK (
        (kind = 'file' AND content IS NOT NULL)
        OR (kind = 'directory' AND content IS NULL)
    )
)
"""

# Transactional migration lock; rollback also restores trigger state.
_DISABLE_TIMESTAMP_TRIGGER = """
ALTER TABLE yascheduler_tasks DISABLE TRIGGER yascheduler_tasks_touch_updated_at
"""

_ENABLE_TIMESTAMP_TRIGGER = """
ALTER TABLE yascheduler_tasks ENABLE TRIGGER yascheduler_tasks_touch_updated_at
"""

# Convert all statuses; task order keeps migration diagnostics deterministic.
_LIST_LEGACY_INPUTS = """
SELECT task_id, engine, extra
FROM yascheduler_tasks
ORDER BY task_id
"""

# Arrays are positionally aligned; all rows share the migration transaction.
_INSERT_INPUTS = """
INSERT INTO yascheduler_task_inputs (task_id, path, kind, content)
SELECT
    :task_id,
    entry_path,
    entry_kind,
    entry_content
FROM unnest(
    cast(:paths AS TEXT []),
    cast(:kinds AS TEXT []),
    cast(:contents AS BYTEA [])
) AS entries (entry_path, entry_kind, entry_content)
"""

# Remove converted raw keys inside PostgreSQL: do not round-trip unrelated JSONB
# values through Python (high-precision numbers must remain exact). The migration
# disables the touch trigger to preserve updated_at as well.
_UPDATE_LEGACY_INPUT_METADATA = """
UPDATE yascheduler_tasks
SET extra = extra - cast(:paths AS TEXT [])
WHERE task_id = :task_id
"""


# region CLASS_TaskInputSnapshots
# PURPOSE: Upgrade all task statuses atomically without changing task identity, execution state, bindings, or unrelated metadata.
class TaskInputSnapshots(Migration):
    """Move engine-selected legacy inputs into normalized owned rows."""

    # region METHOD_migrate
    # PURPOSE: Create input storage and convert every stored task in the open migration transaction, leaving commit and tracker ownership with the runner.
    # REQUIRES: Each stored engine has supplied input declarations; the scheduler and submitters are stopped.
    # ENSURES: Successful conversion removes only selected payload keys; any error aborts the migration with task/engine/path context.
    def migrate(self) -> None:
        """Convert stored inputs without closing the runner's transaction."""
        self.conn.run(_CREATE_INPUTS)
        self.conn.run(_DISABLE_TIMESTAMP_TRIGGER)
        rows: list[tuple[Any, ...]] = self.conn.run(_LIST_LEGACY_INPUTS) or []
        logger.debug("MIGRATE_INPUTS", extra={"tasks": len(rows)})
        for task_id, engine, payload in rows:
            # region BLOCK_convert_task
            try:
                if self.engine_inputs is None or engine not in self.engine_inputs:
                    message = f"engine {engine!r} has no input declarations; supply its configuration"
                    raise ValueError(message)  # noqa: TRY301
                if not isinstance(payload, dict):
                    message = "legacy extra must be a metadata object"
                    raise TypeError(message)  # noqa: TRY301
                snapshot, remaining = _convert_payload(
                    engine, self.engine_inputs[engine], payload
                )
            except (ValidationError, TypeError, ValueError) as err:
                logger.debug(
                    "MIGRATE_INPUTS_FAILED",
                    extra={
                        "task_id": task_id,
                        "engine": engine,
                        "path": getattr(err, "path", getattr(err, "filename", None)),
                        "failure": type(err).__name__,
                    },
                )
                message = f"task {task_id}, engine {engine!r}: {err}"
                raise RuntimeError(message) from err
            # endregion BLOCK_convert_task

            # region BLOCK_store_snapshot
            if snapshot:
                self.conn.run(
                    _INSERT_INPUTS,
                    task_id=task_id,
                    paths=[entry.path for entry in snapshot],
                    kinds=[entry.kind.value for entry in snapshot],
                    contents=[
                        entry.content if isinstance(entry, TaskInputFile) else None
                        for entry in snapshot
                    ],
                )
                self.conn.run(
                    _UPDATE_LEGACY_INPUT_METADATA,
                    task_id=task_id,
                    paths=[key for key in payload if key not in remaining],
                )
            logger.debug(
                "MIGRATE_TASK_INPUTS",
                extra={"task_id": task_id, "engine": engine, "entries": len(snapshot)},
            )
            # endregion BLOCK_store_snapshot
        self.conn.run(_ENABLE_TIMESTAMP_TRIGGER)

    # endregion METHOD_migrate


# endregion CLASS_TaskInputSnapshots


# region FUNC__convert_payload
# PURPOSE: Select legacy files and implied directories with the shared language, preserving unrelated values and enforcing required selections before persistence.
def _convert_payload(
    engine: str, declarations: Sequence[str], payload: Mapping[str, object]
) -> tuple[TaskInputSnapshot, dict[str, object]]:
    """Return the selected snapshot and metadata with only its file keys removed."""
    paths: dict[str, str] = {}
    directories: set[str] = set()
    for key in payload:
        if key in TYPED_METADATA_KEYS:
            continue
        try:
            path = validate_task_path(key)
        except UnsafeTaskPathError:
            # Unsafe unrelated metadata is not a filesystem entry. Unsafe
            # declarations are rejected separately, never silently skipped.
            continue
        paths[key] = path
        parts = path.split("/")
        directories.update("/".join(parts[:depth]) for depth in range(1, len(parts)))

    selected: set[str] = set()
    for declaration in declarations:
        selection = validate_selection(declaration)
        matched_directories = {
            path
            for path in directories
            if selection_matches(selection, path, TaskInputKind.DIRECTORY)
        }
        matched_keys = {
            key
            for key, path in paths.items()
            if selection_matches(selection, path, TaskInputKind.FILE)
            or any(
                path.startswith(directory + "/") for directory in matched_directories
            )
        }
        if not matched_keys:
            raise MissingInputFileError(engine, declaration)
        selected.update(matched_keys)

    snapshot = normalize_input_snapshot(
        [
            TaskInputFile(path=key, content=_legacy_bytes(key, payload[key]))
            for key in sorted(selected)
        ]
    )
    return snapshot, {
        key: value for key, value in payload.items() if key not in selected
    }


# endregion FUNC__convert_payload


# region FUNC__legacy_bytes
# PURPOSE: Preserve historical UTF-8 and exact fort.9 lenient base64 deployment meanings without guessing other encodings.
def _legacy_bytes(path: str, value: object) -> bytes:
    """Convert one selected legacy value, reporting the path but not its body."""
    if not isinstance(value, str):
        raise InvalidTaskSnapshotError(
            path, f"legacy input value must be a str, got {type(value).__name__}"
        )
    if path == "fort.9":
        compact = value.strip().replace("\n", "").replace(" ", "")
        compact += "=" * (-len(compact) % 4)
        try:
            return base64.b64decode(compact)
        except (binascii.Error, ValueError) as err:
            raise InvalidTaskSnapshotError(
                path, "legacy fort.9 value is not valid base64"
            ) from err
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as err:
        raise InvalidTaskSnapshotError(
            path, "legacy input value cannot be encoded as UTF-8"
        ) from err


# endregion FUNC__legacy_bytes
