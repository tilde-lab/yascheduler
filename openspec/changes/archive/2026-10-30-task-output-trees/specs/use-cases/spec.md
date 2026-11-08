## MODIFIED Requirements

### Requirement: Consume a task

The system SHALL resolve a RUNNING task's configured output selections
against its remote task root following `task-file-selection`, and retrieve
the selected paths into the local result root with their task-relative
layout. It SHALL classify each failure as transient or permanent while
continuing the other selections; a missing explicit path or a pattern
matching nothing SHALL be a permanent error naming that selection. While any
transient retrieval failure remains — including when permanent failures also
exist — the system SHALL defer the task: leave it RUNNING, preserve the
remote directory, and keep the in-flight allocation slot. Each pass SHALL
re-attempt every selection and derive its error lists from that pass alone;
a pass with no transient failures SHALL finalise the task on that pass's own
errors. A task whose engine was removed from configuration SHALL finalise
as failed.

Finalisation SHALL precede remote deletion: the system SHALL commit the
terminal transition, publish its terminal event, and release the in-flight
allocation slot before removing the remote task directory. Remote cleanup
SHALL be one best-effort attempt whose failure is logged and leaves the
directory to node teardown; it SHALL NOT change the task's outcome.

#### Scenario: a completed or permanently failed download finalises the task

- **WHEN** the download completes with no transient errors
- **THEN** the task reaches a terminal state, the terminal transition and event land before the remote directory is removed, the remote directory is cleaned, and the in-flight allocation slot is released

#### Scenario: a nested output tree reaches the result root

- **WHEN** a task declares an output directory whose tree contains `OUT.aiida/nested/result.dat`
- **THEN** the task finalises as done and that file is readable at the same relative path under the result root

#### Scenario: a transient download failure defers the task

- **WHEN** the download fails with only transient errors
- **THEN** the task is left RUNNING, the remote directory is preserved, and the in-flight allocation slot is kept

#### Scenario: mixed permanent and transient failures defer the task

- **WHEN** one output fails permanently and another fails transiently in the same pass
- **THEN** the task is left RUNNING, the remote directory is preserved, and a later pass with no transient failures finalises on that pass's own errors

#### Scenario: a removed engine fails the task and cleans up

- **WHEN** a RUNNING task's engine is no longer in configuration during retrieval
- **THEN** the task finalises as failed and its remote directory is cleaned after the failure is committed

#### Scenario: a cleanup failure does not change the outcome

- **WHEN** the remote directory cannot be deleted after the task finalised
- **THEN** the task keeps its committed outcome, the failure is logged, and the remaining directory is left for node teardown
