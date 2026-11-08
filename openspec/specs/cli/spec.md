## Purpose

Define the operator-facing CLI surface: commands and three daemon
launchers. Each command has a stable name, a fixed set of flags, an
exit-code contract, and a defined output. Scripts and the AiiDA plugin
read this output. The daemon launchers start the same daemon under three
process supervisors.

## Requirements

### Requirement: Command surface and shared flags

The system SHALL install the CLI commands and daemon launchers listed in the
command-roster scenario. Every command and launcher SHALL accept `--config PATH`
and `--log-level`. `--config` SHALL point at an existing file; a missing file
SHALL exit `2` with a message of the form `not a file: <path>`. `--log-level`
SHALL accept only `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`. The daemon
launchers SHALL also accept `--log-file`.

#### Scenario: the command roster is installed

- **WHEN** the installed CLI surface is inspected
- **THEN** these commands and daemon launchers are available:

| command        | prog         | purpose                                                  |
| -------------- | ------------ | -------------------------------------------------------- |
| yasubmit       | yasubmit     | Submit a task from an AiiDA script.                      |
| yastatus       | yastatus     | Query and display task status.                           |
| yanodes        | yanodes      | List nodes and their running tasks.                      |
| yasetnode      | yasetnode    | Add, soft-remove, or hard-remove a node.                 |
| yainit         | yainit       | Install the service unit and apply the DB schema.        |
| yascheduler    | yascheduler  | Start the daemon (foreground launcher).                  |
| daemon_systemd | yascheduler  | Start the daemon under systemd, logs to journald.        |
| daemon_sysv    | yascheduler  | Start the daemon under SysV init, detached with a pidfile. |

#### Scenario: a missing config file exits 2

- **WHEN** any command or launcher is invoked with `--config /nonexistent.conf`
- **THEN** a usage error is printed to stderr and the process exits `2`

#### Scenario: an unknown log level exits 2

- **WHEN** any command or launcher is invoked with `--log-level WARN`
- **THEN** a usage error is printed to stderr and the process exits `2`

### Requirement: Exit-code contract

Every command and launcher SHALL use the exit-code contract defined in the
exit-code scenario.

#### Scenario: the exit-code contract is defined

- **WHEN** any command or launcher finishes
- **THEN** it uses exactly one of these exit codes:

| code | meaning                                                                |
| ---- | ---------------------------------------------------------------------- |
| `0`  | Success, or `--help`.                                                  |
| `1`  | Runtime error. The entry point prints `Error: <message>` to stderr.    |
| `2`  | Argument error: unknown flag, bad choice, missing positional, mutex violation, or a `--config` file that does not exist. |

#### Scenario: a runtime error exits 1 with an Error message

- **WHEN** a command fails because the database is unreachable
- **THEN** a line of the form `Error: <message>` is printed to stderr and the process exits `1`

#### Scenario: an argument error exits 2

- **WHEN** a command is invoked with an unknown flag
- **THEN** a usage error is printed to stderr and the process exits `2`

### Requirement: yasubmit parses an AiiDA script

`yasubmit` SHALL take one positional argument: the path to an AiiDA script.
The path SHALL point at an existing file; otherwise the process exits `2`.
`yasubmit` SHALL read `KEY = VALUE` lines from the script. It SHALL require an
`ENGINE` key whose value is a known engine name. Validation failures SHALL print
nothing to stdout. A missing `ENGINE` key or an unsupported engine SHALL print
the corresponding `Error:` message to stderr and exit `1`.

#### Scenario: a missing ENGINE key exits 1

- **WHEN** `yasubmit` is run against a script that has no `ENGINE = ...` line
- **THEN** `Error: Script has not defined an engine` is printed to stderr, stdout stays empty, and the process exits `1`

#### Scenario: an unknown engine exits 1

- **WHEN** `yasubmit` is run against a script with `ENGINE = unknown` and the engine is not configured
- **THEN** `Error: Engine unknown is not supported` is printed to stderr, stdout stays empty, and the process exits `1`

### Requirement: yasubmit stdout is the AiiDA task-id contract

On success, `yasubmit` SHALL print only the task id as a bare number to
stdout. The AiiDA scheduler plugin reads this number. No prefix,
suffix, decoration, or envelope SHALL be added.

#### Scenario: success prints only the task id

- **WHEN** `yasubmit` submits a task and the new task id is `42`
- **THEN** stdout contains exactly `42` (with no other text) and the process exits `0`

