## Purpose

The SSH adapter connects to nodes over SSH and exposes them as
connected-machine handles. It implements the connected-machine
collection and connected-machine handle ports defined in the
`domain-ports` spec.

## Requirements

### Requirement: SSH adapter implements the connected-machine ports

The system SHALL provide an SSH adapter that satisfies the
connected-machine ports. Domain use cases SHALL depend on those ports
and SHALL NOT import the SSH adapter.

#### Scenario: use cases depend on the ports, not on the SSH adapter

- **WHEN** a domain use case reads, queries, or commands a connected machine
- **THEN** it goes through the connected-machine port; it has no import of the SSH adapter

### Requirement: Connection registers a session keyed by node identity

A connection SHALL register one session per node, keyed by node
identity. The transport identity SHALL be read from the node's stored
fields and SHALL NOT be re-resolved from live configuration at connect
time. The jump fields and the direct-connection rule are owned by the
`domain-entities` spec.

#### Scenario: the tunnel leg comes from the stored node

- **WHEN** a connection is opened to a node
- **THEN** the SSH tunnel leg is built from the node's stored jump fields, or omitted when the stored jump host is empty

### Requirement: A session exposes the connected-machine contract

A session SHALL expose, for its lifetime: platform compatibility, CPU-core count, command execution, file upload, task-output download, and the occupy/release state transitions of the connected machine (defined in `domain-entities`). The CPU-core count discovery and memoization decision is owned by ADR-0015.

#### Scenario: the CPU count is discovered once per session lifetime

- **WHEN** the CPU-core count is read twice on the same connected session
- **THEN** the remote machine is queried once; the second read returns the value already discovered for that session

#### Scenario: a reconnected session re-discovers

- **WHEN** a session is closed and a new session is opened for the same node
- **THEN** the new session queries the remote machine for the CPU-core count again

### Requirement: Free-session query

The free-session query SHALL return only FREE sessions, SHALL accept an optional platform filter, and SHALL order the result oldest-first by idle timestamp.

#### Scenario: free sessions are filtered and ordered

- **WHEN** the free-session query is called with a platform filter
- **THEN** only FREE sessions whose platform matches are returned, ordered oldest-first by idle timestamp

### Requirement: Transient SSH errors are retried

Idempotent SSH operations SHALL retry transient SSH errors with exponential
backoff up to a time-based deadline. The retry utility and backoff parameters
are owned by the `shared` spec and ADR-0014. Connection establishment, the
first CPU-core read in a session, remote output-selection resolution
(directory listings and metadata reads), each task-output file download, and
the post-finalisation remote task-directory removal are covered. A connection
whose retries are exhausted SHALL raise the SSH connection domain exception
defined in `domain-exceptions`, not the SSH library's exception.

#### Scenario: an exhausted connection raises the domain exception

- **WHEN** a connection attempt fails with a transient SSH error until the retry deadline expires
- **THEN** the SSH connection domain exception is raised, not the SSH library exception

#### Scenario: each task-output file is retried independently

- **WHEN** one output file fails with a transient SSH error and the next file downloads successfully
- **THEN** a fresh transfer channel is opened per file, the per-file retry applies independently, and the successful file is reported as downloaded

#### Scenario: a resolution failure is one transient error for the pass

- **WHEN** listing a directory or reading a selected entry's metadata fails with a transient SSH error until its retry budget expires
- **THEN** the pass records a transient retrieval error, the remote results stay untouched, and the task is retried on a later pass

### Requirement: Task deploy occupies the session and reverts on failure

A task deploy SHALL mark the session BUSY before the deploy step. Any failure during deploy SHALL revert the session to FREE before the failure propagates. The DB task status and the orchestrator's in-memory running marker are owned by the caller.

#### Scenario: a failed deploy returns the session to FREE

- **WHEN** the deploy step fails after the session was marked BUSY
- **THEN** the session is FREE when the failure propagates, and the original failure is raised

