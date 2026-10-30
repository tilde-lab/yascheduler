# Design: task input snapshots

## Context

This implements part 1 of the [ADR-0019 plan](../../../docs/plans/ADR-0019-implementation-plan.md).
The normalized input model, submission snapshot, and SFTP transport are already
accepted by [ADR-0019](../../../docs/decisions/ADR-0019-recursive-task-file-transfers.md).
This design specifies the implementation seams; it does not introduce a new
transport, storage service, engine snapshot, or lifecycle architecture.

Current coupling points:

| Existing owner | Current behavior to replace or extend |
| --- | --- |
| [CLI submit](../../../yascheduler/entrypoints/cli/submit.py) | Reads each configured name from `Path.cwd()`; stores text or untagged base64 in metadata |
| [Client](../../../yascheduler/entrypoints/client.py), [DI](../../../yascheduler/entrypoints/di.py) | Forward raw metadata; query flattens `extra` into metadata |
| [Submit use case](../../../yascheduler/application/submit_task.py), [Engine](../../../yascheduler/domain/engine.py) | Validate file-name key presence rather than a selected snapshot |
| [Task repository](../../../yascheduler/infra/persistence/postgres.py) | Persists and hydrates task rows without owned input entries |
| [Migration base](../../../yascheduler/infra/persistence/migration_base.py), [runner](../../../yascheduler/infra/persistence/postgres_migrations.py), [init](../../../yascheduler/entrypoints/cli/init.py) | Supply database config only; engine input declarations are unavailable to a migration |
| [Task deployer](../../../yascheduler/infra/ssh/operations/deployment.py) | Reads live engine input names and `task.extra`; only `fort.9` is decoded from base64 |

## Goals / Non-Goals

Implement one complete input path from capture to SFTP, with no legacy input
branch in persistence or deployment. Use existing UoW, repository, session,
error, and layer-facade conventions. Add no dependency.

Do not change output resolution or cleanup, snapshot spawn/check/platform
configuration, add a public status, store output bytes, or introduce a generic
filesystem framework. Keep input capture roots distinct from `local_folder`,
which remains the result destination. The CLI happens to use its working
directory for both today; the Python API must not infer a source root from it.

## Contracts and implementation boundaries

Write module/public contracts and stubs first, then implement inside them.
Names below are the planned implementation surface, not existing APIs.

| Owner | Contract | Boundary |
| --- | --- | --- |
| New domain input module | Frozen `TaskInputFile(path, content: bytes)` and `TaskInputDirectory(path)` values; `TaskInputEntry` union; snapshot validation | No filesystem, SQL, or SSH imports; validate kinds, safe paths, unique paths, and ancestor consistency |
| Same domain module | Pure path validation and component-aware selection matching over relative paths and entry kinds | Shared semantic rules, not a generic local/remote traversal interface |
| New infra local-input module | `capture_input_snapshot(root, selections)` returns a complete immutable tuple of entries or fails with selection/path context | Owns local traversal and byte reads; reusable from a CLI or Python caller; no task write |
| Entrypoints submission normalization | Convert legacy payload or explicit entries to the one domain snapshot and separate unrelated metadata | Legacy encoding and ambiguity handling end here; CLI passes captured typed entries |
| Application submit | Validate known engine and snapshot coverage, construct task, commit task and entries in one UoW | Receives typed entries, not live paths or legacy file dictionaries |
| Existing task repository | Insert and hydrate owned entries along with task; preserve entries on lifecycle save | Reuse task ownership and existing UoW, not a separately committed input repository |
| Existing stateless deployer | Reconstruct directories and file bytes from task snapshot, then spawn | Uses session SFTP primitives; ignores live engine input declarations when choosing upload paths |

The task's immutable input tuple is part of `NewTask` and `Task` and survives
all transition constructors and repository hydration. Export cross-layer
symbols only through existing facades and module `__all__` declarations.
Replace key-presence input validation with one snapshot-validation rule;
do not maintain independent checks in the engine and submit use case.

### Selection and capture

Use task-relative `/`-separated paths independent of the local or remote OS.
Validate before normalizing: reject absolute POSIX, Windows drive/UNC roots,
`..` components, and backslashes that could become remote separators. Normalize
redundant separators and `.` components; the root itself is implicit and has
no stored entry. A root-directory selection captures its descendants. Only
ordinary files and directories are supported.

