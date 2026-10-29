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

Idempotent SSH operations SHALL retry transient SSH errors with exponential backoff up to a time-based deadline. The retry utility and backoff parameters are owned by the `shared` spec and ADR-0014. Connection establishment, the first CPU-core read in a session, and each task-output file download are covered. A connection whose retries are exhausted SHALL raise the SSH connection domain exception defined in `domain-exceptions`, not the SSH library's exception.

#### Scenario: an exhausted connection raises the domain exception

- **WHEN** a connection attempt fails with a transient SSH error until the retry deadline expires
- **THEN** the SSH connection domain exception is raised, not the SSH library exception

#### Scenario: each task-output file is retried independently

- **WHEN** one output file fails with a transient SSH error and the next file downloads successfully
- **THEN** a fresh transfer channel is opened per file, the per-file retry applies independently, and the successful file is reported as downloaded

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
