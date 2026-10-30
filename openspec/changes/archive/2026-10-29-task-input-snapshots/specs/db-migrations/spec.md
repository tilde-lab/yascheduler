## ADDED Requirements

### Requirement: Legacy task inputs migrate without metadata loss

The forward migration SHALL identify legacy file payloads using the input
declarations of each stored task's configured engine, convert those payloads
into task-owned input entries, and remove only converted file keys from
unrelated metadata. Conversion SHALL use stored payloads rather than the
original filesystem. Task identities, lifecycle state, node bindings, result
locations, and unrelated metadata SHALL be preserved for all task statuses.

#### Scenario: existing task data is converted

- **WHEN** the upgrade runs on stored tasks with available engine declarations and valid legacy file payloads
- **THEN** each task has equivalent input entries, only the converted file keys leave metadata, and task state and unrelated data remain unchanged

### Requirement: Legacy encodings convert only by recorded convention

Legacy text SHALL retain its UTF-8 deployment meaning. The existing `fort.9`
base64 convention SHALL retain its decoded byte meaning. Other untagged
strings SHALL remain text even when they resemble base64. The migration SHALL
not claim to recover encodings that were never recorded.

#### Scenario: untagged base64-like text remains text

- **WHEN** a non-`fort.9` legacy file payload is a string that resembles base64
- **THEN** it converts as UTF-8 text and is not decoded as base64

### Requirement: Unsafe conversion stops the upgrade

The migration SHALL fail with task and path or engine diagnostics when a
required engine declaration is unavailable, a required payload is absent, or
a payload cannot be converted safely. It SHALL leave the input conversion and
its migration tracker entry uncommitted on failure. Fresh bootstrap and
upgraded databases SHALL expose the same input storage contract.

#### Scenario: unsafe conversion stops the upgrade

- **WHEN** a task lacks its engine declaration or has a missing, unsafe, or undecodable required payload
- **THEN** migration reports the affected task and cause, retains the pre-conversion task data, and does not record the input migration as applied
