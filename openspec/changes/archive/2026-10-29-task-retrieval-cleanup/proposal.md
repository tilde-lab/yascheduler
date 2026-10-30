# Proposal: task retrieval and cleanup lifecycle

## Why

[ADR-0019 §4](../../../docs/decisions/ADR-0019-recursive-task-file-transfers.md)
(as amended 2026-10-29) requires mixed-error precedence for output retrieval
and remote cleanup ordered after finalisation. Today a permanent download
error finalises the task immediately even when transient errors could still
recover the remaining outputs, the remote directory is removed only on
complete success so failed tasks leak remote directories, and the removal
runs inside the download operation before the task's outcome is committed —
a daemon crash in that window makes the next pass re-download from a partly
deleted tree and fail permanently for files that were already fetched.

## What Changes

- Defer finalisation while any transient retrieval failure remains,
  including when permanent failures also exist: the task stays RUNNING, the
  remote directory is preserved, and the in-flight allocation slot is kept.
  Each pass re-attempts every output and re-derives its error lists; a pass
  with no transient failures finalises on that pass's own errors.
- Finalise strictly before remote deletion: the terminal transition is
  committed, its terminal event published, and the in-flight allocation slot
  released first; only then is the remote task directory removed. A crash
  therefore either re-downloads everything from an untouched tree or never
  re-downloads at all.
- Downloads no longer remove the remote directory. A separate best-effort
  cleanup runs after finalisation with the shared retry budget; an already
  absent directory is fine, and a cleanup failure is logged and left to node
  teardown without changing the task's outcome.
- A task whose engine was removed from configuration finalises as failed and
  its remote directory is cleaned, instead of stranding it.

Public task statuses, query shapes, engine output declarations, the database
schema, and the cloud retention policy are unchanged; this change adds no
persistence.

This is part 2 of the [implementation
plan](../../../docs/plans/ADR-0019-implementation-plan.md). Output selections
remain the existing single-file lists; recursive selection, destination merge,
and tree traversal arrive in part 3. This change does not close issue #183 or
claim recursive output support.

## Capabilities

### Modified Capabilities

- `use-cases`: Consume defers on mixed errors and finalises before cleanup;
  cleanup is best effort after the terminal commit.
- `ssh-infrastructure`: Downloads no longer remove the remote directory; a
  separate post-finalisation cleanup operation owns deletion.

## Impact

Touches the consume use case, the SFTP download operation, and their tests.
No schema, migration, port, or public-interface change; no new dependency.
Python >=3.9 and pip/uv compatibility remain required. This proposal
contains documentation only; implementation tasks remain unchecked and the
canonical specs remain unchanged until sync.
