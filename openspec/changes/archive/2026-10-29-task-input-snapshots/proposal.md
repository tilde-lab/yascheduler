# Proposal: task input snapshots

## Why

[ADR-0019](../../../docs/decisions/ADR-0019-recursive-task-file-transfers.md)
requires recursive task inputs without losing submission-time ownership.
The current path stores flat, ambiguously encoded file values in user metadata
and cannot deploy directory trees or arbitrary binary files reliably.

## What Changes

- Define shared task-relative file selection behavior and use it to capture
  input trees at submission, including hidden entries and empty directories.
- Persist one typed input snapshot per task in normalized task-owned entries;
  keep unrelated metadata separate and commit the task and snapshot atomically.
- Reconstruct the stored tree over SFTP before spawn, without access to the
  original source directory, retaining deployment rollback on failure.
- Normalize legacy Python submissions at the public boundary and add an
  explicit typed-entry input option for binary/tree submissions.
- Public task queries add a typed top-level `input_entries` projection with
  exact bytes while `metadata` keeps a legacy filename/value view derived from
  the stored snapshot; legacy metadata consumers continue to work.
  Existing submission arguments, returned integer task identities, CLI flags,
  task statuses, and AiiDA stdout remain stable.
- Add a forward migration through the existing migration mechanism.
  Preserve known legacy deployment encodings; do not infer unrecorded ones.

This is part 1 of the [implementation plan](../../../docs/plans/ADR-0019-implementation-plan.md).
Output recursion, mixed-error finalisation, cleanup retries, parser changes,
and cloud retention are excluded. Output selectors retain current behavior
until the later output change; this proposal does not advertise recursive
output support.

## Capabilities

### New Capabilities

- `task-file-selection`: Task-relative path and glob semantics, required
  selections, tree closure, deduplication, and confinement.
- `task-input-snapshots`: Submission-time input ownership, binary content,
  directory entries, and independence from the original source.

### Modified Capabilities

- `use-cases`: Submission accepts a complete validated input snapshot atomically.
- `postgres-persistence`: Store and retrieve task-owned input entries independently
  of unrelated metadata and preserve them through lifecycle updates.
- `db-migrations`: Convert legacy stored inputs using configured engine declarations.
- `package-facades`: Legacy submission normalization, explicit input entries,
  and a documented typed input query projection.
- `cli`: Capture engine input selections from the submission working directory.
- `ssh-infrastructure`: Deploy stored input trees safely before execution.

## Impact

Touches domain input values and task records; local snapshot capture;
submission and DI; task repository SQL and schema/migrations; public client
projection; CLI input capture; SFTP deployment; tests and public-client docs.
The migration runner must receive engine declarations from initialization;
its present database-only configuration cannot identify input payload keys.

No new dependencies, CLI commands, INI keys, external blob store, transport,
or scheduler state are needed. Python >=3.9 and pip/uv compatibility remain
required. This proposal contains documentation only; implementation tasks
remain unchecked and the current canonical specs remain unchanged until sync.
