# Proposal: one flat-payload rule for submission and the upgrade

## Why

The rule "which keys of a stored metadata payload are engine inputs" existed
twice: at the submission boundary and in migration 014. The copies diverged. For
a literal declaration the boundary claimed that one key as a file and left every
key below it in metadata, while the migration claimed the whole subtree. The
converted snapshot then held one key as a file and as a parent at once, the
migration aborted with `file entry cannot contain child` on a payload the
boundary accepted, and `yainit` stopped with the database unchanged.

The duplication is the cause: two copies of one claim rule and one value
encoding, only one ever exercised against data the previous release accepted.

The data settles the reading. The pre-snapshot scheduler stored a flat map of
names to text and wrote one file per declared name; `input_files` held file
names, with no globs and no directories. A key containing `/` was a name, so
reading directories out of it invented structure no stored payload had.

## What Changes

- The claim rule and the recorded value encoding exist once, in the
  application layer, and both the submission boundary and migration 014 call
  that one function. Nothing legacy enters the domain.
- A legacy payload is read as flat: a literal declaration claims exactly the
  key of its name; a glob claims keys as file paths, where `*` never crosses
  `/` and a trailing `**` claims every key below its prefix. No key implies a
  directory and no key brings another key along.
- Migration 014 loses its private conversion entirely (`_convert_payload`,
  `_legacy_bytes`); it calls the shared function.
- The submission normalizer loses its claim, key-check, and encoding helpers;
  its ambiguity check reuses the shared selection predicate instead of a
  second copy of the matching rule.
- Unchanged: the snapshot contract, storage, deployment, the migration failure
  policy of the previous change, the public API, and the configuration format.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `db-migrations`: conversion reads a stored payload as a flat name-to-text
  map, so a key is never expanded into its prefix's children and a row the
  boundary accepted cannot stop the upgrade.
- `task-input-snapshots`: the legacy submission rule is stated — one flat key
  per declaration, one text value per key, no inferred directories.

## Impact

Removes more code than it adds: the migration's conversion and encoder, the
normalizer's claim, key-check, and encoder, and the migration's conversion
tests, replaced by one application module and one test module. Touches
migration 014, the input normalizer, the application facade, their unit and
integration tests, and the two specs above. No schema, deployment, or query
changes. The module sits in the application layer: the project already places
code as far from the domain as the import contract allows, and a retired input
format is the last thing the core should carry.

Legacy submitters keep working. A configuration that declares a literal
directory name now needs that key in the payload, or a glob (`basis/**`), or
typed `input_entries`; the first was the only shape the previous scheduler ever
accepted, and the other two already exist.
