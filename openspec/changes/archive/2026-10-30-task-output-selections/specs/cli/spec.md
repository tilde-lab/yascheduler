## MODIFIED Requirements

### Requirement: yastatus view mode connects with node parameters

In view mode, `yastatus` SHALL connect to each running task's node over SSH and
tail its remote output file. The file SHALL be the task's first recorded output
selection, or its engine's first configured selection when the task has no
record. For a pattern selection, the command SHALL name the pattern and print no
remote content. Login username, port, and jump parameters SHALL come from the
node record, not cloud configuration. The convergence snippet SHALL be cleaned
up after display.

#### Scenario: connection parameters come from the node, not from cloud config

- **WHEN** `yastatus -v` connects to a task whose node has username `yascheduler` and jump host `bastion.example.com`, while the cloud config has a different username and jump host
- **THEN** the connection uses the node's username `yascheduler` and the node's jump host `bastion.example.com`

#### Scenario: a pattern selection is named instead of tailed

- **WHEN** the first output selection recorded by a task is a pattern
- **THEN** the command names that pattern in its output and prints no remote file content

#### Scenario: the tailed file follows the task's own record

- **WHEN** `yastatus -v` tails a task whose engine configuration now lists a different first output selection than the task recorded
- **THEN** the tailed file is the one the task recorded
