# ADR-0021: Input bytes are read apart from the task record

- **Status:** Accepted
- **Date:** 2026-10-30
- **Supersedes:**
- **Superseded by:**

## Context

[ADR-0019](ADR-0019-recursive-task-file-transfers.md) part 1 moved a task's
inputs out of the metadata payload into a per-task snapshot that stores exact
file bytes, and gave that snapshot to the task record. Ownership is correct:
the snapshot survives source edits, configuration changes, and restarts.

Placement is not. A repository that returns whole task records must fill every
field, so the snapshot — including every file's bytes — is loaded on every task
read. The daemon reads the queue's snapshots several times per cycle from
producers that need only task identities, and again on each consume attempt.
Input trees grew from kilobytes of text to pseudopotential trees of tens or
hundreds of megabytes, so the cost per cycle grew by orders of magnitude while
the mechanism stayed.

The same placement produces a second defect: an empty snapshot and a snapshot
that was never loaded are the same value, so no reader can tell "this engine
declares no inputs" from "these bytes were not fetched".

Only two readers need bytes: deployment, which uploads them, and the client
query, which returns them by contract.

The options:

- Keep the snapshot on the record and load it on every read.
- Keep it on the record and make it nullable, so readers can tell it apart.
- Keep it on the record and split the repository reads into loading and
  not-loading variants.
- Remove it from the record and read it explicitly at the two points that need
  it.

## Decision

The task record carries no input contents. The store keeps the snapshot,
returns it complete on an explicit snapshot request, and returns entries one at
a time to deployment, which transfers them in path order without holding the
snapshot in memory. The client query projects the snapshot as before.

## Alternatives Considered

### Keep loading on every read

Rejected. It makes every cycle cost proportional to the queue's total input
size, paid by consumers that never look at the bytes. No reader of the daemon
needs the snapshot except deployment.

### Nullable snapshot on the record

Rejected. It legalises the defect instead of removing it: every reader of the
field must remember whether it was loaded, and forgetting yields a silently
empty snapshot. A field whose meaning depends on how it was fetched is a
modelling error, and a sentinel hides it.

### Split the repository reads

Rejected. It adds a second variant of every read to answer a question the
record should not raise. The bytes have exactly two readers; naming them
directly is smaller than doubling the read surface and marking each call with
a flag.

## Consequences

- **Positive:** a task read no longer depends on the size of the input tree.
  Producers, consume, deallocation, and status queries read rows only.
- **Positive:** the empty-snapshot ambiguity disappears with the field. There
  is nothing to load, so nothing can be forgotten.
- **Positive:** deployment memory no longer grows with the snapshot; it holds
  one entry at a time, which also survives moving the bytes out of the
  database later.
- **Negative / trade-offs:** deployment validates each entry as it reaches it
  rather than validating the whole snapshot before the first remote write. A
  corrupt row fails mid-transfer and leaves a partial tree, which a retry
  overwrites. Existing protections stand: duplicates are rejected by the
  primary key, entry kind and content invariants by the table constraints, and
  every path is still checked before it reaches the transfer.
- **Negative / trade-offs:** the client query still materialises the snapshot,
  by contract. A snapshot too large for a query response remains limited by
  the storage choice, not by this decision.
- **Accepted risks:** deployment now issues one store read per entry. The
  transfer itself dominates, so the added round trips are not measurable
  against it.
