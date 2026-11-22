# ARCHITECTURE.md — yascheduler

> **Decision records**: architectural trade-offs (module boundaries, data
> ownership, protocols, tech/library selection, failure semantics) live in
> `docs/decisions/` as ADRs. Consult that set before architectural work;
> record new architectural trade-offs as a new ADR using `_template.md`.

---

## 1. Overview

yascheduler schedules scientific calculation jobs on SSH machines and
cloud-created nodes. It ships a daemon, six CLI tools, a Python client, and an
AiiDA scheduler plugin.

The codebase follows a **hexagonal (ports-and-adapters) architecture** with a
strict, import-linter-enforced layer order:

```txt
yascheduler.entrypoints   →  driving adapters + composition root (outermost)
yascheduler.infra         →  driven adapters: persistence, SSH, cloud, notifier
yascheduler.application   →  use cases, orchestrator, UoW boundary, message bus
yascheduler.domain        →  entities, ports, events, exceptions (stdlib only)
yascheduler.shared        →  shared kernel consumed by ≥2 layers
```

The contract is declared in `[tool.importlinter]` (`pyproject.toml`) as the
"Clean architecture layers" contract and is enforced by `uv run lint-imports`.
`domain` imports nothing from `yascheduler`. `application` imports `domain`
only. `infra` imports `domain`/`application`. `entrypoints` (which contains the
composition root `di.py`) may import everything below.

A frozen-dataclass domain, async I/O ports, and a single composition root that
wires the graph per entry point are the load-bearing ideas of the design.

```txt
                    ┌─────────────────────────────────────────┐
   CLI / client ───▶│  entrypoints                            │
   AiiDA / daemon   │  client.py · cli/ · di.py · config*.py  │
                    └──────────────┬──────────────────────────┘
                                   │ wires
              ┌────────────────────┴────────────────────┐
              ▼                                          ▼
   ┌─────────────────────┐                  ┌───────────────────────┐
   │  application        │  async ports     │  infra (adapters)      │
   │  use cases · orch-  │◀─────uses────────│  pg8000 · asyncssh ·   │
   │  estrator · UoW ·   │                  │  cloud SDKs · webhook  │
   │  message bus        │                  └───────────────────────┘
   └──────────┬──────────┘
              ▼
   ┌─────────────────────┐
   │  domain (stdlib)    │
   │  Task · Node ·      │
   │  Engine · ports ·   │
   │  events · errors    │
   └─────────────────────┘
```

---

## 2. Component Reference

For each subsystem: responsibility, load-bearing contracts, and where the
public surface lives. Method/field enumerations live in `__all__` and
docstrings — not duplicated here.

### 2.1 Domain (`yascheduler/domain/`)

Pure stdlib: frozen-dataclass entities, async `typing.Protocol` ports, a
`DomainError` hierarchy (§4.2), and domain events. No `yascheduler.*`
imports.

**Load-bearing contract — `Task` aggregate.** State changes only through
transition methods (`run`/`reject`/`complete`/`fail`/`abandon`); each
validates the source status, appends the matching `DomainEvent` to the
public `events` tuple, and returns a new frozen instance. There is no
separate `record_event`/`pull_events` API — source-status check and event
payload stay atomically bound (see `model.py` rationale). The UoW reads
`events` directly on commit (§4.4).

**Ports declare `async def`, the domain never awaits** — only the contract
is declared. The `CloudConfig` Protocol is the structural surface every
`ConfigCloud*` DTO satisfies (8 fields incl. `jump_port`).

Public surface: `domain/__init__.py::__all__`.

### 2.2 Application (`yascheduler/application/`)

Async use cases that orchestrate domain objects and adapter ports via
dependency-injected parameters. Every write-side use case is UoW-based;
events flow out through `uow.collect_events()` → `MessageBus` (§4.3, §4.4).

