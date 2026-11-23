## MODIFIED Requirements

### Requirement: yastatus task query and renderers

`yastatus` SHALL query tasks and render them. The flags SHALL be the flags in
the renderer-flags scenario.

`-v`, `-i`, and `--json` SHALL be mutually exclusive. When none is given, the
default AiiDA-compatible renderer SHALL run. `-o` SHALL be valid only with `-v`;
`-o` without `-v` SHALL exit `2`.

The default renderer SHALL print one line per task: the task id, three spaces,
and the status name. `DONE` tasks SHALL appear only when the query is by id
(with `-j`).

The `-i` renderer SHALL print one tab-separated `key=value` line per task with
the keys `task_id`, `status`, `label`, and `node_id`. When the task carries a
failure reason, the line SHALL end with an `error` field holding that reason,
with backslash, tab, carriage return, and newline escaped so the reason stays
on one line.

#### Scenario: the default output is two columns

- **WHEN** `yastatus` is invoked against tasks `1` (RUNNING), `2` (TO_DO), and `3` (DONE)
- **THEN** the output has a line `1   RUNNING` and a line `2   TO_DO`, and no line for task `3`

#### Scenario: conflicting renderers exit 2

- **WHEN** `yastatus -v -i` is invoked
- **THEN** a usage error is printed to stderr and the process exits `2`

#### Scenario: convergence without view exits 2

- **WHEN** `yastatus -o` is invoked without `-v`
- **THEN** a usage error is printed to stderr and the process exits `2`

#### Scenario: the renderer flags are defined

- **WHEN** `yastatus` is invoked
- **THEN** these flags are accepted:

| flag             | behavior                                                              |
| ---------------- | --------------------------------------------------------------------- |
| `-j`/`--jobs ID...` | Filter to the given task ids. When absent, the default query returns `RUNNING` and `TO_DO` tasks. |
| `-v`/`--view`    | Verbose renderer: connect to each running task's node and tail its remote output. |
| `-i`/`--info`    | One-line, tab-separated renderer.                                     |
| `--json`         | JSON renderer with raw domain values.                                 |
| `-o`/`--convergence` | With `-v`, also download and print a CRYSTAL convergence snippet.  |

#### Scenario: a failed task ends its info line with the reason

- **WHEN** `yastatus -i -j 7` lists task `7`, which is `DONE` with the failure reason `Download error: OUTPUT: selection matched no entries`
- **THEN** the line ends with `error=Download error: OUTPUT: selection matched no entries`

#### Scenario: a task without a failure reason prints no error field

- **WHEN** `yastatus -i` lists a task whose state carries no failure reason
- **THEN** its line has no `error` field

### Requirement: yastatus JSON output

When `--json` is given, `yastatus` SHALL print one JSON object per task
with raw domain values and no display tokens. Each object SHALL carry a
nested `node` object (not flat fields). `error` SHALL be the task's failure
reason, or `null` when the task has none. The schemas SHALL be:

```
{"task_id": int, "status": str, "label": str, "engine": str,
 "local_folder": str | null, "remote_folder": str | null,
 "error": str | null,
 "created_at": str, "updated_at": str,
 "node": {"hostname": str, "port": int, "username": str,
          "cloud": str | null, "jump_host": str | null,
          "jump_port": int, "jump_username": str,
          "external_id": str | null, "status": str,
          "created_at": str, "updated_at": str} | null}
```

The `node` object SHALL be `null` when the task has no allocated node.
An empty result SHALL print `[]`.

#### Scenario: an allocated node renders as a nested object

- **WHEN** `yastatus --json` lists a task allocated to a node with hostname `10.0.0.1`
- **THEN** the object has a `node` field containing `{"hostname": "10.0.0.1", ...}` and no flat `ip`/`port`/`cloud` fields on the task object

#### Scenario: an empty result prints an empty list

- **WHEN** `yastatus --json` is invoked and the query returns no tasks
- **THEN** the output is `[]` and the process exits `0`

#### Scenario: a failed task carries its reason

- **WHEN** `yastatus --json -j 7` lists task `7`, which is `DONE` with the failure reason `Download error: OUTPUT: selection matched no entries`
- **THEN** the object's `error` is that reason, and it is `null` for a task without one
