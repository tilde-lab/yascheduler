## MODIFIED Requirements

### Requirement: Input snapshot boundaries are observable

Capture, submission normalization, persistence, migration, and upload SHALL
emit structured DEBUG boundary records using module-local stdlib loggers.
Records SHALL distinguish entry, successful completion, skip, and failure,
carrying task identity when available, the input source or engine where
relevant, entry counts, and the affected selection or path when known.

#### Scenario: rejected inputs leave a failure trajectory

- **WHEN** capture, normalization, persistence, or migration fails
- **THEN** its boundary records identify the failure class and known task or path context, and no successful completion is recorded for the failed operation
- **AND** the failure propagates with the existing acceptance and transaction behavior unchanged

#### Scenario: interrupted upload records failure and rollback

- **WHEN** an upload fails or is cancelled before spawn
- **THEN** its records identify the task, known path, and failure class, and deployment records whether the session rollback released the machine
- **AND** no successful upload completion or spawn is recorded for that attempt