**Task lifecycle use cases** (drive the `TO_DO → RUNNING → DONE` arc of
§3): `submit_task`, `allocate_task` (free-machine match + cloud fallback),
`consume_task` (download, finalise, then best-effort remote cleanup;
defers while any transient SFTP failure remains, finalises on a pass with
none), `abandon_node`
(never-connected cloud-node cleanup: best-effort VM delete + row remove +
tracker discard), `deallocate_nodes` (idle cloud-node disable + delete),
`query_tasks` (read-only, no commit — backs the client and
`yastatus`/`yanodes`).

**Daemon plumbing** (used by `Orchestrator`, §3): `AbstractUnitOfWork`
Protocol, `MessageBus`, `AllocationTracker` (in-flight cloud-allocation
dedup, owned by the orchestrator and injected into allocate/consume),
`UniqueQueue` (deduplicating async queue — the same task/node is not
queued twice under concurrent producers).

Public surface: `application/__init__.py::__all__` re-exports
`AbstractUnitOfWork`, `Orchestrator`, `MessageBus`, `AllocationTracker`,
`submit_task`, `query_tasks`, `abandon_node`.

### 2.3 Persistence Adapter (`yascheduler/infra/persistence/`)

pg8000 backing for `TaskRepository`/`NodeRepository` and
`AbstractUnitOfWork`. Repositories run one SQL file per operation in a
thread pool and map rows to domain entities. A `PostgresUnitOfWork` holds
one connection, opens a transaction on enter, commits or rolls back on
exit, and dispatches domain events **after** commit (§4.4). Each use
case call gets its own UoW.

**SQL in files** (§4.5): one file per query under `sql/{task,node}/`,
lazily cached. `task/update_by_id.sql` and `task/update_status.sql` use
`RETURNING task_id` so a 0-row outcome is detectable and raises
`TaskRowNotFoundError` — a programming-error / contract precondition
violation (callers SHALL NOT catch it; not a domain exception).

**Schema + migrations.** `schema.sql` is the full latest DDL snapshot
(every `CREATE TABLE` carries all current columns, no inline `ALTER`s)
and begins with a PL/pgSQL DO block that bootstraps the
`yascheduler_migrations` tracker with three-case logic (fresh DB → seed
to latest; legacy DB with `yascheduler_nodes` → create empty; modern DB
→ no-op). `apply_migrations` is the forward-only runner: scans
`sql/migrations/{prefix_id}_*.{sql,py}` in string-sorted order, applies
each in its own transaction, records in the tracker. `.sql` migrations
run as a multi-statement string; `.py` migrations define exactly one
`Migration` subclass instantiated with `(config, conn, log)`. No
"down"/rollback path, no generation tool. `prefix_id` uniqueness is
enforced by a unit test. `yainit` runs `apply_schema` then
`apply_migrations`.

**Adding a migration** (documented in the `db-migrations` spec): create the
file under `sql/migrations/`, update the `last_migration` CONSTANT in the
`schema.sql` DO block, and — if the schema changes — update the snapshot
DDL in `schema.sql`. A unit test asserts the CONSTANT matches the latest
migration's `prefix_id`.

### 2.4 SSH Adapter (`yascheduler/infra/ssh/`)

Collection/session split: `SSHMachineRepository` (`repository.py`) owns the
connected-machine collection (keyed by node id) and implements
`MachineRepository`; `SSHMachineSession` (`session.py`) is the per-connection
handle implementing `MachineSession` — it carries domain identity, state
transitions (`occupy`/`release`/`update`), base SSH primitives, and owns its
own monitor task. `_close()` is private, called only by
`SSHMachineRepository.disconnect` — `disconnect(ip)` SHALL NOT touch any
other session's monitor.

**Stateless operations collaborators** (`operations/`: `TaskDeployer`,
`OutputDownloader`, `OccupancyChecker`) — three classes, each taking a
`session: MachineSession` per call. There is no `MachineOperations` port or
facade; the orchestrator receives them as separate dependencies and
resolves a session per tick via `repository.get_session(ip)`. Deployment,
download, and `run_bg`/`upload` are single-attempt (non-idempotent spawn /
`sftp.put` / `sftp.get`); `get_cpu_cores` keeps its backoff (idempotent
read). Download isolates failures per-file and never removes the remote
tree; a separate best-effort `cleanup_remote` removes it only after the
terminal transition is committed.

