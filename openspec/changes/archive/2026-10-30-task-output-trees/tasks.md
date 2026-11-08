# Tasks

These are implementation tasks, not claims of work completed by this
proposal. Scope: part 3 of
[ADR-0019](../../../docs/decisions/ADR-0019-recursive-task-file-transfers.md).

## 1. Remote selection resolution

- [x] 1.1 Add a resolver beside the SSH download operation that walks the
  remote task root for one validated selection: list directories, apply
  `selection_matches` from the domain module component-wise, expand a
  matched directory to its complete subtree, reject symbolic links and
  unsupported entry types with the offending path, and return task-relative
  file and directory entries plus a "matched nothing" outcome for that
  selection. Unit-test the resolver against a fake listing (paths and
  `lstat` types as data): explicit file, directory subtree with hidden and
  empty entries, `**` patterns, overlapping selections deduplicated by
  path, a remote link rejected, an unmatched pattern reported without
  raising.

## 2. Download and consume

- [x] 2.1 Rework `download_outputs` to take the remote task root plus raw
  output selections: resolve them in one pass, create destination
  directories locally, transfer each resolved file to its exact local path
  with `preserve=True`, and return the existing four-tuple with directory
  paths included in the error lists. Keep no remote deletion, the fresh
  channel per file, the retry utility, and the `SFTPRetryExc`
  transient/other-permanent classification. Unit-test directory creation,
  the explicit-destination argument passed to `sftp.get`, and that a
  resolution failure becomes one session-level transient error.
- [x] 2.2 In `consume_task`, stop pre-joining declarations with the remote
  folder and pass the selections instead; keep defer/finalise/cleanup
  decisions byte-for-byte, extend the failure text with selections that
  resolved to nothing, and emit a structured per-entry outcome record at
  the download block boundary. Unit-test tree outcomes: nested success,
  unmatched pattern as permanent, directory creation failure as transient,
  and mixed permanent/transient still deferring.

## 3. SFTP integration

- [x] 3.1 Add `tests/integration/test_output_retrieval.py` using the Docker
  SSH fixture pattern from `test_task_input_deployment.py`: a remote tree
  with nested text and binary files, hidden entries, and empty directories
  retrieved into a `tmp_path` result root with exact layout and bytes;
  overlapping selections; remote symbolic link and unsupported entry type
  reported as permanent; unmatched glob permanent while other selections
  succeed; local destination link and file/directory conflict rejected
  without writing through them; a second pass replacing selected files
  while retaining unrelated local entries.

## 4. End-to-end and documentation

- [x] 4.1 Add `tests/e2e/test_output_trees.py` on the PostgreSQL/SSH
  harness: an engine whose spawn writes `OUT.aiida/nested/result.dat`,
  declared as a directory plus an overlapping glob, lands that tree under
  the task result folder; the remote directory is removed only after the
  terminal transition; a mixed transient/permanent pass defers and a later
  clean pass finalises. Assert the parser-visible layout directly; no
  ABACUS binary, parser modification, or cloud account.
- [x] 4.2 Rewrite the `output_files` section of
  [docs/CONFIG.md](../../../docs/CONFIG.md) to describe explicit files,
  directories, and globs with the shared glob language, remove the
  "individual files only" paragraph, add a directory-and-glob example, and
  state the `yastatus` tailing limitation for non-file declarations.

## 5. Verification

- [x] 5.1 Run `uv run pytest -m unit`, `uv run pytest -m integration`, and
  `uv run pytest -m e2e`; all must pass.
- [x] 5.2 Run `uv run zuban check`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `uv run lint-imports`; verify Python
  >=3.9 syntax, no new dependencies, and no package version edit.
- [x] 5.3 Run `openspec validate --all --json`; review the diff against all
  three capability deltas and confirm no persistence, public-status,
  query-shape, cloud-policy, or INI-shape change slipped in.
- [x] 5.4 Sync/archive the change only after review and acceptance; rerun
  `openspec validate --all --json` after sync/archive.

## 6. Closure

- [x] 6.1 After the acceptance gate passes, record completion on ADR-0019
  without changing its architectural content.
- [ ] 6.2 Close issue #183 only with explicit operator approval, because it
  is an external action.
