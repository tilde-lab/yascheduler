## MODIFIED Requirements

### Requirement: Transient SSH errors are retried

Idempotent SSH operations SHALL retry transient SSH errors with exponential
backoff up to a time-based deadline. The retry utility and backoff parameters
are owned by the `shared` spec and ADR-0014. Connection establishment, the
first CPU-core read in a session, each task-output file download, and the
post-finalisation remote task-directory removal are covered. A connection
whose retries are exhausted SHALL raise the SSH connection domain exception
defined in `domain-exceptions`, not the SSH library's exception.

#### Scenario: an exhausted connection raises the domain exception

- **WHEN** a connection attempt fails with a transient SSH error until the retry deadline expires
- **THEN** the SSH connection domain exception is raised, not the SSH library exception

#### Scenario: each task-output file is retried independently

- **WHEN** one output file fails with a transient SSH error and the next file downloads successfully
- **THEN** a fresh transfer channel is opened per file, the per-file retry applies independently, and the successful file is reported as downloaded

## ADDED Requirements

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
