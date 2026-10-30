# region MODULE_CONTRACT
# PURPOSE: Pin the remote output-selection resolver contract against a fake SFTP listing: explicit files, directory subtree expansion with hidden and empty entries, `**` globs, overlap deduplication independent of order, path sorting, link and unsupported-type rejection, per-selection required-selection errors, unsafe selection rejection, missing-root behavior, and listing-failure isolation.
# SCOPE: resolve_output_selections over in-memory listings built from (name, SFTP file type) data; ResolvedEntry paths and kinds; per-selection error pairs; OUTPUT_RESOLVE record fields.
# NOT: SFTP transport, downloader classification, or local destination writes.
# KEYWORDS: remote selection, resolver, glob, symlink, SFTPName, required selection, dedupe
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from pathlib import PurePosixPath

import pytest
from asyncssh.sftp import (
    FILEXFER_TYPE_DIRECTORY,
    FILEXFER_TYPE_FIFO,
    FILEXFER_TYPE_REGULAR,
    FILEXFER_TYPE_SOCKET,
    FILEXFER_TYPE_SYMLINK,
    SFTPAttrs,
    SFTPFailure,
    SFTPName,
    SFTPNoSuchFile,
)

from tests.log_assertions import extra_fields
from yascheduler.domain import TaskId, TaskInputKind, UnsafeTaskPathError
from yascheduler.infra.ssh.operations.remote_selection import (
    ResolvedEntry,
    resolve_output_selections,
)

REMOTE_ROOT = PurePosixPath("/remote/task")
RESOLVER_LOGGER = "yascheduler.infra.ssh.operations.remote_selection"
UNMATCHED = "selection matched no entries"

FILE = TaskInputKind.FILE
DIRECTORY = TaskInputKind.DIRECTORY


# =============================================================================
# Fake listing: dict of relative directory -> [(entry name, SFTP file type)]
# =============================================================================


def _sftp_name(name: str, entry_type: int) -> SFTPName:
    """Build one readdir entry exactly as asyncssh would return it."""
    return SFTPName(name, None, SFTPAttrs(type=entry_type))


def _reader(
    tree: dict[str, list[tuple[str, int]]],
    *,
    failures: dict[str, Exception] | None = None,
    calls: list[str] | None = None,
):
    """Build an async read_dir over a directory -> entries tree.

    ``failures`` maps a relative directory to the exception its listing raises;
    an absent directory raises SFTPNoSuchFile like a real server.
    """

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


async def _resolve(
    tree: dict[str, list[tuple[str, int]]],
    selections: list[str],
    *,
    failures: dict[str, Exception] | None = None,
    calls: list[str] | None = None,
    task_id: TaskId | None = None,
):
    """Resolve selections against the fake listing for ``REMOTE_ROOT``."""
    return await resolve_output_selections(
        _reader(tree, failures=failures, calls=calls),
        REMOTE_ROOT,
        selections,
        task_id=task_id,
    )


def _entry(path: str, kind: TaskInputKind) -> ResolvedEntry:
    """Expected resolved entry literal."""
    return ResolvedEntry(path=path, kind=kind)


# A shared tree exercising nested files, hidden names, and empty directories.
_SHARED_TREE: dict[str, list[tuple[str, int]]] = {
    "": [
        ("INPUT", FILEXFER_TYPE_REGULAR),
        ("OUT", FILEXFER_TYPE_DIRECTORY),
        ("top.orb", FILEXFER_TYPE_REGULAR),
        ("basis", FILEXFER_TYPE_DIRECTORY),
    ],
    "OUT": [
        ("run.log", FILEXFER_TYPE_REGULAR),
        (".hidden", FILEXFER_TYPE_REGULAR),
        ("nested", FILEXFER_TYPE_DIRECTORY),
        ("empty", FILEXFER_TYPE_DIRECTORY),
    ],
    "OUT/nested": [("result.dat", FILEXFER_TYPE_REGULAR)],
    "OUT/empty": [],
    "basis": [
        ("x.orb", FILEXFER_TYPE_REGULAR),
        ("deep", FILEXFER_TYPE_DIRECTORY),
    ],
    "basis/deep": [("y.orb", FILEXFER_TYPE_REGULAR)],
}

_SHARED_OUT_SUBTREE = [
    _entry("OUT", DIRECTORY),
    _entry("OUT/.hidden", FILE),
    _entry("OUT/empty", DIRECTORY),
    _entry("OUT/nested", DIRECTORY),
    _entry("OUT/nested/result.dat", FILE),
    _entry("OUT/run.log", FILE),
]


# =============================================================================
# Explicit selections
# =============================================================================