### Requirement: Occupancy monitoring marks the session busy and releases on exit

The occupancy check SHALL mark the session BUSY before monitoring begins and SHALL release the session to FREE when the monitored engine process exits. The once-per-node rule for the occupancy check is owned by the `orchestrator` spec.

#### Scenario: engine process exit releases the session

- **WHEN** the monitored engine process exits while the session is BUSY
- **THEN** the session is released to FREE with the idle timestamp set

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

### Requirement: Remote cleanup runs after finalisation

Task-output download SHALL NOT remove the remote directory. A separate
remote-cleanup operation SHALL delete the remote task-directory tree over
its own SFTP channel using the shared retry budget, and SHALL be called only
after the task's terminal transition is committed. The operation SHALL NOT
raise: an already absent directory is logged as absent, any deletion failure
is logged with the remaining directory left to node teardown, and neither
affects the task's outcome.

#### Scenario: downloads no longer delete the remote directory

- **WHEN** every configured output downloads successfully
- **THEN** the remote directory still exists; only the post-finalisation cleanup operation removes it

#### Scenario: an absent directory is not an error

- **WHEN** cleanup runs against a remote task directory that no longer exists
- **THEN** the absence is logged and no warning or error is raised

#### Scenario: a deletion failure is logged and leaves the directory

- **WHEN** the directory deletion fails after its retry budget is exhausted
- **THEN** the failure and remaining directory are logged, and the task's committed outcome is unchanged

### Requirement: Output selections resolve against the remote task root

After execution, retrieval SHALL resolve each configured output selection
against the remote task root following `task-file-selection`. An explicit
file SHALL resolve to its own relative path; an explicit directory or a glob
matching one SHALL include its complete subtree, including hidden entries
and empty directories; overlapping selections SHALL resolve each path once.

#### Scenario: hidden entries and empty directories survive resolution

- **WHEN** a selected remote directory contains hidden entries and empty directories
- **THEN** the resolved tree contains the same hidden paths and empty directories

#### Scenario: overlapping output selections resolve each path once

- **WHEN** one configured path and a glob both select the same file
- **THEN** the resolved tree contains that file once and does not depend on selection order

### Requirement: Resolution rejects links and unsupported entry types

Resolution SHALL NOT follow symbolic links: a selected remote link SHALL be a
permanent error identifying the path, and any other unsupported remote entry
type SHALL fail the same way.

#### Scenario: a selected remote link is a permanent error

- **WHEN** a selection resolves to a symbolic link, or a selected directory contains one
- **THEN** the path is reported as a permanent retrieval error and the link target is not read

### Requirement: Retrieved outputs recreate their task-relative layout

Retrieval SHALL recreate the resolved paths under the local result root with
their task-relative layout, and SHALL transfer files over the existing SFTP
transport with attribute preservation and per-file retry isolation.
Directory modes, ownership, and timestamps are not transferred.

#### Scenario: a nested output tree keeps its layout

- **WHEN** an output selection names a directory containing `nested/result.dat`
- **THEN** the local result root contains `nested/result.dat` at that same relative path with identical bytes

#### Scenario: an explicit file lands at its own path

- **WHEN** an output selection names one file at the task root
- **THEN** the file lands under the result root at that same name, without an added enclosing directory

### Requirement: Retrieval merges into the result root without clearing it

Before writing, retrieval SHALL check each destination path without following
links: a symbolic link or a file/directory type conflict SHALL be a permanent
error for that path and nothing SHALL be written through it. Selected
ordinary files SHALL replace existing local files at the same path, unrelated
local entries SHALL be retained, and retrieval SHALL NOT clear the result
root.

#### Scenario: an unsafe destination path is rejected

- **WHEN** the local destination path for a selected output is a symbolic link, or a directory where a file is required
- **THEN** retrieval reports a permanent error for that path and writes nothing through it

#### Scenario: unrelated local results are retained

- **WHEN** the local result root already holds files that no selection covers
- **THEN** those files remain and only selected paths are replaced
