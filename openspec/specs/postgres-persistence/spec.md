## Purpose

The Postgres adapter satisfies the task repository, the node repository,
and the unit-of-work port defined in the `domain-ports` spec. One
short-lived unit of work spans one use case (see ADR-0004).

## Requirements

### Requirement: Unit-of-work transaction lifecycle

The unit of work SHALL open one connection on context entry. A commit
SHALL persist every change made in the context. An exception SHALL roll
every change back. Context exit SHALL close the connection on success
and on failure.

#### Scenario: commit persists changes and exit closes the connection

- **WHEN** a use case commits its changes and the context exits without error
- **THEN** the changes are persisted to the database and the connection is closed

#### Scenario: exception rolls back and exit closes the connection

- **WHEN** an exception occurs before commit
- **THEN** every change made in the context is rolled back and the connection is closed

### Requirement: Unit-of-work scope of use

A repository, a commit, or a rollback SHALL raise an error when it is
used before context entry or after context exit.

#### Scenario: use outside an open context raises

- **WHEN** a repository, a commit, or a rollback is used before context entry or after context exit
- **THEN** an error is raised

### Requirement: Repository save on a missing row raises

A repository save SHALL raise an error when the target row is not
present. The error SHALL be raised before the entity is tracked for
event dispatch.

#### Scenario: save on a missing row raises before event tracking

- **WHEN** a save targets an identity that is absent from the database
- **THEN** an error is raised and no entity is left for event dispatch

### Requirement: Repository rows round-trip as entities

The task and node repositories SHALL return entities that match the
stored rows. Identity attachment on insert is owned by `domain-ports`;
the task created event is owned by `domain-entities`.

#### Scenario: an insert or read returns the entity matching the database row

- **WHEN** a record is inserted or a stored row is read
- **THEN** the returned entity carries the values written to the database

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
