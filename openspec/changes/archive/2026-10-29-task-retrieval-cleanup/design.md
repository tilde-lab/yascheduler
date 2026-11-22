# Design: task retrieval and cleanup lifecycle

## Context

This implements part 2 of the [ADR-0019
plan](../../../docs/plans/ADR-0019-implementation-plan.md) as amended after
review: mixed-error deferral plus finalise-before-delete ordering, with no
durable retrieval-progress state.

Current coupling points:

| Existing owner | Current behavior to replace or extend |
| --- | --- |
| [Consume use case](../../../yascheduler/application/consume_task.py) | Permanent errors finalise immediately even alongside transient ones |
| [OutputDownloader](../../../yascheduler/infra/ssh/operations/download.py) | `download_outputs` rmtrees inline on full success only, before the task outcome is committed; failed tasks never clean up |

## Decisions

- **Finalise, then delete.** The terminal transition is committed, the
  terminal event dispatched, and the allocation slot released before the
  first remote deletion. This removes the partial-deletion crash window by
  ordering instead of by stored state: before finalisation nothing is
  deleted remotely; after finalisation the task is terminal and is never
  consumed again. A crashed daemon either re-downloads from an untouched
  tree or not at all.
- **Defer on any transient error, including mixed.** While any transient
  failure remains the task stays RUNNING and every output is re-attempted
  next pass; error lists are derived per pass. A pass with zero transient
  failures finalises on its own errors. No diagnostics are carried across
  passes: a permanent error that vanishes later means the file recovered —
  finalising on the clean pass's own errors is correct, and retention would
  wrongly punish that recovery.
- **Cleanup is best effort and never changes the outcome.** One attempt per
  finalisation with the shared retry budget, on its own SFTP channel; an
  absent directory is fine; failures are logged and left to node teardown
  (cloud directories die with the VM).
- **No persistence.** No table, migration, port method, or domain value is
  added. The task row's existing lifecycle fields carry everything the next
  pass needs.

### Rejected during review: persisted boundary and retained diagnostics

An earlier draft stored a retrieval-progress row (phase, selected outcome,
sticky per-path diagnostics) to make cleanup resumable and to prevent a
later pass finalising success over an earlier permanent failure. Rejected:
the ordering above achieves the crash-safety goal without any state, and
sticky diagnostics guarded only the case of a permanent error vanishing
between passes — impossible for outputs of a finished process, or a signal
of misclassification that retention would convert into a wrong failure.

## Contracts and implementation boundaries

| Owner | Contract | Boundary |
| --- | --- | --- |
| [OutputDownloader](../../../yascheduler/infra/ssh/operations/download.py) | `download_outputs` loses its rmtree; new `cleanup_remote(session, remote_dir, task_id=None) -> None` never raises, uses its own channel and the shared retry budget, logs absent-dir at debug and failures at warning | Classification owns SFTP specifics; no file contents in logs |
| [Consume use case](../../../yascheduler/application/consume_task.py) | Defer on any transient error; on zero transient, apply `complete()`/`fail()` with that pass's errors; `_finalize_and_clean` commits the terminal save, logs, discards the tracker slot, then attempts cleanup exactly once | Cleanup never influences the outcome or return value |

## Testing strategy

- Unit: consume arcs (success, permanent-only, transient-only defer, mixed
  defer, missing engine, missing task) with an ordering trace proving the
  terminal commit precedes the cleanup call; downloader never rmtree's;
  `cleanup_remote` success/absent/failure logging without raising.
- E2E (PostgreSQL + SSH testcontainers): transient defers then succeeds;
  permanent failure finalises with the path in the error and cleans up;
  mixed defers then finalises failed once the transient file recovers;
  daemon restart between passes re-downloads from scratch and completes; a
  cleanup failure leaves the task DONE and the directory for teardown.
