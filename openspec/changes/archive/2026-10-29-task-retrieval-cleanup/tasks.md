# Tasks

These are implementation tasks, not claims of work completed by this proposal.
Scope: part 2 (as amended after review) of the
[ADR-0019 plan](../../../docs/plans/ADR-0019-implementation-plan.md).

## 1. SSH cleanup operation

- [x] 1.1 Extract the rmtree from `download_outputs` and add the
  post-finalisation `cleanup_remote` operation with its own channel and the
  shared retry budget. Unit-test that downloads never rmtree, and the
  cleanup paths: success, already-absent directory (debug, no warning),
  deletion failure (warning, no raise); assert no file contents or
  credentials in log records.

## 2. Consume use case

- [x] 2.1 Rework `consume_task`: defer on any transient error including
  mixed outcomes; finalise on the first zero-transient pass using that
  pass's own errors; commit the terminal transition, publish the event, and
  discard the allocation slot strictly before the single best-effort cleanup
  attempt; fail a task whose engine was removed and clean its directory.
  Unit-test every arc with an ordering trace proving the terminal commit
  precedes the cleanup call, plus the FINALISE_DECISION / RETRIEVAL_DEFER /
  FINALISE_OK / FINALISE_FAIL markers.

## 3. End-to-end fault injection

- [x] 3.1 In the PostgreSQL/SSH e2e harness: transient-only defers then
  succeeds with the remote directory removed after finalisation;
  permanent-only finalises failed with the path in the error and cleans up;
  mixed defers and finalises failed once the transient file recovers;
  daemon restart between passes re-downloads from scratch and completes; a
  cleanup failure leaves the committed outcome and the remaining directory
  untouched. Assert terminal events and allocation-slot release occur at
  the finalisation boundary.

## 4. Verification and sync

- [x] 4.1 Run `uv run pytest -m unit`, `uv run pytest -m integration`, and
  `uv run pytest -m e2e`; all must pass.
- [x] 4.2 Run `uv run zuban check`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `uv run lint-imports`; verify Python
  >=3.9 syntax, no new dependencies, and no package version edit.
- [x] 4.3 Run `openspec validate --all --json` and review the final diff
  against both capability deltas; confirm no persistence, public status,
  query-shape, or cloud-policy change slipped in.
- [ ] 4.4 Sync/archive the change only after review and acceptance; rerun
  `openspec validate --all --json` after sync/archive; prepare part 3's
  proposal before implementing output recursion.
