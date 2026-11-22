"""Integration tests for PostgresUnitOfWork + repositories against real PostgreSQL."""
# region MODULE_CONTRACT
# PURPOSE: Integration tests for PostgresUnitOfWork + repositories against real PostgreSQL via testcontainers.
# SCOPE: Node CRUD, Task CRUD with flat typed fields + JSONB extra, status transitions, UoW-based composition queries (list_by_jobs, get_by_ids), tmp-node lifecycle via insert, migration 003 backfill + constraint drop, migration 004 pre-create table at 002-era schema (so 004 ALTER ADD COLUMN is valid).
# KEYWORDS: PostgresUnitOfWork, Node CRUD, Task CRUD, JSONB, migration
# endregion MODULE_CONTRACT

from collections.abc import Callable

from yascheduler.domain.model import (
    Done,
    NewNode,
    NewTask,
    Node,
    NodeId,
    Task,
    allocated_node_id_of,
    error_of,
)
from yascheduler.domain.model import TaskStatus as DomainTaskStatus
from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

# ---------------------------------------------------------------------------
# Node CRUD
# ---------------------------------------------------------------------------


async def test_add_and_get_node(uow_factory: Callable[[], PostgresUnitOfWork]) -> None:
    """Insert a node and retrieve it; verify all fields match."""
    new_node = NewNode(
        hostname="10.0.0.1",
        username="admin",
        port=2222,
        ncpus=8,
        cloud="azure",
        enabled=True,
    )
    assert new_node.hostname == "10.0.0.1"
    assert new_node.username == "admin"
    assert new_node.port == 2222
    assert new_node.ncpus == 8
    assert new_node.cloud == "azure"
    assert new_node.enabled is True

    async with uow_factory() as uow:
        persisted = await uow.nodes.insert(new_node)
        await uow.commit()
        assert isinstance(persisted, Node)
        assert isinstance(persisted.node_id, NodeId)
        assert persisted.node_id.value >= 1

    async with uow_factory() as uow:
        retrieved = await uow.nodes.get_by_id(persisted.node_id)
        assert retrieved is not None
        assert retrieved.hostname == "10.0.0.1"
        assert retrieved.username == "admin"
        assert retrieved.port == 2222
        assert retrieved.ncpus == 8
        assert retrieved.cloud == "azure"
        assert retrieved.enabled is True


