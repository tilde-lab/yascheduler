# Design: recursive task output retrieval

## Context

This implements part 3 of
[ADR-0019](../../../docs/decisions/ADR-0019-recursive-task-file-transfers.md),
after the input-snapshot and retrieval-lifecycle parts were delivered. The
selection language, snapshot model, and defer/finalise rules already exist;
this change applies them to the output direction.

Current coupling points:

| Existing owner | Current behavior to replace or extend |
| --- | --- |
| [Consume use case](../../../yascheduler/application/consume_task.py) | `_prepare_store_folder` joins each declaration with the remote folder, so a declaration cannot stay a selection; one `mkdir` prepares a flat destination |
| [OutputDownloader](../../../yascheduler/infra/ssh/operations/download.py) | `download_outputs` calls `sftp.get(remote_path, local_dir, preserve=True)` per file, which flattens the tree; per-file exceptions become path/error tuples |
| [Task deployer](../../../yascheduler/infra/ssh/operations/deployment.py) | Already implements destination safety (lstat without following links, link and type-conflict rejection) for the remote side of uploads |
| [Local input capture](../../../yascheduler/infra/local_inputs.py) | Walks a local root by selection, expands matched directories, and rejects links and unsupported entry types |
| [docs/CONFIG.md](../../../docs/CONFIG.md) | States that `output_files` supports individual files only |

## Goals / Non-Goals

Goals: one selection language in both directions; parser-visible output
layout preserved; per-path error classification that leaves part 2's
lifecycle untouched; a merge that never clears the result root; no new
persistence, port method, or configuration key.

Non-goals: archive transport, a remote archiver, engine or parser changes,
cloud-node retention policy, transfer batching, `yastatus` tailing redesign,
and early validation of `output_files` at configuration parse time.

## Decisions

- **Selection matching stays in the domain; traversal lives in infra.**
  Resolution reuses `validate_selection` and `selection_matches` from
  [`domain/task_inputs.py`](../../../yascheduler/domain/task_inputs.py) and
  adds an SFTP walker beside the download operation. A second matcher or a
  remote `find`/shell glob would duplicate semantics and lose per-entry
  error recovery, so neither is used.
- **Resolution runs once per retrieval pass and yields task-relative
  entries.** Each configured selection is resolved independently, matched
  directories expand to their complete subtree, and the merged entry list is
  deduplicated by canonical path, so overlapping selections transfer each
  path once regardless of order. A selection that resolves to nothing is a
  permanent error naming that selection; the remaining selections are still
  attempted.
- **Every transfer names its exact destination file.** SFTP `get` treats an
  existing directory target as "copy into it" and re-appends the remote base
  name, so the downloader passes the full local file path and creates parent
  directories first. Relying on `get(..., recurse=True)` was rejected: it
  hides per-entry outcomes in one call, cannot apply per-entry retry
  isolation or requiredness, and would traverse paths outside the validated
  contract.
- **Classification and isolation are unchanged in kind.** Listing, metadata
  reads, and file transfers run under the existing retry utility;
  `SFTPRetryExc` is transient, other failures are permanent, and a
  session-level failure while resolving defers the pass as one transient
  error. Fresh SFTP channel per file transfer is retained; resolution uses
  one channel per pass. Reusing channels across files would change a
  documented isolation guarantee, so it waits for measurements showing
  session setup dominates large-tree retrieval.
- **The local result root is merged, never cleared.** No `rmtree` exists in
  retrieval. Before writing, each destination path is checked with a
  non-following `lstat`: a symbolic link or a file/directory type conflict
  is a permanent error for that path and nothing is written through it; a
  missing path is created; an ordinary file is replaced. Unrelated local
  entries survive. This mirrors the deployer's destination checks instead of
  inventing a second rule set.
- **Directories are entries, not side effects.** Matched empty directories
  are created locally, and a directory creation failure is reported as that
  directory's permanent or transient error, exactly like a file failure.
  Attribute preservation applies to files (`preserve=True`); directory
  modes, ownership, and timestamps are not transferred, matching the input
  contract.
- **Consume stops pre-joining paths.** It hands the downloader the remote
  task root and the engine's raw selections, keeping its own finalisation
  logic untouched. Configuration keeps its current shape and the "at least
  one output entry" rule; unsafe declarations are rejected during
  resolution as permanent errors naming the selection, because parse-time
  rejection would newly refuse configurations that load today and the ADR
  does not require it.

## Contracts and implementation boundaries

| Owner | Contract | Boundary |
| --- | --- | --- |
| New remote resolver in the SSH operation layer | Resolve validated selections under a remote task root into task-relative entries (files and directories), rejecting links and unsupported types, expanding matched directories, deduplicating paths, and returning per-selection errors as path/exception pairs | Owns SFTP listing and metadata only; reuses the domain matcher; never reads or logs file contents |
| [OutputDownloader](../../../yascheduler/infra/ssh/operations/download.py) | `download_outputs` receives the remote root plus selections, creates destination directories, transfers each resolved file to its explicit local path with `preserve=True`, and returns the existing `(local_folder, remote_folder, transient_errors, permanent_errors)` tuple with directory paths included | Keeps retry, classification, and SFTP specifics; no task state, no remote deletion |
| [Consume use case](../../../yascheduler/application/consume_task.py) | Passes selections instead of joined paths; defer/finalise/cleanup ordering and decisions unchanged; error text includes selections that resolved to nothing | Adds no lifecycle rule and no new outcome |

The public port surface is unchanged: retrieval still uses `open_sftp()` and
the existing retry utility. No database, domain value, or DI change is
planned.

## Testing strategy

- Unit: consume arcs for tree outcomes (nested success, unmatched pattern
  as permanent, directory creation failure as transient, mixed errors
  still defer), error-message formatting for selection-level failures, and
  local destination checks (link, type conflict, retained unrelated file)
  against `tmp_path` without a remote connection.
- Integration (Docker SSH, following
  `tests/integration/test_task_input_deployment.py`): build a remote tree
  with nested text and binary files, hidden entries, and empty directories;
  retrieve it and assert exact layout and bytes; overlapping selections;
  a remote symbolic link and an unsupported entry type are permanent; an
  unmatched glob is permanent while other selections succeed; a local
  destination link and a file/directory conflict are rejected without
  writing through them; a second pass replaces files and keeps unrelated
  local entries.
- End-to-end (PostgreSQL + SSH): an engine whose spawn produces
  `OUT.aiida/nested/result.dat` with a declared directory and an
  overlapping glob must land that tree under the task result folder, with
  the remote directory removed only after the terminal transition; a mixed
  transient/permanent pass defers and a later clean pass finalises. No
  ABACUS binary, parser change, or cloud account is involved.

## Risks

- Large trees multiply per-file channel setup cost. Retrieval logs entry
  counts and per-file outcomes so the cost is visible; batching is a
  follow-up decision, not part of this change.
- SFTP offers no `openat`-style confinement, so resolution checks each
  visited path with `lstat` on validated components instead of pinning a
  directory descriptor. The window is bounded to scheduler-owned remote
  task directories read by one daemon pass.
- A pass aborted by transient errors leaves a partly updated local tree;
  the next pass re-derives selections and replaces selected files, which is
  the accepted merge behavior, not a resumable state.
