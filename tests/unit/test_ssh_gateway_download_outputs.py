# region MODULE_CONTRACT
# PURPOSE: Unit-test OutputDownloader's tree-preserving retrieval contract — selection resolution against fake SFTP listings, exact local destinations with preserve=True, local directory creation, merge-without-clear semantics, destination link/type rejection, per-entry transient/permanent classification, per-file fresh-channel isolation, and best-effort post-finalisation remote cleanup.
# SCOPE: download_outputs tree layouts, explicit local get arguments, local destination safety, resolution and listing failures, preparation faults, structured OUTPUT_ENTRY/OUTPUT_DOWNLOAD records, content-free logs, plus cleanup_remote success/already-absent/failure paths.
# NOT: real SFTP transport, consume_task's finalisation decisions, or the resolver's own matching rules.
# KEYWORDS: download_outputs, output tree, selection, preserve, destination safety, SFTP isolation, retry classification, cleanup_remote
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncssh.sftp import (
    FILEXFER_TYPE_DIRECTORY,
    FILEXFER_TYPE_REGULAR,
    SFTPAttrs,
    SFTPError,
    SFTPFailure,
    SFTPName,
    SFTPNoSuchFile,
)

from tests.log_assertions import extra_fields
from tests.unit.test_ssh_gateway import _make_state
from yascheduler.domain import UnsafeTaskPathError
from yascheduler.domain.model import TaskId
from yascheduler.infra.ssh.operations import OutputDownloader
from yascheduler.infra.ssh.operations import download as download_module
from yascheduler.infra.ssh.platform.types import SFTPConnectionLost
from yascheduler.infra.ssh.session import SSHMachineSession

REMOTE_DIR = "/remote/task"
REMOTE_ROOT = PurePosixPath(REMOTE_DIR)
DOWNLOAD_LOGGER = "yascheduler.infra.ssh.operations.download"

_CONTENT_SECRET = "UNIQUE_DOWNLOAD_FILE_CONTENT_SECRET_4f19"
_CREDENTIAL_SECRET = "UNIQUE_DOWNLOAD_CREDENTIAL_SECRET_8b2c"


