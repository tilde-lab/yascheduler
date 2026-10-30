"""Integration tests for persistence adapter repositories and Unit of Work."""
# region MODULE_CONTRACT
# PURPOSE: Integration tests for persistence adapter against real PostgreSQL via testcontainers.
# SCOPE: PostgresTaskRepository CRUD, PostgresNodeRepository CRUD, PostgresUnitOfWork commit/rollback.
# KEYWORDS: PostgresTaskRepository, PostgresNodeRepository, commit, rollback
# endregion MODULE_CONTRACT

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pg8000.native
import pytest

from yascheduler.application.message_bus import MessageBus
from yascheduler.domain.exceptions import NodeRowNotFoundError
from yascheduler.domain.model import (
    NewNode,
    NewTask,
    Node,
    NodeId,
    TaskId,
    Todo,
)
from yascheduler.domain.model import (
    TaskStatus as DomainTaskStatus,
)
from yascheduler.infra.persistence import PostgresDbConfig
from yascheduler.infra.persistence.postgres import (
    PostgresNodeRepository,
    PostgresTaskRepository,
)
from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

# ====================================================================
# PostgresTaskRepository CRUD
# ====================================================================


async def test_repo_task_insert_and_get(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """Insert a task via repo, get it back, verify all fields including JSONB roundtrip."""
    repo = PostgresTaskRepository(pg_conn, pg_executor)

    new_task = NewTask(
        label="test-task",
        engine="fleur",
        local_folder="/l",
        webhook_url="https://hook.example.com",
        extra={"param": 42},
    )
    inserted = await repo.insert(new_task)
    assert inserted.task_id.value >= 1
    assert inserted.label == "test-task"
    assert inserted.status == DomainTaskStatus.TO_DO
    assert inserted.engine == "fleur"
    # local_folder is a status-independent Task field (D6): the NewTask value
    # is persisted at insert and carried through to the consume step, then
    # overwritten by complete/fail with the actual download path.
    assert inserted.local_folder == "/l"
    assert inserted.webhook_url == "https://hook.example.com"
    assert inserted.extra["param"] == 42
    assert inserted.webhook_custom_params == {}
    assert inserted.state.remote_folder is None

    # Set remote_folder post-insert on the TO_DO state to verify round-trip
    inserted = replace(inserted, state=Todo(remote_folder="/r"))
    await repo.save(inserted)

    # Retrieve by ID
    retrieved = await repo.get(inserted.task_id)
    assert retrieved is not None
    assert retrieved.task_id == inserted.task_id
    assert retrieved.label == inserted.label
    assert retrieved.engine == "fleur"
    assert retrieved.state.remote_folder == "/r"
    assert retrieved.extra["param"] == 42


async def test_repo_task_get_none(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """get() returns None for non-existent task."""
    repo = PostgresTaskRepository(pg_conn, pg_executor)
    assert await repo.get(TaskId(99999)) is None


async def test_repo_task_save_updates(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """Save updates an existing task's fields via update_by_id."""
    task_repo = PostgresTaskRepository(pg_conn, pg_executor)
    node_repo = PostgresNodeRepository(pg_conn, pg_executor)
    task = await task_repo.insert(NewTask(label="initial", engine="fleur"))
    node = await node_repo.insert(NewNode(hostname="10.0.0.1", ncpus=2, enabled=True))

    # Build a CHECK-valid RUNNING task via task.run.
    # A bare Task(status=RUNNING) with NULL allocated_node_id/remote_folder is
    # rejected by the task_status_field_invariants CHECK.
    updated = task.run(node.node_id, "/r")
    updated = replace(updated, label="renamed")
    await task_repo.save(updated)

    retrieved = await task_repo.get(task.task_id)
    assert retrieved is not None
    assert retrieved.label == "renamed"
    assert retrieved.status == DomainTaskStatus.RUNNING


async def test_repo_task_list_by_status(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """list_by_status filters correctly."""
    task_repo = PostgresTaskRepository(pg_conn, pg_executor)
    node_repo = PostgresNodeRepository(pg_conn, pg_executor)
    t1 = await task_repo.insert(NewTask(label="todo", engine="fleur"))
    node = await node_repo.insert(NewNode(hostname="10.0.0.1", ncpus=2, enabled=True))
    t2 = await task_repo.insert(NewTask(label="running", engine="fleur"))
    # CHECK-valid RUNNING via task.run.
    t2 = t2.run(node.node_id, "/r")
    await task_repo.save(t2)
    t2_id = t2.task_id

    todos = await task_repo.list_by_status({DomainTaskStatus.TO_DO})
    assert len(todos) == 1
    assert todos[0].task_id == t1.task_id

    running = await task_repo.list_by_status({DomainTaskStatus.RUNNING})
    assert len(running) == 1
    assert running[0].task_id == t2_id


async def test_repo_task_count_by_status(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """count_by_status returns correct aggregates."""
    repo = PostgresTaskRepository(pg_conn, pg_executor)
    await repo.insert(NewTask(label="t1", engine="fleur"))
    await repo.insert(NewTask(label="t2", engine="fleur"))
    t3 = await repo.insert(NewTask(label="t3", engine="fleur"))
    # Reach DONE via a legitimate transition (reject from TO_DO).
    t3_done = t3.reject("test done")
    await repo.save(t3_done)

    counts = await repo.count_by_status()
    assert counts.get(DomainTaskStatus.TO_DO) == 2
    assert counts.get(DomainTaskStatus.DONE) == 1


async def test_repo_task_list_running_and_todo(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """list_running/list_todo return only the matching state."""
    task_repo = PostgresTaskRepository(pg_conn, pg_executor)
    node_repo = PostgresNodeRepository(pg_conn, pg_executor)
    node = await node_repo.insert(NewNode(hostname="10.0.0.1", ncpus=2, enabled=True))
    t_todo = await task_repo.insert(NewTask(label="todo", engine="fleur"))
    t_running = await task_repo.insert(NewTask(label="running", engine="fleur"))
    t_running = t_running.run(node.node_id, "/r")
    await task_repo.save(t_running)
    t_done = await task_repo.insert(NewTask(label="done", engine="fleur"))
    t_done = t_done.reject("done")
    await task_repo.save(t_done)

    todos = await task_repo.list_todo()
    running = await task_repo.list_running()
    assert [t.task_id for t in todos] == [t_todo.task_id]
    assert [t.task_id for t in running] == [t_running.task_id]
    # All elements have the matching state type.
    assert all(t.status is DomainTaskStatus.TO_DO for t in todos)
    assert all(t.status is DomainTaskStatus.RUNNING for t in running)


async def test_repo_task_insert_returns_created_updated_at(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """Insert returns Task with created_at and updated_at populated."""
    repo = PostgresTaskRepository(pg_conn, pg_executor)
    task = await repo.insert(NewTask(label="ts-test", engine="fleur"))
    assert task.created_at is not None
    assert task.updated_at is not None
    assert task.created_at <= task.updated_at


async def test_repo_task_save_triggers_updated_at(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """Save (UPDATE) triggers updated_at to advance; created_at unchanged."""
    repo = PostgresTaskRepository(pg_conn, pg_executor)
    task = await repo.insert(NewTask(label="touch-test", engine="fleur"))
    assert task.created_at is not None
    assert task.updated_at is not None
    created_before = task.created_at
    updated_before = task.updated_at

    # Perform an update via save — re-save as a CHECK-valid TO_DO row with a
    # changed label (the trigger fires on any UPDATE; the status value is
    # incidental to this test). A bare Task(status=RUNNING) with NULL
    # allocated_node_id/remote_folder is rejected by task_status_field_invariants.
    updated = replace(task, label="touch-test-renamed")
    await repo.save(updated)

    retrieved = await repo.get(task.task_id)
    assert retrieved is not None
    assert retrieved.created_at == created_before
    assert retrieved.updated_at is not None
    assert retrieved.updated_at > updated_before


async def test_repo_task_list_by_status_enum_cast(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """list_by_status with enum-label cast works."""
    repo = PostgresTaskRepository(pg_conn, pg_executor)
    t1 = await repo.insert(NewTask(label="enum-todo", engine="fleur"))
    t2 = await repo.insert(NewTask(label="enum-done", engine="fleur"))
    t2_done = t2.reject("done")
    await repo.save(t2_done)

    todos = await repo.list_by_status({DomainTaskStatus.TO_DO})
    assert len(todos) == 1
    assert todos[0].task_id == t1.task_id


async def test_repo_task_count_by_status_name_lookup(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """count_by_status keys are TaskStatus members accessible via name lookup."""
    repo = PostgresTaskRepository(pg_conn, pg_executor)
    await repo.insert(NewTask(label="n1", engine="fleur"))
    await repo.insert(NewTask(label="n2", engine="fleur"))
    t3 = await repo.insert(NewTask(label="n3", engine="fleur"))
    t3_done = t3.reject("done")
    await repo.save(t3_done)

    counts = await repo.count_by_status()
    # Keys are TaskStatus enum members, accessible by name
    assert counts[DomainTaskStatus.TO_DO] == 2
    assert counts[DomainTaskStatus.DONE] == 1


async def test_repo_task_list_ids_by_node_id_and_status(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """list_ids_by_node_id_and_status filters by allocated_node_id and status."""
    task_repo = PostgresTaskRepository(pg_conn, pg_executor)
    node_repo = PostgresNodeRepository(pg_conn, pg_executor)

    node = await node_repo.insert(NewNode(hostname="10.0.0.99", ncpus=2, enabled=True))
    other = await node_repo.insert(NewNode(hostname="10.0.0.98", ncpus=2, enabled=True))

    # t1: a RUNNING task on `node` (CHECK-valid: allocated_node_id +
    # remote_folder set). The query is exercised against RUNNING, which is
    # the production usage (_remove_node_hard/_soft look up RUNNING tasks).
    t1 = await task_repo.insert(NewTask(label="for-node", engine="fleur"))
    t1 = t1.run(node.node_id, "/r/t1")
    await task_repo.save(t1)

    # t2: a RUNNING task on `other` (should NOT match the node query).
    t2 = await task_repo.insert(NewTask(label="other-node", engine="fleur"))
    t2 = t2.run(other.node_id, "/r/t2")
    await task_repo.save(t2)

    # t3: a DONE task on `node` (CHECK-valid: DONE is unconstrained), built
    # via the real lifecycle so every persisted state satisfies the CHECK.
    t3 = await task_repo.insert(NewTask(label="done-on-node", engine="fleur"))
    t3 = t3.run(node.node_id, "/r/t3")
    await task_repo.save(t3)
    t3_done = t3.complete(local_folder="/l", remote_folder="/r/t3")
    await task_repo.save(t3_done)

    ids = await task_repo.list_ids_by_node_id_and_status(
        node.node_id,
        DomainTaskStatus.RUNNING,
    )
    assert ids == [t1.task_id]


# ====================================================================
# PostgresNodeRepository
# ====================================================================


async def test_repo_node_crud(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """Full node lifecycle through repository."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)

    new_node = NewNode(
        hostname="10.0.0.10",
        ncpus=4,
        enabled=True,
        cloud="aws",
        username="admin",
        port=2222,
    )
    persisted = await repo.insert(new_node)
    assert isinstance(persisted, Node)
    assert isinstance(persisted.node_id, NodeId)
    assert persisted.node_id.value >= 1

    # Get
    retrieved = await repo.get_by_id(persisted.node_id)
    assert retrieved is not None
    assert retrieved.ncpus == 4
    assert retrieved.cloud == "aws"
    assert retrieved.username == "admin"
    assert retrieved.port == 2222
    assert retrieved.enabled is True

    # Disable
    await repo.disable(persisted.node_id)
    n = await repo.get_by_id(persisted.node_id)
    assert n is not None and n.enabled is False

    # Enable
    await repo.enable(persisted.node_id)
    n = await repo.get_by_id(persisted.node_id)
    assert n is not None and n.enabled is True

    # Remove
    await repo.remove(persisted.node_id)
    assert await repo.get_by_id(persisted.node_id) is None


async def test_repo_node_list_filters(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """list_enabled/disabled return correct subsets."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    await repo.insert(NewNode(hostname="10.0.0.1", ncpus=2, enabled=True))
    await repo.insert(NewNode(hostname="10.0.0.2", ncpus=2, enabled=False))

    enabled = await repo.list_enabled()
    disabled = await repo.list_disabled()
    all_nodes = await repo.list_all()

    assert len(enabled) == 1 and enabled[0].hostname == "10.0.0.1"
    assert len(disabled) == 1 and disabled[0].hostname == "10.0.0.2"
    assert len(all_nodes) == 2


async def test_repo_node_update(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """Update persists all mutable fields (including ip — V1 cloud lifecycle relies on this)."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    # Insert with ip="" mirroring the tmp-reservation row (NewNode cloud defaults).
    persisted = await repo.insert(
        NewNode(hostname="", ncpus=None, enabled=False, cloud="aws"),
    )

    # Flip to enabled=True + real hostname/ncpus via update (the V1 single-row lifecycle).
    await repo.update(
        Node(
            node_id=NodeId(persisted.node_id.value),
            hostname="10.0.0.1",
            ncpus=8,
            enabled=True,
            cloud="azure",
            username="admin",
            port=2222,
        ),
    )
    n = await repo.get_by_id(persisted.node_id)
    assert n is not None
    assert n.hostname == "10.0.0.1", (
        "update must persist ip (V1 cloud lifecycle sets real ip via update)"
    )
    assert n.ncpus == 8
    assert n.enabled is True
    assert n.cloud == "azure"
    assert n.username == "admin"
    assert n.port == 2222


async def test_repo_node_update_raises_on_missing_row(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """update raises NodeRowNotFoundError for an absent node_id — the orphan-VM guard."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    ghost = NodeId(999999)
    with pytest.raises(NodeRowNotFoundError) as exc:
        await repo.update(
            Node(
                node_id=ghost,
                hostname="10.0.0.1",
                ncpus=8,
                enabled=True,
                cloud="azure",
                username="admin",
                port=2222,
            ),
        )
    assert exc.value.node_id == ghost


async def test_repo_node_enable_disable_remove_raise_on_missing_row(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """enable/disable/remove raise NodeRowNotFoundError for an absent node_id."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    ghost = NodeId(999998)
    with pytest.raises(NodeRowNotFoundError):
        await repo.enable(ghost)
    with pytest.raises(NodeRowNotFoundError):
        await repo.disable(ghost)
    with pytest.raises(NodeRowNotFoundError):
        await repo.remove(ghost)


async def test_repo_node_tmp_via_insert(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """insert(NewNode(cloud=..., enabled=False)) inserts a tmp row with ip="" and node_id."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    node = await repo.insert(NewNode(cloud="aws", enabled=False))
    assert isinstance(node, Node)
    assert isinstance(node.node_id, NodeId)
    assert node.node_id.value >= 1
    assert node.hostname == ""
    assert node.enabled is False
    assert node.cloud == "aws"
    assert node.username == "root"
    assert node.port == 22


async def test_repo_node_count(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """Count aggregations work correctly."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    await repo.insert(NewNode(hostname="10.0.0.1", ncpus=2, cloud="aws", enabled=True))
    await repo.insert(NewNode(hostname="10.0.0.2", ncpus=2, cloud="aws", enabled=False))
    await repo.insert(
        NewNode(hostname="10.0.0.3", ncpus=2, cloud="azure", enabled=True),
    )

    statuses = await repo.count_by_status()
    assert statuses[True] == 2
    assert statuses[False] == 1


async def test_repo_node_get_by_ids(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """Batch get_by_ids returns only matching nodes keyed by NodeId."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    n1 = await repo.insert(NewNode(hostname="10.0.0.1", ncpus=2, cloud="aws"))
    await repo.insert(NewNode(hostname="10.0.0.2", ncpus=2, cloud="gcp"))
    n3 = await repo.insert(NewNode(hostname="10.0.0.3", ncpus=2, cloud="azure"))

    nodes = await repo.get_by_ids([n1.node_id, n3.node_id, NodeId(99999)])
    assert len(nodes) == 2
    assert nodes[n1.node_id].cloud == "aws"
    assert nodes[n3.node_id].cloud == "azure"
    assert NodeId(99999) not in nodes


async def test_repo_node_get_by_id(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """get_by_id returns node by primary key, None for missing id."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    persisted = await repo.insert(
        NewNode(hostname="10.0.0.20", ncpus=4, enabled=True, cloud="aws"),
    )

    fetched = await repo.get_by_id(persisted.node_id)
    assert fetched is not None
    assert fetched.node_id == persisted.node_id
    assert fetched.hostname == "10.0.0.20"
    assert fetched.ncpus == 4
    assert fetched.cloud == "aws"

    missing = await repo.get_by_id(NodeId(99999))
    assert missing is None


async def test_repo_node_get_by_ids_empty(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """get_by_ids([]) returns empty dict."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    assert await repo.get_by_ids([]) == {}


async def test_repo_node_list_all_ordered_by_node_id(
    pg_conn: pg8000.native.Connection,
    pg_executor: ThreadPoolExecutor,
) -> None:
    """list_all returns nodes ordered by node_id ascending."""
    repo = PostgresNodeRepository(pg_conn, pg_executor)
    await repo.insert(NewNode(hostname="10.0.0.1", ncpus=2))
    await repo.insert(NewNode(hostname="10.0.0.2", ncpus=2))
    await repo.insert(NewNode(hostname="10.0.0.3", ncpus=2))

    all_nodes = await repo.list_all()
    node_ids = [n.node_id.value for n in all_nodes]
    assert node_ids == sorted(node_ids)


# ====================================================================
# PostgresUnitOfWork
# ====================================================================


async def test_uow_integration(
    _db_config: PostgresDbConfig,
    _init_schema: None,
) -> None:
    """UoW creates repos from config, commit persists, exit closes."""
    config: PostgresDbConfig = _db_config

    async with PostgresUnitOfWork(config, MessageBus()) as uow:
        assert uow.tasks is not None
        assert uow.nodes is not None

        # Insert through repo
        new_node = NewNode(hostname="10.0.0.50", ncpus=2, enabled=True)
        persisted = await uow.nodes.insert(new_node)
        await uow.commit()
        assert isinstance(persisted, Node)
        assert isinstance(persisted.node_id, NodeId)
        assert persisted.node_id.value >= 1

        # Verify persisted
        retrieved = await uow.nodes.get_by_id(persisted.node_id)
        assert retrieved is not None
        assert retrieved.hostname == "10.0.0.50"


async def test_uow_rollback_integration(
    _db_config: PostgresDbConfig,
    _init_schema: None,
) -> None:
    """Uncommitted changes are lost on rollback."""
    config: PostgresDbConfig = _db_config

    with pytest.raises(ValueError):
        async with PostgresUnitOfWork(config, MessageBus()) as uow:
            persisted = await uow.nodes.insert(
                NewNode(hostname="10.0.0.99", ncpus=2, enabled=True),
            )
            raise ValueError("simulated error")

    # After rollback, node should NOT exist
    async with PostgresUnitOfWork(config, MessageBus()) as uow:
        n = await uow.nodes.get_by_id(persisted.node_id)
        assert n is None


# ====================================================================
# DB wrapper coverage
# ====================================================================
# Coverage for the DB wrapper (PostgresTaskRepository/PostgresNodeRepository
# through DB._task_repo / DB._node_repo) is provided by test_db_integration.py.
# Tests 8.2-8.4 above cover the repository and UoW layer directly.
# See test_db_integration.py for DB-level integration coverage.
