## Purpose

Define task-relative selection behavior for files and directory trees. Input
capture uses this contract; output retrieval adopts it in the later output change.

## ADDED Requirements

### Requirement: Task-relative selection language

A configured selection SHALL name a relative file, directory, or glob pattern.
Matching SHALL be case-sensitive, use `/` as the component separator, and match
`*`, `?`, and character classes within one path component, including hidden
names. A `**` component SHALL match zero or more directory levels, and a glob
matching a directory SHALL select that directory and its complete subtree.

#### Scenario: glob metacharacters and shell syntax stay literal

- **WHEN** a selection contains a character class with a glob character, or shell variable, home-directory, or brace syntax
- **THEN** the class matches the enclosed glob character literally and the shell syntax is treated literally without expansion

### Requirement: Every configured selection is required

Each explicit selection SHALL exist and each glob SHALL match at least one
entry before overlap deduplication. An empty directory SHALL count as a match.
Input capture SHALL fail if any selection is absent or cannot be read,
identifying the failing selection or path. Successful matches from other
selections SHALL not make an incomplete submission acceptable.

#### Scenario: one missing selection rejects the submission

- **WHEN** `INPUT` exists but the required pattern `basis/**/*.orb` has no matches
- **THEN** submission fails with a diagnostic identifying `basis/**/*.orb` and no task is accepted

### Requirement: Selected trees keep task-relative layout

Selecting a directory or a glob matching one SHALL include its complete
subtree, including hidden descendants and empty directories. Overlapping
selections SHALL yield each task-relative path once. Selected files SHALL
retain their parent paths, with the directories needed to reconstruct the
tree, without flattening or an extra enclosing directory.

#### Scenario: overlapping selections preserve the selected tree

- **WHEN** inputs select `INPUT`, `basis`, `basis/**/*.orb`, and `**/result?.dat` from a tree containing hidden and empty directories
- **THEN** the result includes `INPUT`, the complete `basis` subtree, and matching result files at both root and nested levels, with each path once and its original task-relative location

### Requirement: Selection stays within the task root

Selections SHALL reject absolute paths and any `..` component before path
normalization, and resolution SHALL remain inside its task root. Source
entries and traversed ancestors SHALL be checked without following symbolic
links; a selected link, including a dangling link or a link inside a selected
tree, SHALL fail explicitly. Only ordinary files and directories are
supported; other selected filesystem entry types SHALL fail explicitly.

#### Scenario: unsafe source traversal fails explicitly

- **WHEN** a selected path escapes its root, traverses a symbolic link, or encounters an unsupported entry type
- **THEN** the operation fails with the offending path identified and does not read the referenced out-of-root content

### Requirement: Destination traversal stays within the task root

Destination traversal SHALL remain inside the destination task root and
SHALL reject symbolic links and file/directory type conflicts, identifying
the offending path. An unrelated unselected entry SHALL not invalidate an
otherwise valid selection unless traversal requires it.

#### Scenario: unsafe destination traversal fails explicitly

- **WHEN** destination traversal meets a symbolic link or a file/directory type conflict under the destination task root
- **THEN** the operation fails with the offending path identified and does not write through the link or the conflicting entry