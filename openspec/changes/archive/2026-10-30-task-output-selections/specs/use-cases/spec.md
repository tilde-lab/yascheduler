## MODIFIED Requirements

### Requirement: Submit a task

The system SHALL reject unknown engines and invalid input before any
task-store write. Accepted input SHALL include a complete snapshot satisfying
`task-input-snapshots` and its engine's selections in `task-file-selection`. It
SHALL atomically persist a `TO_DO` task and snapshot, returning its identity
only after commit. A persistence failure SHALL leave neither a task nor partial
input entries.

#### Scenario: a known engine creates a TO_DO task

- **WHEN** a task is submitted with a known engine and a complete valid input snapshot
- **THEN** a task is persisted in `TO_DO` state with its snapshot, the change is committed, and the new task identity is returned

#### Scenario: an unknown engine prevents the write

- **WHEN** a task is submitted with an engine the system does not know
- **THEN** the submission fails before any write and no task or input entries are persisted

#### Scenario: invalid submission or failed persistence prevents acceptance

- **WHEN** required inputs are invalid or incomplete, or persistence fails
- **THEN** submission fails and neither an accepted task nor partial input entries are committed

### Requirement: Consume a task

The system SHALL resolve a RUNNING task's recorded output selections against
its remote task root following `task-file-selection` and retrieve them into the
local result root with their task-relative layout. The selections recorded at
acceptance SHALL remain authoritative for every retrieval pass. A task accepted
before output selections were recorded SHALL use its engine's configuration in
force at retrieval, and the fallback SHALL be logged with the task identity.

#### Scenario: a nested output tree reaches the result root

- **WHEN** a task declares an output directory whose tree contains `OUT.aiida/nested/result.dat`
- **THEN** the task finalises as done and that file is readable at the same relative path under the result root

#### Scenario: an accepted task keeps the selections it recorded

- **WHEN** the engine's configured output selections change after a task is accepted, and that task is later retrieved
- **THEN** the task retrieves the selections recorded at acceptance, and its outcome does not depend on the changed configuration

#### Scenario: a task without recorded selections uses the configuration

- **WHEN** a task accepted before output selections were recorded is retrieved
- **THEN** it retrieves its engine's configured output selections, and the fallback is reported in the log with the task identity

## ADDED Requirements

### Requirement: Record output selections at acceptance

An accepted task SHALL record its engine's output selections in force at
acceptance and keep them for its whole lifecycle. A submission MAY supply a
replacement list. It SHALL be non-empty and each entry SHALL satisfy
`task-file-selection`; otherwise submission SHALL fail before any task-store
write. A valid supplied list SHALL replace the engine's selections for that
task.

#### Scenario: a submission records the selections it supplies

- **WHEN** a submission supplies its own output selections for a known engine
- **THEN** the accepted task records those selections, and the engine's configured selections do not apply to that task

### Requirement: Classify and defer retrieval failures

Retrieval SHALL classify failures as transient or permanent while continuing
others. An absent explicit path or unmatched pattern SHALL be a permanent error
naming that selection. While any transient failure remains, even with permanent
failures, the system SHALL defer: leave the task RUNNING, preserve the remote
directory, and keep its allocation slot. Each pass SHALL re-attempt every
selection and derive errors from that pass; without transient failures it SHALL
finalise on those errors.

#### Scenario: a transient download failure defers the task

- **WHEN** the download fails with only transient errors
- **THEN** the task is left RUNNING, the remote directory is preserved, and the in-flight allocation slot is kept

#### Scenario: mixed permanent and transient failures defer the task

- **WHEN** one output fails permanently and another fails transiently in the same pass
- **THEN** the task is left RUNNING, the remote directory is preserved, and a later pass with no transient failures finalises on that pass's own errors

### Requirement: Finalise before remote cleanup

Finalisation SHALL precede remote deletion: the system SHALL commit the terminal
transition, publish its terminal event, and release the in-flight allocation slot
before removing the remote task directory. Remote cleanup SHALL be one
best-effort attempt whose failure is logged and leaves the directory to node
teardown; cleanup SHALL NOT change the task's outcome. A task whose engine was
removed from configuration SHALL finalise as failed.

#### Scenario: a completed or permanently failed download finalises the task

- **WHEN** the download completes with no transient errors
- **THEN** the task reaches a terminal state, the terminal transition and event land before the remote directory is removed, the remote directory is cleaned, and the in-flight allocation slot is released

#### Scenario: a removed engine fails the task and cleans up

- **WHEN** a RUNNING task's engine is no longer in configuration during retrieval
- **THEN** the task finalises as failed and its remote directory is cleaned after the failure is committed

#### Scenario: a cleanup failure does not change the outcome

- **WHEN** the remote directory cannot be deleted after the task finalised
- **THEN** the task keeps its committed outcome, the failure is logged, and the remaining directory is left for node teardown
