# region MODULE_CONTRACT
# PURPOSE: Pin the task-output-selection persistence contract at the SQL boundary so every full task read and both write paths carry the recorded selections.
# SCOPE: The five task queries that feed Task hydration include output_selections; insert/update bind it.
# KEYWORDS: task output selections, SQL, round-trip
# endregion MODULE_CONTRACT

from __future__ import annotations

import pytest

from yascheduler.infra.persistence.sql_loader import load_query

pytestmark = pytest.mark.unit

__all__: list[str] = []


@pytest.mark.parametrize(
    "query",
    [
        "task/get_by_id",
        "task/get_by_id_and_status",
        "task/list_by_jobs",
        "task/list_by_status",
    ],
)
def test_full_task_reads_return_output_selections(query: str) -> None:
    assert "output_selections" in load_query(query)


def test_insert_and_update_persist_output_selections() -> None:
    insert_sql = load_query("task/insert")
    assert "output_selections" in insert_sql
    assert ":output_selections" in insert_sql
    assert "output_selections" in insert_sql[insert_sql.index("RETURNING") :]

    update_sql = load_query("task/update_by_id")
    assert "output_selections = :output_selections" in update_sql