Use component matching with stdlib `fnmatchcase` for `*`, `?`, and bracket
classes; handle a component equal to `**` separately. Do not use a whole-path
`fnmatch`, whose `*` can cross separators, or rely on platform-dependent glob
hidden-name defaults. Traverse only branches needed by selections, checking
entry types with non-following metadata operations. A matched directory
expands to its complete subtree. Check each selection for a match before
merging; materialize required parent directory entries and deduplicate by path.
A wildcard-matched symbolic link is an error, not an unmatched path.

The local walker reads files as bytes without text decoding or newline
normalization. Explicit API entries represent the caller's supplied tree;
validate selection coverage and keep only selected entries plus their parents.
The normalizer materializes missing parent directories, rejects duplicate
explicit paths and file/ancestor conflicts, and copies the input sequence to
an immutable tuple. It does not fetch missing filesystem data.

Capture is not a filesystem transaction: callers must keep inputs stable
until submission returns. Detectable read/type changes abort capture. Do not
promise a simultaneous snapshot across files being modified by another process.
Use non-following opens where available and recheck traversed components
before reads/writes. Avoid recursive convenience transfers that follow links.

### Public Python boundary

Keep positional arguments and current webhook options on both public submit
methods. Add keyword-only `input_entries=None` accepting a sequence of the
exported typed entry values. `None` selects legacy normalization; an explicit
empty sequence is validated as an empty snapshot, not a fallback to metadata.
CLI capture calls the same normalization/submission boundary with typed entries.
Python callers can construct entries directly or use the exported capture
helper; no additional submit-from-directory method is needed.

Legacy mode first separates the existing typed metadata fields (engine,
folders, webhook settings, and error) from arbitrary payload, then uses the
engine input declarations to identify file keys in that payload. A glob must
not consume typed metadata fields. Literal file declarations use exact keys. For directory/glob declarations,
match safe file-path keys using the shared language and derive parent entries;
legacy mode cannot express a genuinely empty directory. Required selections
still apply. Other metadata keys remain untouched. Explicit entries plus keys
which would be classified as legacy inputs are ambiguous and are rejected.

Supported legacy values are strings. Encode ordinary strings as UTF-8, using
the old lenient base64 normalization for the exact `fort.9` convention only.
Keep that compatibility decoder at the boundary/migration conversion seam;
remove it from deployment. Never guess that another string is base64. Non-string
legacy payloads fail with an actionable diagnostic; arbitrary `str(object)`
coercion is not a file encoding contract.

Queries add top-level `input_entries` while `metadata` keeps a legacy view of
the task's input files: one filename/value key per file entry, derived from
the immutable snapshot with the legacy CLI encoding (UTF-8 text as a plain
string, otherwise canonical padded base64) — never a second stored copy.
Directories have no legacy representation and appear only in `input_entries`.
Typed metadata fields are never overwritten by input paths, and transitional
`extra` copies keep precedence over derived values while they exist. Use the
exact JSON-safe projection in the
[public-client delta](specs/package-facades/spec.md): directory records have
no content field; file bytes use canonical padded base64. Keep unrelated
metadata, node projection, identity, status representation, and CLI status
renderers unchanged. Document `input_entries` with before/after examples as
an additive change; legacy filename/value consumers continue to work.

### Persistence

Add `yascheduler_task_inputs` with this proposed shape:

| Column | Constraint |
| --- | --- |
| `task_id` | Task foreign key, delete cascade; part of primary key |
| `path` | Canonical relative text path; part of primary key |
| `kind` | Text constrained to `file` or `directory` |
| `content` | `BYTEA`, non-null for files and null for directories |

An empty byte string represents an empty file, never a directory. Validate
path/tree invariants before insertion; enforce key, ownership, and kind/content
invariants in SQL. Task insertion and owned-entry insertion share the current
transaction and event dispatch happens after commit. A failed entry write
rolls back task creation. Lifecycle save updates task state without rewriting
immutable input payloads. Batch hydrate entries for list queries rather than
issuing a query per input file. Preserve existing identity/status guards.

Add a forward migration with the next free prefix (currently `014`, not
reserved), update the latest prefix and DDL in the
[schema snapshot](../../../yascheduler/infra/persistence/sql/schema.sql),
and keep the migration's statements inside the migration module so the file
is one frozen artifact: a migration is never re-read from `sql/` and cannot
drift into a shared runtime query. Fresh bootstrap must create the table even
when migrations are skipped as already current.

