## ADDED Requirements

### Requirement: Input snapshots persist as task-owned entries

The task store SHALL persist input snapshots as normalized file and directory
entries owned by a task, separate from unrelated metadata. Each task-relative
path SHALL identify at most one entry within its task. Directory entries SHALL
be distinguishable from zero-length files. The database SHALL reject duplicate
paths and orphan entries.

#### Scenario: task ownership remains consistent

- **WHEN** a duplicate or orphan input entry is written, or a task is deleted
- **THEN** an invalid write fails atomically, while task deletion removes all of that task's input entries without affecting another task

### Requirement: Snapshot lifecycle follows the task transaction

Reading an accepted task SHALL recover its complete snapshot, including exact
file bytes. Lifecycle updates SHALL preserve that snapshot, and deleting a
task SHALL delete its owned input entries in the same transaction. A task and
its entries SHALL share the transaction boundary defined by the unit-of-work
contract.

#### Scenario: snapshot survives persistence and lifecycle updates

- **WHEN** a task with binary files, a zero-length file, an empty directory, and unrelated metadata is committed, reloaded, and advanced through its lifecycle
- **THEN** its input paths, entry kinds, file bytes, and unrelated metadata retain their values