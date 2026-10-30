## ADDED Requirements

### Requirement: Legacy metadata claims one flat key per declaration

A legacy payload SHALL be read as a flat map of names to text values: a literal
declaration claims the key of that name, a glob claims keys as file paths where
`*` never crosses `/` and a trailing `**` claims every key below its prefix, and
no key implies a directory. Every declaration SHALL claim at least one key, every
claimed value SHALL be text, and typed metadata fields SHALL never be claimed.

#### Scenario: a declared key that also has children

- **WHEN** a submission carries `basis` and `basis/Si.orb` while the engine declares the literal `basis`
- **THEN** the snapshot holds `basis` as one file with its own value, `basis/Si.orb` remains unrelated metadata, and the submission is accepted

#### Scenario: a declaration without its own key

- **WHEN** a submission carries `basis/Si.orb` while the engine declares the literal `basis`
- **THEN** the submission is rejected with a missing-input error naming `basis`, and no directory is inferred from the nested key

#### Scenario: a trailing wildcard selects a subtree

- **WHEN** a submission carries `basis/one.orb` and `basis/nested/two.orb` while the engine declares `basis/**`
- **THEN** both keys are claimed with their recorded values and the snapshot carries the parent directories they need
