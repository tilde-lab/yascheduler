# Tasks

Implementation checklist. Scope: the ownership gap
listed as issue 003 in the scratch backlog. See
[ADR-0020](../../../../docs/decisions/ADR-0020-task-owned-output-selections.md)
for the decision and `design.md` for the trade-offs.

## 1. Domain and persistence

- [x] 1.1 Add the recorded output selections to the pre-persistence task shape
  and to the task record, defaulting to an empty tuple and surviving every
  state transition the way the input snapshot does. Extend the task record
  invariants comment. Unit-test that a transition preserves a non-empty record.
- [x] 1.2 Add nullable `output_selections TEXT[]` to `yascheduler_tasks` in the
  schema snapshot, set the snapshot's last-migration constant, and add
  `015_task_output_selections.sql` as plain DDL. Map the column in every task
  query that returns a task and in the insert and update statements; leave
  legacy rows NULL. Integration-test the round trip, the lifecycle update, and
  a pre-change row returning no record.

## 2. Use cases and CLI

- [x] 2.1 In `submit_task`, record the engine's output selections with the new
  task. Unit-test that the accepted task carries the selections in force.
- [x] 2.2 In `consume_task`, resolve the task's recorded selections; when the
  record is absent, fall back to the engine's configured selections and emit a
  warning carrying the task identity. Keep the defer, finalise, cleanup, and
  missing-engine paths unchanged. Unit-test: a configuration change after
  acceptance does not alter the retrieved set; an absent recorded selection
  stays a permanent error; a task without a record uses the configuration and
  logs the warning.
- [x] 2.3 In the `yastatus` view command, tail the task's first recorded
  selection, falling back to the engine's first configured selection. When that
  selection is a pattern, print a diagnostic naming it and skip the remote
  tail. Unit-test all three branches; the pattern branch asserts that the
  pattern appears in the output and that no remote command runs.

- [x] 2.4 Accept an optional explicit output selection list on submission:
  validate every entry with the selection language before any write, reject an
  empty list, and record the supplied list instead of the engine's. Expose it
  as a keyword on the public client's submit methods and leave the
  command-line submit surface unchanged. Unit-test that a supplied list is
  recorded verbatim, that an unsafe entry and an empty list are both rejected
  before the task write, and that a submission without the keyword records the
  engine's selections.

## 3. Specs, decision record, and verification

- [x] 3.1 Apply the spec deltas to `use-cases`, `postgres-persistence`, `cli`,
  and `package-facades`, and run `openspec validate --all --json`.
- [x] 3.2 Add the decision record and the pointer from ADR-0019.
- [x] 3.3 Run the gates: `uv run pytest -m unit`, `uv run pytest -m integration`,
  `uv run zuban check`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run lint-imports`.
- [x] 3.4 Add an e2e case that changes the engine's output selections after
  submission and retrieves the task, asserting the recorded set and the task
  outcome.

## 4. Verification Evidence

- Unit: `1581 passed`.
- Integration: `154 passed, 1 skipped`.
- E2E: `13 passed, 3 skipped` — live-cloud tests explicitly disabled.
- Static and spec gates: `zuban check`, `ruff check .`,
  `ruff format --check .`, `lint-imports`, and
  `openspec validate --all --json` passed.
