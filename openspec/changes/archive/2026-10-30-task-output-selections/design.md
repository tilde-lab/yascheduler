# Design: task-owned output selections

## Context

Retrieval reads the engine repository once, at completion
(`consume_task`), and passes the live `output_files` to the downloader. The
engine repository is a frozen mapping parsed once at daemon start, so the
window between acceptance and retrieval spans a daemon restart: an operator who
edits an engine section and restarts rewrites the retrieval contract of every
in-flight task of that engine.

Inputs crossed this bridge in part 1 of
[ADR-0019](../../../../docs/decisions/ADR-0019-recursive-task-file-transfers.md):
the task owns its input snapshot, deployment reads the snapshot. Outputs kept
reading configuration. This change closes that asymmetry for the selection
list only.

## Decisions

- **The record lives on the task row, not in a child table.** An input
  snapshot is binary content with per-entry kinds, so it became normalized
  entries. An output selection list is a handful of short strings with no
  per-entry attributes — one nullable `TEXT[]` column on `yascheduler_tasks`
  carries it. A child table would add a join, a foreign key, an ordering
  column, and a second insert path for no constraint the column lacks.
- **The column is nullable, and NULL means "no record".** Tasks accepted
  before the change have no record and must stay readable. Engine configuration
  and submission each require at least one output selection; a stored empty
  list is not a valid record, and no sentinel distinguishes empty from absent.
- **The record is taken at acceptance.** Acceptance is the boundary where the
  client's submission and the engine contract meet, and it is the only moment
  before the task can be deployed. Taking it at allocation instead would leave
  the cloud-provisioning wait — minutes to hours — inside the window.
- **One rule, three call sites.** Submission writes the engine's selections
  into the record. Retrieval and the view command read the record first and
  fall back to the configured selections only when the record is absent;
  sources never mix inside one task. The fallback logs a warning with the task
  identity, which is the observable behaviour an operator needs during the
  upgrade window.
- **A submission may supply the selections it wants recorded.** The engine's
  selections are the default; an explicit list replaces them for that task
  only. The public client, dependency seam, and use case accept
  `output_selections: list[str] | None`; `None` uses the engine's selections.
  This is the escape hatch that makes a fixed record workable: "one debug task,
  take everything on the machine" no longer requires editing the engine section
  and restarting the daemon, which is the action that used to corrupt in-flight
  tasks. Engine defaults and supplied overrides both require at least one
  selection and pass `task-file-selection` validation before the write, so a
  selection cannot reach outside the task root; the API contract is typed
  `list[str] | None`, with no runtime container-type guard. The list lands in
  the same record, so retrieval keeps one source. A client-supplied list can
  make a task return less than the engine expects, but the record states exactly
  what was asked for.
- **Naming: `output_selections` on the task, `output_files` in the INI.** The
  INI key is a stable public format and does not change. The task field is
  named for what it holds — a selection list in the `task-file-selection`
  language, not resolved paths — matching the wording the specs already use.
- **The engine repository remains required at retrieval.** A task whose
  engine was removed from configuration still finalises as failed. That is an
  operator stop signal, not a data dependency: it applies whether or not the
  task carries a record. The record does not remove the engine lookup.
- **Missing selections keep their current classification.** A record does not
  soften requiredness: an unmatched explicit path or pattern stays a permanent
  retrieval error, and remaining selections are still attempted. This change
  moves the source of the list, nothing else.

## Alternatives Considered

### Snapshot the whole engine specification into the task

Rejected. `deployable` and `spawn` name local build artefacts of the engine
package: they must follow the release that is deployed on the machine, not the
release that existed at acceptance. Freezing them would deploy stale commands
and stale artefacts, and would flip the studied outcome of both allocation
validation and the missengine retrieval policy. The staleness that matters is
the retrieval contract; the rest is release configuration.

### Record the selections at allocation

Rejected. It narrows the window but does not close it: a TO_DO task waiting for
a cloud node can sit for hours, and its output contract would still be decided
by a configuration that changed after the client was told the task was
accepted.

### Version the engine configuration and store only a version reference

Rejected. Every read would need the historical configuration, so the daemon
must retain old configuration versions. That is a bigger persistence surface
than the values it protects, and a task's outcome would depend on an external
file that can be deleted.

### Union the supplied selections with the engine's

Rejected. A debug submission that takes everything (`**/*`) gains nothing from
a union, and a union hides the difference between "the engine asks for this"
and "the client asks for this" in one list, which removes the property that
makes the record useful for diagnosis.

### Command-line flag on the submit command

Deferred. The ad-hoc case is served by the public client, where a submission is
one call. A flag on the script-driven submit command adds a surface that is
worth adding when a shell-based workflow asks for it.

### Keep reading configuration, log a warning when it changed

Rejected. Detecting the change requires storing what was in force at
acceptance, which is the whole point. Storing it and then not using it for
retrieval would leave the silent incomplete result in place.

## Risks

- An operator who fixes a mistaken engine section mid-flight no longer repairs
  in-flight tasks: they keep the selections recorded at acceptance and must be
  resubmitted. Accepted — determinism is the requested outcome, and the
  alternative is a result that depends on restart timing.
- The upgrade window keeps the configuration fallback, so the old behaviour
  survives for pre-change tasks until they drain.
- A recorded first selection may be a pattern, so the view command has nothing
  to tail. It names the pattern instead of attempting the transfer, so the
  operator sees why a progress tail is missing rather than a misleading
  "outdated task" line. Tailing the first concrete selection after a leading
  pattern is not implemented: a pattern selects a set, and picking a member of
  it would guess which file carries the progress.

## Follow-through

A new ADR records the ownership decision
([ADR-0020](../../../../docs/decisions/ADR-0020-task-owned-output-selections.md));
ADR-0019 keeps authority for transfer behaviour and gains a pointer for the
output direction.
