"""Convert legacy task input payloads to task-owned snapshots."""
# region MODULE_CONTRACT
# PURPOSE: Preserve deployable task inputs while allowing already-started tasks with unconvertible legacy inputs to stop blocking the upgrade.
# SCOPE: Forward migration 014; configured engine declarations select stored payload paths, never local files. No submission, deployment, or output behavior.
# INVARIANTS: DDL, conversion, metadata removal, and tracker recording share the runner's transaction; no intermediate commits. Missing context, TO_DO conversion failures, and storage failures abort; RUNNING/DONE conversion failures leave rows unchanged and are reported per engine. All task columns except converted extra keys remain unchanged, including timestamps. Content-free boundary traces carry task/engine/path counts and failure classes, never file bodies, payloads, SQL params, or exception text.
# DEPENDENCIES: Domain legacy payload conversion — the same function the submission boundary calls — plus domain snapshot rules; Migration engine context; SQL frozen in this module, not mutable runtime task queries.
# endregion MODULE_CONTRACT

# Dynamic migration modules live outside a package; the runner loads them by path.
# ruff: noqa: INP001

from __future__ import annotations

import logging
from itertools import islice
from typing import Any

from yascheduler.application import legacy_input_snapshot
from yascheduler.domain import (
    MissingInputFileError,
    TaskInputFile,
    UnsafeTaskPathError,
    ValidationError,
)
from yascheduler.infra.persistence.migration_base import Migration

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
SELECT task_id, engine, status, extra
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
    # PURPOSE: Convert representable inputs without letting historical conversion failures block pending work, leaving commit and tracker ownership with the runner.
    # REQUIRES: An explicit engine-declarations mapping (possibly empty); the scheduler and submitters are stopped.
    # ENSURES: Successful conversion removes only selected payload keys; unconvertible RUNNING/DONE rows remain unchanged and are reported. TO_DO conversion failures abort with bounded key diagnostics; storage and unexpected failures always abort.
    def migrate(self) -> None:  # noqa: PLR0915
        """Convert stored inputs without closing the runner's transaction."""
        rows: list[tuple[Any, ...]] = []
        converted_entries = 0
        skipped_by_engine: dict[str, int] = {}
        current_task_id: int | None = None
        current_engine: str | None = None
        current_path: str | None = None
        current_selection: str | None = None
        logger.debug("MIGRATE_INPUTS_START")
        try:
            if self.engine_inputs is None:
                message = "migration 014 requires engine input declarations; supply engine_inputs (an empty mapping is allowed)"
                raise RuntimeError(message)  # noqa: TRY301
            self.conn.run(_CREATE_INPUTS)
            self.conn.run(_DISABLE_TIMESTAMP_TRIGGER)
            rows = self.conn.run(_LIST_LEGACY_INPUTS) or []
            logger.debug("MIGRATE_INPUTS", extra={"tasks": len(rows)})
            for task_id, engine, status, payload in rows:
                current_task_id = task_id
                current_engine = engine
                current_path = None
                current_selection = None
                # region BLOCK_convert_task
                logger.debug(
                    "MIGRATE_TASK_INPUTS_START",
                    extra={"task_id": task_id, "engine": engine},
                )
                try:
                    if engine not in self.engine_inputs:
                        message = f"engine {engine!r} has no input declarations; supply its configuration"
                        raise ValueError(message)  # noqa: TRY301
                    if not isinstance(payload, dict):
                        message = "legacy extra must be a metadata object"
                        raise TypeError(message)  # noqa: TRY301
                    snapshot, remaining = legacy_input_snapshot(
                        engine, self.engine_inputs[engine], payload
                    )
                except BaseException as err:
                    failure_path = getattr(err, "path", getattr(err, "filename", None))
                    if failure_path is not None:
                        current_path = failure_path
                    failure_fields: dict[str, Any] = {
                        "task_id": task_id,
                        "engine": engine,
                        "entry_count": 0,
                        "failure": type(err).__name__,
                    }
                    if failure_path is not None:
                        failure_fields["path"] = failure_path
                        if isinstance(
                            err, (MissingInputFileError, UnsafeTaskPathError)
                        ):
                            current_selection = failure_path
                            failure_fields["selection"] = failure_path
                    conversion_error = isinstance(
                        err, (ValidationError, TypeError, ValueError)
                    )
                    # Frozen SQL enum labels; unknown states stay fail-closed
                    # just like TO_DO.
                    if conversion_error and status in ("RUNNING", "DONE"):
                        failure_fields["key_count"] = (
                            len(payload) if isinstance(payload, dict) else 0
                        )
                        logger.debug(
                            "MIGRATE_TASK_INPUTS_SKIPPED", extra=failure_fields
                        )
                        skipped_by_engine[engine] = skipped_by_engine.get(engine, 0) + 1
                        continue
                    logger.debug(
                        "MIGRATE_TASK_INPUTS_FAILED",
                        extra=failure_fields,
                    )
                    if conversion_error:
                        keys = (
                            [key[:80] for key in islice(payload, 10)]
                            if isinstance(payload, dict)
                            else []
                        )
                        key_count = len(payload) if isinstance(payload, dict) else 0
                        message = (
                            f"task {task_id}, engine {engine!r}: {err}; "
                            f"payload keys ({key_count} total, first 10, up to 80 characters each): {keys!r}; "
                            "configure the engine input_files to match these keys or mark the task DONE"
                        )
                        raise RuntimeError(message) from err
                    raise
                # endregion BLOCK_convert_task

                # region BLOCK_store_snapshot
                entry_count = len(snapshot)
                logger.debug(
                    "MIGRATE_TASK_STORAGE_START",
                    extra={
                        "task_id": task_id,
                        "engine": engine,
                        "entry_count": entry_count,
                    },
                )
                try:
                    if snapshot:
                        self.conn.run(
                            _INSERT_INPUTS,
                            task_id=task_id,
                            paths=[entry.path for entry in snapshot],
                            kinds=[entry.kind.value for entry in snapshot],
                            contents=[
                                entry.content
                                if isinstance(entry, TaskInputFile)
                                else None
                                for entry in snapshot
                            ],
                        )
                        self.conn.run(
                            _UPDATE_LEGACY_INPUT_METADATA,
                            task_id=task_id,
                            paths=[key for key in payload if key not in remaining],
                        )
                except BaseException as err:
                    failure_fields = {
                        "task_id": task_id,
                        "engine": engine,
                        "entry_count": entry_count,
                        "failure": type(err).__name__,
                    }
                    logger.debug(
                        "MIGRATE_TASK_INPUTS_FAILED",
                        extra=failure_fields,
                    )
                    raise
                logger.debug(
                    "MIGRATE_TASK_STORAGE_DONE",
                    extra={
                        "task_id": task_id,
                        "engine": engine,
                        "entry_count": entry_count,
                    },
                )
                logger.debug(
                    "MIGRATE_TASK_INPUTS",
                    extra={
                        "task_id": task_id,
                        "engine": engine,
                        "entry_count": entry_count,
                    },
                )
                converted_entries += entry_count
                # endregion BLOCK_store_snapshot
            # The trigger restoration is migration-wide, not attributable to
            # the last task that happened to convert successfully.
            current_task_id = None
            current_engine = None
            current_path = None
            current_selection = None
            self.conn.run(_ENABLE_TIMESTAMP_TRIGGER)
        except BaseException as err:
            failure_fields = {
                "tasks": len(rows),
                "entry_count": converted_entries,
                "failure": type(err).__name__,
            }
            if current_task_id is not None:
                failure_fields["task_id"] = current_task_id
            if current_engine is not None:
                failure_fields["engine"] = current_engine
            if current_path is not None:
                failure_fields["path"] = current_path
            if current_selection is not None:
                failure_fields["selection"] = current_selection
            logger.debug("MIGRATE_INPUTS_FAILED", extra=failure_fields)
            raise
        for engine, count in skipped_by_engine.items():
            logger.warning(
                "Migration 014 left %d task(s) unconverted for engine %r",
                count,
                engine,
                extra={"engine": engine, "skipped_count": count},
            )
        logger.debug(
            "MIGRATE_INPUTS_DONE",
            extra={"tasks": len(rows), "entry_count": converted_entries},
        )

    # endregion METHOD_migrate


# endregion CLASS_TaskInputSnapshots
