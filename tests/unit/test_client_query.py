"""Unit tests for Yascheduler queue-query methods.

Exercises the post-swap implementation via the `deps_factory` constructor
seam: a `FakeCLIDeps`-returning factory whose `uow_factory()` returns a
`FakeUnitOfWork` carrying a `FakeTaskRepository`. The seam keeps these
tests stable across future refactors of the query body.
"""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for Yascheduler queue-query methods via the deps_factory constructor seam.
# SCOPE: status/jobs dispatch, mutual-exclusivity ValueError, empty-in empty-out, 6-key dict shape with nested node, node object for allocated/unallocated, factory-per-call, input-entry projection (empty snapshot, path ordering, padded base64 files, content-free directories, legacy metadata derivation from snapshot files, directory omission, typed-field and extra-copy precedence).
# KEYWORDS: Yascheduler queue query, status/jobs dispatch, dict shape
# endregion MODULE_CONTRACT

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yascheduler.domain import (
    TaskInputDirectory,
    TaskInputFile,
    TaskInputSnapshot,
)
from yascheduler.domain.model import (
    Done,
    Node,
    NodeId,
    Running,
    Task,
    TaskId,
    TaskStatus,
    Todo,
)
from yascheduler.entrypoints.client import Yascheduler

EXPECTED_KEYS = {"task_id", "label", "status", "metadata", "input_entries", "node"}


class FakeTaskRepository:
    """In-memory task repository capturing dispatched query calls."""

    def __init__(
        self,
        tasks: list[Task] | None = None,
        snapshots: dict[TaskId, TaskInputSnapshot] | None = None,
    ) -> None:
        self._tasks = tasks or []
        self._snapshots = snapshots or {}
        self.list_by_status_calls: list[set[TaskStatus]] = []
        self.list_by_jobs_calls: list[list[TaskId]] = []

    async def list_by_status(
        self,
        statuses: set[TaskStatus],
        *,
        limit: int | None = None,
    ) -> list[Task]:
        self.list_by_status_calls.append(statuses)
        return self._tasks

    async def list_by_jobs(self, job_ids: list[TaskId]) -> list[Task]:
        self.list_by_jobs_calls.append(job_ids)
        return self._tasks

    async def list_input_snapshots(
        self, task_ids: list[TaskId]
    ) -> dict[TaskId, TaskInputSnapshot]:
        return {tid: self._snapshots[tid] for tid in task_ids if tid in self._snapshots}


class FakeNodeRepository:
    """In-memory node repository returning stored nodes for get_by_ids queries."""

    def __init__(self, nodes: list[Node] | None = None) -> None:
        self._nodes = {n.node_id: n for n in (nodes or [])}

    async def get_by_ids(self, node_ids: list[NodeId]) -> dict[NodeId, Node]:
        return {nid: self._nodes[nid] for nid in node_ids if nid in self._nodes}


class FakeUnitOfWork:
    """In-memory UoW exposing FakeTaskRepository + FakeNodeRepository."""

    def __init__(
        self,
        repo: FakeTaskRepository,
        nodes: FakeNodeRepository | None = None,
    ) -> None:
        self.tasks = repo
        self.nodes = nodes or FakeNodeRepository()
        self.commit_calls = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    async def commit(self) -> None:
        self.commit_calls += 1


class FakeCLIDeps:
    """Lightweight CLIDeps stub — only `uow_factory` is exercised by the query path."""

    def __init__(self, uow: FakeUnitOfWork) -> None:
        self.uow_factory = lambda: uow
        # Unused by query path; present to mirror CLIDeps shape.
        self.engines = SimpleNamespace()


def _make_task(
    task_id: int = 1,
    status: TaskStatus = TaskStatus.TO_DO,
    allocated_node_id: NodeId | None = None,
    extra: dict[str, object] | None = None,
) -> Task:
    """Build a Task; input contents live in the repository snapshot, not the record."""
    from datetime import datetime

    if status is TaskStatus.TO_DO:
        state: Todo | Running | Done = Todo()
    elif status is TaskStatus.RUNNING:
        state = Running(
            allocated_node_id=allocated_node_id or NodeId(1), remote_folder="/r"
        )
    else:
        state = Done(allocated_node_id=allocated_node_id, remote_folder="/r")
    return Task(
        task_id=TaskId(task_id),
        label=f"task-{task_id}",
        engine="test_engine",
        state=state,
        webhook_url=None,
        webhook_custom_params={},
        extra=extra or {},
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )


