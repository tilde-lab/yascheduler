# Tasks

These are implementation tasks, not claims of work completed by this proposal.
Scope: part 1 of the [ADR-0019 plan](../../../docs/plans/ADR-0019-implementation-plan.md).

## 1. Input contracts and selection

- [x] 1.1 Add domain module and public contracts/stubs for file entries, directory entries, path validation, and snapshot selection; update task record contracts and layer exports. Verify dependency direction and review ownership against the design before implementing bodies.
- [x] 1.2 Implement the pure shared selection rules and snapshot validation. Unit-test explicit files/directories, `*`, `?`, classes and escaped metacharacters, zero-level and nested `**`, hidden entries, overlap deduplication, empty directories, missing selections, absolute/drive/UNC/parent paths, and entry/ancestor conflicts.
- [x] 1.3 Implement local capture with non-following traversal and binary reads. Verify byte equality, parent creation, source read/disappearance failures, selected and dangling symlinks, and rejection of unsupported filesystem entries. Document that callers keep sources stable during capture.

## 2. Submission boundaries and public projection

- [x] 2.1 Add the optional typed-entry argument to synchronous/asynchronous client and DI submission; normalize legacy values once before the application use case. Verify existing positional/webhook calls, legacy UTF-8 and `fort.9`, explicit bytes, empty explicit input, unrelated metadata, mixed-source rejection, and the absence of filesystem fallback.
- [x] 2.2 Replace raw-metadata input validation in submission with typed snapshot validation. Verify missing engine/selection failure before task writes, snapshot coverage, copied immutable entries, and existing task identity/event behavior.
- [x] 2.3 Replace CLI text/base64 capture with the local snapshot path. Update CLI tests for trees and arbitrary bytes, working-directory versus script-directory behavior, runtime failure stderr/exit `1`, and unchanged bare task-id stdout.
- [x] 2.4 Implement the public query projection specified in `package-facades`. Test exact top-level keys, deterministic entry ordering, padded base64 file data, content-free directories, unchanged node/status fields, and unrelated metadata. Update client documentation and examples with before/after query shapes and typed submission usage.

## 3. Persistence and forward migration

- [x] 3.1 Add the task-owned input table, owned-entry SQL, repository insert/hydration, and lifecycle preservation. Use the existing UoW; verify with real PostgreSQL that binary and empty-directory snapshots round-trip, duplicate/orphan entries fail, entry failure rolls back task creation, and task deletion cascades only to its own inputs.
- [x] 3.2 Pass engine input declarations from initialization to the migration context without importing entrypoints from infra or breaking historical migrations. Verify existing migration tests and an input conversion with supplied and absent engine context.
- [x] 3.3 Add the next free forward migration and update the canonical schema DDL and latest-prefix constant. Test fresh bootstrap, legacy databases without a tracker, and current databases with TO_DO/RUNNING/DONE rows; compare resulting schema and converted bytes, metadata, identities, and node bindings.
- [x] 3.4 Test failed conversion and retry: unknown engine, missing input, unsafe path, malformed known encoding, and unsupported legacy value leave conversion/tracker uncommitted; repair fixtures and rerun successfully. Verify already applied migration is not repeated and non-`fort.9` base64-looking text remains text.

## 4. Stored-tree SFTP deployment

- [x] 4.1 Update deployer contracts, then implement parent-first directory creation and binary writes from stored entries. Remove the live-engine input list and metadata decoding from upload. Verify through real SSH that nested/hidden files, empty files, and empty directories retain exact paths and bytes.
- [x] 4.2 Enforce destination confinement, symbolic-link rejection, and file/directory conflict handling. Test remote platform path joining and fault injection: every upload failure or cancellation prevents spawn and retains the existing session/allocation rollback behavior.
- [x] 4.3 Add structured capture/normalization/persistence/migration/upload boundary logs using module-local stdlib loggers. Assert critical failure trajectory records and confirm logs contain no file contents or credentials. Update engine configuration documentation with input tree/glob examples and clearly label recursive outputs as a later change.

## 5. Integrated acceptance

- [x] 5.1 Add a real PostgreSQL/SSH end-to-end test: submit mixed text/binary/hidden/empty input tree, change then remove the source, reload through fresh dependencies, and deploy. Assert captured bytes and paths before a harmless spawn; change live input declarations to prove stored entries determine deployment.
- [x] 5.2 Run `uv run pytest -m unit`, `uv run pytest -m integration`, and `uv run pytest -m e2e`; all must pass, including unchanged single-file output and cleanup regressions.
- [x] 5.3 Run `uv run zuban check`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run lint-imports`; all must pass. Verify Python >=3.9 syntax, no new dependencies, and no package version edit.
- [x] 5.4 Run `openspec validate --all --json` and review the final diff against all eight capability deltas. Confirm only part 1 is claimed complete and no output/cloud policy change slipped into the implementation.

## Workflow follow-up

- Sync/archive the implemented change only after review and acceptance; rerun `openspec validate --all --json` after sync/archive.
- Prepare part 2's retrieval/cleanup proposal before implementing output recursion.
