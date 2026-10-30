## ADDED Requirements

### Requirement: yasubmit captures the selected input tree

`yasubmit` SHALL resolve the engine's `input_files` selections against its
current working directory and submit the captured snapshot defined by
`task-input-snapshots`. The script's parent directory SHALL not replace the
working directory as the submission root. A selection or capture failure
SHALL leave no accepted task.

#### Scenario: submission captures from the working directory

- **WHEN** `yasubmit` runs from a directory containing the engine's selected input tree and receives a script located in a different directory
- **THEN** the accepted snapshot comes from the working directory, and stdout contains only the new task identity followed by a newline
- **AND** binary files enter the typed snapshot directly rather than untagged file-value metadata

#### Scenario: existing submit surface remains stable

- **WHEN** `yasubmit` submits with the current engine INI list syntax, script metadata handling, and flags
- **THEN** that handling is unchanged, including the bare task-id stdout contract

#### Scenario: unsafe or unreadable input fails cleanly

- **WHEN** a required selection is missing, unsafe, or unreadable
- **THEN** stderr identifies the failing selection or path with an `Error:` diagnostic, stdout is empty, exit status is `1`, and no task is accepted