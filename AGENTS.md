# AGENTS.md

<!-- #region SECTION_Dev_Rules -->

## Development Rules

- Follow the OpenSpec, YAGNI, DRY, KISS, SOLID principles.
- Top-down approach: Start with requirements and a bird's-eye view plan. Define
  module contracts with purpose and boundaries before any code. Specify
  contracts for public classes, methods, and functions. Create stubs. Only then
  write code inside the contracted regions.
- Public interface stability: CLI commands, public API, INI config format, DB
  schema. Schema changes MUST include migrations.
- NEVER modify `pyproject.toml` version; release automation owns it.
- Target Python `>=3.9`.
- To add new dependencies: FIRST declare them in an OpenSpec change proposal
  with rationale.
- Maintain compatibility with both `pip` and `uv`. Use only PEP 621 standard
  fields in `pyproject.toml`.
- Prefer minimal changes over broad refactors.
- Do not add compatibility layers without a concrete need.
- If a commit is required, format the message according to Conventional Commits.
- Never cite plans, task numbers, or change slugs in comments, docstrings,
  tests, or docs — nobody looks them up.
- Every module should export only the public API via `__all__`.

<!-- #endregion SECTION_Dev_Rules -->

<!-- #region SECTION_OpenSpec_Rule -->

## OpenSpec Rule

Any code, configuration, CLI, workflow, engine contract, cloud behavior, DB
schema, or operational behavior change must consult `openspec/specs/` before
implementation and update the relevant OpenSpec requirements in the same change.

Use `openspec/changes/` proposals for behavior-changing work before
implementation.

If the user requests changes outside the OpenSpec workflow, offer to use
OpenSpec via `/opsx-propose`, but do not block or refuse the requested work.

<!-- #endregion SECTION_OpenSpec_Rule -->

<!-- #region SECTION_ADR_Rule -->

## Architectural Decisions Rule

Architectural trade-offs (module boundaries, data ownership, protocols,
tech/library selection, security model, failure/error handling,
identity/lifecycle design, dependency direction) must consult `docs/decisions/`
before implementation.

If a change introduces a new architectural trade-off with viable alternatives,
record it as a new ADR in `docs/decisions/` using `_template.md`. Sequential
numbering starts at the next free slot; no numbers are reserved. Bug fixes, file
relocations, test additions, spec maintenance, and feature work are not ADRs.

<!-- #endregion SECTION_ADR_Rule -->

<!-- #region SECTION_Verification -->

## Verification

- New code should include focused unit tests for core logic and pure behavior;
  test happy paths first, then meaningful edge cases.
- For changes touching DB queries, node lifecycle, SSH interaction, or
  orchestrator flow, also add or update integration/e2e tests per the relevant
  OpenSpec specs.
- Run tests: `uv run pytest -m unit`, `uv run pytest -m integration`,
  `uv run pytest -m e2e`. Assume Docker is available and running, no pre-flight
  checks.
- Static checks: `uv run zuban check`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run lint-imports`
- Spec validation: `openspec validate --all --json` must pass after creating a
  change proposal and after any modification to `openspec/specs/`, and also
  after archiving or syncing changes.
- Use testcontainers for integration and e2e tests (PostgreSQL, SSH). Avoid only
  uncontrolled production resources (real cloud accounts, production DBs, live
  SSH servers) unless explicitly requested and configured.

<!-- #region SECTION_Logging -->

### Logging & Verification

Structured logs = primary observability. Block boundary log entries declare
what code assumes at that point. Runtime behavior traceable back to contract.

**Trace method.** Emit `logger.debug("BLOCK", extra={"k": v, ...})` at block
boundaries. The positional block marker is the debug message; structured fields
are the flat `extra` dict (no nested sentinel, no wrapper function). Structured
fields preferred; redact secrets. Missing trace logging on critical branches =
verification defect.

**Logger binding.** Modules bind loggers via stdlib:

```python
import logging

logger = logging.getLogger(__name__)
```

**Module-local logger names.** Modules obtain a logger via
logging.getLogger(**name**), which yields names like
`yascheduler.<dotted.module.path>`.

**Record contract for tests.** Trace records expose `getMessage()` and
structured fields as record attributes. Tests assert the block marker and extra
fields.

**Tests:** deterministic assertions first. Trace/log assertions when trajectory
matters. Module-local tests stay close to module. Update tests when log markers
change intentionally.

<!-- #endregion SECTION_Logging -->

<!-- #endregion SECTION_Verification -->

<!-- #region SECTION_Project -->

## Project

`yascheduler` schedules scientific calculation jobs on SSH machines and
cloud-created nodes. It provides a daemon, CLI tools, a Python client, and an
AiiDA scheduler plugin.

### Core Flow

1. A client or `yasubmit` creates a DB task with status `TO_DO`.
2. The daemon connects enabled nodes from `yascheduler_nodes`.
3. The allocator picks a compatible free node or requests a cloud node.
4. Inputs are uploaded, `spawn` starts remotely, and the task is `RUNNING`.
5. The daemon detects completion, downloads outputs, and marks `DONE`.
6. Idle cloud nodes are disabled and deleted after provider tolerance.

### Structure

Hexagonal architecture:
domain (no external deps) <- application <- infra <- entrypoints.

```txt
yascheduler/
├── entrypoints/  # drivers: cli, entrypoints, public API, DI
├── infra/        # driven: PSQL schema, UoW, ssh, cloud adapters, webhooks
├── application/  # use cases, orchestrator, message bus
├── domain/       # entities, ports, events, exceptions
└── shared/       # shared kernel
```

<!-- #endregion SECTION_Project -->

<!-- #region RULES_REPEATED -->

<critical_rules>
<rule>Follow OpenSpec, YAGNI, DRY, KISS, SOLID</rule>
<rule>Top-down: requirements → module contract → contracts → stubs → code</rule>
<rule>Stable public interfaces; DB schema changes require migrations</rule>
<rule>pip+uv compatible</rule>
<rule>Minimal changes; NEVER compatibility layers without concrete need</rule>
<rule>Conventional Commits when committing</rule>
<rule>Consult openspec/specs, update specs; use openspec/changes for proposals</rule>
<rule>Consult docs/decisions/ before architectural work; new trade-off → new ADR</rule>
<rule>Offer /opsx-propose if outside OpenSpec, NEVER block requested work</rule>
<rule>Unit tests for core logic; integration/e2e tests for DB, SSH</rule>
<rule>Run: uv run pytest -m unit / -m integration / -m e2e (assume Docker running)</rule>
<rule>Static checks: uv run zuban check, ruff check, ruff format --check, lint-imports</rule>
<rule>openspec validate --all --json must pass after any spec changes</rule>
<rule>Use testcontainers; avoid real production resources unless explicitly configured</rule>
<rule>Structured logging: `logger.debug("BLOCK", extra={...});`,
test log records.</rule>
<rule>Hexagonal architecture; adhere to yascheduler/ structure</rule>
</critical_rules>

<!-- #endregion RULES_REPEATED -->
