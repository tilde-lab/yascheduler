## MODIFIED Requirements

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

## ADDED Requirements

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
