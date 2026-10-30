"""Focused unit tests for task-input persistence boundary logging."""
# region MODULE_CONTRACT
# PURPOSE: Pin failure/cancellation trajectories and payload redaction at the repository input-insert and input-read boundaries without a database.
# SCOPE: Repository DEBUG markers, task/engine/count context, original-exception preservation, no success record after failure, streamed entry counts on completion, and no file contents or credentials in record fields or rendered traces.
# KEYWORDS: postgres persistence, task inputs, logging, cancellation, redaction
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.log_assertions import extra_fields
from yascheduler.domain import NewTask, TaskId, TaskInputFile
from yascheduler.infra.persistence.postgres import PostgresTaskRepository
from yascheduler.shared.log import LogFormatter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from yascheduler.domain import TaskInputEntry


pytestmark = pytest.mark.unit

__all__: list[str] = []

_SECRET = b"UNIQUE_REPOSITORY_INPUT_SECRET_57af"
_CREDENTIAL = b"UNIQUE_REPOSITORY_CREDENTIAL_2c81"
_INPUT_ENTRIES: tuple[TaskInputEntry, ...] = (
    TaskInputFile(path="INPUT", content=_SECRET + _CREDENTIAL),
)


def _repository() -> PostgresTaskRepository:
    """Return a repository whose SQL executor is replaced by an AsyncMock."""
    return PostgresTaskRepository(MagicMock(), MagicMock())


def _assert_no_payload_leak(
    rendered: str, records: Sequence[logging.LogRecord]
) -> None:
    """Assert no input bytes, base64 payload, or credential text reached a trace."""
    field_dump = repr([extra_fields(record) for record in records])
    for marker in (
        _SECRET.decode(),
        _CREDENTIAL.decode(),
        base64.b64encode(_SECRET).decode(),
        base64.b64encode(_CREDENTIAL).decode(),
    ):
        assert marker not in rendered
        assert marker not in field_dump


async def test_snapshot_read_failure_preserves_error_and_emits_no_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A batched snapshot-read exception logs task context and re-raises unchanged."""
    repository = _repository()
    error = RuntimeError(
        f"database rejected {_SECRET!r} using credential {_CREDENTIAL!r}"
    )
    repository._run = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    with (
        caplog.at_level(logging.DEBUG, logger="yascheduler.infra.persistence.postgres"),
        pytest.raises(RuntimeError) as info,
    ):
        await repository.list_input_snapshots([TaskId(7)])

    assert info.value is error
    failed = [
        record
        for record in caplog.records
        if record.getMessage() == "read_task_input_snapshots_FAILED"
    ]
    assert len(failed) == 1
    fields = extra_fields(failed[0])
    assert {key: value for key, value in fields.items() if key != "message"} == {
        "task_count": 1,
        "task_id": 7,
        "failure": "RuntimeError",
    }
    assert not any(
        record.getMessage() == "read_task_input_snapshots_DONE"
        for record in caplog.records
    )
    rendered = LogFormatter().format(failed[0])
    assert f"task_id={7}" in rendered
    assert "failure='RuntimeError'" in rendered
    _assert_no_payload_leak(rendered, caplog.records)


async def test_snapshot_read_cancellation_logs_failure_and_propagates_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancellation is a failure boundary, not a silent or successful snapshot read."""
    repository = _repository()
    cancellation = asyncio.CancelledError(
        f"cancelled while reading {_SECRET!r} with {_CREDENTIAL!r}"
    )
    repository._run = AsyncMock(side_effect=cancellation)  # type: ignore[method-assign]

    with (
        caplog.at_level(logging.DEBUG, logger="yascheduler.infra.persistence.postgres"),
        pytest.raises(asyncio.CancelledError) as info,
    ):
        await repository.list_input_snapshots([TaskId(7), TaskId(8)])

    assert info.value is cancellation
    failed = [
        record
        for record in caplog.records
        if record.getMessage() == "read_task_input_snapshots_FAILED"
    ]
    assert len(failed) == 1
    # A multi-id batch reports the batch size, not a single task id.
    assert {
        key: value for key, value in extra_fields(failed[0]).items() if key != "message"
    } == {
        "task_count": 2,
        "failure": "CancelledError",
    }
    assert not any(
        record.getMessage() == "read_task_input_snapshots_DONE"
        for record in caplog.records
    )
    _assert_no_payload_leak(LogFormatter().format(failed[0]), caplog.records)


