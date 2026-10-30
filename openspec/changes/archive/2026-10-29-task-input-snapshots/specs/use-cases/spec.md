## MODIFIED Requirements

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
