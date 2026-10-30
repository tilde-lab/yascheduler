# ADR-0020: A task owns the output selections it was accepted under

- **Status:** Accepted
- **Date:** 2026-10-30
- **Supersedes:**
- **Superseded by:**

## Context

Output retrieval resolves the engine's configured output selections at
completion. The engine repository is frozen at daemon start, so the record that
decides what a task must return is a file that can be edited between acceptance
and retrieval, with a restart as the only trigger needed. Two failures follow:
an added selection fails a task that ran under the old declarations, and a
removed selection silently produces a done task with an incomplete result. The
second is invisible in the scheduler and surfaces at the consumer.

Inputs already crossed this boundary in
[ADR-0019](ADR-0019-recursive-task-file-transfers.md) part 1: the task owns its
input snapshot and deployment reads the snapshot, not the submission source.
Outputs kept the configuration as source. The alternatives:

- Record the selection list on the task at acceptance.
- Record the whole engine specification on the task, so every later stage reads
  one snapshot.
- Record the selections at allocation, narrowing the window to the deployment
  phase.
- Keep the configuration as source and only detect changes, reporting them.

## Decision

An accepted task records the output selections of its engine in force at
acceptance and keeps them for its whole lifecycle. Retrieval, and the operator
view command, resolve the recorded selections. A task accepted before the
record exists resolves the configured selections and reports the fallback in
the log with its identity. Nothing else about the engine is frozen.

## Alternatives Considered

### Record the whole engine specification

Rejected. The deployment artefacts and the spawn command name files of the
engine package that is deployed on the machine; they follow the release, not
the acceptance time. Freezing them would deploy stale commands and stale
artefacts. The staleness that changes outcomes is the retrieval contract, and
that is the only part frozen.

### Record the selections at allocation

Rejected. It shortens the window without closing it: a task waiting for a cloud
node can wait for hours, and its output contract would still be decided by a
configuration newer than its acceptance.

### Detect configuration changes and report them

Rejected. Detection needs the acceptance-time values stored anyway, and
reporting an already wrong retrieval does not restore the missing result.

## Consequences

- **Positive:** A task's outcome is derivable from the database, so a completed
  task with a missing result can be explained after the configuration that
  produced it was edited.
- **Positive:** Retrieval of an engine's in-flight tasks is stable across the
  configuration edits and restarts that accompany a release.
- **Negative / trade-offs:** A corrected engine configuration no longer repairs
  tasks already accepted; they must be resubmitted. This is the price of
  determinism and is reported in the release note.
- **Accepted risks:** During the upgrade window, pre-change tasks keep the old
  behaviour through the configuration fallback. The fallback is observable only
  in the log, so a long drain of old tasks prolongs the mixed source.
