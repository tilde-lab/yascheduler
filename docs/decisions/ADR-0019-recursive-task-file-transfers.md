# ADR-0019: Transfer task input and output trees recursively over SFTP

- **Status:** Accepted
- **Date:** 2026-10-28
- **Amended:** 2026-10-29 — §4 simplified during part-2 implementation
  review: no durable retrieval-progress state; finalisation now precedes
  remote cleanup, which makes the persisted retrieval-to-cleanup boundary
  and retained-diagnostics machinery unnecessary.
- **Implemented:** 2026-10-30 — all three parts delivered: input snapshots
  and recursive upload, the retrieval lifecycle with post-finalisation
  cleanup, and recursive output selection and retrieval.
- **Amended:** 2026-10-30 — the output direction is now task-owned:
  [ADR-0020](ADR-0020-task-owned-output-selections.md) records the accepted
  task's output selections. The selection language, tree layout, and
  classification in this ADR stand; §3's "no database storage" covers output
  content, not the selection record.
- **Amended:** 2026-10-30 — the input snapshot is read on demand: the task
  record no longer carries input contents, and deployment transfers the
  snapshot entry by entry. Ownership, storage, and the upload contract in this
  ADR stand; see
  [ADR-0021](ADR-0021-input-bytes-read-apart-from-task.md).
- **Supersedes:** [ADR-0013](ADR-0013-task-metadata-typed-columns-not-jsonb.md),
  only its placement of task input files in `extra`
- **Superseded by:**

## Context