### Requirement: yanodes lists nodes and their running tasks

`yanodes` SHALL list every node and the single task running on it, if any. Each
node SHALL produce one output row or one JSON object, in node identity ascending
order.

`yanodes` SHALL accept the filters in the filter-roster scenario. Filters SHALL
compose by AND: a node is shown only when it passes every active filter.
`--cloud` and `--no-cloud` SHALL be mutually exclusive.
`--enabled`/`--disabled` and `--busy`/`--free` SHALL be subset selectors.

#### Scenario: the filter roster is defined

- **WHEN** `yanodes` filters are inspected
- **THEN** these flags keep nodes as follows:

| flag           | keeps nodes where...                                  |
| -------------- | ------------------------------------------------------ |
| `--enabled`    | enabled is true.                                       |
| `--disabled`   | enabled is false.                                      |
| `--busy`       | a running task is allocated to the node.               |
| `--free`       | no running task is allocated to the node.              |
| `--cloud NAME` | cloud equals NAME (exact match).                       |
| `--no-cloud`   | cloud is unset.                                        |

- **AND** when both members of the `--enabled`/`--disabled` or
  `--busy`/`--free` pair are given, the pair selects all nodes on that axis.

#### Scenario: filters compose by AND

- **WHEN** `yanodes --enabled --busy --cloud hetzner` is invoked
- **THEN** only nodes that are enabled, busy, and have cloud `hetzner` are listed

#### Scenario: cloud and no-cloud are mutually exclusive

- **WHEN** `yanodes --cloud hetzner --no-cloud` is invoked
- **THEN** a usage error is printed to stderr and the process exits `2`

### Requirement: yanodes default table output

When `--json` is not given, `yanodes` SHALL print a header row followed by one
data row per node. The columns SHALL be the columns in the table-output scenario,
in order. Column widths SHALL be computed from the data so the table is
self-aligning. The cells SHALL apply the display transformations in that
scenario.

#### Scenario: the table columns and display transformations are defined

- **WHEN** `yanodes` renders the default table
- **THEN** the header cells are `NODE_ID`, `HOSTNAME`, `PORT`, `NCPUS`,
  `ENABLED`, `CLOUD`, `TASK_ID`, and `LABEL`, in that order
- **AND** cells use these display transformations:

| column   | cell value                                                  |
| -------- | ----------------------------------------------------------- |
| NODE_ID  | the node identity                                           |
| HOSTNAME | the hostname                                                |
| PORT     | `-` when the port is `22`, else the port                    |
| NCPUS    | `MAX` when the count is unset (or the legacy `0`), else the count |
| ENABLED  | `yes` or `no`                                               |
| CLOUD    | `-` when cloud is unset, else the cloud name                |
| TASK_ID  | `-` when no running task is allocated, else the task id     |
| LABEL    | `-` when no running task is allocated, else the label       |

#### Scenario: a busy node renders a full row

- **WHEN** `yanodes` lists an enabled node on port `22` with a running task `7` labeled `my_job`
- **THEN** the row shows `NODE_ID` set, `PORT` `-`, `NCPUS` set, `ENABLED` `yes`, `CLOUD` `-`, `TASK_ID` `7`, `LABEL` `my_job`

### Requirement: yanodes JSON output

When `--json` is given, `yanodes` SHALL print one JSON object per node
with raw domain values and no display tokens (no `-`, no `MAX`, no
`yes`/`no`). The object SHALL carry these fields:

```
{"node_id": int, "hostname": str, "port": int, "ncpus": int | null,
 "enabled": bool, "cloud": str | null, "jump_host": str | null,
 "jump_port": int, "jump_username": str, "external_id": str | null,
 "status": str, "created_at": str, "updated_at": str,
 "occupied_by": {"task_id": int, "label": str} | null}
```

`occupied_by` SHALL be the running task on that node, or `null` when
the node is free. An empty result SHALL print `[]`.

#### Scenario: a node with a running task emits raw values

- **WHEN** `yanodes --json` lists a node with an unset CPU count and a running task
- **THEN** the object has `ncpus: null`, `occupied_by` set to `{"task_id": ..., "label": ...}`, and no display tokens

#### Scenario: an empty result prints an empty list

- **WHEN** `yanodes --json` is invoked and no node matches the filters
- **THEN** the output is `[]` and the process exits `0`

### Requirement: yasetnode host grammar

