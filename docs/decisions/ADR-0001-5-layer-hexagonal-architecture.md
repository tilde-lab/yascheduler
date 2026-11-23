# ADR-0001: Adopt 5-layer hexagonal architecture with import-linter enforcement

- **Status:** Accepted
- **Date:** 2026-06-25
- **Supersedes:**
- **Superseded by:**

## Context

The codebase had a conventional layered architecture (domain, application,
adapters) enforced only by convention and code review. Cross-layer imports
bypassed `__init__.py` facades, hiding dependency direction. The `adapters/`
directory conflated driving adapters (CLI, client) with driven adapters
(persistence, SSH, cloud), and the word "adapter" collided with in-layer class
names like `CloudAdapter` and module basenames like `adapters.py`. The top-level
package root accumulated utilities, entry points, and a legacy data layer with
no import discipline — `compat.py`, `variables.py`, `client.py`, `db.py`,
`daemon_*.py` lived as flat siblings.

Without enforcement, layer violations would reappear on every contributor's
first PR. Without dedicated homes for each architectural concern, future
refactors would battle the same naming ambiguity and root-level accretion.

Three approaches were considered for import enforcement: rolling a custom CI
script that greps for forbidden patterns, adopting the `forbidden` contract
type (which requires enumerating every forbidden deep path per layer), and
adopting the `layers` contract type (which declares an ordered list and lets
the library check direction automatically). The `layers` contract was chosen
for zero-maintenance enforcement.

For the shared kernel, two definitions were considered: a negative definition
("no business logic, no I/O, no domain types") and a positive definition
("typing shims consumed by ≥2 architectural layers; a module whose consumers
are in a single layer belongs to that layer"). The positive definition was
adopted to make the rule actionable by reviewers.

## Decision

The codebase is organized into 5 architectural layers with strict import
direction enforced by `import-linter`'s `layers` contract type:

```toml
[[tool.importlinter.contracts]]
name = "Clean architecture layers"
type = "layers"
layers = [
  "yascheduler.entrypoints",
  "yascheduler.infra",
  "yascheduler.application",
  "yascheduler.domain",
  "yascheduler.shared",
]
```

Import direction flows top-to-bottom: `entrypoints → infra → application →
domain → shared`. No layer imports from a layer above it.

## Alternatives Considered

### `forbidden` contract type (per-path enumeration)

Rejected as brittle — every new submodule would need an entry. The `layers`
contract matches the mental model directly: one ordered list, zero custom
code, zero maintenance.

### Negative shared-kernel definition ("no business logic / no I/O")

Used transitively but replaced by the positive definition after
`prune-shared-kernel`. The negative definition could not distinguish a true
cross-layer kernel from a misplaced single-layer utility.

### `config` as a layer in the `layers` contract

Rejected to avoid reclassifying `config` (which stayed outside-layer-set by
prior decision). The `shared ↔ config` cycle risk is blocked by a separate
`forbidden` contract instead.

### Full R1/R2 tooling enforcement

Rejected as overkill. The `layers` contract enforces only R3 (layer direction).
R1 (within-package relative imports) and R2 (facade-only cross-package imports)
are convention + code review.

## Consequences

### Positive

- **Zero-maintenance direction enforcement.** The `layers` contract catches any
  upward import without per-path configuration. One CI command (`lint-imports`
  via `uv run lint-imports`) exit-code 0/1 protects the architecture.
- **Layer naming disambiguated.** `infra/` replaces `adapters/`, removing the
  collision with in-layer adapter classes and module basenames.
- **Entrypoints isolated.** Driving adapters (client, CLI, daemon, AiiDA
  plugin, composition root) have a dedicated outermost layer. `client.py`
  retains its public import paths via a compat shim (`yascheduler/client.py`
  → `yascheduler/entrypoints/client.py`).
- **Shared kernel pruned to honest content.** `shared/` contains only `Self`
  and `Unpack` typing shims — symbols consumed by ≥2 architectural layers.
  Single-consumer utilities (`to_sync`, `asleep_until`, path constants) live
  in their consumer's layer.
- **Public surface explicit.** Each layer's `__init__.py` is its sole public
  facade. Empty facades are valid — symbols are added only when a real
  consumer needs them (lazy publication).
- **TYPE_CHECKING imports not penalized.** `exclude_type_checking_imports = true`
  allows type-only references across layers without violating the contract.

### Negative / trade-offs

- **R1 and R2 are unenforced by tooling.** Within-package relative imports and
  facade-only cross-package imports depend on spec text, AGENTS.md triggers,
  and code review. A reviewer skipping the check lets violations through.
- **Compatibility shim for `client.py`.** A real file at `yascheduler/client.py`
  re-exports `Yascheduler` from `entrypoints/client.py`.

### Accepted risks

- **`ignore_imports` could become permanent.** The original R3 residual edges
  (`application.consume_task` and `application.orchestrator` importing SSH
  exceptions) were added to `ignore_imports` with a follow-up change
  commitment. If the follow-up never lands, the two suppress-import entries
  become permanent wart. Policy: do not extend `ignore_imports` ad hoc beyond
  the documented edges.
- **`config` stays outside the layer set.** The `forbidden` contract
  (`shared → config`) blocks the only real cycle risk, but `config` is not
  subject to R3 direction checks. Adding it as a layer is deferred.
- **Positive shared-kernel definition can be gamed.** A contributor could add a
  trivial import to a second layer just to qualify a utility for `shared`. The
  "no SSH/DB/HTTP/cloud I/O" clause remains as a second guardrail.