**Platform detection** (`platform/`): `RemoteMachineAdapter` registry
of Linux/Windows variants; `detect.py` runs adapter checks concurrently;
OS-specific commands in `linux.py`/`windows.py`; callable Protocols and
retry exception tuples in `protocol.py`. The pure `list_private_keys`
the orchestrator consumes via injection lives in `ssh/keys.py`.

### 2.5 Cloud Adapter (`yascheduler/infra/cloud/`)

`CloudProvisionerImpl` (`manager.py`) implements `CloudProvisioner` — pure
cloud-API adapter (create/delete VM, setup, SSH keys), **no DB access**.
`provider_selection.py` picks the best provider by priority, capacity, and
platform support. Provider integration lives in `providers/`
(**Azure, Hetzner, UpCloud, VastAI, Vultr**); `adapters.py` registers
provider factories and resolves them by config prefix. Azure/Hetzner/
UpCloud SDKs are optional extras; VastAI and Vultr use REST APIs with no
extra dependency.

`cloud_configs.py` holds the frozen cloud-config DTOs (one per provider)
that satisfy the domain `CloudConfig` Protocol, each with a per-provider
`connect_grace` default. `cloud_init.py` renders user-data; `ssh_keys.py`
loads or generates keys; `dto.py` carries the `CloudCreateNodeDTO` across
the adapter boundary.

### 2.6 Notifier (`yascheduler/infra/notifier/`)

`webhook_handler(event, http)` is registered on the `MessageBus` for all
five event types. Maps each event to a `WebhookPayload`, POSTs to
`event.webhook_url` over a shared `aiohttp.ClientSession` with fibonacci
backoff and bounded concurrency. Failures are logged and swallowed after
backoff exhausts.

### 2.7 CLI & Daemon (`yascheduler/entrypoints/cli/`)

Six per-command modules (argparse → use cases via DI → formatted output):

| Script | Module | Purpose |
| --- | --- | --- |
| `yasubmit` | `submit.py` | Parse AiiDA script, submit a task |
| `yastatus` | `check_status.py` | Query tasks; verbose mode tails OUTPUT |
| `yanodes` | `show_nodes.py` | List nodes and running tasks |
| `yasetnode` | `manage_node.py` | Add / soft-remove / hard-remove a node |
| `yainit` | `init.py` | Install service unit files and/or apply schema + migrations |
| `yascheduler` | `daemonize.py` | Start the daemon in the foreground |

Three daemon launchers (`daemonize.py`, `daemon_systemd.py`,
`daemon_sysv.py`) share the daemon core in `daemon_common.py`
(`configure_logger` + `run_daemon`) and argparse helpers in `args.py`.
`daemon_systemd` runs in the foreground (stderr → journald); `daemon_sysv`
opens a `DaemonContext` (double-fork, pidfile); `daemonize` runs in the
foreground for debug/container use. Each launcher registers SIGTERM/SIGINT
handlers that call `orch.stop()`.

### 2.8 Composition Root (`yascheduler/entrypoints/di.py`)

- **`async make_daemon(config, *, clouds=None)`** — wires the full daemon
  graph: `MessageBus` (webhook handler registered for all event types),
  shared aiohttp session, `PostgresUnitOfWork` factory,
  `CloudProvisionerImpl`, **one** `SSHMachineRepository` shared between
  the cloud provisioner and the orchestrator so VM-setup connections are
  visible to the orchestrator (no double-connect), the three stateless
  SSH collaborators, the `AllocationTracker`, and the SSH key list.
  Does not create a DB or run schema migration — the operator runs
  `yainit` first. Accepts pre-built `clouds` for tests.
- **`make_cli_deps(config)`** — lightweight `CLIDeps` container
  (`engines`, `uow_factory`, `submit()`). No SSH/cloud/daemon dependencies.

### 2.9 Public API & AiiDA Plugin

- **`entrypoints/client.py`** — `class Yascheduler`, the real public API.
  `queue_submit_task_async()` → `make_cli_deps()` → `CLIDeps.submit()` →
  `submit_task` over a UoW (no daemon graph). Query methods route through
  `query_tasks`. Sync wrappers use a private `to_sync` helper.
