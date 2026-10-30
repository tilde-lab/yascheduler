# Design: input snapshot is read on demand

## Context

The snapshot lives in `yascheduler_task_inputs` as one row per entry with
`content BYTEA`, and the task record carries it as a field. Because the
repository returns whole records, `_hydrate_input_entries` runs on every read:
single-task lookups, status lists, and job-id lists. The daemon's producers
read the whole queue's bytes several times per cycle, and each consume attempt
reads one task's bytes to inspect an allocation.

Bytes have two readers: deployment, which uploads them, and the client query,
which returns them by contract. Everything else reads a field it does not use.

Trees are now pseudopotential and basis sets — tens or hundreds of megabytes
per task — so the multiplier matters.

## Decisions

- **The record stops carrying bytes.** The field goes off the task record and
  stays on the pre-persistence submission shape. Submission captures the bytes
  anyway, writes them once, and nothing reads them back through the record.
  The empty-versus-not-loaded ambiguity disappears with the field: there is no
  field to leave unfilled, so no sentinel is needed and no reader has to
  remember how the record was fetched.

- **Two reads, one shape each.** The store exposes the complete snapshot for a
  set of tasks — the batched query that already exists — and one entry at a
  time for a single task. The query use case takes the batched shape because
  it answers a request that returns many tasks; deployment takes the
  per-entry shape because it uploads one entry and moves on. Collapsing them
  into one shape forces either the query into per-entry reads or deployment
  into whole-snapshot memory, so they stay apart.

- **The per-entry read walks the path in byte order.** Keyset pagination:
  select the next entry with a path greater than the last one returned, ordered
  by path under the `C` collation, one row per query. Byte order guarantees a
  parent sorts before its descendants regardless of the database locale, which
  keeps the transfer order equal to the creation order. Directory rows come
  from the normalized snapshot written at submission, so deployment creates no
  parent that is not itself an entry.

- **Deployment receives an iterator, not a snapshot.** The allocation use case
  reads the entries from the unit of work it already holds while it claims the
  task row, and hands the iterator to the deployment operation. The deployment
  operation stays stateless and knows nothing about the store; the wrapper on
  the orchestrator threads the parameter through. No second connection is
  opened: the unit of work holds one connection and one transaction for the
  whole allocation, and the iteration runs inside it.

- **Whole-snapshot re-validation is dropped at deployment.** The snapshot is
  normalized before it is written, the primary key rejects duplicate paths,
  table constraints reject kind and content violations, and every path is
  still validated before it reaches the transfer. What is lost is the
  guarantee that nothing is written to the machine before the whole tree is
  known to be valid. A corrupt row now fails after the preceding entries
  reached the machine and leaves a partial tree, which the retry overwrites. A
  transfer that fails mid-way already produces that state today.

- **The query keeps materialising the snapshot.** The public projection
  returns exact bytes for every file, so a query for many tasks holds their
  snapshots at once. That is the contract, not an accident of the read model.
  Changing it is a public-contract decision and stays outside this change.

- **No migration.** The schema does not change; only the read path does.
  `save` already leaves the entries table untouched, so the snapshot stays
  immutable after submission without any code to enforce it.

## Risks

- A reader that expects input contents on a task record no longer compiles,
  which is the intent. The risk is the opposite direction: a caller that
  receives a task and silently assumes an empty snapshot means "no inputs".
  Since the field is gone, such a caller has nothing to read and fails at type
  check rather than at runtime.

- The per-entry read adds one store round trip per entry during deployment.
  The transfer over the network dominates; the round trips are noise against
  it.

- The size ceiling on a snapshot is set by storing bytes in the database and
  by the query contract that returns them. This change lowers neither, and
  does not claim to.
