# Contributing to yascheduler

Quick-start for contributors: set up a dev environment, run the daemon, write
and test code following the project conventions.

## Prerequisites

- Python ≥ 3.9
- [uv](https://docs.astral.sh/uv/) (package manager)
- Docker (for the dev environment and integration/e2e tests)

## Dev environment

`dev.py` at the repository root manages a self-contained dev sandbox:
PostgreSQL + an sshd target node in containers, with the daemon running on the
host so you can set breakpoints and iterate quickly.

### Start

```sh
./dev.py up
```

On first run this will:

1. Generate an SSH keypair under `.run/ssh/` (gitignored).
2. Render `yascheduler.conf` at the repo root (gitignored) — merging dev
  defaults into `[db]`, `[local]`, `[remote]`, and `[clouds]` **without
  clobbering** any engines or cloud sections you may have added by hand.
3. Start `postgres:16-alpine` (port `15432`) and `serversideup/docker-ssh`
  (port `2222`) containers.
4. Apply the DB schema + migrations via `yainit --schema`.
5. Register the sshd container as a scheduler node via `yasetnode`.
6. Start the daemon in the foreground at `DEBUG` log level (`Ctrl-C` to stop).

Subsequent runs skip bootstrap if the containers are already healthy and jump
straight to the daemon.

### Other commands

```sh
./dev.py down          # stop + remove containers (DB volume preserved)
./dev.py reinit        # wipe DB volume and re-bootstrap (clean slate)
./dev.py run yanodes   # run any CLI tool against the dev DB
./dev.py run yastatus
./dev.py run yasubmit --engine test_shell --payload '{"foo": 1}'
```

`./dev.py run <tool>` is just `uv run <tool>` with `YASCHEDULER_CONF_PATH`
pointing at the dev config — use it for any of the CLI entry points
(`yanodes`, `yastatus`, `yasubmit`, `yasetnode`, ...).

### What lives where

| Path | Purpose | Tracked? |
| --- | --- | --- |
| `dev.py` | dev environment manager | yes |
| `yascheduler.conf` | generated dev config | no (gitignored) |
| `.run/` | SSH keys, sample engine, runtime data | no (gitignored) |

## Tests

Tests are split into three tiers via pytest markers:

```sh
uv run pytest -m unit        # pure logic, no external services
uv run pytest -m integration # PostgreSQL via testcontainers
uv run pytest -m e2e         # PostgreSQL + SSH pool via testcontainers
```

Docker is assumed to be running — no pre-flight checks are performed.
Integration and e2e tests use `testcontainers` and spin up their own
short-lived containers, independent of the `dev.py` sandbox.

## Static checks

The project uses pre-commit for formatting and linting. Install hooks once:

```sh
uv run prek install
```

After that, hooks run automatically on `git commit`. To run all checks
manually:

```sh
uv run ruff check .
uv run ruff format --check .
uv run zuban check
uv run lint-imports
```

Markdown, TOML, and SQL have their own formatters/linters (`mdlint`, `tombi`,
`sqlfluff`) wired into the same pre-commit config.

## Development conventions

### Architecture

The codebase follows a hexagonal (ports-and-adapters) architecture with a
strict, import-linter-enforced layer order:

```txt
entrypoints  →  driving adapters + composition root (outermost)
infra        →  driven adapters: persistence, SSH, cloud, notifier
application  →  use cases, orchestrator, UoW boundary, message bus
domain       →  entities, ports, events, exceptions (stdlib only)
shared       →  shared kernel
```

`domain` imports nothing from `yascheduler`. `application` imports `domain`
only. `infra` imports `domain`/`application`. `entrypoints` wires everything
together. This contract is enforced by `uv run lint-imports`.

### Top-down approach

Before writing code:

1. Define module contracts (purpose, scope, keywords) in a `# region
   MODULE_CONTRACT` block.
2. Specify contracts for public classes, methods, and functions.
3. Create stubs.
4. Implement inside the contracted regions.

### Public interface stability

The following are stable public interfaces — changes require care and may
require migrations:

- CLI commands (`yainit`, `yascheduler`, `yanodes`, `yasetnode`, `yastatus`,
  `yasubmit`).
- The Python client (`yascheduler.Yascheduler`).
- INI config format (`yascheduler.conf`).
- DB schema — **schema changes MUST include migrations** under
  `yascheduler/infra/persistence/sql/migrations/`.

### Dependencies

- Target Python `>=3.9`.
- Maintain compatibility with both `pip` and `uv` — use only PEP 621 standard
  fields in `pyproject.toml`.
- **Never modify `pyproject.toml` `version`** — release automation owns it.
- To add a new dependency, first declare it in an OpenSpec change proposal
  with rationale.

### Logging

Structured logs are the primary observability mechanism. Emit
`logger.debug("BLOCK", extra={...})` at block boundaries — the positional
message is the block marker, the flat `extra` dict carries structured fields.
Bind loggers via `logging.getLogger(__name__)` (yields
`yascheduler.<dotted.module.path>`). Tests can assert on log records.

### Commits

Follow [Conventional Commits](https://www.conventionalcommits.org/). The
project uses `commitizen` for automated versioning and changelog generation.

### Release automation

A push to `master` in `tilde-lab/yascheduler` runs the draft workflow with full
Git history and the Commitizen version from `uv.lock`. The `uv` version provider
updates `pyproject.toml` and the project's entry in `uv.lock`; the pre-bump hook
stages the lockfile in the same commit as the version and changelog.
Dependencies are not upgraded by the bump. No new commits or no eligible changes
means no bump and no draft.

The workflow validates the lockfile and builds/checks distributions before
atomically pushing the bump commit and tag, then creates a draft release.
Publishing that draft triggers `.github/workflows/release.yml`. A manual retry
must select an already-published release tag matching the package version;
branches and drafts are rejected. PyPI Trusted Publishing must authorize owner
`tilde-lab`, repository `yascheduler`, workflow `release.yml`, environment
`pypi`.

Commitizen detects breaking changes from `!` in a Conventional Commit header or
from a `BREAKING CHANGE:` footer, including in an empty commit. Preserve that
marker in the final commit message when squash-merging. Preview the next bump
without changing files using `uv run --locked cz bump --dry-run`. Local
regression tests run real bumps in temporary repositories without pushing or
publishing: `uv run pytest -m unit tests/unit/test_release_automation.py`.

## OpenSpec

Behavior-changing work (code, config, CLI, DB schema, operational behavior)
should consult `openspec/specs/` before implementation and update the
relevant requirements in the same change. Use `openspec/changes/` proposals
for behavior-changing work before implementation.

If you're working outside the OpenSpec workflow, that's fine — but consider
opening a proposal for non-trivial changes. After any modification to
`openspec/specs/`, `openspec validate --all --json` must pass.

## Architectural decisions

Architectural trade-offs (module boundaries, data ownership, protocols,
tech/library selection, security model, failure/error handling,
identity/lifecycle design, dependency direction) are recorded as ADRs in
`docs/decisions/`. Consult that set before architectural work. If a change
introduces a new trade-off with viable alternatives, add a new ADR using
`docs/decisions/_template.md`; numbering starts at the next free slot.

Bug fixes, file relocations, test additions, spec maintenance, and feature work
are not ADRs.

## Project structure

```txt
yascheduler/
├── entrypoints/  # drivers: cli, entrypoints, public API, DI
├── infra/        # driven: PSQL schema, UoW, ssh, cloud adapters, webhooks
├── application/  # use cases, orchestrator, message bus
├── domain/       # entities, ports, events, exceptions
└── shared/       # shared kernel
```

See `docs/ARCHITECTURE.md` for the full architectural rationale.