def _build_client(uow: FakeUnitOfWork) -> Yascheduler:
    """Construct a Yascheduler with Config.from_config_parser patched out and deps_factory wired."""
    fake_deps = FakeCLIDeps(uow)
    with patch("yascheduler.entrypoints.client.parse_config") as mock_cfg:
        mock_cfg.return_value = SimpleNamespace()
        # FakeCLIDeps is a structural stand-in for CLIDeps; the seam is test-only.
        return Yascheduler(deps_factory=lambda cfg: fake_deps)  # type: ignore[arg-type]


class TestClientQueryDispatch:
    """5 testing-unit spec scenarios for Yascheduler.queue_get_tasks_async."""

    async def test_status_filter_dispatches_list_by_status(self) -> None:
        repo = FakeTaskRepository(tasks=[_make_task(status=TaskStatus.TO_DO)])
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(status=[0])

        assert len(result) == 1
        assert repo.list_by_status_calls == [{TaskStatus.TO_DO}]
        assert repo.list_by_jobs_calls == []

    async def test_jobs_filter_dispatches_list_by_jobs(self) -> None:
        repo = FakeTaskRepository(tasks=[_make_task(task_id=7)])
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[7])

        assert len(result) == 1
        assert repo.list_by_jobs_calls == [[TaskId(7)]]
        assert repo.list_by_status_calls == []

    async def test_both_filters_raises_value_error(self) -> None:
        repo = FakeTaskRepository(tasks=[_make_task()])
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        with pytest.raises(ValueError, match="mutually exclusive"):
            await client.queue_get_tasks_async(jobs=[1], status=[0])

    async def test_neither_filter_returns_empty(self) -> None:
        repo = FakeTaskRepository(tasks=[_make_task()])
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async()

        assert result == []
        assert repo.list_by_status_calls == []
        assert repo.list_by_jobs_calls == []

    async def test_node_object_for_allocated_task(self) -> None:
        """Task with allocated_node_id returns nested node object with hostname, port, username, cloud, and new fields."""
        node = Node(
            node_id=NodeId(7),
            hostname="10.0.0.1",
            ncpus=2,
            port=22,
            username="root",
            cloud="hetzner",
        )
        repo = FakeTaskRepository(
            tasks=[
                _make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(7),
                )
            ],
        )
        nodes_repo = FakeNodeRepository(nodes=[node])
        uow = FakeUnitOfWork(repo, nodes=nodes_repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        assert len(result) == 1
        mapping = result[0]
        node_dict = mapping["node"]
        assert node_dict["hostname"] == "10.0.0.1"
        assert node_dict["port"] == 22
        assert node_dict["username"] == "root"
        assert node_dict["cloud"] == "hetzner"
        assert node_dict["jump_host"] is None
        assert node_dict["jump_port"] == 22
        assert node_dict["jump_username"] == "root"
        assert node_dict["external_id"] is None
        assert node_dict["status"] == "OTHER"
        assert "created_at" in node_dict
        assert "updated_at" in node_dict

    async def test_node_is_null_for_unallocated_task(self) -> None:
        """Task with allocated_node_id=None has node is None."""
        repo = FakeTaskRepository(tasks=[_make_task(allocated_node_id=None)])
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        assert len(result) == 1
        mapping = result[0]
        assert mapping["node"] is None

    async def test_flat_ip_and_cloud_keys_absent(self) -> None:
        """Flat ip and cloud keys are absent from the returned mapping."""
        repo = FakeTaskRepository(tasks=[_make_task()])
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        assert len(result) == 1
        mapping = result[0]
        assert "ip" not in mapping
        assert "cloud" not in mapping

    async def test_returned_dict_shape_and_types(self) -> None:
        repo = FakeTaskRepository(tasks=[_make_task(allocated_node_id=None)])
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        assert len(result) == 1
        mapping = result[0]
        assert set(mapping.keys()) == EXPECTED_KEYS
        assert isinstance(mapping["status"], TaskStatus)
        assert mapping["status"] is TaskStatus.TO_DO
        assert mapping["node"] is None
        assert isinstance(mapping["metadata"], dict)


class TestDepsFactoryInvocation:
    """dependency-injection spec: factory invoked once per query call (no caching)."""

    async def test_factory_invoked_once_per_call(self) -> None:
        repo = FakeTaskRepository(tasks=[_make_task()])
        uow = FakeUnitOfWork(repo)
        fake_deps = FakeCLIDeps(uow)

        invocation_count = 0

        def counting_factory(cfg) -> FakeCLIDeps:
            nonlocal invocation_count
            invocation_count += 1
            return fake_deps

        with patch("yascheduler.entrypoints.client.parse_config") as mock_cfg:
            mock_cfg.return_value = SimpleNamespace()
            # FakeCLIDeps is a structural stand-in for CLIDeps; the seam is test-only.
            client = Yascheduler(deps_factory=counting_factory)  # type: ignore[arg-type]

            await client.queue_get_tasks_async(jobs=[1])
            await client.queue_get_tasks_async(jobs=[1])

        assert invocation_count == 2


class TestQueryInputProjection:
    """input-entry projection spec: inputs are top-level, ordered, and files keep legacy metadata keys."""

    async def test_file_entries_keep_legacy_metadata_text_value(self) -> None:
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={TaskId(1): (TaskInputFile(path="fort.34", content=b"hello"),)},
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        metadata = result[0]["metadata"]
        assert metadata["fort.34"] == "hello"

    async def test_file_entries_keep_legacy_metadata_binary_base64_value(
        self,
    ) -> None:
        payload = b"\x00\xff"
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={TaskId(1): (TaskInputFile(path="blob.bin", content=payload),)},
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        encoded = result[0]["metadata"]["blob.bin"]
        assert encoded == base64.b64encode(payload).decode("ascii")
        assert len(encoded) % 4 == 0
        assert "\n" not in encoded

    async def test_fort9_keeps_legacy_metadata_base64_value(self) -> None:
        """`fort.9` has its own recorded meaning: always base64, never plain text."""
        payload = b"ABC"
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={TaskId(1): (TaskInputFile(path="fort.9", content=payload),)},
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        assert result[0]["metadata"]["fort.9"] == base64.b64encode(payload).decode(
            "ascii"
        )

    async def test_file_entries_keep_empty_legacy_metadata_value(self) -> None:
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={TaskId(1): (TaskInputFile(path="empty", content=b""),)},
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        assert result[0]["metadata"]["empty"] == ""

    async def test_directory_entries_are_absent_from_metadata(self) -> None:
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={TaskId(1): (TaskInputDirectory(path="basis"),)},
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        metadata = result[0]["metadata"]
        assert "basis" not in metadata
        assert metadata["engine"] == "test_engine"

    async def test_input_path_never_overwrites_a_typed_field(self) -> None:
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={TaskId(1): (TaskInputFile(path="engine", content=b"g09"),)},
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        assert result[0]["metadata"]["engine"] == "test_engine"

    async def test_absent_snapshot_projects_empty_entries_and_keeps_extras(
        self,
    ) -> None:
        repo = FakeTaskRepository(
            tasks=[_make_task(extra={"user_note": "keep me", "retries": 3})],
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        assert len(result) == 1
        mapping = result[0]
        assert mapping["input_entries"] == []
        metadata = mapping["metadata"]
        assert metadata["engine"] == "test_engine"
        assert metadata["user_note"] == "keep me"
        assert metadata["retries"] == 3

    async def test_entries_are_ordered_by_task_relative_path(self) -> None:
        unsorted_snapshot = (
            TaskInputFile(path="basis/Si.orb", content=b"orb"),
            TaskInputFile(path="INPUT", content=b"in"),
            TaskInputDirectory(path="basis"),
        )
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={TaskId(1): unsorted_snapshot},
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        mapping = result[0]
        paths = [entry["path"] for entry in mapping["input_entries"]]
        assert paths == ["INPUT", "basis", "basis/Si.orb"]

    async def test_file_entries_use_padded_standard_base64(self) -> None:
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={
                TaskId(1): (
                    TaskInputFile(path="empty.bin", content=b""),
                    TaskInputFile(path="hi.bin", content=b"hi"),
                    TaskInputFile(path="a.bin", content=b"a"),
                )
            },
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        entries = result[0]["input_entries"]
        assert [entry["path"] for entry in entries] == ["a.bin", "empty.bin", "hi.bin"]
        for entry in entries:
            assert set(entry.keys()) == {"path", "kind", "content_base64"}
            assert entry["kind"] == "file"
        assert {entry["path"]: entry["content_base64"] for entry in entries} == {
            "a.bin": "YQ==",
            "empty.bin": "",
            "hi.bin": "aGk=",
        }

    async def test_binary_file_content_round_trips_without_line_breaks(self) -> None:
        payload = bytes(range(256))
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={TaskId(1): (TaskInputFile(path="blob.bin", content=payload),)},
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        entry = result[0]["input_entries"][0]
        assert entry["path"] == "blob.bin"
        assert entry["kind"] == "file"
        assert set(entry.keys()) == {"path", "kind", "content_base64"}
        encoded = entry["content_base64"]
        assert "\n" not in encoded
        assert "\r" not in encoded
        assert len(encoded) % 4 == 0
        assert base64.b64decode(encoded, validate=True) == payload

    async def test_directory_entries_carry_no_content(self) -> None:
        repo = FakeTaskRepository(
            tasks=[_make_task()],
            snapshots={
                TaskId(1): (
                    TaskInputDirectory(path="basis"),
                    TaskInputFile(path="basis/Si.orb", content=b"orb"),
                )
            },
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        by_path = {entry["path"]: entry for entry in result[0]["input_entries"]}
        directory = by_path["basis"]
        assert set(directory.keys()) == {"path", "kind"}
        assert directory["kind"] == "directory"
        assert "content_base64" not in directory
        assert by_path["basis/Si.orb"]["kind"] == "file"
        assert base64.b64decode(by_path["basis/Si.orb"]["content_base64"]) == b"orb"

    async def test_extra_copies_win_over_derived_metadata_values(
        self,
    ) -> None:
        repo = FakeTaskRepository(
            tasks=[
                _make_task(
                    extra={
                        "fort.34": "legacy text",
                        "INPUT": "legacy text",
                        "basis": "directory entry",
                        "user_note": "keep me",
                    }
                )
            ],
            snapshots={
                TaskId(1): (
                    TaskInputFile(path="fort.34", content=b"legacy bytes"),
                    TaskInputFile(path="INPUT", content=b"in"),
                    TaskInputDirectory(path="basis"),
                )
            },
        )
        uow = FakeUnitOfWork(repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        mapping = result[0]
        metadata = mapping["metadata"]
        # Transitional extra copies keep precedence over snapshot-derived values.
        assert metadata["fort.34"] == "legacy text"
        assert metadata["INPUT"] == "legacy text"
        # Only snapshot *file* paths are derived; unrelated extras stay.
        assert metadata["basis"] == "directory entry"
        assert metadata["user_note"] == "keep me"
        assert metadata["engine"] == "test_engine"

        by_path = {entry["path"]: entry for entry in mapping["input_entries"]}
        assert set(by_path) == {"INPUT", "basis", "fort.34"}
        assert by_path["fort.34"]["kind"] == "file"
        assert base64.b64decode(by_path["fort.34"]["content_base64"]) == b"legacy bytes"
        assert by_path["INPUT"]["kind"] == "file"
        assert base64.b64decode(by_path["INPUT"]["content_base64"]) == b"in"

    async def test_node_and_status_projection_unchanged_with_input_entries(
        self,
    ) -> None:
        node = Node(
            node_id=NodeId(7),
            hostname="10.0.0.1",
            ncpus=2,
            port=22,
            username="root",
            cloud="hetzner",
        )
        repo = FakeTaskRepository(
            tasks=[
                _make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(7),
                )
            ],
            snapshots={TaskId(1): (TaskInputFile(path="INPUT", content=b"in"),)},
        )
        nodes_repo = FakeNodeRepository(nodes=[node])
        uow = FakeUnitOfWork(repo, nodes=nodes_repo)
        client = _build_client(uow)

        result = await client.queue_get_tasks_async(jobs=[1])

        mapping = result[0]
        assert set(mapping.keys()) == EXPECTED_KEYS
        assert isinstance(mapping["status"], TaskStatus)
        assert mapping["status"] is TaskStatus.RUNNING
        node_dict = mapping["node"]
        assert node_dict["hostname"] == "10.0.0.1"
        assert node_dict["port"] == 22
        assert node_dict["username"] == "root"
        assert node_dict["cloud"] == "hetzner"
        assert mapping["input_entries"] == [
            {"path": "INPUT", "kind": "file", "content_base64": "aW4="}
        ]
