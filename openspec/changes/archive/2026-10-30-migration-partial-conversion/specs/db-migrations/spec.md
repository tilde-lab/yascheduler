## RENAMED Requirements

- FROM: `### Requirement: Unsafe conversion stops the upgrade`
- TO: `### Requirement: A task that has not started stops the upgrade`

## MODIFIED Requirements

### Requirement: Legacy task inputs migrate without metadata loss

The forward migration SHALL identify legacy file payloads with the input
declarations of each stored task's engine, convert those payloads into
task-owned input entries where the declarations allow it, and remove only
converted file keys from unrelated metadata. Conversion SHALL use stored
payloads rather than the original filesystem. Task identities, lifecycle state,
node bindings, result locations, and unrelated metadata SHALL be preserved for
every task.

#### Scenario: existing task data is converted

- **WHEN** the upgrade runs on stored tasks with available engine declarations and valid legacy file payloads
- **THEN** each task has equivalent input entries, only the converted file keys leave metadata, and task state and unrelated data remain unchanged

### Requirement: A task that has not started stops the upgrade

The upgrade SHALL stop and leave its conversion and its tracker entry
uncommitted when a task that has not started cannot be converted: its engine
declaration is unavailable, a declared input is absent from its payload, or its
payload cannot be converted safely. The stop SHALL name the task, its engine,
the failing input when known, and the input keys that task carries. Fresh
bootstrap and upgraded databases SHALL expose the same input storage contract.

#### Scenario: unsafe conversion stops the upgrade

- **WHEN** the upgrade meets a task that has not started whose engine declaration is missing or whose declared payload cannot be converted
- **THEN** the upgrade stops naming the task, its engine, the failing input when known, and the input keys that task carries, and no conversion or tracker entry is committed

## ADDED Requirements

### Requirement: The upgrade requires engine declarations

The upgrade SHALL stop before converting any stored task when it receives no
engine declaration context, and SHALL leave its tracker entry uncommitted. An
empty engine declaration mapping is a valid context.

#### Scenario: the upgrade refuses to run without declarations

- **WHEN** the upgrade runs without an engine declaration context against a database that holds stored tasks
- **THEN** it stops before converting any task and no tracker entry is committed

### Requirement: A task that has started is left unconverted

The upgrade SHALL continue past a task that has started and cannot be
converted. That task SHALL keep its stored payload and metadata, and SHALL gain
no input entries. The upgrade SHALL report each engine left with unconverted
tasks and the number of tasks that engine left.

#### Scenario: a task that has started is left unconverted

- **WHEN** the upgrade meets a task that has started and cannot be converted
- **THEN** the upgrade continues, that task keeps its stored payload and metadata and gains no input entries, and each engine left with unconverted tasks is reported with its count
