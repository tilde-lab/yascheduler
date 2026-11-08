## ADDED Requirements

### Requirement: A stored legacy payload is flat

The forward migration SHALL read a stored legacy input payload as a flat map of
names to text values: each declaration identifies keys on its own, and no key
expands into a directory or into the keys sharing its prefix.

#### Scenario: a key that also has children stays one file

- **WHEN** a stored payload holds `basis` and `basis/Si.orb` while the engine declares the literal `basis`
- **THEN** the migration converts `basis` as one file entry with its stored value and leaves `basis/Si.orb` in unrelated metadata
- **AND** the run does not fail for a file/child conflict

#### Scenario: conversion agrees with submission

- **WHEN** the same payload and declarations are converted by the upgrade and accepted at submission
- **THEN** both produce the same input entries and the same leftover metadata
