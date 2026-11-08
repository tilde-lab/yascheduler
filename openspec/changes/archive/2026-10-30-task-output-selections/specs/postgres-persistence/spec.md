## ADDED Requirements

### Requirement: Output selections persist with the task

The task store SHALL persist the output selections a task recorded at
acceptance and SHALL return them unchanged on every task read, for the whole
lifecycle of the task. A task accepted before output selections were recorded
SHALL be returned without them.

#### Scenario: recorded selections survive persistence and lifecycle updates

- **WHEN** a task committed with recorded output selections is reloaded and advanced through its lifecycle
- **THEN** every read returns the selections it recorded, and a task from before the record exists is returned without them