@pytest.fixture(autouse=True)
def _no_sftp_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize my_retry so unit tests classify immediate failures without backoff sleep."""
    monkeypatch.setattr(download_module, "my_retry", lambda: lambda fn: fn)


# =============================================================================
# Fake SFTP: dict of relative directory -> [(name, SFTP file type)]
# =============================================================================


def _sftp_name(name: str, entry_type: int) -> SFTPName:
    """Build one readdir entry exactly as asyncssh would return it."""
    return SFTPName(name, None, SFTPAttrs(type=entry_type))


def _listing(
    tree: dict[str, list[tuple[str, int]]],
    *,
    failures: dict[str, Exception] | None = None,
    calls: list[str] | None = None,
):
    """Build an async readdir over a directory -> entries tree for REMOTE_ROOT."""

    async def read_dir(path: str) -> list[SFTPName]:
        if calls is not None:
            calls.append(path)
        relative = PurePosixPath(path).relative_to(REMOTE_ROOT)
        key = "" if str(relative) == "." else str(relative)
        if failures is not None and key in failures:
            raise failures[key]
        if key not in tree:
            raise SFTPNoSuchFile("No such file")
        return [_sftp_name(name, entry_type) for name, entry_type in tree[key]]

    return read_dir


def _write_payload(local: Any, payload: bytes) -> None:
    """Write the fake transferred bytes to the local destination."""
    Path(local).write_bytes(payload)


def _recording_get(
    calls: list[tuple[str, Path, bool]],
    *,
    payload: bytes = b"payload",
    error: Exception | None = None,
    write_before_error: bool = False,
):
    """Build an async sftp.get that records its arguments and writes a payload."""

    async def get(remote: Any, local: Any, preserve: bool = False) -> None:
        calls.append((str(remote), Path(local), preserve))
        if error is not None and not write_before_error:
            raise error
        _write_payload(local, payload)
        if error is not None:
            raise error

    return get


def _make_sftp(
    tree: dict[str, list[tuple[str, int]]],
    *,
    get_calls: list[tuple[str, Path, bool]] | None = None,
    payload: bytes = b"payload",
    readdir_failures: dict[str, Exception] | None = None,
) -> AsyncMock:
    """Build an SFTP client mock serving one listing and recording get calls."""
    sftp = AsyncMock()
    sftp.readdir = _listing(tree, failures=readdir_failures)
    sftp.get = _recording_get(
        [] if get_calls is None else get_calls,
        payload=payload,
    )
    sftp.rmtree = AsyncMock(return_value=None)
    return sftp


def _make_session_with_sftp(sftp_mock: AsyncMock) -> SSHMachineSession:
    """Construct a session whose open_sftp() yields sftp_mock and whose .path is PurePosixPath."""
    session = _make_state()
    session._adapter.path = PurePosixPath  # type: ignore[method-assign,misc]

    @asynccontextmanager
    async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
        yield sftp_mock

    session._conn.start_sftp_client = _sftp_ctx  # type: ignore[method-assign,misc,assignment]
    return session


def _make_session_with_factory(start_sftp_client: Any) -> SSHMachineSession:
    """Construct a session whose open_sftp() delegates to a per-call factory."""
    session = _make_state()
    session._adapter.path = PurePosixPath  # type: ignore[method-assign,misc]
    session._conn.start_sftp_client = start_sftp_client  # type: ignore[method-assign,misc]
    return session


async def _download(
    session: SSHMachineSession,
    local_dir: Path,
    selections: list[str],
    *,
    task_id: TaskId | None = None,
):
    """Invoke the real downloader with the new root-plus-selections contract."""
    return await OutputDownloader().download_outputs(
        session,
        remote_dir=REMOTE_DIR,
        local_dir=local_dir,
        selections=selections,
        task_id=task_id,
    )


def _make_cleanup_session(sftp_mock: AsyncMock) -> SimpleNamespace:
    """Minimal fake MachineSession for cleanup_remote, recording path() calls."""

    @asynccontextmanager
    async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
        yield sftp_mock

    return SimpleNamespace(
        open_sftp=_sftp_ctx,
        path=MagicMock(side_effect=PurePosixPath),
    )


def _make_broken_cleanup_session(error: Exception) -> SimpleNamespace:
    """Minimal fake MachineSession whose cleanup SFTP channel open fails."""

    @asynccontextmanager
    async def _sftp_ctx() -> AsyncGenerator[AsyncMock, None]:
        raise error
        yield AsyncMock()  # type: ignore[unreachable]

    return SimpleNamespace(
        open_sftp=_sftp_ctx,
        path=MagicMock(side_effect=PurePosixPath),
    )


# =============================================================================
# Tree layout and exact destinations
# =============================================================================


class TestDownloadTrees:
    async def test_all_files_download_to_explicit_local_paths(
        self, tmp_path: Path
    ) -> None:
        """Each file is fetched to its full local path with preserve=True."""
        tree = {
            "": [
                ("f1.out", FILEXFER_TYPE_REGULAR),
                ("f2.out", FILEXFER_TYPE_REGULAR),
            ],
        }
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls, payload=b"one")
        session = _make_session_with_sftp(sftp)

        (
            transient_errors,
            permanent_errors,
        ) = await _download(session, tmp_path, ["f1.out", "f2.out"], task_id=TaskId(42))

        assert transient_errors == []
        assert permanent_errors == []
        assert get_calls == [
            (f"{REMOTE_DIR}/f1.out", tmp_path / "f1.out", True),
            (f"{REMOTE_DIR}/f2.out", tmp_path / "f2.out", True),
        ]
        assert (tmp_path / "f1.out").is_file()
        assert (tmp_path / "f2.out").is_file()
        assert (tmp_path / "f1.out").read_bytes() == b"one"
        sftp.rmtree.assert_not_awaited()

    async def test_directories_created_locally_without_get(
        self, tmp_path: Path
    ) -> None:
        """Resolved directories are created and never passed to sftp.get."""
        tree = {
            "": [("OUT", FILEXFER_TYPE_DIRECTORY)],
            "OUT": [("empty", FILEXFER_TYPE_DIRECTORY)],
            "OUT/empty": [],
        }
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls)
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(session, tmp_path, ["OUT"])

        assert transient_errors == []
        assert permanent_errors == []
        assert get_calls == []
        assert (tmp_path / "OUT").is_dir()
        assert (tmp_path / "OUT" / "empty").is_dir()
        sftp.rmtree.assert_not_awaited()

    async def test_nested_layout_is_recreated_under_result_root(
        self, tmp_path: Path
    ) -> None:
        """A selected directory keeps its nested layout locally."""
        tree = {
            "": [("OUT", FILEXFER_TYPE_DIRECTORY)],
            "OUT": [("nested", FILEXFER_TYPE_DIRECTORY)],
            "OUT/nested": [("result.dat", FILEXFER_TYPE_REGULAR)],
        }
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls, payload=b"nested-result")
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(session, tmp_path, ["OUT"])

        expected = tmp_path / "OUT" / "nested" / "result.dat"
        assert transient_errors == []
        assert permanent_errors == []
        assert get_calls == [
            (f"{REMOTE_DIR}/OUT/nested/result.dat", expected, True),
        ]
        assert expected.is_file()
        assert expected.read_bytes() == b"nested-result"

    async def test_single_file_selection_lands_at_result_root(
        self, tmp_path: Path
    ) -> None:
        """A root-level file lands at its own name with no enclosing directory."""
        tree = {"": [("OUTPUT", FILEXFER_TYPE_REGULAR)]}
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls, payload=b"body")
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(
            session, tmp_path, ["OUTPUT"]
        )

        assert transient_errors == []
        assert permanent_errors == []
        assert (tmp_path / "OUTPUT").is_file()
        assert (tmp_path / "OUTPUT").read_bytes() == b"body"
        assert get_calls == [(f"{REMOTE_DIR}/OUTPUT", tmp_path / "OUTPUT", True)]
        assert not (tmp_path / "OUTPUT" / "OUTPUT").exists()

    async def test_unrelated_local_entries_are_retained(self, tmp_path: Path) -> None:
        """The result root is merged: unrelated files survive retrieval."""
        keep = tmp_path / "keep.txt"
        keep.write_bytes(b"keep")
        out = tmp_path / "OUT"
        out.mkdir()
        old = out / "old.txt"
        old.write_bytes(b"old")
        tree = {
            "": [("OUT", FILEXFER_TYPE_DIRECTORY)],
            "OUT": [("run.log", FILEXFER_TYPE_REGULAR)],
        }
        sftp = _make_sftp(tree, payload=b"log")
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(session, tmp_path, ["OUT"])

        assert transient_errors == []
        assert permanent_errors == []
        assert keep.read_bytes() == b"keep"
        assert old.read_bytes() == b"old"
        assert (out / "run.log").read_bytes() == b"log"
        sftp.rmtree.assert_not_awaited()

    async def test_second_pass_replaces_selected_file(self, tmp_path: Path) -> None:
        """A later pass replaces a selected file rather than clearing the root."""
        tree = {"": [("result.dat", FILEXFER_TYPE_REGULAR)]}
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls, payload=b"first")
        session = _make_session_with_sftp(sftp)

        await _download(session, tmp_path, ["result.dat"])
        assert (tmp_path / "result.dat").read_bytes() == b"first"

        sftp.get = _recording_get(get_calls, payload=b"second")
        transient_errors, permanent_errors = await _download(
            session, tmp_path, ["result.dat"]
        )

        assert transient_errors == []
        assert permanent_errors == []
        assert (tmp_path / "result.dat").read_bytes() == b"second"
        assert len(get_calls) == 2
        assert not (tmp_path / "result.dat").is_dir()


# =============================================================================
# Local destination safety
# =============================================================================


class TestLocalDestinationSafety:
    async def test_destination_symlink_is_permanent_and_never_written_through(
        self, tmp_path: Path
    ) -> None:
        """A symlink at the selected file's destination is a permanent error."""
        out = tmp_path / "OUT"
        out.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"outside")
        link = out / "run.log"
        link.symlink_to(outside)

        tree = {
            "": [("OUT", FILEXFER_TYPE_DIRECTORY)],
            "OUT": [("run.log", FILEXFER_TYPE_REGULAR)],
        }
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls, payload=b"fresh")
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(session, tmp_path, ["OUT"])

        assert transient_errors == []
        assert [path for path, _ in permanent_errors] == ["OUT/run.log"]
        _, err = permanent_errors[0]
        assert isinstance(err, UnsafeTaskPathError)
        assert get_calls == []
        assert outside.read_bytes() == b"outside"
        assert link.is_symlink()

    async def test_file_target_existing_as_directory_is_permanent(
        self, tmp_path: Path
    ) -> None:
        """A directory where a selected file belongs is a permanent conflict."""
        out = tmp_path / "OUT"
        out.mkdir()
        (out / "run.log").mkdir()

        tree = {
            "": [("OUT", FILEXFER_TYPE_DIRECTORY)],
            "OUT": [("run.log", FILEXFER_TYPE_REGULAR)],
        }
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls)
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(session, tmp_path, ["OUT"])

        assert transient_errors == []
        assert [path for path, _ in permanent_errors] == ["OUT/run.log"]
        _, err = permanent_errors[0]
        assert isinstance(err, UnsafeTaskPathError)
        assert get_calls == []
        assert (out / "run.log").is_dir()

    async def test_directory_target_existing_as_file_is_permanent(
        self, tmp_path: Path
    ) -> None:
        """A regular file where a selected directory belongs is a permanent conflict."""
        (tmp_path / "OUT").write_bytes(b"not a directory")

        tree = {
            "": [("OUT", FILEXFER_TYPE_DIRECTORY)],
            "OUT": [],
        }
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls)
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(session, tmp_path, ["OUT"])

        assert transient_errors == []
        assert [path for path, _ in permanent_errors] == ["OUT"]
        _, err = permanent_errors[0]
        assert isinstance(err, UnsafeTaskPathError)
        assert get_calls == []
        assert (tmp_path / "OUT").read_bytes() == b"not a directory"


