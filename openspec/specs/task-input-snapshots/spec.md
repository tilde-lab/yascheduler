# task-input-snapshots Specification

## Purpose
Keep each accepted task's input tree independent of the submitting filesystem
and preserve file bytes and directory layout through storage and deployment.

## Requirements

### Requirement: Submission captures a complete input snapshot

Before accepting a filesystem submission, the system SHALL resolve all engine
input selections under the submission root and capture the selected file
contents and directory entries per `task-file-selection`. The snapshot SHALL
record ordinary file contents as exact bytes and represent empty directories
explicitly. The caller SHALL keep source inputs stable during capture; a
capture failure SHALL reject the submission with an actionable path diagnostic.

#### Scenario: a mixed tree is captured

- **WHEN** a task is submitted with a text file, a non-UTF-8 binary file, hidden descendants, and an empty directory selected under its submission root
- **THEN** the accepted snapshot contains the same file bytes and directory layout, including the empty directory
- **AND** file ownership, permissions, and timestamps are outside the captured snapshot contract

#### Scenario: a source read failure prevents acceptance

- **WHEN** a selected file disappears or becomes unreadable during capture
- **THEN** the submission fails and no task or partial snapshot is committed

### Requirement: Accepted inputs are owned by the task

An accepted task SHALL retain the captured snapshot through later lifecycle
transitions and scheduler restarts. Deployment SHALL use that snapshot as the
complete source of input paths and bytes. Later source edits, deletion, or
changes to engine input selections SHALL leave the accepted snapshot unchanged.
Unrelated user metadata SHALL remain independent of input contents.

#### Scenario: deployment survives source deletion and configuration changes

- **WHEN** the submitting directory is deleted, the engine input selections change, and the scheduler restarts after accepting a task
- **THEN** deployment reconstructs the originally accepted snapshot with the same bytes and empty directories

### Requirement: Input snapshot boundaries are observable

Capture, submission normalization, persistence, migration, and upload SHALL
emit structured DEBUG boundary records using module-local stdlib loggers.
Records SHALL distinguish entry, successful completion, skip, and failure,
carrying task identity when available, the input source or engine where
relevant, entry counts, and the affected selection or path when known.

#### Scenario: rejected inputs leave a failure trajectory

- **WHEN** capture, normalization, persistence, or migration fails
- **THEN** its boundary records identify the failure class and known task or path context, and no successful completion is recorded for the failed operation
- **AND** the failure propagates with the existing acceptance and transaction behavior unchanged

#### Scenario: interrupted upload records failure and rollback

- **WHEN** an upload fails or is cancelled before spawn
- **THEN** its records identify the task, known path, and failure class, and deployment records whether the session rollback released the machine
- **AND** no successful upload completion or spawn is recorded for that attempt

### Requirement: Boundary records stay content-free and own their outcome

Boundary and failure records SHALL identify the failure class without
logging file contents, encoded payloads, credentials, or arbitrary exception
text. Records SHALL describe the emitting boundary's own outcome and SHALL
NOT claim a transaction commit owned by their caller.

#### Scenario: input contents and credentials stay out of logs

- **WHEN** snapshot files or transport/database error messages contain sensitive data
- **THEN** boundary and input-transfer diagnostic records contain neither the file bodies, their encoded payloads, nor credentials from those error messages

### Requirement: Deployment transfers the snapshot entry by entry

Deployment SHALL transfer the accepted snapshot to the machine one entry at a
time, so the memory the scheduler holds for a task stays at the size of that
task's largest entry. Entries SHALL reach the machine in path order, with a
directory materialised before any entry beneath it. Each entry path SHALL pass
the existing path validation before the transfer writes it.

#### Scenario: a large snapshot deploys without being held whole

- **WHEN** a task whose input snapshot is larger than the scheduler could hold at once is deployed
- **THEN** every entry reaches the machine in path order, and the memory the scheduler holds for that task stays at the size of its largest entry
