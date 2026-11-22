## ADDED Requirements

### Requirement: Task deployment reconstructs the stored input tree

Before starting a calculation, deployment SHALL create the stored input
directories and write each stored file's bytes under the remote task root,
following `task-file-selection` for paths and destination traversal and
`task-input-snapshots` for input ownership and source independence. Empty
directories and zero-length files SHALL remain distinct. The whole upload
SHALL complete before spawn; a failure SHALL prevent execution and report
the affected task and path.

#### Scenario: a stored tree reaches the remote task directory

- **WHEN** an accepted task with nested text and binary files and empty directories is deployed
- **THEN** the remote task directory contains the captured paths and exact bytes, and spawn occurs only after the complete snapshot is uploaded
- **AND** the existing SFTP transport performs the upload

#### Scenario: upload failure prevents execution

- **WHEN** a directory cannot be created, a destination is unsafe, or a file upload fails
- **THEN** the task is not spawned, the affected task and path are reported, and deployment follows `Task deploy occupies the session and reverts on failure`, preserving caller-owned allocation rollback