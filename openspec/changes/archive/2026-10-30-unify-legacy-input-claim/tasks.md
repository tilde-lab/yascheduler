## 1. Shared conversion

- [x] 1.1 Add `yascheduler/application/legacy_inputs.py` (application layer,
  farthest from the domain the import contract allows): the flat claim rule,
  the recorded value encoder, and one entry point returning the snapshot and
  the leftover payload; export it from the application facade.
- [x] 1.2 Have the submission normalizer call it and delete its claim,
  key-check, and encoding helpers together with the ambiguity check's second
  copy of the matching rule.

## 2. Migration

- [x] 2.1 Replace migration 014's private conversion with the shared call and
  delete `_convert_payload` and `_legacy_bytes`.
- [x] 2.2 Update the migration module contract: the dependency is the shared
  conversion, not a private rule.

## 3. Specifications

- [x] 3.1 Apply the `db-migrations` delta.
- [x] 3.2 Apply the `task-input-snapshots` delta.
- [x] 3.3 Note the legacy submission rule change in the unreleased change note.

## 4. Tests

- [x] 4.1 Move the conversion and encoding tests to
  `tests/unit/test_legacy_inputs.py`, including the case where a declared key
  also has children and the case where only children exist.
- [x] 4.2 Keep migration 014's unit tests for its failure policy; point the
  unexpected-failure seam at the shared function.
- [x] 4.3 Update the integration fixtures whose declarations relied on inferred
  directories, and keep their expected entries unchanged.
- [x] 4.4 Run `uv run pytest -m unit`, `uv run pytest -m integration`; after the module moved to the application layer, the affected files only.
- [x] 4.5 Run `uv run zuban check`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run lint-imports`.
- [x] 4.6 `openspec validate --all --json`.