[Issue #183](https://github.com/tilde-lab/yascheduler/issues/183) requires
recursive upload and download for ABACUS. Its inputs include directories
of pseudopotentials and basis sets. Its outputs must reach the AiiDA parser
inside `OUT.aiida`, with the internal tree intact. Downloading the same
files into a flat directory does not satisfy this contract.

The existing input path captures file contents at submission, stores them
in task metadata, and later writes them over SFTP. The output path retrieves
individual files and classifies failures for task finalisation. Supporting
trees therefore affects selection, input ownership, deployment, retrieval,
and cleanup, rather than just adding recursion to one SSH call.

The agreed scope includes glob selection in both directions. We need one
engine-independent tree contract, preservation of the submission-time input
snapshot, and explicit handling of partial retrieval. Alternatives include
native SFTP tree transfers, archive-based transport, and parser-specific
reconstruction. Input snapshots can remain opaque JSONB payloads or become
normalized task-owned entries; reading live source files at deployment would
change the existing snapshot guarantee.

## Decision

Support recursive, structure-preserving transfers through `input_files` and
`output_files`, using a common task-relative selection contract and the
existing SFTP transport. Resolve and persist input snapshots at submission,
reconstruct them before execution, and retrieve output trees before applying
the agreed retry, cleanup, and finalisation policy.

### 1. Shared selection and path contract

- Entries may be explicit files, explicit directories, or glob patterns.
  Selecting a directory includes its complete subtree, including hidden
  entries and empty directories. Overlapping selections transfer each path
  once.
- Both directions use the same glob semantics: `*`, `?`, and character
  classes match within a path component; `**` as a complete component matches
  zero or more directory levels. Globs include hidden names. Shell variable,
  home-directory, and brace expansion are not supported; character classes
  can express literal glob characters.
- Paths are relative to the task root. Absolute paths and `..` components
  are rejected. Source entries and destination traversal must stay within
  their respective task roots; symbolic links are rejected with an explicit
  error rather than followed or preserved.
- Every configured entry is required. A missing explicit path or a glob
  with no matches fails input submission or constitutes a permanent output
  retrieval error, respectively.
- Transfers preserve complete task-relative paths and file contents,
  including arbitrary binary data. They neither flatten directories nor
  introduce an extra enclosing directory. Existing single-file selections
  and mixed file/directory lists remain supported.

### 2. Submission snapshot and recursive upload

Resolve input selections against the submission directory and capture the
selected tree before accepting the task. Store the snapshot in a normalized
table of task-owned file and directory entries. Entries are identified by
relative paths, including paths below the top level; directories are
represented explicitly so empty directories survive the transfer.

Keep unrelated user metadata in `extra`. Normalize legacy submit payloads
at the public API boundary; persistence and deployment use one typed input
model. This partially supersedes ADR-0013's placement of input files in
JSONB, while retaining its decisions about typed task fields, unrelated
metadata, and lifecycle mutations.

Deployment reads the stored snapshot, creates its directory structure, and
uploads its file contents over SFTP before starting the calculation. It
must not rediscover inputs from the original source directory: subsequent
changes or deletion there do not affect an accepted task. Input filesystem
ownership, permissions, and timestamps are not transferred as part of this
contract.
An upload failure prevents execution and retains the existing deploy-failure
rollback behavior.

### 3. Recursive output retrieval

After execution, resolve output selections against the remote task root and
recreate the selected tree under the local result root. For example,
`OUT.aiida/nested/result.dat` remains `OUT.aiida/nested/result.dat` locally,
including when selected through a glob. The parser receives the expected
layout without changes to `aiida-abacus` or manual archive extraction.

Merge into the destination tree: replace selected ordinary files, retain
unrelated local files, and report file/directory type conflicts or symbolic
links in the destination path as errors. Preserve the existing output
attribute-preservation behavior. Outputs remain filesystem results; this
decision adds no database storage for their content. The selections themselves
are recorded on the task by
[ADR-0020](ADR-0020-task-owned-output-selections.md).

SFTP sessions and stateless transfer operations keep their existing roles
from [ADR-0006](ADR-0006-ssh-machine-architecture.md). Recursive transfer is
scheduler behavior shared across engines, not an ABACUS-specific adapter.

### 4. Partial results, retries, and cleanup

Attempt other selected outputs when an individual entry fails. Retain the
existing per-file retry isolation and local transient-retry budget. When
that budget is exhausted, unresolved transient failures defer the task to a
later daemon pass, without a new task-wide retry limit.

Transient failures defer finalisation even when permanent failures also
exist: the task stays pending retrieval and its remote results stay
untouched while transient failures remain. Each pass re-attempts every
selected output and re-derives the error lists from that pass alone; a pass
with no transient failures finalises the task on its own errors. There is no
durable retrieval-progress state: no retained diagnostics and no persisted
retrieval-to-cleanup boundary.

Remote cleanup runs strictly after the terminal transition is committed.
This ordering removes the partial-deletion crash window by construction: a
crash before finalisation changes nothing remotely, and after finalisation
the task is terminal and is never consumed again. Cleanup is one best-effort
attempt per finalisation (transient errors retried within the shared
time-based budget); a failure is logged and the remaining directory is left
to node teardown. These rules do not introduce a separate policy for
retaining cloud nodes.

### 5. Migration and compatibility

Convert existing stored inputs through a forward migration, using the
existing engine input declarations to identify file payloads and leaving
unrelated metadata intact. Legacy submitters remain supported through
boundary normalization; public query documentation must reflect typed input
entries rather than promise legacy file-value shapes.

Perform the upgrade with submitters and the daemon stopped. Old and new
daemons must not concurrently use the database during the transition;
remote calculations may continue while the scheduler is stopped.

## Alternatives Considered

### Archive each input or output tree

Rejected. Archives preserve hierarchy, but introduce packing, extraction,
and another failure boundary. They also group individually selectable and
retryable files into an opaque transfer unit. Existing SFTP infrastructure
can implement the required tree contract without a remote archiver.

### Reconstruct directories in the parser or an engine-specific wrapper

Rejected. This makes each engine responsible for repairing scheduler
output layout, does not solve recursive input upload, and cannot generally
recover relative paths lost by flattening.

### Keep structured input trees in JSONB

Rejected. Typed JSON records would still embed a scheduler-owned input model
inside arbitrary user metadata. Normalized entries give that model an
explicit persistence boundary while preserving snapshot ownership.

### Read live inputs only when a node becomes available

Rejected. Changes or deletion after submission could alter or invalidate a
queued calculation and require continued access to the source filesystem.

### Finalise immediately on any permanent retrieval error

Rejected. When transient errors also exist, immediate finalisation and
cleanup discard outputs that a later retry could still recover. We accept
longer task lifetimes to retrieve those outputs first.

### Persist a retrieval-to-cleanup boundary and retained diagnostics

Rejected during part-2 implementation review. Ordering finalisation before
remote deletion removes the partial-deletion crash window without any stored
state: before finalisation nothing is deleted, after finalisation the task
is never consumed again. Retained per-path diagnostics added a table, a
migration, and a second state machine to guard only against a permanent
error vanishing between passes — which either never happens for a finished
process or signals a misclassification that retention would wrongly punish.

## Consequences

- **Positive:** Both transfer directions preserve task-relative trees;
  ABACUS inputs and `OUT.aiida` outputs work through the shared scheduler
  contract without parser workarounds. Selection and path validation have
  consistent semantics across engines.
- **Positive:** Input snapshots remain independent of the source filesystem.
  Partial output retrieval retains recoverable data while transient errors
  remain, and cleanup failures are logged rather than silently ignored.
- **Negative / trade-offs:** Adoption requires a data migration, coordinated
  downtime, and documented query-payload changes. Input snapshots still
  consume database capacity; normalization does not provide external blob
  storage. Legacy encodings that were never recorded cannot be inferred
  reliably by migration.
- **Accepted risks:** Required patterns make absent optional-looking outputs
  an error. Symbolic links cannot be used to reference shared datasets.
  Persistent transient failures can defer completion indefinitely; this ADR
  does not guarantee cloud-node retention. After transient failures clear,
  cleanup deliberately discards outputs affected by permanent errors.
- **Follow-through:** The implementation proposal must update OpenSpec for
  selection, tree transfers, mixed-error precedence, and cleanup retries.
  Table columns, encodings, and traversal algorithms remain implementation
  details. Acceptance of this ADR records the intended architecture, not
  completion of that implementation.