`yasetnode` SHALL take one positional argument. A pure-digit argument SHALL be
read as a node identity. Any other argument SHALL be parsed as the host spec
defined in the host-grammar scenario. A malformed argument SHALL exit `2`. A node
identity SHALL be allowed only on a remove path; an identity on the add path
SHALL exit `2`.

#### Scenario: a full host spec parses

- **WHEN** the positional argument `deploy@[10.0.0.1]:2222~4` is parsed
- **THEN** the resolved target has host `10.0.0.1`, username `deploy`, port `2222`, and count `4`

#### Scenario: an unbracketed IPv6 address exits 2

- **WHEN** `yasetnode ::1` is invoked
- **THEN** a usage error is printed to stderr and the process exits `2`

#### Scenario: adding by identity exits 2

- **WHEN** `yasetnode 5` is invoked with no remove flag
- **THEN** a usage error is printed to stderr and the process exits `2`

#### Scenario: the host grammar defines a target

- **WHEN** a non-digit positional argument is parsed
- **THEN** it uses the grammar `[user@]host[:port][~ncpus]`
- **AND** `user` is optional
- **AND** `host` is an IPv4 address or a bracketed IPv6 address (for example,
  `[fe80::1]`); an unbracketed IPv6 address is rejected
- **AND** `port` is `1`..`65535` and defaults to `22`
- **AND** `ncpus` is a non-negative integer; absent or `~0` means unlimited (no
  stored count)

### Requirement: yasetnode add and remove paths

`yasetnode` SHALL dispatch to exactly one path. The add path SHALL create a
node, connect to it, optionally run remote setup, then mark the node enabled. The
remove-hard path SHALL mark each running task on the node `DONE`, then remove the
node. The remove-soft path SHALL disable the node when it has running tasks,
otherwise remove it.

On success, `yasetnode` SHALL print the path-specific messages in the
path-output scenario verbatim to stdout after the commit succeeds.

#### Scenario: add success prints the verbatim messages after commit

- **WHEN** `yasetnode [10.0.0.1]` succeeds without `--skip-setup`
- **THEN** stdout contains `Setup host...` followed by `Added host to yascheduler: 10.0.0.1:22`

#### Scenario: a connect failure leaves no enabled node

- **WHEN** the add path cannot connect to the host
- **THEN** the created row is removed and no enabled row for that host remains

#### Scenario: add stores the jump-leg parameters from the config

- **WHEN** `yasetnode` adds a node and the remote config section specifies a jump
  host, jump username, and jump port
- **THEN** those values are read from the config and stored on the new node

#### Scenario: each successful path prints its verbatim messages after commit

- **WHEN** a `yasetnode` add or remove path succeeds and its commit completes
- **THEN** stdout contains these path-specific messages:

| path                | message (verbatim)                                                        |
| ------------------- | ------------------------------------------------------------------------- |
| add, before setup   | `Setup host...`                                                           |
| add, after commit   | `Added host to yascheduler: {host}:{port}`                                |
| remove-hard, per task | `An associated task {task_id} at {host} is now marked done!`            |
| remove-hard, after commit | `Removed host from yascheduler: {host}`                            |
| remove-soft, has tasks | `A task associated, prevent from assigning the new tasks` followed by `Prevented from assigning the new tasks: {host}` |
| remove-soft, no tasks  | `No tasks associated, remove node immediately` followed by `Removed host from yascheduler: {host}` |

- **AND** `{host}` is the parsed host, `{port}` is the parsed port, and
  `{task_id}` is a running task identity on the node.

### Requirement: yasetnode rejects invalid combinations

`--remove-soft` and `--remove-hard` SHALL be mutually exclusive.
`--skip-setup` SHALL be valid only on the add path. Removing a node
that is not in the database SHALL exit `1` with an `Error:` message.
The flag-combination violations above SHALL exit `2`.

#### Scenario: both remove flags exit 2

- **WHEN** `yasetnode [10.0.0.1] --remove-soft --remove-hard` is invoked
- **THEN** a usage error is printed to stderr and the process exits `2`

#### Scenario: removing an unknown node exits 1

- **WHEN** `yasetnode 999 --remove-hard` is invoked and no node `999` exists
- **THEN** an `Error:` message is printed to stderr and the process exits `1`

### Requirement: yastatus task query and renderers

`yastatus` SHALL query tasks and render them. The flags SHALL be the flags in
the renderer-flags scenario.

`-v`, `-i`, and `--json` SHALL be mutually exclusive. When none is given, the
default AiiDA-compatible renderer SHALL run. `-o` SHALL be valid only with `-v`;
`-o` without `-v` SHALL exit `2`.