- **`client.py`** (package root) — compat shim re-exporting `Yascheduler`,
  preserving `from yascheduler.client import Yascheduler`.
- **`entrypoints/aiida_plugin.py`** — AiiDA scheduler plugin (`YaScheduler`),
  discovered via `[project.entry-points."aiida.schedulers"]` under the name
  `yascheduler`. Talks to yascheduler over SSH transport (runs
  `yasubmit`/`yastatus` remotely); it does **not** use the `Yascheduler`
  client.
- **`entrypoints/paths.py`** — `CONFIG_FILE`/`LOG_FILE`/`PID_FILE`
  (env-overridable).

### 2.10 Configuration

INI-parsed configuration assembled entirely as **frozen stdlib dataclasses**
(no attrs). `parse_config` (`entrypoints/config_parser.py`) reads the INI
and builds the settings (`LocalSettings`/`RemoteDefaults`), `PostgresDbConfig`,
per-provider `ConfigCloud*` DTOs (union `ConfigCloud`), and per-section
`Engine`s gathered into an `EngineRepository`. `entrypoints/config.py`
bundles them into the `Config` aggregate (`db`, `local`, `remote`,
`clouds`, `engines`), consumed only by the composition root. The INI format
(including `[engine.*]` sections and `%(key)s` interpolation) is a stable
public interface (§4.6).

---

## 3. Daemon & Task Lifecycle

The daemon's runtime work is the `Orchestrator`. `start()` launches four
**producer-consumer loop pairs**, each wired through a de-duplicating
`UniqueQueue` (keyed so the same task/node is not processed twice):

| Loop | Producer scans | Consumer does | Limit knob |
| --- | --- | --- | --- |
| **Connect** | enabled nodes | connect newly enabled machines | `conn_machine` |
| **Allocate** | `TO_DO` tasks | engine → free machine → cloud | `allocate` |
| **Consume** | `RUNNING` tasks | completion check; if done → finalise | `consume` |
| **Deallocate** | idle free machines | disable + delete sweep | `deallocate` |

