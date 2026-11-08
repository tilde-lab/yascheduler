# Tasks

Implementation checklist. Scope: issue 005 in the scratch backlog
("input bytes ride on the task record"). See
[ADR-0021](../../../docs/decisions/ADR-0021-input-bytes-read-apart-from-task.md)
for the decision and `design.md` for the trade-offs.

Each section leaves the tree green on its own.

## 1. Port and persistence

- [x] 1.1 Add the complete-snapshot read and the per-entry read to the task
  repository port and the unit-of-work protocol. The complete-snapshot read
  reuses the existing batched query; the per-entry read is a new keyset query
  ordered by path under the `C` collation with a single-row limit.
  Integration-test the round trip of a mixed tree (text, binary, zero-length,
  empty directory), the entry order across nested paths, and that a second
  call after the last entry returns nothing.

## 2. Deployment

- [x] 2.1 Read the entries in the allocation use case inside the unit of work
  that claims the task, and pass the iterator to the deployment operation
  through the orchestrator wrapper. The deployment operation takes no store
  dependency.
- [x] 2.2 Upload from the iterator: create each directory entry, write each
  file entry, and validate every path before the remote write. Remove the
  whole-snapshot re-validation.
- [x] 2.3 Adapt the deployment integration and fault tests to the iterator
  contract. Let the unsafe-path test state the new outcome: entries before the
  bad one are written and the transfer fails on it.

## 3. Record and reads

- [x] 3.1 Remove the snapshot from the task record, keep it on the
  pre-persistence submission shape, delete the hydration method and its five
  call sites, and drop the insert-side rewrite of the record.
- [x] 3.2 Read the snapshot explicitly in the query use case and hand it to the
  public projection. The projected response stays byte-identical.
- [x] 3.3 Adapt unit, integration, and e2e tests that read a snapshot back from
  the store; delete the hydration logging test with the method it covers.

## 4. Specs, decision record, gates

- [x] 4.1 Apply the deltas to `postgres-persistence` and `task-input-snapshots`
  and run `openspec validate --all --json`.
- [x] 4.2 Add ADR-0021 and the pointer from ADR-0019.
- [x] 4.3 Run the gates: `uv run pytest -m unit`, `uv run pytest -m integration`,
  `uv run pytest -m e2e`, `uv run zuban check`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run lint-imports`.

## 5. Verification Evidence

- 2026-10-30, gates (all green): `pytest -m unit` — 1584 passed, 173
  deselected; `pytest -m integration` — 157 passed, 1 skipped; `pytest -m e2e`
  — 13 passed, 3 skipped; `zuban check` — no issues in 240 files; `ruff
  check .` / `ruff format --check .` — clean; `lint-imports` — layers kept,
  contracts 1 kept / 0 broken; `openspec validate --all --json` — all specs
  valid, no warnings on the two modified specs.
- 1.1 evidence: `test_streamed_entries_round_trip_a_mixed_tree` (mixed tree,
  C-order parent-before-child, exhausted iterator yields nothing on a further
  pass), `test_list_input_snapshots_batches_complete_trees`,
  `test_plain_reads_carry_no_input_field` (integration).
- 2.1-2.3 evidence: `test_deploy_streams_until_unsafe_entry_then_fails`
  (predecessors written, transfer fails on the unsafe entry, nothing outside
  the task root, no spawn, rollback to FREE) plus adapted deployment tests;
  unit fault tests assert per-entry validation and streamed entry counts.
- 3.1 evidence: `test_has_no_input_entries_field` / `test_has_input_entries_field`
  (unit, dataclasses.fields); hydration method and its five call sites deleted;
  `test_snapshot_survives_run_complete_and_fail_saves` asserts task_inputs rows
  stay byte-identical across TO_DO→RUNNING→DONE transitions.
- 3.2 evidence: `test_input_snapshots_batch_loaded_once_for_returned_tasks`
  (unit) and e2e `tests/e2e/test_task_input_snapshots.py` pinning
  `content_base64` and the legacy metadata view.
- Review fix-ups after agent review: GeneratorExit at an abandoned stream no
  longer logs a spurious `stream_task_inputs_FAILED` (guard narrowed to the
  store read; regression test
  `test_streamed_input_early_close_is_not_a_failure`); the batched read now
  orders by `path COLLATE "C"` matching the stream order.