### Migration configuration and conversion

Thread an optional engine-name-to-input-declarations mapping from initialization
into `apply_migrations` and the base migration context. Use domain/plain values;
infra must not import the entrypoints config parser. Existing migration classes
continue to receive their existing constructor dependencies. Store the new
mapping on the base context after construction, or extend the base constructor
compatibly without requiring changes to historical migrations. The input
migration fails clearly when required declarations are absent; fresh bootstrap
and previously applied migrations do not require conversion context.

Convert all stored tasks, regardless of status, using their stored engine
identity and supplied declarations. Resolve input keys against the legacy
payload tree, not the current local filesystem. Reuse the specified legacy
encoding meaning; remove only successfully converted file keys. Preserve all
other columns. Missing engines, missing required entries, unsafe paths,
non-string payloads, and decoding failures stop conversion with task/path
context. Operators must restore the appropriate declarations or repair known
legacy data before retrying, never silently skip such tasks.

Run DDL, input conversion, metadata-key removal, and tracker recording in the
runner's open transaction without intermediate commits. Schema bootstrap may
already have created the empty table, so migration DDL must tolerate it.
A conversion failure preserves legacy payloads and leaves the migration pending.
Regression tests must cover both fresh and upgrade paths.

### SFTP reconstruction and failures

Validate snapshot paths again at the adapter trust boundary. Resolve the remote
task root through the existing session path rules, then join canonical relative
components using the remote platform, not the local OS. Inspect destination
ancestors without following links, create directories parent-first, and write
bytes in binary mode. Existing ordinary files at selected file paths may be
replaced; file/directory conflicts and symbolic links fail. Input attributes
are not copied. Spawn occurs only after all entries have been uploaded.

Keep the existing session occupy/release and allocation rollback paths,
including cancellation behavior. Do not add input retries, remote cleanup,
or a new task failure policy in this change. A failed upload may leave partial
remote inputs as today, but must not spawn a calculation.

Use module-local stdlib loggers. Emit structured boundary records for capture,
normalization, persistence, migration, upload, and failure with task identity
when available, selection/path, entry count, and failure classification.
Never log file bodies, base64 payloads, or credentials. Tests assert records
where trajectory matters; marker names belong in code, not behavior specs.

## Risks / Trade-offs

- Untagged historical binary values outside `fort.9` are unrecoverable from
  metadata alone. Preserve their old text deployment meaning; recovery requires
  known-source repair or resubmission.
- Missing engine configuration can block even historical completed-task
  conversion. Fail before conversion commit; inventory engines during downtime.
- Snapshots still occupy database and process memory. Keep existing in-database
  ownership; streaming/blob storage and size limits are outside this proposal.
- SFTP type checks are not atomic against a hostile remote filesystem writer.
  Keep the task root scheduler-controlled; do not claim race-proof isolation
  from processes that can mutate that root concurrently.
- Query shape stays backward compatible: `input_entries` is additive and the
  legacy filename/value view in `metadata` is derived from the immutable
  snapshot (single storage source), so no dual input representation is
  stored. Update examples and client documentation in the same
  implementation.

## Migration integration

Apply the forward migration through the existing `yainit --schema` flow with
configured engine declarations. Existing migration tracking, retry behavior,
and daemon startup compatibility checks remain unchanged. ADR-0019's offline
migration constraint applies; no separate upgrade procedure is introduced.

## Acceptance evidence

| Boundary | Required evidence |
| --- | --- |
| Pure rules and local capture | Selection matrix, path confinement, hidden/empty trees, exact binary bytes, missing/unreadable inputs, duplicate/conflict validation |
| Public CLI/client | Existing calls still submit; explicit entries accepted; ambiguous payload rejected; exact query projection; CLI CWD and stdout/exit contracts |
| PostgreSQL | Atomic insert rollback, binary round-trip, lifecycle preservation, ownership constraints, fresh/legacy/current migration and failed-conversion recovery |
| SFTP | Remote tree content and directory assertions; unsafe destination rejected; injected upload failure prevents spawn and returns session to FREE |
| End-to-end | Submit mixed tree, delete source, recreate dependencies/reload from DB, deploy through real SSH, compare bytes and paths before a harmless test spawn |

Use the existing unit/integration/e2e suites, with testcontainers for PostgreSQL
and SSH. No real cloud, live production database, or ABACUS executable is needed.
