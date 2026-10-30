## Purpose

Define the forward-only database migration system: the schema bootstrap,
the migration runner, the Python migration base class, the migration
tracker, the migrations directory file format, and the migration edit
procedure. `yainit` invokes the bootstrap, which applies the canonical
schema snapshot in one transactional apply, then the runner brings
legacy and intermediate databases forward to that snapshot.

## Requirements

### Requirement: Schema bootstrap is idempotent

The system SHALL apply the canonical schema snapshot in a single
transaction. The snapshot SHALL guard every object creation so that
re-applying it on an initialized database leaves the schema unchanged.
The snapshot SHALL create the migration tracker: seeded to the latest
migration prefix on a fresh database (no tables present), so the runner
treats it as fully migrated; empty on a legacy database (tables present,
no tracker), so the runner applies every migration.

#### Scenario: bootstrap is idempotent across database states

- **WHEN** the schema bootstrap is applied to a database
- **THEN** a fresh database receives every table, type, and constraint
  plus a tracker seeded to the latest prefix, an initialized database is
  left unchanged, and a legacy database with tables but no tracker gets an
  empty tracker

### Requirement: Migration runner applies pending migrations sequentially

The system SHALL apply pending migrations in sorted filename order,
recording each in the migration tracker after success. The system is
forward-only and the tracker is idempotent: once a prefix is recorded, the
runner SHALL never delete the record, reverse the migration, or apply the
same prefix twice. A migration is pending when its filename prefix token
is greater than the last recorded id, or when the tracker is empty.

#### Scenario: the runner applies only pending migrations

- **WHEN** the migration runner is invoked against a database
- **THEN** every file whose prefix token is greater than the last recorded
  id (or every file when the tracker is empty) is applied in sorted
  filename order and recorded in the tracker exactly once

### Requirement: SQL migrations run in their own transaction

The system SHALL apply each SQL migration file as a multi-statement string
inside a single transaction and insert its tracker row on the same commit.
On any error the transaction SHALL roll back and the error SHALL re-raise;
no tracker row SHALL be inserted for the failed migration.

#### Scenario: SQL migrations commit atomically or roll back

- **WHEN** an SQL migration file is applied
- **THEN** its SQL text and its tracker row commit together in one
  transaction, and on error the transaction rolls back, the error
  re-raises, and no tracker row is inserted

### Requirement: Python migrations receive dependencies and control their own transaction

The system SHALL load each Python migration as a subclass of the migration
base class and inject the database configuration, the connection, and a
logger. The migration body SHALL control its own transaction lifecycle,
including the ability to commit and reopen a transaction mid-run for
operations that cannot run inside one. Migrations are NOT required to be
idempotent; the tracker guards against re-application.

#### Scenario: a Python migration receives deps and owns its transaction

- **WHEN** a Python migration file is applied
- **THEN** its migration class receives the database configuration, the
  connection, and a logger, and the migration body commits and reopens its
  own transactions as needed

### Requirement: Exactly one migration subclass per Python file

Each Python migration file SHALL define exactly one subclass of the
migration base class. A file that defines zero or more than one SHALL
raise an error that identifies the file.

#### Scenario: exactly one subclass per file

- **WHEN** the runner loads a Python migration file
- **THEN** the single subclass is instantiated and run, or an error
  identifying the file is raised when the file defines zero or more than
  one subclass

### Requirement: Python migration tracker recording

After a Python migration body returns, the runner SHALL record the
migration in the tracker and commit, whether the migration left the
runner's transaction open or closed and reopened it. A transient database
error on that commit SHALL retry once in a fresh transaction. Any other
error SHALL roll back, re-raise, and insert no tracker row.

#### Scenario: the tracker records atomically and retries transient errors

- **WHEN** a Python migration body returns
- **THEN** a tracker row is recorded on commit, a transient database error
  on that commit retries once in a fresh transaction, and any other error
  rolls back, re-raises, and inserts no tracker row

### Requirement: Migrations directory and file format

Migration filenames SHALL match the pattern `{prefix}_{rest}.{sql,py}`,
where the prefix token is the part before the first underscore, is unique
across every migration file, and determines application order by string
sort on the filename. Prefix uniqueness is an authoring constraint; the
runner SHALL NOT detect duplicate prefixes at apply time.

#### Scenario: duplicate prefix tokens are not rejected at apply time

- **WHEN** two migration files share the same prefix token
- **THEN** the runner applies the pending files in sorted order without
  reporting the collision; uniqueness is enforced off the apply path

### Requirement: Migration edit procedure

Adding a migration SHALL perform three edits: create the migration file in
the migrations directory; set the snapshot's last-migration constant to the
new prefix token; and, when the migration changes the schema (DDL), update
the snapshot's table and type definitions to match. A data-only migration
needs no snapshot DDL edit.

#### Scenario: a new migration updates file, constant, and snapshot

- **WHEN** a developer adds a migration
- **THEN** a migration file is created, the snapshot's last-migration
  constant is set to the new prefix token, and the snapshot's table and
  type definitions are updated only when the migration changes the schema

### Requirement: Legacy task inputs migrate without metadata loss

The forward migration SHALL identify legacy file payloads with the input
declarations of each stored task's engine, convert those payloads into
task-owned input entries where the declarations allow it, and remove only
converted file keys from unrelated metadata. Conversion SHALL use stored
payloads rather than the original filesystem. Task identities, lifecycle state,
node bindings, result locations, and unrelated metadata SHALL be preserved for
every task.

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

### Requirement: A task that has not started stops the upgrade

The upgrade SHALL stop and leave its conversion and its tracker entry
uncommitted when a task that has not started cannot be converted: its engine
declaration is unavailable, a declared input is absent from its payload, or its
payload cannot be converted safely. The stop SHALL name the task, its engine,
the failing input when known, and the input keys that task carries. Fresh
bootstrap and upgraded databases SHALL expose the same input storage contract.

#### Scenario: unsafe conversion stops the upgrade

- **WHEN** the upgrade meets a task that has not started whose engine declaration is missing or whose declared payload cannot be converted
- **THEN** the upgrade stops naming the task, its engine, the failing input when known, and the input keys that task carries, and no conversion or tracker entry is committed

### Requirement: The upgrade requires engine declarations

The upgrade SHALL stop before converting any stored task when it receives no
engine declaration context, and SHALL leave its tracker entry uncommitted. An
empty engine declaration mapping is a valid context.

#### Scenario: the upgrade refuses to run without declarations

- **WHEN** the upgrade runs without an engine declaration context against a database that holds stored tasks
- **THEN** it stops before converting any task and no tracker entry is committed

### Requirement: A task that has started is left unconverted

The upgrade SHALL continue past a task that has started and cannot be
converted. That task SHALL keep its stored payload and metadata, and SHALL gain
no input entries. The upgrade SHALL report each engine left with unconverted
tasks and the number of tasks that engine left.

#### Scenario: a task that has started is left unconverted

- **WHEN** the upgrade meets a task that has started and cannot be converted
- **THEN** the upgrade continues, that task keeps its stored payload and metadata and gains no input entries, and each engine left with unconverted tasks is reported with its count
