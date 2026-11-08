## MODIFIED Requirements

### Requirement: Yascheduler public client contract

The `Yascheduler` client SHALL expose synchronous and asynchronous queue
methods to submit and query tasks. Submission SHALL accept an optional list of
output selections to record with the accepted task. Submission SHALL return the
new task's identity as an integer. Query methods SHALL return one mapping per
matched task: a sequence for list queries, and a single mapping or none for a
single-task query. This contract SHALL be identical across all public client
facades.

#### Scenario: submissions return integer identities through every facade

- **WHEN** a task is submitted through the synchronous or asynchronous client method of any public facade
- **THEN** the call returns the new task's identity as an integer

#### Scenario: every facade accepts supplied output selections

- **WHEN** a task is submitted with an explicit output selection list through the synchronous or the asynchronous client method
- **THEN** the accepted task records that list