class TestExplicitSelections:
    async def test_explicit_file_resolves_alone(self) -> None:
        entries, errors = await _resolve(_SHARED_TREE, ["INPUT"])

        assert entries == [_entry("INPUT", FILE)]
        assert errors == []

    async def test_explicit_directory_resolves_complete_subtree(self) -> None:
        entries, errors = await _resolve(_SHARED_TREE, ["OUT"])

        assert entries == _SHARED_OUT_SUBTREE
        assert errors == []

    async def test_empty_directory_satisfies_selection(self) -> None:
        tree = {
            "": [("empty", FILEXFER_TYPE_DIRECTORY)],
            "empty": [],
        }

        entries, errors = await _resolve(tree, ["empty"])

        assert entries == [_entry("empty", DIRECTORY)]
        assert errors == []


# =============================================================================
# Glob patterns
# =============================================================================


class TestGlobPatterns:
    async def test_double_star_matches_orb_files_at_any_depth(self) -> None:
        entries, errors = await _resolve(_SHARED_TREE, ["**/*.orb"])

        assert entries == [
            _entry("basis/deep/y.orb", FILE),
            _entry("basis/x.orb", FILE),
            _entry("top.orb", FILE),
        ]
        assert errors == []

    async def test_directory_prefixed_double_star_matches_dat_files(self) -> None:
        entries, errors = await _resolve(_SHARED_TREE, ["OUT/**/*.dat"])

        assert entries == [_entry("OUT/nested/result.dat", FILE)]
        assert errors == []


# =============================================================================
# Overlap, order, and sorting
# =============================================================================


class TestSelectionMerging:
    @pytest.mark.parametrize(
        "selections",
        [
            ["OUT", "OUT/nested/result.dat", "OUT/*.log"],
            ["OUT/*.log", "OUT/nested/result.dat", "OUT"],
            ["OUT/*.log", "OUT", "OUT/nested/result.dat"],
        ],
        ids=["parent-first", "leaf-first", "glob-first"],
    )
    async def test_overlapping_selections_dedupe_independent_of_order(
        self, selections: list[str]
    ) -> None:
        entries, errors = await _resolve(_SHARED_TREE, selections)

        assert entries == _SHARED_OUT_SUBTREE
        assert errors == []

    async def test_result_is_sorted_by_path_not_listing_order(self) -> None:
        tree = {
            "": [
                ("zeta", FILEXFER_TYPE_REGULAR),
                ("alpha", FILEXFER_TYPE_REGULAR),
                ("mid", FILEXFER_TYPE_REGULAR),
            ],
        }

        entries, errors = await _resolve(tree, ["*"])

        assert [entry.path for entry in entries] == ["alpha", "mid", "zeta"]
        assert entries == [
            _entry("alpha", FILE),
            _entry("mid", FILE),
            _entry("zeta", FILE),
        ]
        assert errors == []

    async def test_dot_and_dotdot_listing_names_are_ignored(self) -> None:
        tree = {
            "": [
                (".", FILEXFER_TYPE_DIRECTORY),
                ("..", FILEXFER_TYPE_DIRECTORY),
                ("visible", FILEXFER_TYPE_REGULAR),
            ],
        }
        calls: list[str] = []

        entries, errors = await _resolve(tree, ["*"], calls=calls)

        assert entries == [_entry("visible", FILE)]
        assert errors == []
        assert calls == ["/remote/task"]


# =============================================================================
# Link and unsupported-type rejection
# =============================================================================


