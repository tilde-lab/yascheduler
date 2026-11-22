# Design: input conversion failures stop only for a task that has not started

## Context

Migration 014 moves legacy task inputs out of the task's JSONB metadata into
task-owned entries. It reads every stored task, converts the payload keys that
the task's engine declares as inputs, writes the entries, and removes only the
converted keys from the metadata. All of it runs in one transaction with the
tracker row.

The declaration of what counts as an input comes from the running
configuration, not from the database. The configuration and the database are
separate artefacts with separate histories. Two drift shapes follow:

- the engine is gone from the configuration;
- the engine is still configured, but its `input_files` changed after older
  tasks were accepted.

Both end in the same place today: the first task that fails to convert stops
the migration, nothing is committed, and the scheduler does not start. The
migration runs once per database, so there is no retry that helps and no
later migration can repair it, because a later migration never runs.

Only one state still needs the conversion: a task that has not started. Its
entries are what deployment transfers when the allocator picks a machine. The
transfer sends the stored entries and never consults the declarations, so a
task left with fewer entries would arrive with fewer inputs and no error.

A task that is already running was transferred before it started. A task that
has finished is terminal and is never allocated again. Neither can lose
anything by being left in its legacy shape.

## Decisions

- **The axis is whether the task has started, not whether its engine is
  declared.** The declaration question explains one of the two drift shapes.
  The other one - a declared engine whose declarations changed - reaches
  finished tasks as well, and those are the bulk of a long-lived database.
  Scoping by declaration would still stop on them and would still force the
  operator to invent engine sections for engines nobody runs.

- **A task that has not started keeps the stop.** Skipping it would change its
  fate silently: it would run with the inputs that survived conversion, and
  nothing downstream would notice. Stopping is noisy but honest, and the
  operator has two ways out: declare the engine and its inputs to match the
  stored payload, or retire the task.

- **A skipped task keeps its payload and metadata exactly as stored.** No
  partial conversion, no guessed entries. The public projection already keeps
  the legacy metadata view for a task whose entries are absent, so the query
  keeps its shape for these rows.

- **No backfill.** Migration 014 records itself as applied, so a task left
  unconverted stays unconverted until another migration converts it. That is
  accepted: no deployment has reported such a task, and a repair path without
  a case is speculative.

- **The upgrade refuses a missing engine-declaration context.** A missing
  context (`None`) is a caller error: without it, a forgotten argument would
  convert nothing while recording the migration as applied. An explicit empty
  mapping is valid and declares no engines. The declarations are mandatory for
  this migration even though the runner keeps them optional for migrations that
  do not need them.

- **Store failures still stop the upgrade for every task.** A failed write is a
  database fault, not a task whose data cannot be represented. It keeps the
  current behaviour and the current rollback.

- **The report is aggregate, the stop is specific.** Each engine left with
  unconverted tasks is reported once, with a count. The stop for a task that has
  not started names the task, the engine, the failing input when known, and the
  input keys the task carries, because that is the only place where the operator
  writes declarations from it.

## Alternatives Considered

### Skip every task whose engine is not declared

This is the mechanism recorded in the reported issue. Rejected: it covers one
of the two causes. A finished task whose engine is declared, but whose
declarations changed, still stops the upgrade, and those tasks are the ones a
long-lived database accumulates.

### Skip every conversion failure

Rejected. A task that has not started would reach a machine with whatever
entries survived. Deployment transfers stored entries and does not compare them
with the declarations, so the run would proceed without input files and no
error would be recorded.

### Convert every stored key into an input entry

Rejected in ADR-0019 and still rejected: user metadata would become inputs,
non-text values cannot become files, and the snapshot would stop being the
engine's input set.

### Keep the legacy payload readable at deployment

Two stored forms forever, with the legacy decoder in the deployment path.
Rejected by ADR-0019 §5, which chose one stored form.

### Report the mismatch before the upgrade

A preflight inventory of engines in the database against the configuration.
Rejected as a substitute: it explains the wall instead of removing it, and the
operator still has to declare engines nobody runs.

### Stage the unconverted rows in a separate table

A later migration would convert them once the configuration was repaired.
Rejected: no reported case, and the schema gains a table whose only purpose is
a repair path nobody has needed.

## Risks

- Unconverted tasks stay in the legacy shape for good: the query returns no
  input entries for them and shows the legacy metadata view only. This is the
  one row-level exception to a single stored form, and the spec states it.
- The integration tests currently assert conversion of every status and run the
  failure matrix against a finished task. Both move: conversion of a finished
  task that succeeds is still asserted, and the failure matrix needs a task
  that has not started.

## Follow-through

- Update the unreleased change note: the breaking entry states the current
  fail-fast behaviour and must state the narrowed one.
- Update spec boundaries for `db-migrations` and `task-input-snapshots` per
  ADR-0016: behaviour, one scenario per outcome, no implementation detail.
- No ADR. The failure rule belongs to one migration; ADR-0019 §5 states the
  conversion intent and the stopped-runner precondition and does not prescribe
  the failure behaviour.
