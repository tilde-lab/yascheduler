# Proposal: input snapshot is read on demand, not with the task

## Why

A task's input snapshot is stored with exact file bytes and is part of the task
record. Every read that returns a task therefore loads every byte of that
task's input tree, because a repository returning whole records fills every
field.

The readers that pay for this never look at the bytes. The daemon reads the
queue's snapshots several times per cycle from producers that hand task
identities to workers, and again on each consume attempt that only inspects the
allocation. Only deployment, which uploads the bytes, and the client query,
which returns them by contract, need them.

The cost is now real: input trees hold pseudopotential and basis files instead
of text inputs, so a cycle reads tens or hundreds of megabytes per queued task
from a daemon that is doing nothing with them.

The same placement produces a second defect. An empty snapshot and a snapshot
that was never loaded are the same value, so a reader cannot tell an engine
that declares no inputs from a task whose bytes were not fetched.

See [ADR-0021](../../../docs/decisions/ADR-0021-input-bytes-read-apart-from-task.md).

## What Changes

- The task record carries no input contents. The pre-persistence submission
  shape keeps them: submission is the one place that must hold the bytes, and
  it writes them once.
- The store returns a task without its inputs, and returns the snapshot
  complete on an explicit request: every entry, its kind, its exact file bytes,
  and its empty directories.
- Deployment transfers the snapshot one entry at a time in path order, so the
  daemon holds one entry's contents instead of the whole tree.
- The client query keeps its current projection: the snapshot it returns and
  the legacy metadata view derived from it are unchanged.
- Unchanged: the storage layout, the schema, migrations, submission, the
  public client contract, the engine configuration format, and the removal of a
  task's inputs with the task.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `postgres-persistence`: the store returns the task record without input
  contents and returns the snapshot on a separate request, complete and inside
  the same transaction boundary.
- `task-input-snapshots`: deployment transfers the snapshot entry by entry and
  does not require the whole snapshot in memory.

## Impact

Touches the task domain record, the task repository port, the PostgreSQL
adapter and its queries, the deployment operation, the allocation use case, the
query use case, and the client projection; plus the decision record and the
tests of all of the above. No schema change, no migration, no public
configuration, no CLI change, no change to the query projection or the submit
surface.

Excluded: moving input bytes out of the database. The storage choice sets the
ceiling on snapshot size and is a separate decision, already out of scope for
the recursive-transfer change.
