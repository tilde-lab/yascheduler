# Tasks

Implementation checklist. Scope: issue 006 in the scratch backlog ("migration
014 blocks the upgrade on a task with an unknown engine"). See `design.md` for
the trade-offs and the two spec deltas for the contract.

Each section leaves the tree green on its own.

## 1. Migration behaviour

- [x] 1.1 Read the task status with the rest of the row in the migration's
  legacy-row query, and add the missing-declaration-context guard for the run:
  a run without an engine declaration context (`None`) stops before any
  conversion and writes no tracker entry; an explicit empty mapping is a valid
  context.
- [x] 1.2 Move the per-row failure handling so it can skip: a task that has
  started keeps its stored payload and metadata, gains no input entries, and
  the run continues; a task that has not started re-raises with the current
  message. Leave the write block failing for every task, because a failed
  write is a database fault rather than unconvertible data.
- [x] 1.3 Name the input keys the task carries in the stop for a task that has
  not started, bounded in length, so the operator writes declarations from the
  message instead of guessing.
- [x] 1.4 Count the tasks left unconverted per engine and report each engine
  with its count once the run finishes.
- [x] 1.5 Update the migration module contract: it no longer aborts for every
  error and no longer requires declarations for every stored engine.

## 2. Specs, change note, decision record

- [x] 2.1 Apply the `db-migrations` delta: one requirement states that a
  missing engine declaration context (`None`) stops the upgrade while an empty
  mapping is valid, one narrows the stop to a task that has not started, one
  states that a task that has started is left unconverted and reported, and the
  conversion requirement states that an unconvertible task keeps its payload.
- [x] 2.2 Apply the `task-input-snapshots` delta: boundary records distinguish
  a skip from a success and from a failure.
- [x] 2.3 Rewrite the breaking entry in the unreleased change note: the
  upgrade stops only for a task that has not started and reports the tasks it
  left unconverted.
- [x] 2.4 Confirm ADR-0019 §5 needs no amendment: it states the conversion
  intent and the stopped-runner precondition, and prescribes no failure
  behaviour.

## 3. Tests

- [x] 3.1 Move the failure matrix to a task that has not started. The five
  current cases all run against a finished task, so the narrowed contract must
  be asserted on a task that can still reach a machine: rollback, repaired
  retry, and the exact diagnostics stay as they are apart from the status.
- [x] 3.2 Assert the skip for a task that has started: no input entries, both
  the stored payload and unrelated metadata byte-identical after the upgrade,
  the tracker entry written, and a second run a no-op.
- [x] 3.3 Add a mixed cohort in one run: a convertible task of a declared
  engine, a convertible finished task, and a finished task of an undeclared
  engine. Assert the entries of the first two, the untouched payload of the
  third, and the report naming the undeclared engine with its count.
- [x] 3.4 Assert that a run with no engine declaration context (`None`) on a
  database with stored tasks stops and writes no tracker entry.
- [x] 3.5 Give the fresh-bootstrap test an explicit empty declaration context,
  since the migration now requires one.
- [x] 3.6 Keep the all-status conversion coverage: a convertible task is still
  converted whatever its status, so the existing status-parameterised success
  case stays valid.

## 4. Gates

- [x] 4.1 `uv run pytest -m unit`
- [x] 4.2 `uv run pytest -m integration`
- [x] 4.3 `uv run pytest -m e2e`
- [x] 4.4 `uv run zuban check`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run lint-imports`
- [x] 4.5 `openspec validate --all --json`

## 5. Verification Evidence

- `uv run pytest -m unit`: 1598 passed, 189 deselected, 2 AsyncMock warnings,
  7.64s.
- `uv run pytest -m integration`: 173 passed, 1 skipped (`vultr_live`), 1613
  deselected, 442.13s. `.run/verification/integration-recovery.log` is the
  authoritative integration output.
- `uv run pytest -m e2e`: 13 passed, 3 live-skipped, 95.12s.
- `uv run zuban check`: 240 source files clean. `uv run ruff check .`: all pass.
  `uv run ruff format --check .`: 337 already formatted.
  `uv run lint-imports`: 1 kept, 0 broken.
- `openspec validate --all --json`: 25 valid.
- Coverage `1.1`/`3.4`/`3.5`:
  `test_migrate_requires_engine_context_before_touching_the_database`,
  `test_migrate_accepts_an_explicit_empty_engine_mapping`,
  `test_fresh_bootstrap_exposes_migration_014_input_storage_with_empty_engine_context`,
  `test_migration_014_requires_engine_context_even_on_empty_database`,
  `test_migration_014_missing_engine_context_rolls_back_existing_rows`.
- Coverage `1.2`/`1.5`/`3.2`:
  `test_migration_014_skips_unconvertible_started_task`,
  `test_storage_failure_aborts_for_started_task`,
  `test_unexpected_error_is_not_skipped_for_started_task`,
  `test_migration_014_non_object_extra_status_boundary`.
- Coverage `1.3`/`3.1`:
  `test_todo_abort_diagnostic_bounds_keys_and_includes_remedy`,
  `test_migration_014_failure_rolls_back_and_repaired_retry_is_idempotent`.
- Coverage `1.4`/`3.3`:
  `test_skip_diagnostics_aggregate_one_warning_per_engine`,
  `test_migration_014_mixed_cohort_aggregates_skipped_engine_warning`.
- Coverage `3.6`:
  `test_modern_013_migration_creates_table_and_converts_all_statuses`.
