## MODIFIED Requirements

### Requirement: Every configured selection is required

Each explicit selection SHALL exist and each glob SHALL match at least one
entry before overlap deduplication. An empty directory SHALL count as a
match. Input capture SHALL fail if any selection is absent or cannot be
read, identifying the failing selection or path. Successful matches from
other selections SHALL not make an incomplete submission acceptable. On
output retrieval, a missing explicit path or a glob that matches nothing
SHALL be a permanent retrieval error identifying that selection, and SHALL
not prevent the remaining selections from being attempted.

#### Scenario: one missing selection rejects the submission

- **WHEN** `INPUT` exists but the required pattern `basis/**/*.orb` has no matches
- **THEN** submission fails with a diagnostic identifying `basis/**/*.orb` and no task is accepted

#### Scenario: an unmatched output selection is a permanent error

- **WHEN** a task declares an output directory or glob that is absent from the remote task root
- **THEN** that selection is reported as a permanent retrieval error naming it, and the remaining output selections are still retrieved

#### Scenario: an empty directory satisfies a selection

- **WHEN** a configured output selection matches a remote directory that contains no entries
- **THEN** the selection is satisfied and the empty directory is recreated locally
