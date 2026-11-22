# Proposal: input conversion failures stop only for a task that has not started

## Why

The upgrade that moves legacy task inputs into task-owned entries converts
every stored task and stops when any stored task cannot be converted. Two
routine causes reach that stop:

- the task's engine is no longer declared in the configuration, because the
  engine was retired, renamed, or the database was moved to a host with a
  different configuration;
- the engine is still declared, but its input declarations changed after the
  task was accepted, so the stored payload no longer satisfies them.

The configuration lives apart from the database and drifts from it over years.
A stopped upgrade leaves the database unchanged and the tracker unwritten, so
the scheduler does not start either: the operator has a wall, and the only way
through it is to declare engines nobody runs, or to edit the database by hand.

The wall is out of proportion to what is at stake. Only a task that has not
started can still be sent to a machine, and only then do its inputs matter. A
task that is running has already been transferred, and a task that has finished
is terminal. Their stored payloads have no further use, so their conversion
failure costs nothing and blocks everything.

The tests and the recorded policy show why the current behaviour exists: the
migration cannot tell a file from a user's metadata without declarations, so
it refuses to guess. That reason justifies refusing to guess. It does not
justify stopping the upgrade for tasks that can never run again.

## What Changes

- The upgrade converts each stored task where the declarations allow it. A
  conversion failure stops the upgrade only when the failing task has not
  started yet; a missing declaration context or an operational fault remains
  fatal regardless of task status.
- A task that has already started, and cannot be converted, keeps its stored
  payload and metadata unchanged, gains no input entries, and does not stop the
  upgrade. It stays in its legacy shape.
- The upgrade refuses to run when it receives no engine declaration context
  (`None`); an explicit empty mapping is valid. Without the context it would
  convert nothing and still record itself as applied.
- A task that has not started still stops the upgrade, whether its engine
  declaration is missing or its payload disagrees with the declaration. The
  stop names the task, the engine, the failing input when known, and the inputs
  that task carries, so the operator can either declare the engine and its
  inputs or retire the task.
- The operator sees a report naming each engine whose tasks were left
  unconverted and how many tasks each engine left.
- Unchanged: the schema, the tracker semantics, the input storage contract,
  the public query projection, deployment, the command-line surfaces, and the
  configuration format.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `db-migrations`: the upgrade converts what it can, conversion failures stop
  only for a task that has not started, an engine declaration context is
  required (an empty mapping is allowed), and the engines left with unconverted
  tasks are reported.
- `task-input-snapshots`: boundary records distinguish a skipped conversion
  from a successful one and from a failure.

## Impact

Touches migration 014, its integration tests, the two specs above, and the
unreleased change note. No runtime path changes: allocation, deployment,
retrieval, submission, and the query projection keep their current behaviour.

Excluded: converting a task left unconverted. The migration records itself as
applied, so converting those tasks later needs another migration, and no
reported deployment needs one.