async def test_get_all_nodes_filtering(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """Add enabled and disabled nodes; verify filtered queries."""
    async with uow_factory() as uow:
        await uow.nodes.insert(NewNode(hostname="10.0.0.1", ncpus=None, enabled=True))
        await uow.nodes.insert(NewNode(hostname="10.0.0.2", ncpus=None, enabled=False))
        await uow.commit()

    async with uow_factory() as uow:
        all_nodes = await uow.nodes.list_all()
        assert len(all_nodes) == 2

        enabled = await uow.nodes.list_enabled()
        assert len(enabled) == 1
        assert enabled[0].hostname == "10.0.0.1"

        disabled = await uow.nodes.list_disabled()
        assert len(disabled) == 1
        assert disabled[0].hostname == "10.0.0.2"


async def test_has_node(uow_factory: Callable[[], PostgresUnitOfWork]) -> None:
    """Check has_node for existing and non-existing IPs."""
    async with uow_factory() as uow:
        node = await uow.nodes.insert(NewNode(hostname="10.0.0.1", ncpus=None))
        await uow.commit()

    async with uow_factory() as uow:
        assert bool(await uow.nodes.get_by_id(node.node_id)) is True
        assert await uow.nodes.get_by_id(NodeId(99999)) is None


async def test_enable_disable_node(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """Toggle node enabled status and verify."""
    async with uow_factory() as uow:
        node = await uow.nodes.insert(
            NewNode(hostname="10.0.0.1", ncpus=None, enabled=False),
        )
        await uow.commit()

    async with uow_factory() as uow:
        await uow.nodes.enable(node.node_id)
        await uow.commit()

    async with uow_factory() as uow:
        fetched = await uow.nodes.get_by_id(node.node_id)
        assert fetched is not None
        assert fetched.enabled is True

    async with uow_factory() as uow:
        await uow.nodes.disable(node.node_id)
        await uow.commit()

    async with uow_factory() as uow:
        fetched = await uow.nodes.get_by_id(node.node_id)
        assert fetched is not None
        assert fetched.enabled is False


async def test_remove_node(uow_factory: Callable[[], PostgresUnitOfWork]) -> None:
    """Remove a node and verify it is gone."""
    async with uow_factory() as uow:
        node = await uow.nodes.insert(NewNode(hostname="10.0.0.1", ncpus=None))
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.nodes.get_by_id(node.node_id) is not None
        await uow.nodes.remove(node.node_id)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.nodes.get_by_id(node.node_id) is None


async def test_count_aggregations(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """Verify cloud and status aggregation queries."""
    async with uow_factory() as uow:
        await uow.nodes.insert(
            NewNode(hostname="10.0.0.1", ncpus=None, cloud="azure", enabled=True),
        )
        await uow.nodes.insert(
            NewNode(hostname="10.0.0.2", ncpus=None, cloud="azure", enabled=False),
        )
        await uow.nodes.insert(
            NewNode(hostname="10.0.0.3", ncpus=None, cloud="hetzner", enabled=True),
        )
        await uow.commit()

    async with uow_factory() as uow:
        by_status = await uow.nodes.count_by_status()
        assert by_status[True] == 2
        assert by_status[False] == 1


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


async def test_add_and_get_task(uow_factory: Callable[[], PostgresUnitOfWork]) -> None:
    """Add a task and retrieve it; verify all fields including typed fields and JSONB extra."""
    async with uow_factory() as uow:
        node = await uow.nodes.insert(
            NewNode(hostname="10.0.0.1", ncpus=4, enabled=True),
        )
        task = await uow.tasks.insert(
            NewTask(
                label="calc",
                engine="fleur",
                webhook_custom_params={},
                extra={"param": 42},
            ),
        )
        # Transition to a CHECK-valid RUNNING state via task.run.
        # A TO_DO + allocated_node_id save is rejected by the
        # task_status_field_invariants CHECK (TO_DO requires
        # allocated_node_id IS NULL); production always uses run
        # before save.
        task = task.run(node.node_id, "/r")
        await uow.tasks.save(task)
        await uow.commit()
        assert task.task_id.value >= 1
        assert task.label == "calc"
        assert task.state.allocated_node_id == node.node_id
        assert task.status == DomainTaskStatus.RUNNING
        assert task.engine == "fleur"
        assert task.webhook_custom_params == {}
        assert task.extra == {"param": 42}

    async with uow_factory() as uow:
        retrieved = await uow.tasks.get(task.task_id)
        assert retrieved is not None
        assert retrieved.task_id == task.task_id
        assert retrieved.label == "calc"
        assert allocated_node_id_of(retrieved) == node.node_id
        assert retrieved.status == DomainTaskStatus.RUNNING
        assert retrieved.engine == "fleur"
        assert retrieved.webhook_custom_params == {}
        assert retrieved.extra == {"param": 42}


async def test_task_lifecycle(uow_factory: Callable[[], PostgresUnitOfWork]) -> None:
    """Walk a task through TO_DO -> RUNNING -> DONE and verify each step."""
    async with uow_factory() as uow:
        # Insert a node first so allocate_to(node) can bind a real node_id
        # (the DB FK allocated_node_id REFERENCES yascheduler_nodes(node_id)).
        node = await uow.nodes.insert(
            NewNode(hostname="10.0.0.5", ncpus=4, enabled=True),
        )
        task = await uow.tasks.insert(
            NewTask(
                label="sim",
                engine="fleur",
                webhook_custom_params={},
                extra={"param": 42},
            ),
        )
        await uow.commit()
        assert task.status == DomainTaskStatus.TO_DO
        task_id = task.task_id
        node_id = node.node_id

    async with uow_factory() as uow:
        task = await uow.tasks.get_todo(task_id)
        assert task is not None
        # CHECK-valid RUNNING: task.run sets allocated_node_id + remote_folder.
        updated = task.run(node_id, "/r")
        await uow.tasks.save(updated)
        await uow.commit()

    async with uow_factory() as uow:
        running = await uow.tasks.get_running(task_id)
        assert running is not None
        assert running.status == DomainTaskStatus.RUNNING
        assert running.state.allocated_node_id == node_id

    async with uow_factory() as uow:
        task = await uow.tasks.get_running(task_id)
        assert task is not None
        updated = task.complete(local_folder="/l", remote_folder="/r")
        await uow.tasks.save(updated)
        await uow.commit()

    async with uow_factory() as uow:
        done = await uow.tasks.get(task_id)
        assert done is not None
        assert done.status == DomainTaskStatus.DONE
        assert done.extra == {"param": 42}
        assert done.local_folder == "/l"
        assert done.state.remote_folder == "/r"
        assert allocated_node_id_of(done) == node_id


async def test_set_task_error(uow_factory: Callable[[], PostgresUnitOfWork]) -> None:
    """set_task_error embeds error in typed error field; without error extra is preserved."""
    # With error message
    async with uow_factory() as uow:
        task = await uow.tasks.insert(
            NewTask(label="fail-job", engine="fleur", webhook_custom_params={}),
        )
        await uow.commit()
        task_id = task.task_id

    async with uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        assert task is not None
        updated = Task(
            task_id=task.task_id,
            label=task.label,
            engine=task.engine,
            state=Done(error="crash", allocated_node_id=allocated_node_id_of(task)),
            extra={"key": "val"},
        )
        await uow.tasks.save(updated)
        await uow.commit()

    async with uow_factory() as uow:
        t = await uow.tasks.get(task_id)
        assert t is not None
        assert t.status == DomainTaskStatus.DONE
        assert error_of(t) == "crash"
        assert t.extra == {"key": "val"}

    # Without error message (use a new task for clarity)
    async with uow_factory() as uow:
        task2 = await uow.tasks.insert(
            NewTask(label="fail-job2", engine="fleur", webhook_custom_params={}),
        )
        await uow.commit()
        task2_id = task2.task_id

    async with uow_factory() as uow:
        task2 = await uow.tasks.get(task2_id)
        assert task2 is not None
        updated = Task(
            task_id=task2.task_id,
            label=task2.label,
            engine=task2.engine,
            state=Done(allocated_node_id=allocated_node_id_of(task2)),
            extra={"only": "meta"},
        )
        await uow.tasks.save(updated)
        await uow.commit()

    async with uow_factory() as uow:
        t2 = await uow.tasks.get(task2_id)
        assert t2 is not None
        assert t2.status == DomainTaskStatus.DONE
        assert error_of(t2) is None
        assert t2.extra == {"only": "meta"}


async def test_get_tasks_by_status(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """Filter tasks by status; multiple statuses supported."""
    async with uow_factory() as uow:
        await uow.tasks.insert(NewTask(label="todo", engine="fleur"))
        node = await uow.nodes.insert(
            NewNode(hostname="10.0.0.7", ncpus=2, enabled=True),
        )
        t2 = await uow.tasks.insert(NewTask(label="running", engine="fleur"))
        t3 = await uow.tasks.insert(NewTask(label="done", engine="fleur"))
        # CHECK-valid RUNNING via real domain transitions (a bare
        # Task(status=RUNNING) with NULL allocated_node_id/remote_folder is
        # rejected by task_status_field_invariants).
        await uow.tasks.save(t2.run(node.node_id, "/r"))
        await uow.tasks.save(
            Task(
                task_id=t3.task_id,
                engine="fleur",
                label="done",
                state=Done(),
            ),
        )
        await uow.commit()

    async with uow_factory() as uow:
        todo = await uow.tasks.list_by_status({DomainTaskStatus.TO_DO})
        assert len(todo) == 1
        assert todo[0].label == "todo"

        running = await uow.tasks.list_by_status({DomainTaskStatus.RUNNING})
        assert len(running) == 1
        assert running[0].label == "running"

        done = await uow.tasks.list_by_status({DomainTaskStatus.DONE})
        assert len(done) == 1
        assert done[0].label == "done"

        # Multiple statuses
        multi = await uow.tasks.list_by_status(
            {DomainTaskStatus.TO_DO, DomainTaskStatus.DONE},
        )
        assert len(multi) == 2


async def test_get_tasks_by_jobs(uow_factory: Callable[[], PostgresUnitOfWork]) -> None:
    """Retrieve tasks by array of IDs; only requested IDs returned."""
    async with uow_factory() as uow:
        t1 = await uow.tasks.insert(NewTask(label="a", engine="fleur"))
        await uow.tasks.insert(NewTask(label="b", engine="fleur"))
        t3 = await uow.tasks.insert(NewTask(label="c", engine="fleur"))
        await uow.commit()

    async with uow_factory() as uow:
        results = await uow.tasks.list_by_jobs([t1.task_id, t3.task_id])
        assert len(results) == 2
        ids = {r.task_id for r in results}
        assert ids == {t1.task_id, t3.task_id}


async def test_get_task_ids_by_node_id_and_status(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """Filter task IDs by node_id and status."""
    async with uow_factory() as uow:
        node = await uow.nodes.insert(
            NewNode(hostname="192.168.1.1", ncpus=4, enabled=True),
        )
        node_id = node.node_id
        other_node = await uow.nodes.insert(
            NewNode(hostname="10.0.0.1", ncpus=4, enabled=True),
        )

        t1 = await uow.tasks.insert(NewTask(label="x", engine="fleur"))
        t1 = t1.run(node.node_id, "/r/x")
        await uow.tasks.save(t1)

        ty = await uow.tasks.insert(NewTask(label="y", engine="fleur"))
        ty = ty.run(other_node.node_id, "/r/y")
        await uow.tasks.save(ty)

        # tz: a DONE task allocated to `node`. Build via the real lifecycle
        # (allocate_to + mark_running + with_remote_folder → save → complete →
        # save) so every persisted state satisfies the
        # task_status_field_invariants CHECK (a TO_DO + allocated_node_id save
        # is rejected; DONE is unconstrained).
        tz = await uow.tasks.insert(NewTask(label="z", engine="fleur"))
        tz = tz.run(node.node_id, "/r/z")
        await uow.tasks.save(tz)
        tz_done = tz.complete(local_folder="/l", remote_folder="/r/z")
        await uow.tasks.save(tz_done)
        await uow.commit()

    async with uow_factory() as uow:
        ids = await uow.tasks.list_ids_by_node_id_and_status(
            node_id,
            DomainTaskStatus.RUNNING,
        )
        assert ids == [t1.task_id]


async def test_get_tasks_with_cloud_by_id_status(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """Compose list_by_jobs + get_by_ids to get cloud attribute."""
    async with uow_factory() as uow:
        node = await uow.nodes.insert(
            NewNode(hostname="10.0.0.1", ncpus=None, cloud="azure", enabled=True),
        )
        node_id = node.node_id
        await uow.commit()

    async with uow_factory() as uow:
        t = await uow.tasks.insert(NewTask(label="", engine="fleur"))
        t = t.run(node.node_id, "/r/t")
        await uow.tasks.save(t)

        # t2: a DONE task allocated to `node`, built via the real lifecycle so
        # every persisted state satisfies the task_status_field_invariants
        # CHECK (a TO_DO + allocated_node_id save is rejected; RUNNING requires
        # remote_folder; DONE is unconstrained).
        t2 = await uow.tasks.insert(NewTask(label="", engine="fleur"))
        t2 = t2.run(node.node_id, "/r/t2")
        await uow.tasks.save(t2)
        t2_done = t2.complete(local_folder="/l", remote_folder="/r/t2")
        await uow.tasks.save(t2_done)
        await uow.commit()

    async with uow_factory() as uow:
        tasks = await uow.tasks.list_by_jobs([t.task_id, t2.task_id])
        matching = [task for task in tasks if task.status == DomainTaskStatus.RUNNING]
        assert len(matching) == 1
        assert matching[0].task_id == t.task_id
        node_id = allocated_node_id_of(matching[0])
        assert node_id is not None
        nodes = await uow.nodes.get_by_ids([node_id])
        assert nodes
        node = nodes[node_id]
        assert node.cloud == "azure"


# ---------------------------------------------------------------------------
# PostgreSQL-specific features
# ---------------------------------------------------------------------------


async def test_tmp_node_lifecycle_via_insert(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """insert(NewNode(cloud=..., enabled=False)) creates a tmp-node row; remove(node_id) cleans up."""
    async with uow_factory() as uow:
        node = await uow.nodes.insert(NewNode(cloud="azure", enabled=False))
        await uow.commit()
        assert isinstance(node, Node)
        assert isinstance(node.node_id, NodeId)
        assert node.node_id.value >= 1
        assert node.hostname == ""
        assert node.enabled is False
        assert node.cloud == "azure"
        assert node.username == "root"
        assert node.port == 22

    async with uow_factory() as uow:
        # The tmp row is invisible to list_disabled (ip <> '' SQL filter excludes ip="").
        disabled = await uow.nodes.list_disabled()
        assert all(n.hostname != "" for n in disabled)
        # But visible to list_all (counts toward capacity).
        all_nodes = await uow.nodes.list_all()
        assert any(n.node_id == node.node_id for n in all_nodes)

    async with uow_factory() as uow:
        await uow.nodes.remove(node.node_id)
        await uow.commit()

    async with uow_factory() as uow:
        all_nodes = await uow.nodes.list_all()
        assert all(n.node_id != node.node_id for n in all_nodes)


async def test_migration_003_backfills_prov_ips_and_drops_unique() -> None:
    """Migration 003 backfills prov... → '' and drops yascheduler_nodes_ip_key."""
    from urllib.parse import urlparse

    from testcontainers.postgres import PostgresContainer

    from yascheduler.infra.persistence import PostgresDbConfig, apply_migrations
    from yascheduler.infra.persistence.postgres_schema import apply_schema

    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        url = urlparse(pg.get_connection_url())
        config = PostgresDbConfig(
            user=url.username or "test",
            password=url.password or "test",
            database=url.path.lstrip("/"),
            host=url.hostname or "localhost",
            port=url.port or 5432,
        )

        # Seed a legacy-style DB already at migration 002 (with the UNIQUE
        # constraint, a node_id column, and a prov... row that the old add_tmp
        # would have produced). The tracker is seeded to '002' so apply_migrations
        # only runs 003 (backfill + DROP CONSTRAINT).
        import pg8000.native

        conn = pg8000.native.Connection(
            user=config.user,
            host=config.host,
            database=config.database,
            port=config.port,
            password=config.password,
        )
        try:
            conn.run(
                "CREATE TABLE yascheduler_migrations "
                "(migration_id TEXT PRIMARY KEY, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            )
            conn.run("INSERT INTO yascheduler_migrations (migration_id) VALUES ('002')")
            conn.run(
                "CREATE TABLE yascheduler_nodes ("
                "node_id SERIAL PRIMARY KEY, ip VARCHAR(15) UNIQUE, "
                "port INTEGER DEFAULT 22, username VARCHAR(255) DEFAULT 'root', "
                "ncpus SMALLINT DEFAULT NULL, enabled BOOLEAN DEFAULT TRUE, "
                "cloud VARCHAR(32) DEFAULT NULL)",
            )
            # Two prov... rows: migration 003 must DROP the ip UNIQUE
            # constraint BEFORE backfilling both to '', else the second
            # UPDATE row collides on the still-present UNIQUE index.
            conn.run(
                "INSERT INTO yascheduler_nodes (ip, enabled, cloud) "
                "VALUES ('provabc1234567', FALSE, 'aws')",
            )
            conn.run(
                "INSERT INTO yascheduler_nodes (ip, enabled, cloud) "
                "VALUES ('provxyz7654321', FALSE, 'aws')",
            )
            # Pre-create yascheduler_tasks at the 002-era schema so
            # apply_schema's CREATE TABLE IF NOT EXISTS stays a no-op instead
            # of introducing a fresh current-snapshot tasks table.
            conn.run(
                "CREATE TABLE yascheduler_tasks ("
                "task_id SERIAL PRIMARY KEY, label VARCHAR(256), "
                "metadata JSONB, ip VARCHAR(15), status SMALLINT)",
            )
        finally:
            conn.close()

        # apply_schema is a no-op on the existing tables (CREATE TABLE IF NOT
        # EXISTS); apply_migrations runs exactly 003 (backfill + DROP
        # CONSTRAINT) — upto caps the chain at this era.
        apply_schema(config)
        apply_migrations(config, upto="003")

        conn = pg8000.native.Connection(
            user=config.user,
            host=config.host,
            database=config.database,
            port=config.port,
            password=config.password,
        )
        try:
            conn.run("BEGIN")
            try:
                rows = conn.run(
                    "SELECT ip, enabled, cloud FROM yascheduler_nodes "
                    "WHERE cloud = 'aws'",
                )
            finally:
                conn.run("ROLLBACK")
            # Both prov... rows were backfilled to '' (DROP ran before UPDATE).
            assert len(rows) == 2
            assert all(r[0] == "" for r in rows)
            assert all(r[1] is False for r in rows)

            # The UNIQUE constraint is gone — a duplicate real ip insert succeeds.
            conn.run("BEGIN")
            try:
                conn.run(
                    "INSERT INTO yascheduler_nodes (ip, enabled, cloud) "
                    "VALUES ('10.0.0.99', TRUE, 'aws')",
                )
                conn.run(
                    "INSERT INTO yascheduler_nodes (ip, enabled, cloud) "
                    "VALUES ('10.0.0.99', TRUE, 'hetzner')",
                )
            finally:
                conn.run("ROLLBACK")
        finally:
            conn.close()


async def test_list_filters_empty_ip_in_sql(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    """list_enabled/list_disabled filtering is in SQL, not python (remove-tmp-node-fake-ip)."""
    async with uow_factory() as uow:
        # A real enabled node (has a real ip).
        await uow.nodes.insert(NewNode(hostname="10.0.0.1", ncpus=4, enabled=True))
        # A tmp/pending row (ip="", enabled=False) — excluded by list_disabled SQL.
        await uow.nodes.insert(NewNode(cloud="aws", enabled=False))
        # A real-disabled VM (ip<>"", enabled=False) — included by list_disabled.
        await uow.nodes.insert(NewNode(hostname="10.0.0.2", ncpus=4, enabled=False))
        await uow.commit()

    async with uow_factory() as uow:
        enabled = await uow.nodes.list_enabled()
        # Only the enabled real node; no python post-filter needed (invariant:
        # no enabled row has ip="").
        assert len(enabled) == 1
        assert enabled[0].hostname == "10.0.0.1"

        disabled = await uow.nodes.list_disabled()
        # Only the real-disabled VM (ip <> '' is filtered in SQL); the tmp row
        # with ip="" is excluded at the SQL layer.
        assert len(disabled) == 1
        assert disabled[0].hostname == "10.0.0.2"

        all_nodes = await uow.nodes.list_all()
        # list_all returns everything (including the tmp row with ip="").
        assert len(all_nodes) == 3
