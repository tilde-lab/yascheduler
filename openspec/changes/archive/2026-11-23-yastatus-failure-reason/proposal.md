# Proposal: print the task failure reason in yastatus

## Why

A failed task keeps its reason: `Task.fail()`, `Task.reject()`, and
`Task.abandon()` write it into the DONE state, and retrieval records every
failed output download. The Python client returns it as `metadata["error"]`.
`yastatus` drops it. `--info` and `--json` print `DONE` with no reason, so an
operator cannot tell a failed task from a successful one without a database
query.

## What Changes

- `--json`: each task object carries an `error` field, the failure reason or
  `null`.
- `-i`: a task with a failure reason ends its line with an `error` field.
  Backslash, tab, carriage return, and newline are escaped in it, so the
  reason cannot break the one-line, tab-separated shape.
- Unchanged: the default renderer. The AiiDA scheduler plugin parses exactly
  two whitespace-separated fields per line.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cli`: the `-i` and `--json` renderers of `yastatus` print the failure
  reason of a task that has one.

## Impact

Touches the two renderers of `yastatus` and their tests. `--json` gains one
field; existing consumers that read named keys keep working. An `-i` line
without a failure reason is unchanged byte for byte.

Excluded: the webhook payload has no way to express a failure outcome, and
the AiiDA plugin cannot report one. Each is a change of its own.
