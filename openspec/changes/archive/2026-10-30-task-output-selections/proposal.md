# Proposal: task-owned output selections

## Why

A task decides what to retrieve from its engine's live configuration at
completion time. It records nothing about the output contract it was accepted
under. An engine configuration edited between acceptance and retrieval
therefore changes the outcome of an in-flight task:

- an output selection added to the configuration makes a task that ran under
  the old declarations fail permanently, because the new selection matches
  nothing on the machine;
- an output selection removed from the configuration is silently skipped, and
  the task finalises as done with an incomplete result.

Both directions are reachable by a routine operator action: edit the engine
section, restart the daemon, and every pending or running task of that engine
is retrieved under the new declarations. The second direction is the damaging
one, because no error is recorded and the missing result surfaces downstream,
outside the scheduler. The same ownership gap makes a completed task
unexplainable after the fact: the configuration that determined its result is
no longer recoverable.

Inputs already have submission-time ownership through `task-input-snapshots`.
Outputs do not.

## What Changes

- A submitted task records the output selections of its engine in force at
  acceptance, and keeps them for its whole lifecycle.
- Retrieval resolves the task's recorded selections. A task accepted before
  this record exists falls back to its engine's configured selections and
  reports that fallback in the log with the task identity.
- A submission may supply its own output selections instead of the engine's;
  they pass the same selection validation and become the task's record. The
  public client and use case accept an optional `list[str] | None`; `None` uses
  the engine's selections. The command-line submit surface keeps its current
  arguments.
- The operator's view command tails the task's first recorded selection,
  under the same fallback.
- The recording is one nullable task column through a forward migration; tasks
  from before the change stay readable and unchanged.
- Unchanged: a missing explicit selection or a pattern matching nothing stays a
  permanent retrieval error; a task whose engine was removed from configuration
  still finalises as failed; input snapshots, deployment, allocation, and the
  configuration format are not touched.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `use-cases`: submission records the engine's output selections; retrieval
  resolves the recorded selections and falls back to the configuration only for
  a task without a record.
- `postgres-persistence`: the task store owns the recorded output selections
  and returns them through the task's lifecycle.
- `cli`: the view command tails the task's recorded first output selection.
- `package-facades`: both public client submit methods accept an optional
  output selections list.

## Impact

Touches the task domain record and its pre-persistence shape; submission
including one optional argument of the public client, retrieval, and the view
command; the task SQL queries, the schema snapshot, and
one forward migration; the decision record and its tests. No public
configuration, CLI flag, task status, or query projection changes.

Excluded: freezing the whole engine specification in a task. Deployment
artefacts and the spawn command follow the deployed release, not the acceptance
time, so a full engine snapshot would break deployment rather than make it
reproducible. See the design.