async def test_streamed_input_done_carries_entry_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Draining the entry stream logs completion with the streamed entry count."""
    repository = _repository()
    repository._run = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            [{"path": "INPUT", "kind": "file", "content": _SECRET}],
            [{"path": "basis", "kind": "directory", "content": None}],
            [],
        ]
    )

    with caplog.at_level(
        logging.DEBUG, logger="yascheduler.infra.persistence.postgres"
    ):
        entries = [entry async for entry in repository.iter_input_entries(TaskId(7))]

    assert [entry.path for entry in entries] == ["INPUT", "basis"]
    done = [
        record
        for record in caplog.records
        if record.getMessage() == "stream_task_inputs_DONE"
    ]
    assert len(done) == 1
    fields = extra_fields(done[0])
    assert {key: value for key, value in fields.items() if key != "message"} == {
        "task_id": 7,
        "entry_count": 2,
    }
    _assert_no_payload_leak(LogFormatter().format(done[0]), caplog.records)


async def test_streamed_input_failure_mid_stream_logs_and_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure after the first entry logs progress and re-raises unchanged."""
    repository = _repository()
    error = RuntimeError(
        f"database rejected {_SECRET!r} using credential {_CREDENTIAL!r}"
    )
    repository._run = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[{"path": "INPUT", "kind": "file", "content": _SECRET}], error]
    )

    with (
        caplog.at_level(logging.DEBUG, logger="yascheduler.infra.persistence.postgres"),
        pytest.raises(RuntimeError) as info,
    ):
        async for _entry in repository.iter_input_entries(TaskId(7)):
            pass

    assert info.value is error
    failed = [
        record
        for record in caplog.records
        if record.getMessage() == "stream_task_inputs_FAILED"
    ]
    assert len(failed) == 1
    fields = extra_fields(failed[0])
    assert {key: value for key, value in fields.items() if key != "message"} == {
        "task_id": 7,
        "entry_count": 1,
        "failure": "RuntimeError",
    }
    assert not any(
        record.getMessage() == "stream_task_inputs_DONE" for record in caplog.records
    )
    _assert_no_payload_leak(LogFormatter().format(failed[0]), caplog.records)


async def test_streamed_input_early_close_is_not_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A consumer abandoning the stream mid-way is a normal end, not a failure boundary."""
    repository = _repository()
    repository._run = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[{"path": "INPUT", "kind": "file", "content": _SECRET}], []]
    )

    with caplog.at_level(
        logging.DEBUG, logger="yascheduler.infra.persistence.postgres"
    ):
        # The port types the stream as AsyncIterator; the repository returns an
        # async generator, whose explicit close is exactly what this test pins.
        stream = cast(
            "AsyncGenerator[TaskInputEntry, None]",
            repository.iter_input_entries(TaskId(7)),
        )
        async for _entry in stream:
            break
        await stream.aclose()

    # Only the start marker: an abandoned stream is neither a failure nor a
    # completion — the consumer owns that outcome.
    assert [record.getMessage() for record in caplog.records] == [
        "stream_task_inputs_START"
    ]


async def test_input_insert_cancellation_logs_failure_and_propagates_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancellation after the task row insert logs the failed entry boundary and preserves it."""
    repository = _repository()
    task_row: dict[str, object] = {
        "task_id": 11,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
        "title": "cancelled",
        "engine": "fleur",
        "extra": {},
        "webhook_url": None,
        "webhook_custom_params": {},
        "local_folder": None,
        "status": "TO_DO",
        "remote_folder": None,
        "allocated_node_id": None,
    }
    cancellation = asyncio.CancelledError(
        f"cancelled while inserting {_SECRET!r} with {_CREDENTIAL!r}"
    )
    repository._run = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[task_row], cancellation]
    )

    with (
        caplog.at_level(logging.DEBUG, logger="yascheduler.infra.persistence.postgres"),
        pytest.raises(asyncio.CancelledError) as info,
    ):
        await repository.insert(
            NewTask(label="cancelled", engine="fleur", input_entries=_INPUT_ENTRIES)
        )

    assert info.value is cancellation
    failed = [
        record
        for record in caplog.records
        if record.getMessage() == "persist_task_inputs_FAILED"
    ]
    assert len(failed) == 1
    fields = extra_fields(failed[0])
    assert {key: value for key, value in fields.items() if key != "message"} == {
        "task_id": 11,
        "engine": "fleur",
        "entry_count": 1,
        "failure": "CancelledError",
    }
    assert not any(
        record.getMessage() == "persist_task_inputs" for record in caplog.records
    )
    rendered = LogFormatter().format(failed[0])
    assert "task_id=11" in rendered
    assert "failure='CancelledError'" in rendered
    _assert_no_payload_leak(rendered, caplog.records)