class TestUnsafeRemoteEntries:
    async def test_selected_symlink_is_rejected(self) -> None:
        tree = {"": [("link", FILEXFER_TYPE_SYMLINK)]}

        entries, errors = await _resolve(tree, ["link"])

        assert entries == []
        assert len(errors) == 1
        path, err = errors[0]
        assert path == "link"
        assert isinstance(err, UnsafeTaskPathError)
        assert err.path == "link"
        assert "symbolic link" in err.reason

    async def test_symlink_inside_selected_directory_is_rejected_not_followed(
        self,
    ) -> None:
        tree = {
            "": [("OUT", FILEXFER_TYPE_DIRECTORY)],
            "OUT": [
                ("target", FILEXFER_TYPE_REGULAR),
                ("link", FILEXFER_TYPE_SYMLINK),
            ],
        }
        calls: list[str] = []

        entries, errors = await _resolve(tree, ["OUT"], calls=calls)

        assert entries == [_entry("OUT", DIRECTORY), _entry("OUT/target", FILE)]
        assert [(path, type(err)) for path, err in errors] == [
            ("OUT/link", UnsafeTaskPathError)
        ]
        link_path, link_error = errors[0]
        assert isinstance(link_error, UnsafeTaskPathError)
        assert link_error.path == link_path == "OUT/link"
        assert "symbolic link" in link_error.reason
        assert calls == ["/remote/task", "/remote/task/OUT"]

    @pytest.mark.parametrize(
        "entry_type",
        [FILEXFER_TYPE_FIFO, FILEXFER_TYPE_SOCKET],
        ids=["fifo", "socket"],
    )
    async def test_selected_unsupported_entry_type_is_rejected(
        self, entry_type: int
    ) -> None:
        tree = {"": [("special", entry_type)]}

        entries, errors = await _resolve(tree, ["special"])

        assert entries == []
        assert len(errors) == 1
        path, err = errors[0]
        assert path == "special"
        assert isinstance(err, UnsafeTaskPathError)
        assert err.path == "special"
        assert "unsupported" in err.reason

    @pytest.mark.parametrize(
        "selection",
        ["/etc/passwd", "../escape", "OUT/../../escape"],
        ids=["absolute", "parent", "nested-parent"],
    )
    async def test_unsafe_selection_is_rejected_before_any_listing(
        self, selection: str
    ) -> None:
        calls: list[str] = []

        entries, errors = await _resolve(_SHARED_TREE, [selection], calls=calls)

        assert entries == []
        assert calls == []
        assert len(errors) == 1
        path, err = errors[0]
        assert path == selection
        assert isinstance(err, UnsafeTaskPathError)
        assert err.path == selection

    async def test_unsafe_selection_does_not_abort_other_selections(self) -> None:
        entries, errors = await _resolve(_SHARED_TREE, ["INPUT", "../escape"])

        assert entries == [_entry("INPUT", FILE)]
        assert [(path, type(err)) for path, err in errors] == [
            ("../escape", UnsafeTaskPathError)
        ]


# =============================================================================
# Required selections and resolution failures
# =============================================================================


class TestRequiredSelections:
    @pytest.mark.parametrize(
        "selection",
        ["missing", "missing/*.orb", "OUT/*.zzz"],
        ids=["explicit", "glob", "existing-directory-glob"],
    )
    async def test_unmatched_selection_is_a_per_selection_error(
        self, selection: str
    ) -> None:
        entries, errors = await _resolve(_SHARED_TREE, [selection])

        assert entries == []
        assert len(errors) == 1
        path, err = errors[0]
        assert path == selection
        assert isinstance(err, FileNotFoundError)
        assert str(err) == UNMATCHED

    async def test_unmatched_selection_does_not_abort_other_selections(self) -> None:
        entries, errors = await _resolve(_SHARED_TREE, ["INPUT", "missing/*.orb"])

        assert entries == [_entry("INPUT", FILE)]
        assert [(path, type(err)) for path, err in errors] == [
            ("missing/*.orb", FileNotFoundError)
        ]

    async def test_missing_remote_root_reports_each_selection_unmatched(self) -> None:
        calls: list[str] = []

        entries, errors = await _resolve({}, ["INPUT", "OUT/**/*.orb"], calls=calls)

        assert entries == []
        assert calls == ["/remote/task", "/remote/task"]
        assert [(path, type(err)) for path, err in errors] == [
            ("INPUT", FileNotFoundError),
            ("OUT/**/*.orb", FileNotFoundError),
        ]
        assert all(isinstance(err, FileNotFoundError) for _, err in errors)

    async def test_listing_failure_is_reported_for_that_selection_only(self) -> None:
        tree = {
            "": [
                ("GOOD", FILEXFER_TYPE_REGULAR),
                ("BAD", FILEXFER_TYPE_DIRECTORY),
            ],
            "BAD": [("x", FILEXFER_TYPE_REGULAR)],
        }
        failure = SFTPFailure("listing failed")

        entries, errors = await _resolve(
            tree,
            ["GOOD", "BAD/x"],
            failures={"BAD": failure},
        )

        assert entries == [_entry("GOOD", FILE)]
        assert len(errors) == 1
        path, err = errors[0]
        assert path == "BAD/x"
        assert err is failure


# =============================================================================
# Trace contract
# =============================================================================


class TestResolveTrace:
    async def test_task_id_is_accepted_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=RESOLVER_LOGGER):
            entries, errors = await _resolve(
                _SHARED_TREE, ["INPUT"], task_id=TaskId(42)
            )

        assert entries == [_entry("INPUT", FILE)]
        assert errors == []
        records = [
            record
            for record in caplog.records
            if record.name == RESOLVER_LOGGER
            and record.getMessage() == "OUTPUT_RESOLVE"
        ]
        assert len(records) == 1
        fields = extra_fields(records[0])
        fields.pop("message", None)
        assert fields == {
            "task_id": TaskId(42),
            "selections": 1,
            "entries": 1,
            "files": 1,
            "directories": 0,
            "errors": 0,
        }