The default renderer SHALL print one line per task: the task id, three spaces,
and the status name. `DONE` tasks SHALL appear only when the query is by id
(with `-j`).

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

### Requirement: yastatus JSON output

When `--json` is given, `yastatus` SHALL print one JSON object per task
with raw domain values and no display tokens. Each object SHALL carry a
nested `node` object (not flat fields). The schemas SHALL be:

```
{"task_id": int, "status": str, "label": str, "engine": str,
 "local_folder": str | null, "remote_folder": str | null,
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

### Requirement: yastatus view mode connects with node parameters

In view mode, `yastatus` SHALL connect to each running task's node over SSH and
tail its remote output file. The file SHALL be the task's first recorded output
selection, or its engine's first configured selection when the task has no
record. For a pattern selection, the command SHALL name the pattern and print no
remote content. Login username, port, and jump parameters SHALL come from the
node record, not cloud configuration. The convergence snippet SHALL be cleaned
up after display.

#### Scenario: connection parameters come from the node, not from cloud config

- **WHEN** `yastatus -v` connects to a task whose node has username `yascheduler` and jump host `bastion.example.com`, while the cloud config has a different username and jump host
- **THEN** the connection uses the node's username `yascheduler` and the node's jump host `bastion.example.com`

#### Scenario: a pattern selection is named instead of tailed

- **WHEN** the first output selection recorded by a task is a pattern
- **THEN** the command names that pattern in its output and prints no remote file content

#### Scenario: the tailed file follows the task's own record

- **WHEN** `yastatus -v` tails a task whose engine configuration now lists a different first output selection than the task recorded
- **THEN** the tailed file is the one the task recorded

### Requirement: yainit bootstraps the service and the database

`yainit` SHALL install the service unit and apply the database schema.
`--schema` and `--daemon` SHALL be subset selectors as defined in the
invocation-matrix scenario.

The service unit SHALL be systemd on a host with `/run/systemd/system`, otherwise
SysV. The schema apply SHALL be idempotent: an already-initialized database SHALL
succeed and exit `0`. A database error or service-file write failure SHALL exit
`1` with an `Error:` message.

#### Scenario: default invocation installs the service and applies the schema

- **WHEN** `yainit` is invoked with no flags on an uninitialized host
- **THEN** the service unit is written, the schema and migrations are applied, and the process exits `0`

#### Scenario: a repeated run is idempotent

- **WHEN** `yainit` is run again against the already-initialized database
- **THEN** the schema and migrations apply without error and the process exits `0`

#### Scenario: a database error exits 1

- **WHEN** the database is unreachable during `yainit`
- **THEN** an `Error:` message is printed to stderr and the process exits `1`

#### Scenario: yainit flags select service and schema work

- **WHEN** `yainit` is invoked with flag combinations
- **THEN** the selected work is:

| invocation      | service unit   | schema + migrations |
| --------------- | -------------- | ------------------- |
| no flag         | installed      | applied             |
| `--schema`      | skipped        | applied             |
| `--daemon`      | installed      | skipped             |
| `--schema --daemon` | installed  | applied             |

### Requirement: Daemon launcher roster

The three daemon launchers SHALL share the program name `yascheduler` and the
`--config` and `--log-level` flags. They SHALL differ as defined in the
launcher-roster scenario. `daemon_sysv` SHALL keep `--config` and `--log-level`
long-only. The timestamp prefix SHALL be on for launchers whose output is not
stamped by journald, and off for `daemon_systemd`.

#### Scenario: daemonize accepts -l as the log-level alias

- **WHEN** `yascheduler -l DEBUG` is invoked
- **THEN** the daemon runs at log level `DEBUG`

#### Scenario: daemon_sysv writes to the configured log file by default

- **WHEN** the SysV launcher is started with no `--log-file`
- **THEN** the daemon logs to `/var/log/yascheduler.log` (or the `YASCHEDULER_LOG_PATH` override)

#### Scenario: the daemon launcher roster is defined

- **WHEN** the daemon launchers are inspected
- **THEN** they differ as follows:

| launcher        | process mode                | short flags              | default `--log-file`        | timestamp |
| --------------- | --------------------------- | ------------------------ | --------------------------- | --------- |
| `daemonize`     | foreground                  | `-l` aliases `--log-level` | unset (stderr)            | on        |
| `daemon_systemd` | foreground, stderr to journald | none                  | unset (stderr)            | off       |
| `daemon_sysv`   | detached with a pidfile     | `-l` aliases `--log-file`, `-p` aliases `--pid-file` | `/var/log/yascheduler.log` (or the `YASCHEDULER_LOG_PATH` override) | on        |

### Requirement: Logger configuration

The daemon launchers SHALL configure the root logger to always emit to stderr,
and to a file only when `--log-file` is set. Both handlers SHALL share one
formatter, with the timestamp prefix enabled or disabled per the launcher table.
The `asyncssh` logger SHALL be set to `ERROR` and `warnings.warn` output SHALL be
routed through logging.

#### Scenario: a set log file is written alongside stderr with a timestamp

- **WHEN** a daemon launcher is started with `--log-file /tmp/y.log`
- **THEN** log records reach both stderr and `/tmp/y.log`, and each rendered line carries a leading timestamp

### Requirement: Non-daemon logger configuration

The five non-daemon commands SHALL share a separate logger setup. It SHALL emit
to stderr with the timestamp prefix off, SHALL add no file handler, and SHALL
leave the `asyncssh` logger unchanged. It SHALL not remove a handler that an
outer harness attached before the command ran.

#### Scenario: non-daemon log records render without a timestamp

- **WHEN** any non-daemon command runs at `--log-level DEBUG` and emits a record
- **THEN** the rendered stderr line carries no leading timestamp

### Requirement: Daemon migration compatibility preflight

Before constructing the daemon dependency graph or starting scheduling loops, every daemon launcher SHALL inspect the packaged migration set and the configured database migration tracker. The preflight SHALL only read state; it SHALL NOT initialize the schema, apply migrations, invoke `yainit`, wait for `yainit`, or otherwise coordinate with another process.

#### Scenario: current migration tracker permits startup

- **WHEN** the configured database tracker is present and its latest migration identifier equals the latest packaged migration identifier
- **THEN** the daemon proceeds to construct and start normally

#### Scenario: missing migration tracker stops startup with initialization guidance

- **WHEN** the configured database has no `yascheduler_migrations` tracker
- **THEN** the daemon exits nonzero before constructing the daemon graph and reports that migration metadata is absent and that the operator must run `yainit --schema` before restarting

#### Scenario: empty migration tracker stops startup with migration guidance

- **WHEN** the configured database tracker is present but has no applied migration identifier
- **THEN** the daemon exits nonzero before constructing the daemon graph and reports that migrations are unapplied and that the operator must run `yainit --schema` before restarting

#### Scenario: pending migrations stop startup with actionable versions

- **WHEN** the latest tracker identifier is older than the latest packaged migration identifier
- **THEN** the daemon exits nonzero before constructing the daemon graph and reports the applied and required identifiers plus the command `yainit --schema` for the operator to run before restarting

#### Scenario: database tracker is newer than the package

- **WHEN** the latest tracker identifier is newer than the latest packaged migration identifier
- **THEN** the daemon exits nonzero before constructing the daemon graph and reports a package/database compatibility error that directs the operator to install a compatible yascheduler version without recommending `yainit --schema`

#### Scenario: migration preflight cannot read the database

- **WHEN** the preflight encounters a database connection, authentication, permission, or query error
- **THEN** the daemon exits nonzero and exposes the original operational error without labelling it as missing or pending migrations

### Requirement: yasubmit captures the selected input tree

`yasubmit` SHALL resolve the engine's `input_files` selections against its
current working directory and submit the captured snapshot defined by
`task-input-snapshots`. The script's parent directory SHALL not replace the
working directory as the submission root. A selection or capture failure
SHALL leave no accepted task.

#### Scenario: submission captures from the working directory

- **WHEN** `yasubmit` runs from a directory containing the engine's selected input tree and receives a script located in a different directory
- **THEN** the accepted snapshot comes from the working directory, and stdout contains only the new task identity followed by a newline
- **AND** binary files enter the typed snapshot directly rather than untagged file-value metadata

#### Scenario: existing submit surface remains stable

- **WHEN** `yasubmit` submits with the current engine INI list syntax, script metadata handling, and flags
- **THEN** that handling is unchanged, including the bare task-id stdout contract

#### Scenario: unsafe or unreadable input fails cleanly

- **WHEN** a required selection is missing, unsafe, or unreadable
- **THEN** stderr identifies the failing selection or path with an `Error:` diagnostic, stdout is empty, exit status is `1`, and no task is accepted