Per-loop concurrency limits and queue sizes come from `LocalSettings`; the
sleep interval is the minimum engine sleep interval. Shutdown is
cooperative: SIGTERM/SIGINT → `orch.stop()` (idempotent, exception-safe),
which drains the queues, cancels workers, then stops the cloud
provisioner and disconnects all machines. `run_daemon` wraps
`await orch.start()` in `try/finally: await orch.stop()` so cleanup runs
on every exit path (normal `start()` return, `start()` exception,
signal-driven shutdown where the handler's `stop()` runs first and the
`finally`'s `stop()` is an idempotent no-op).

**Cloud fallback** (`allocate_task`): if no free compatible machine exists,
the allocation tracker dedups the request; under a shared lock it runs a
capacity check, inserts a temporary node, and commits; the cloud VM is then
provisioned and the temp node replaced with the real one. On any
post-allocate failure the VM is best-effort deallocated and the temp node
cleaned up.

**Never-connected-node cleanup**: a cloud node that is provisioned and
persisted but fails to establish its SSH connection is bounded by the
provider's `connect_grace`. The connect consumer tracks first-seen time
(monotonic) per IP; on each failure it compares elapsed age against
`connect_grace`; on success it clears the entry. Once `connect_grace` is
exhausted it dispatches to `abandon_node` (best-effort VM delete + DB-row
remove + tracker discard for the stuck `TO_DO` task).

**Lost-node detection**: after a bounded number of consecutive consume
passes where a task's machine is gone, the consumer records
`TaskAbandoned`, fails the task, and discards the tracker slot.

**Consume error handling**: `consume_task` defers while any transient SFTP
failure remains, including alongside permanent ones (task stays `RUNNING`,
remote dir preserved, tracker slot retained, re-consumed next tick), and
finalises on a pass whose transient list is empty, using that pass's own
errors (→ `DONE`). The terminal transition is committed first; the remote
dir is then removed by a best-effort cleanup whose failure is logged and
cannot change the outcome. An in-flight consume guard prevents two
workers from concurrently finalising the same `RUNNING` task across
overlapping producer cycles.

```txt
submit_task ──▶ TO_DO ──allocate_task──▶ RUNNING ──consume_task──▶ DONE
                  │                          │
                  │              cloud fallback (tracker + lock)
                  │                          │
                  ├──── never-connected cloud node (connect_grace) ──▶ abandon_node ──▶ TO_DO (re-allocate)
                  │
                  └──────── TaskAbandoned (lost-node passes) ───────────────────▶ DONE
```

---

## 4. Key Design Topics

### 4.1 Sync Domain / Async Adapters

- Domain methods are synchronous — compute, validate, return.
- I/O ports declare `async def`; the domain never awaits, it only declares the
  contract.
- Use cases are `async def` — they await repository calls, pass results to
  sync domain services, await persistence back.
- pg8000 is synchronous; runs in a `ThreadPoolExecutor` inside the
  persistence adapter.

### 4.2 Exception Hierarchy

```txt
DomainError                              (domain/exceptions.py)
├── ValidationError
│   ├── UnsupportedEngineError
│   └── MissingInputFileError
├── TaskError
│   ├── TaskNotTodoError
│   └── TaskNotRunningError
├── MachineBusyError
├── NodeRowNotFoundError
├── SchedulingError
│   ├── NoCompatibleNodeError
│   └── CloudCapacityExhaustedError
├── MachineConnectionError
└── CloudError
    ├── CloudAllocateError
    └── CloudSetupError

UnitOfWorkNotInitializedError            (infra/persistence/exceptions.py)
TaskRowNotFoundError                      (infra/persistence/exceptions.py,
                                           sibling of the above — programming-error,
                                           not a domain exception; raised by the
                                           task repository on a 0-row UPDATE)
```

`DomainError` subclasses carry domain context (task_id, engine name, ip). Use
cases catch `ValidationError` → mark the task with an error, `SchedulingError`
→ retry later, infrastructure failures → log and retry.

### 4.3 Unit of Work

- Each use case call gets a fresh UoW via the injected factory.
- The UoW manages the transaction: `commit()` on success, `rollback()` on
  exception. Repositories within one UoW share one connection and one
  transaction.
- Transition methods append events to `Task.events` inline (no separate
  `record_event` API — source-status check and event payload stay
  atomically bound). On `uow.commit()`, the UoW reads `task.events` from
  saved aggregates via `collect_events()` and dispatches them through the
  `MessageBus` **after** the commit succeeds — so notifications never fire
  for rolled-back work.

### 4.4 Domain Events

```txt
Task transition methods ──append──> events tuple (public field)
                                            │
PostgresUnitOfWork.commit() ─collect_events┘
                  │
                  ▼
           MessageBus.dispatch(events)
                  │
                  ▼  (per event type)
         webhook_handler (notifier adapter)
                  │
                  ▼
       HTTP POST (fibonacci backoff, bounded concurrency)
```

Events decouple side effects from use cases. Transition methods emit events
as part of state changes; the message bus dispatches them to handlers
registered by the composition root. Adding a side effect (metrics, audit
log) means registering a new handler — no use case changes.

### 4.5 SQL in Files

- One file per query, named `entity/operation.sql`.
- Lazy-loaded and cached; the query text is a plain string.
- Parameter placeholders use pg8000 `:param` style.
- Schema DDL lives in `sql/schema.sql`; applied by `apply_schema()`.
  `schema.sql` is the **full latest snapshot** (all current columns in the
  `CREATE TABLE`s, no inline `ALTER`s) and begins with a DO block that
  bootstraps the `yascheduler_migrations` tracker.
- Schema **evolution** is expressed via migration files under
  `sql/migrations/` (`{prefix_id}_{rest}.sql` or `.py`), applied by
  `apply_migrations()`. `yainit` runs `apply_schema` then `apply_migrations`.
  See §2.3 for the migration model and edit procedure.
- `TaskRepository.save(task)` runs `task/update_by_id.sql` with
  `RETURNING task_id`; a 0-row outcome raises `TaskRowNotFoundError`
  (predecessor-violation, not a domain error). `update_status` uses
  `task/update_status.sql` with the same `RETURNING` guard.

### 4.6 Public API Stability

- The facade `class Yascheduler` lives in `entrypoints/client.py`; `client.py`
  at the package root is a compat shim re-exporting it.
- CLI commands (`yasubmit`, `yastatus`, `yanodes`, `yasetnode`, `yainit`,
  `yascheduler`) preserve their user-facing behavior.
- INI config format (including `[engine.*]` sections and `%(key)s`
  interpolation) is preserved.
- DB schema (`schema.sql`) is preserved; schema changes require migrations.
- The AiiDA plugin entry point is preserved under the name `yascheduler`.

### 4.7 Orchestrator & Daemon Resilience

The daemon self-heals through transient failures and shuts down without
leaking resources.

- **Producer/consumer error resilience**: a producer failure (DB timeout,
  gateway error) is logged and the loop continues on the next sleep tick;
  graceful shutdown (`CancelledError`) still propagates to the drain path.
  A consumer failure on one message is logged and the worker continues with
  the next message; the item is still dequeued on raise. This covers the
  `TaskRowNotFoundError` from the task-abandon path, which races the row's
  lifetime.
- **Idempotent, exception-safe `stop()`**: the cleanup body runs exactly once
  across concurrent/interleaved/repeated callers. Each cleanup step (stop
  clouds, disconnect all machines, close the shared HTTP session) is
  isolated so one failing step cannot skip the others.
- **`run_daemon` cleanup guarantee**: `await orch.start()` is wrapped so
  cleanup runs on every exit path (normal return, exception, signal-driven
  shutdown where the handler's `stop()` runs first and the finally's
  `stop()` is an idempotent no-op).

---

## 5. Project Structure

```txt
yascheduler/
├── entrypoints/                 # driving adapters + composition root (outermost layer)
│   ├── client.py                #   Yascheduler facade (real public API)
│   ├── paths.py                 #   CONFIG_FILE / LOG_FILE / PID_FILE
│   ├── aiida_plugin.py          #   AiiDA scheduler plugin (SSH transport)
│   ├── di.py                    #   composition root: make_daemon / make_cli_deps
│   ├── config.py                #   Config aggregate (frozen dataclass)
│   ├── config_parser.py         #   parse_config (INI → frozen dataclasses)
│   ├── _config_utils.py         #   ConfigWarning / unknown-key warnings
│   └── cli/                     #   six CLI commands + three daemon launchers
│       ├── daemon_common.py     #     shared daemon core (configure_logger, run_daemon)
│       └── args.py              #     shared argparse helpers
├── infra/                       # driven adapters
│   ├── persistence/             #   pg8000: repos, UoW, schema, SQL files
│   ├── ssh/                     #   asyncssh
│   ├── cloud/                   #   CloudProvisionerImpl + providers/ (5 providers)
│   └── notifier/                #   webhook handler
├── application/                 # use cases, orchestrator, UoW, message bus
├── domain/                      # entities, ports, events, exceptions (stdlib only)
└── shared/                      # shared kernel (typing shims, retry, log formatter)
```

---

## 6. Testing Strategy

Tests are split into three pytest markers (`unit`, `integration`, `e2e`):

- **Unit** (`tests/unit`) — use cases driven by in-memory fakes for every port
  (`TaskRepository`, `NodeRepository`, `MachineRepository`,
  `MachineSession`, `CloudProvisioner`). No real DB,
  SSH, or cloud.
- **Integration** (`tests/integration`) — persistence and use cases against
  real PostgreSQL and Docker SSH servers via `testcontainers[postgres]`.
- **End-to-end** (`tests/e2e`) — full task lifecycle
  (`TO_DO → RUNNING → DONE`), node deallocation, and webhook dispatch against
  real PostgreSQL and SSH containers.

Run with `uv run pytest -m unit|integration|e2e`. Static checks:
`uv run zuban check`, `uv run ruff check .`, `uv run ruff format --check .`,
`uv run lint-imports`.
