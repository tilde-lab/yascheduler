"""Implementation-agnostic integration test — Yascheduler query path against real PostgreSQL (6-key shape).

Implementation-agnostic: asserts observable behavior only (never isinstance
on db.TaskStatus). Valid both before the UoW swap (DB-backed) and after.
"""
# region MODULE_CONTRACT
# PURPOSE: Implementation-agnostic integration test pinning the Yascheduler query-method output shape against real PostgreSQL.
# SCOPE: Submit a real task, query via jobs=[id] and status=[0], assert 6-key dict shape with top-level input entries and a nested node object.
# KEYWORDS: Yascheduler query, Postgres, task dict shape
# endregion MODULE_CONTRACT

from typing import TYPE_CHECKING

import pytest

from yascheduler.entrypoints.client import Yascheduler

if TYPE_CHECKING:
    from collections.abc import Iterator

    from yascheduler.infra.persistence import PostgresDbConfig

EXPECTED_KEYS = {"task_id", "label", "status", "metadata", "input_entries", "node"}


@pytest.fixture(scope="session")
def _query_config(
    _db_config: "PostgresDbConfig",
    tmp_path_factory: pytest.TempPathFactory,
) -> "Iterator[str]":
    tmp = tmp_path_factory.mktemp("query_config")
    engines_dir = tmp / "data" / "engines" / "test_query"
    engines_dir.mkdir(parents=True)

    db_cfg = _db_config
    ini_path = tmp / "yascheduler.conf"
    ini_path.write_text(
        f"[db]\n"
        f"host = {db_cfg.host}\n"
        f"port = {db_cfg.port}\n"
        f"user = {db_cfg.user}\n"
        f"password = {db_cfg.password}\n"
        f"database = {db_cfg.database}\n"
        f"\n"
        f"[local]\n"
        f"data_dir = {tmp / 'data'}\n"
        f"\n"
        f"[remote]\n"
        f"user = test\n"
        f"\n"
        f"[engine.test_query]\n"
        f"spawn = {{engine_path}}/run.sh\n"
        f"check_pname = sleep\n"
        f"input_files = 1.input\n"
        f"output_files = 1.input.out\n"
        f"platforms = linux\n",
    )
    return str(ini_path)  # type: ignore[return-value]


@pytest.fixture
def _submit_task(
    _query_config: str,
    _init_schema: None,
) -> int:
    task_id = Yascheduler(_query_config).queue_submit_task(
        label="gamma-query-test",
        metadata={"1.input": "hello"},
        engine_name="test_query",
    )
    assert task_id > 0
    return task_id


class TestClientQueryIntegration:
    """Implementation-agnostic: query methods return 6-key Mapping shape with input entries and nested node object."""

    def test_query_by_jobs_returns_six_key_mapping_with_node(
        self,
        _query_config: str,
        _submit_task: int,
    ) -> None:
        task_id = _submit_task
        result = Yascheduler(_query_config).queue_get_tasks(jobs=[task_id])

        assert isinstance(result, list)
        assert len(result) == 1
        mapping = result[0]
        assert set(mapping.keys()) == EXPECTED_KEYS
        assert mapping["task_id"] == task_id
        assert mapping["label"] == "gamma-query-test"
        assert int(mapping["status"]) == 0
        assert mapping["status"].name == "TO_DO"
        assert "ip" not in mapping
        assert "cloud" not in mapping
        assert "node" in mapping
        assert isinstance(mapping["metadata"], dict)
        # The legacy filename/value key remains in metadata: the projection
        # keeps the pre-snapshot query shape (the transitional extra copy wins
        # over the snapshot-derived value while it exists).
        assert mapping["metadata"]["1.input"] == "hello"
        assert mapping["input_entries"] == [
            {
                "path": "1.input",
                "kind": "file",
                "content_base64": "aGVsbG8=",
            }
        ]

    def test_query_by_status_returns_six_key_mapping_with_node(
        self,
        _query_config: str,
        _submit_task: int,
    ) -> None:
        task_id = _submit_task
        result = Yascheduler(_query_config).queue_get_tasks(status=[0])

        assert isinstance(result, list)
        matching = [m for m in result if m["task_id"] == task_id]
        assert len(matching) == 1
        mapping = matching[0]
        assert set(mapping.keys()) == EXPECTED_KEYS
        assert mapping["status"] == 0
        assert "cloud" not in mapping
        assert "node" in mapping

    def test_query_single_task_returns_mapping_or_none(
        self,
        _query_config: str,
        _submit_task: int,
    ) -> None:
        task_id = _submit_task
        found = Yascheduler(_query_config).queue_get_task(task_id)

        assert found is not None
        assert set(found.keys()) == EXPECTED_KEYS
        assert found["task_id"] == task_id

        unknown = Yascheduler(_query_config).queue_get_task(9_999_999)
        assert unknown is None
