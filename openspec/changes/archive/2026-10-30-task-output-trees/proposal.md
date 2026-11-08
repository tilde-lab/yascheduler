# Proposal: recursive task output retrieval

## Why

[ADR-0019 §3](../../../docs/decisions/ADR-0019-recursive-task-file-transfers.md)
requires retrieved outputs to keep their task-relative tree, so the ABACUS
parser receives `OUT.aiida/nested/result.dat` unchanged
(issue #183). Today `output_files` is treated as a list of individual remote
files and each `SFTPClient.get` call copies one file into a single local
directory: a declared directory or glob is fetched as one literal path,
nested results are flattened, and empty or hidden entries disappear.
Directories and globs cannot be declared at all, and
[docs/CONFIG.md](../../../docs/CONFIG.md) says so explicitly.

This is the output-retrieval part of ADR-0019. Parts 1 and 2 are delivered:
submission-time input snapshots with recursive upload, and the retrieval
lifecycle with mixed-error precedence and post-finalisation cleanup. Neither
closes issue #183 nor claims full ADR support.

## What Changes

- `output_files` entries become selections in the same task-relative
  language as inputs: an explicit file, an explicit directory with its
  complete subtree, or a glob. Overlapping selections transfer each path
  once.
- Retrieval resolves every selection against the remote task root without
  following symbolic links, and writes the selected files to their
  task-relative paths under the local result root. Nested layout, hidden
  entries, arbitrary bytes, and empty directories are preserved.
- A missing explicit path or a pattern matching nothing is a permanent
  retrieval error for that selection, reported alongside the other
  selections' outcomes instead of aborting them.
- The local result root is merged, not cleared: selected ordinary files are
  replaced, unrelated local entries are retained, and a symbolic link or a
  file/directory type conflict at a destination path is a permanent error
  for it. Output attribute preservation is unchanged.
- Resolved directory entries are created locally; directory creation and
  traversal failures enter the same per-path transient/permanent lists as
  file transfers, so part 2's defer/finalise lifecycle is unchanged.
- The `output_files` documentation describes both selection kinds and the
  supported glob language.

No archive transport, remote archiver, parser change, database column,
migration, task state, public status, cloud-node retention policy, or new
INI key is introduced.

## Capabilities

### Modified Capabilities

- `task-file-selection`: Output retrieval adopts the shared selection
  language over the remote task root, including required selections and
  permanent per-selection errors.
- `use-cases`: Consume retrieves selected output trees into the local result
  root and classifies per-path failures without changing its
  defer/finalise rules.
- `ssh-infrastructure`: The download operation resolves and transfers
  selected trees with link rejection, destination checks, per-file retry
  isolation, and path-level error classification.

## Impact

Touches the consume use case, the SSH output-download operation, a remote
selection resolver in the same operation layer,
[docs/CONFIG.md](../../../docs/CONFIG.md), and tests (unit, SFTP
integration, PostgreSQL/SSH end-to-end). The INI shape and existing
single-file `output_files` lists keep working; no new dependency, and
Python >=3.9 plus pip/uv compatibility still apply.

`yastatus` verbose tailing keeps using the first `output_files` entry
literally, so a directory or glob declaration cannot be tailed; that
limitation predates this change and is documented rather than redesigned
here. Closing issue #183 and marking ADR-0019 implemented happen only after
the acceptance gate passes.

Implementation progress and completion gates are tracked in
[tasks.md](tasks.md); the canonical specs remain unchanged until sync.