# =============================================================================
# Failure classification and isolation
# =============================================================================


class TestDownloadFailureClassification:
    async def test_unmatched_selection_is_permanent_while_other_transfers(
        self, tmp_path: Path
    ) -> None:
        """An unmatched selection stays permanent; a matching one still downloads."""
        tree = {"": [("GOOD", FILEXFER_TYPE_REGULAR)]}
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls, payload=b"good")
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(
            session, tmp_path, ["GOOD", "missing/*.orb"]
        )

        assert transient_errors == []
        assert [path for path, _ in permanent_errors] == ["missing/*.orb"]
        _, err = permanent_errors[0]
        assert isinstance(err, FileNotFoundError)
        assert get_calls == [(f"{REMOTE_DIR}/GOOD", tmp_path / "GOOD", True)]
        assert (tmp_path / "GOOD").read_bytes() == b"good"

    async def test_listing_failure_is_transient_for_that_selection(
        self, tmp_path: Path
    ) -> None:
        """A failed listing classifies as transient for that selection only."""
        tree = {
            "": [
                ("GOOD", FILEXFER_TYPE_REGULAR),
                ("BAD", FILEXFER_TYPE_DIRECTORY),
            ],
            "BAD": [("x", FILEXFER_TYPE_REGULAR)],
        }
        failure = SFTPFailure("listing failed")
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(
            tree,
            get_calls=get_calls,
            readdir_failures={"BAD": failure},
        )
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(
            session, tmp_path, ["BAD/x", "GOOD"]
        )

        assert len(transient_errors) == 1
        path, err = transient_errors[0]
        assert path == "BAD/x"
        assert err is failure
        assert permanent_errors == []
        assert get_calls == [(f"{REMOTE_DIR}/GOOD", tmp_path / "GOOD", True)]

    async def test_resolution_channel_open_failure_is_one_transient(
        self, tmp_path: Path
    ) -> None:
        """A channel-open failure during resolution yields exactly one transient."""
        session = _make_state()
        session._adapter.path = PurePosixPath  # type: ignore[method-assign,misc]
        failure = SFTPConnectionLost("no session")

        @asynccontextmanager
        async def broken_sftp() -> AsyncGenerator[Any, None]:
            raise failure
            yield  # type: ignore[unreachable]

        session._conn.start_sftp_client = broken_sftp  # type: ignore[method-assign,misc]

        (
            transient_errors,
            permanent_errors,
        ) = await _download(session, tmp_path, ["f1.out", "f2.out"], task_id=TaskId(42))

        assert permanent_errors == []
        assert len(transient_errors) == 1
        path, err = transient_errors[0]
        assert path == REMOTE_DIR
        assert err is failure

    async def test_per_file_fresh_channel_isolates_a_dropped_file(
        self, tmp_path: Path
    ) -> None:
        """A dropped channel on file 2 does not fail file 3 (fresh channel per file)."""
        tree = {
            "": [
                ("f1", FILEXFER_TYPE_REGULAR),
                ("f2", FILEXFER_TYPE_REGULAR),
                ("f3", FILEXFER_TYPE_REGULAR),
            ],
        }
        get_calls: list[tuple[str, Path, bool]] = []
        clients: list[AsyncMock] = []
        behaviors: list[tuple[bytes | None, Exception | None]] = [
            (b"one", None),
            (None, SFTPConnectionLost("dropped")),
            (b"three", None),
        ]

        @asynccontextmanager
        async def fresh_sftp() -> AsyncGenerator[AsyncMock, None]:
            index = len(clients)
            sftp = AsyncMock()
            sftp.rmtree = AsyncMock(return_value=None)
            if index == 0:
                sftp.readdir = _listing(tree)
            else:
                payload, error = behaviors[index - 1]
                sftp.get = _recording_get(
                    get_calls,
                    payload=b"" if payload is None else payload,
                    error=error,
                )
            clients.append(sftp)
            yield sftp

        session = _make_session_with_factory(fresh_sftp)

        transient_errors, permanent_errors = await _download(
            session, tmp_path, ["f1", "f2", "f3"], task_id=TaskId(7)
        )

        assert len(clients) == 4
        assert len(transient_errors) == 1
        assert transient_errors[0][0] == "f2"
        assert isinstance(transient_errors[0][1], SFTPConnectionLost)
        assert permanent_errors == []
        assert [path for path, _local, _preserve in get_calls] == [
            f"{REMOTE_DIR}/f1",
            f"{REMOTE_DIR}/f2",
            f"{REMOTE_DIR}/f3",
        ]
        assert (tmp_path / "f1").read_bytes() == b"one"
        assert not (tmp_path / "f2").exists()
        assert (tmp_path / "f3").read_bytes() == b"three"
        for client in clients:
            client.rmtree.assert_not_awaited()

    async def test_local_prep_failure_is_permanent_and_other_entries_continue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-SFTP local preparation error is permanent for that path only."""
        tree = {
            "": [
                ("bad", FILEXFER_TYPE_REGULAR),
                ("good", FILEXFER_TYPE_REGULAR),
            ],
        }
        get_calls: list[tuple[str, Path, bool]] = []
        sftp = _make_sftp(tree, get_calls=get_calls, payload=b"good")
        session = _make_session_with_sftp(sftp)
        real_prepare = download_module._prepare_local_entry

        def failing_prepare(local_root: Path, entry: Any) -> Path:
            if entry.path == "bad":
                raise OSError("prep failed")
            return real_prepare(local_root, entry)

        monkeypatch.setattr(download_module, "_prepare_local_entry", failing_prepare)

        transient_errors, permanent_errors = await _download(
            session, tmp_path, ["bad", "good"]
        )

        assert transient_errors == []
        assert [path for path, _ in permanent_errors] == ["bad"]
        _, err = permanent_errors[0]
        assert isinstance(err, OSError)
        assert get_calls == [(f"{REMOTE_DIR}/good", tmp_path / "good", True)]
        assert not (tmp_path / "bad").exists()
        assert (tmp_path / "good").read_bytes() == b"good"

    async def test_download_never_calls_rmtree(self, tmp_path: Path) -> None:
        """A full successful download leaves the remote tree for cleanup_remote."""
        tree = {"": [("f1", FILEXFER_TYPE_REGULAR)]}
        sftp = _make_sftp(tree)
        session = _make_session_with_sftp(sftp)

        transient_errors, permanent_errors = await _download(session, tmp_path, ["f1"])

        assert transient_errors == []
        assert permanent_errors == []
        sftp.rmtree.assert_not_awaited()


# =============================================================================
# Structured records
# =============================================================================


def _records(caplog: pytest.LogCaptureFixture, marker: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == DOWNLOAD_LOGGER and record.getMessage() == marker
    ]


def _fields(record: logging.LogRecord) -> dict[str, object]:
    fields = extra_fields(record)
    fields.pop("message", None)
    return fields


class TestStructuredRecords:
    async def test_success_emits_entry_and_download_records(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OUTPUT_ENTRY and OUTPUT_DOWNLOAD carry the documented fields."""
        tree = {
            "": [("OUT", FILEXFER_TYPE_DIRECTORY)],
            "OUT": [("run.log", FILEXFER_TYPE_REGULAR)],
        }
        sftp = _make_sftp(tree, payload=_CONTENT_SECRET.encode())
        session = _make_session_with_sftp(sftp)

        with caplog.at_level(logging.DEBUG, logger=DOWNLOAD_LOGGER):
            transient_errors, permanent_errors = await _download(
                session, tmp_path, ["OUT"], task_id=TaskId(42)
            )

        assert transient_errors == []
        assert permanent_errors == []

        entry_records = _records(caplog, "OUTPUT_ENTRY")
        assert [_fields(record) for record in entry_records] == [
            {
                "task_id": TaskId(42),
                "path": "OUT",
                "kind": "directory",
                "outcome": "directory",
                "failure": "",
            },
            {
                "task_id": TaskId(42),
                "path": "OUT/run.log",
                "kind": "file",
                "outcome": "downloaded",
                "failure": "",
            },
        ]
        download_records = _records(caplog, "OUTPUT_DOWNLOAD")
        assert len(download_records) == 1
        assert _fields(download_records[0]) == {
            "task_id": TaskId(42),
            "entries": 2,
            "transient": 0,
            "permanent": 0,
        }
        assert _CONTENT_SECRET not in caplog.text
        assert _CONTENT_SECRET not in repr(
            [_fields(record) for record in caplog.records]
        )

    async def test_failure_record_names_the_path_and_failure_class(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed transfer logs the path and exception class, never file content."""
        tree = {"": [("secret.bin", FILEXFER_TYPE_REGULAR)]}
        sftp = _make_sftp(tree)
        sftp.get = _recording_get(
            [],
            payload=_CONTENT_SECRET.encode(),
            error=OSError("disk full"),
            write_before_error=True,
        )
        session = _make_session_with_sftp(sftp)

        with caplog.at_level(logging.DEBUG, logger=DOWNLOAD_LOGGER):
            transient_errors, permanent_errors = await _download(
                session, tmp_path, ["secret.bin"], task_id=TaskId(42)
            )

        assert transient_errors == []
        assert [path for path, _ in permanent_errors] == ["secret.bin"]
        failed = _records(caplog, "OUTPUT_ENTRY")
        assert [_fields(record) for record in failed] == [
            {
                "task_id": TaskId(42),
                "path": "secret.bin",
                "kind": "file",
                "outcome": "failed",
                "failure": "OSError",
            },
        ]
        download_records = _records(caplog, "OUTPUT_DOWNLOAD")
        assert len(download_records) == 1
        assert _fields(download_records[0]) == {
            "task_id": TaskId(42),
            "entries": 1,
            "transient": 0,
            "permanent": 1,
        }
        assert _CONTENT_SECRET not in caplog.text
        assert _CREDENTIAL_SECRET not in caplog.text


# =============================================================================
# cleanup_remote — best-effort post-finalisation deletion
# =============================================================================


def _cleanup_fields(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    records = [
        record for record in caplog.records if record.getMessage() == "CLEANUP_REMOTE"
    ]
    assert len(records) == 1
    fields = extra_fields(records[0])
    fields.pop("message", None)
    return fields


def _assert_cleanup_logs_are_content_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup logs must not render file contents or credentials."""
    rendered = caplog.text
    for record in caplog.records:
        fields = extra_fields(record)
        rendered = f"{rendered}\n{record.getMessage()} {fields!r}"
    assert _CONTENT_SECRET not in rendered
    assert _CREDENTIAL_SECRET not in rendered


@pytest.mark.asyncio
async def test_cleanup_remote_success_uses_fresh_channel_and_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful cleanup returns None and rmtree receives session.path(remote_dir)."""
    sftp_mock = AsyncMock()
    sftp_mock.rmtree = AsyncMock(return_value=None)
    session = _make_cleanup_session(sftp_mock)
    operations = OutputDownloader()

    with caplog.at_level(logging.DEBUG, logger=DOWNLOAD_LOGGER):
        await operations.cleanup_remote(
            session,  # type: ignore[arg-type]
            remote_dir="/remote/path",
            task_id=TaskId(42),
        )
    session.path.assert_called_once_with("/remote/path")
    sftp_mock.rmtree.assert_awaited_once_with(PurePosixPath("/remote/path"))
    sftp_mock.get.assert_not_awaited()
    assert _cleanup_fields(caplog) == {
        "task_id": TaskId(42),
        "remote_dir": "/remote/path",
    }
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    _assert_cleanup_logs_are_content_free(caplog)


@pytest.mark.asyncio
async def test_cleanup_remote_already_absent_logs_debug_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SFTPNoSuchFile is a debug no-op, not a warning, and cleanup still returns None."""
    sftp_mock = AsyncMock()
    sftp_mock.rmtree = AsyncMock(side_effect=SFTPNoSuchFile("No such file"))
    session = _make_cleanup_session(sftp_mock)
    operations = OutputDownloader()

    with caplog.at_level(logging.DEBUG, logger=DOWNLOAD_LOGGER):
        await operations.cleanup_remote(
            session,  # type: ignore[arg-type]
            remote_dir="/remote/path",
            task_id=TaskId(42),
        )
    session.path.assert_called_once_with("/remote/path")
    sftp_mock.rmtree.assert_awaited_once_with(PurePosixPath("/remote/path"))
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    absent_records = [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG and "already absent" in record.getMessage()
    ]
    assert len(absent_records) == 1
    assert not any(record.getMessage() == "CLEANUP_REMOTE" for record in caplog.records)
    _assert_cleanup_logs_are_content_free(caplog)


@pytest.mark.asyncio
async def test_cleanup_remote_failure_warns_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A generic SFTPError is logged as a warning and swallowed."""
    sftp_mock = AsyncMock()
    sftp_mock.rmtree = AsyncMock(side_effect=SFTPError(4, "permission denied"))
    session = _make_cleanup_session(sftp_mock)
    operations = OutputDownloader()

    with caplog.at_level(logging.DEBUG, logger=DOWNLOAD_LOGGER):
        await operations.cleanup_remote(
            session,  # type: ignore[arg-type]
            remote_dir="/remote/path",
            task_id=TaskId(42),
        )
    sftp_mock.rmtree.assert_awaited_once_with(PurePosixPath("/remote/path"))
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "Cannot clean remote dir" in message
    assert "(left for node teardown)" in message
    assert "permission denied" in message
    assert not any(record.getMessage() == "CLEANUP_REMOTE" for record in caplog.records)
    _assert_cleanup_logs_are_content_free(caplog)


@pytest.mark.asyncio
async def test_cleanup_remote_open_sftp_oserror_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError opening the cleanup SFTP channel is swallowed with a warning."""
    session = _make_broken_cleanup_session(OSError("channel open failed"))
    operations = OutputDownloader()

    with caplog.at_level(logging.DEBUG, logger=DOWNLOAD_LOGGER):
        await operations.cleanup_remote(
            session,  # type: ignore[arg-type]
            remote_dir="/remote/path",
            task_id=TaskId(42),
        )
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "Cannot clean remote dir" in message
    assert "(left for node teardown)" in message
    assert "channel open failed" in message
    _assert_cleanup_logs_are_content_free(caplog)
