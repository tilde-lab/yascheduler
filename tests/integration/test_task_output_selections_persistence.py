# region MODULE_CONTRACT
# PURPOSE: Verify task-owned output selections survive every persistence read and lifecycle write, and that a pre-change NULL record remains NULL and hydrates as no record.
# SCOPE: insert, get, get_todo, get_running, list_todo, list_running, list_by_jobs, save through TO_DO→RUNNING→DONE; legacy NULL round-trip.
# KEYWORDS: task output selections, postgres, round-trip, lifecycle, legacy NULL
# endregion MODULE_CONTRACT

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from yascheduler.domain import NewNode, NewTask, TaskId

if TYPE_CHECKING:
    from collections.abc import Callable

    import pg8000.native

    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

pytestmark = pytest.mark.integration

__all__: list[str] = []


async def test_output_selections_round_trip_across_all_task_reads_and_lifecycle(
    uow_factory: Callable[[], PostgresUnitOfWork],
) -> None:
    selections = ("OUT.aiida", "**/*.dat")
    async with uow_factory() as uow:
        node = await uow.nodes.insert(
            NewNode(hostname="10.0.0.1", ncpus=2, enabled=True),
        )
        task = await uow.tasks.insert(
            NewTask(label="outputs", engine="fleur", output_selections=selections),
        )
        await uow.commit()
        task_id = task.task_id
        node_id = node.node_id

    assert task.output_selections == selections
    async with uow_factory() as uow:
        todo = await uow.tasks.get_todo(task_id)
        todos = await uow.tasks.list_todo()
        jobs = await uow.tasks.list_by_jobs([task_id])
        await uow.commit()
    assert todo is not None and todo.output_selections == selections
    assert [t.output_selections for t in todos] == [selections]
    assert [t.output_selections for t in jobs] == [selections]

    async with uow_factory() as uow:
        todo = await uow.tasks.get_todo(task_id)
        assert todo is not None
        running = todo.run(node_id, "/remote/outputs")
        await uow.tasks.save(running, expected_status=todo.status)
        await uow.commit()

    async with uow_factory() as uow:
        running = await uow.tasks.get_running(task_id)
        running_tasks = await uow.tasks.list_running()
        await uow.commit()
    assert running is not None and running.output_selections == selections
    assert [t.output_selections for t in running_tasks] == [selections]

    async with uow_factory() as uow:
        running = await uow.tasks.get_running(task_id)
        assert running is not None
        done = running.complete(
            local_folder="/local/outputs", remote_folder="/remote/outputs"
        )
        await uow.tasks.save(done, expected_status=running.status)
        await uow.commit()

    async with uow_factory() as uow:
        reloaded = await uow.tasks.get(task_id)
        jobs = await uow.tasks.list_by_jobs([task_id])
        await uow.commit()
    assert reloaded is not None and reloaded.output_selections == selections
    assert [t.output_selections for t in jobs] == [selections]


async def test_legacy_null_output_selections_hydrates_empty_and_stays_null_on_save(
    uow_factory: Callable[[], PostgresUnitOfWork],
    pg_conn: pg8000.native.Connection,
) -> None:
    rows = pg_conn.run(
        "INSERT INTO yascheduler_tasks (title, engine) "
        "VALUES ('legacy', 'fleur') RETURNING task_id",
    )
    task_id = TaskId(int(rows[0][0]))

    async with uow_factory() as uow:
        task = await uow.tasks.get_todo(task_id)
        assert task is not None
        assert task.output_selections == ()
        await uow.tasks.save(replace(task, label="legacy-touched"))
        await uow.commit()

    assert pg_conn.run(
        "SELECT output_selections IS NULL FROM yascheduler_tasks WHERE task_id = :task_id",
        task_id=task_id.value,
    ) == [[True]]

    async with uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        await uow.commit()
    assert task is not None
    assert task.output_selections == ()
