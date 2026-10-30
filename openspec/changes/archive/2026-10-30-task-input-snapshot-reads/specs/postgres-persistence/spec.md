## MODIFIED Requirements

### Requirement: Snapshot lifecycle follows the task transaction

The task store SHALL keep the complete input snapshot of an accepted task,
including exact file bytes, apart from the task record. A task read SHALL
return the record alone. A snapshot request SHALL return the complete snapshot:
every entry, its kind, its exact file bytes, and its empty directories.
Lifecycle updates SHALL preserve the snapshot. Deleting a task SHALL delete its
snapshot in the same transaction. A task and its snapshot SHALL share one
transaction boundary.

#### Scenario: snapshot survives persistence and lifecycle updates

- **WHEN** a task with binary files, a zero-length file, an empty directory, and unrelated metadata is committed, reloaded, and advanced through its lifecycle
- **THEN** every task read returns the record without input contents, every snapshot request returns the same paths, entry kinds, and file bytes, unrelated metadata retains its values, and deleting the task removes its snapshot
