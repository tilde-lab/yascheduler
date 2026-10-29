## Purpose

Application-layer use cases orchestrate task submission, allocation,
consumption, and node deallocation. They drive the domain entities
through the repository, SSH, and cloud ports. They do not own business
rules; the domain entities do.

## Requirements

### Requirement: Submit a task

The system SHALL create a new task in `TO_DO` state when the engine is known
and a complete input snapshot satisfies `task-input-snapshots` and the engine's
required selections in `task-file-selection`. Unknown engines and invalid
inputs SHALL be rejected before writing the task store. The task and its
complete snapshot SHALL commit together; acceptance SHALL return the new task
identity only after commit. A persistence failure SHALL leave neither an
accepted task nor partial input entries.

#### Scenario: a known engine creates a TO_DO task

- **WHEN** a task is submitted with a known engine and a complete valid input snapshot
- **THEN** a task is persisted in `TO_DO` state with its snapshot, the change is committed, and the new task identity is returned

#### Scenario: an unknown engine prevents the write

- **WHEN** a task is submitted with an engine the system does not know
- **THEN** the submission fails before any write and no task or input entries are persisted

#### Scenario: invalid submission or failed persistence prevents acceptance

- **WHEN** required inputs are invalid or incomplete, or persistence fails
- **THEN** submission fails and neither an accepted task nor partial input entries are committed

### Requirement: Allocate a task

The system SHALL match a `TO_DO` task to a free compatible machine. When a
match is found, the task SHALL transition to `RUNNING` on that machine. When no
match is found, the system SHALL request cloud provisioning and the task SHALL
stay pending.

Concurrent allocations SHALL be serialised at the capacity check so they do not
over-provision. A failure inside the cloud-fallback path SHALL remove the
temporary node row and SHALL NOT leave a partial allocation.

#### Scenario: a free compatible machine runs the task

- **WHEN** a TO_DO task is allocated and a free compatible machine exists
- **THEN** the task transitions to RUNNING on that machine and the change is committed

#### Scenario: no free machine triggers cloud fallback

- **WHEN** a TO_DO task is allocated and no free compatible machine exists
- **THEN** cloud provisioning is requested and the task stays pending

#### Scenario: a cloud-fallback failure cleans up the temporary node

- **WHEN** a failure occurs after the temporary node is inserted but before the allocation completes
- **THEN** the temporary node row is removed and no partial allocation is committed

### Requirement: Reject allocation for an unusable task engine

When the task engine is unknown or has no platforms, the task SHALL be marked as
failed and no provisioning SHALL be requested.

#### Scenario: an unusable task engine fails the allocation

- **WHEN** a `TO_DO` task is allocated and its engine is unknown or has no
  platforms
- **THEN** the task is marked as failed and no provisioning is requested

### Requirement: Deallocate idle nodes

The system SHALL disable each enabled cloud node whose idle time
exceeds the configured tolerance. Each disabled node SHALL be torn
down in this order: SSH disconnect, DB disable, cloud VM deletion, DB
row removal. The system SHALL return the disabled nodes so their VMs
can be deleted.

#### Scenario: an idle cloud node is disabled and torn down

- **WHEN** an enabled cloud node's idle time exceeds its configured tolerance
- **THEN** the node is disabled, its SSH session is disconnected, its VM is deleted, and its DB row is removed

### Requirement: Abandon a node

The system SHALL clean up a cloud node that never established its SSH
connection. The system SHALL disable the node's DB row before it attempts to
delete the cloud VM. In all cases, the system SHALL release all in-flight
allocation entries linked to the node and SHALL report the count released. The
DB-row removal, failure reporting, and no-cloud behavior SHALL follow the abandon
scenarios.

#### Scenario: an abandoned cloud node is fully cleaned up

- **WHEN** an abandoned cloud node with one in-flight allocation entry is cleaned up and the VM deletion succeeds
- **THEN** its VM is deleted, its DB row is removed, and its in-flight allocation entry is released

#### Scenario: a failed VM deletion leaves the DB row for retry

- **WHEN** the VM deletion fails during the abandon of a cloud node with one in-flight allocation entry
- **THEN** the DB row stays disabled so the VM deletion can be retried, and the in-flight allocation entry is released

#### Scenario: an abandoned node without a cloud VM is removed

- **WHEN** an abandoned node has no cloud
- **THEN** VM deletion is skipped, the DB row is removed, and the in-flight
  allocation entries are released

#### Scenario: a cleanup failure is reported

- **WHEN** VM deletion fails during abandonment or DB-row removal fails after a
  successful VM deletion
- **THEN** the failure is reported

### Requirement: Consume a task

The system SHALL download a RUNNING task's outputs from its remote
machine. When the download succeeds or fails permanently, the system
SHALL finalise the task: apply the terminal transition, clean the
remote directory, and release the in-flight allocation slot. When the
download fails transiently, the system SHALL defer the task: leave it
RUNNING, preserve the remote directory, and keep the slot.

#### Scenario: a completed or permanently failed download finalises the task

- **WHEN** the download completes with no transient errors
- **THEN** the task reaches a terminal state, the remote directory is cleaned, and the in-flight allocation slot is released

#### Scenario: a transient download failure defers the task

- **WHEN** the download fails with only transient errors
- **THEN** the task is left RUNNING, the remote directory is preserved, and the in-flight allocation slot is kept

### Requirement: Query tasks

The system SHALL return the tasks that match a supplied jobs filter or
a supplied statuses filter, together with the nodes allocated to those
tasks. The system SHALL reject a query that supplies both filters. The
system SHALL return no tasks when neither filter is supplied. The
query SHALL be read-only: it SHALL NOT commit changes.

#### Scenario: a supplied filter returns matching tasks with their nodes

- **WHEN** a query supplies a jobs filter or a statuses filter, but not both
- **THEN** the matching tasks are returned together with the nodes allocated to them, and no change is committed

#### Scenario: both filters supplied is rejected

- **WHEN** a query supplies both a jobs filter and a statuses filter
- **THEN** the query is rejected and no data is read

#### Scenario: neither filter supplied returns no tasks

- **WHEN** a query supplies neither a jobs filter nor a statuses filter
- **THEN** no tasks are returned and no data is read

### Requirement: Track in-flight cloud allocations

The system SHALL track in-flight cloud allocations by task identity.
An allocation SHALL be tracked once per task identity: a duplicate add
SHALL be rejected. The system SHALL release an allocation by task
identity. The system SHALL release every allocation linked to a node
and SHALL report the count released.

#### Scenario: an allocation is deduplicated by task identity

- **WHEN** an allocation is tracked for a task that is already tracked
- **THEN** the duplicate is rejected and the existing entry is kept

#### Scenario: release by node releases every linked allocation

- **WHEN** allocations linked to a node are released
- **THEN** every allocation linked to that node is removed and the count removed is reported
