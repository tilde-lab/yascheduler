# ADR-0013: Task metadata — typed columns for known fields, JSONB for the rest

- **Status:** Accepted
- **Date:** 2026-07-06
- **Supersedes:**
- **Superseded by:** [ADR-0019](ADR-0019-recursive-task-file-transfers.md),
  only the placement of task input files in `extra`

Task input storage is partially superseded by ADR-0019. The decisions below
about typed task fields, unrelated user metadata, and lifecycle mutations
remain in force.

## Context

Task state was stored as an opaque JSONB blob: every domain field —
engine, folders, webhook parameters, error text — was a stringly-typed
key inside one column. The schema was self-describing only by
convention, every read paid a JSON indirection, and the fields the
application actually uses could not be filtered or indexed from SQL.

At the same time, tasks carry engine-supplied input-file payloads of
unbounded shape that the application passes through opaquely. Those
keys are not domain fields; they are user payload.

## Decision

Split the task's metadata into two regimes:

1. **Known domain fields become typed columns.** Each field the
  application reads or writes at a concrete call site — engine,
  folders, webhook parameters, error text — becomes a real column on
  `yascheduler_tasks` and a typed field on the `Task` / `NewTask`
  entities. The schema is now self-describing; these fields are
  SQL-filterable and indexable.

2. **Unknown user payload stays in a JSONB catch-all column** (`extra`).
  Engine input-file keys and any future arbitrary payload live there
  without requiring a schema change.

Mutations of the typed fields happen only through the task's named
lifecycle transition methods (ADR-0012); there is no generic
replace-with-overrides API for arbitrary metadata keys.

## Alternatives Considered

### Keep everything in one JSONB column

Rejected — hides the domain fields behind stringly-typed keys, blocks
SQL filtering and indexing, and forces every read through a JSON
indirection for fields the application uses directly.

### Explode every JSONB key to a typed column

Rejected — user payload is unbounded and engine-specific; promoting
each key to a column would churn the schema on every new engine input.

### Split the catch-all into multiple JSONB buckets (e.g. a dedicated `inputs`)

Rejected — nothing reads the payload as a structured sub-object today;
keys are accessed individually. One JSONB bucket is sufficient until a
second consumer appears.

## Consequences

- **Positive:** The schema describes the task's domain fields directly;
  every `task.X` access is a direct attribute read, not a dict lookup.
- **Positive:** Domain fields are SQL-filterable and indexable.
- **Positive:** User payload retains the flexibility of JSONB without
  forcing schema changes on the domain fields.
- **Positive:** Mutations go through named transitions, so callers
  cannot construct partial or invalid metadata states.
- **Negative / trade-offs:** Adding a new domain field requires a DB
  migration — the rigidity cost of typed columns. User payload remains
  unbounded JSONB, with no size cap (no regression, but unbounded
  growth is possible).
