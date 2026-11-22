## ADDED Requirements

### Requirement: Deployment transfers the snapshot entry by entry

Deployment SHALL transfer the accepted snapshot to the machine one entry at a
time, so the memory the scheduler holds for a task stays at the size of that
task's largest entry. Entries SHALL reach the machine in path order, with a
directory materialised before any entry beneath it. Each entry path SHALL pass
the existing path validation before the transfer writes it.

#### Scenario: a large snapshot deploys without being held whole

- **WHEN** a task whose input snapshot is larger than the scheduler could hold at once is deployed
- **THEN** every entry reaches the machine in path order, and the memory the scheduler holds for that task stays at the size of its largest entry
