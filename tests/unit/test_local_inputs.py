# region MODULE_CONTRACT
# PURPOSE: Pin the local input-capture contract — exact bytes, explicit complete trees, component-aware glob selection, required selections, non-followed symlink and unsupported-entry rejection, root and selection validation ordering, read-time source rechecks, immutable normalized results, facade exports, and content-free structured logs.
# SCOPE: capture_input_snapshot behavior over temporary filesystem fixtures, including binary and large files, hidden and empty entries, overlapping selections, symlinks, FIFOs, TOCTOU-style mutations, root failures, result shape/kind/frozen semantics, and content-free CAPTURE/CAPTURE_DONE/CAPTURE_FAILED trajectory logs.
# KEYWORDS: task inputs, capture, snapshot, local filesystem, glob, symlink, TOCTOU, ADR-0019
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.log_assertions import extra_fields
from yascheduler.domain import (
    TaskInputCaptureError,
    TaskInputDirectory,
    TaskInputFile,
    TaskInputKind,
    TaskInputSnapshot,
    UnsafeTaskPathError,
)
from yascheduler.infra import capture_input_snapshot, local_inputs


class _ScandirResult:
    """Minimal context-manager shim used to inject mutations around os.scandir."""

    def __init__(self, items: list[os.DirEntry[str]]) -> None:
        self._items = items

    def __enter__(self) -> Iterator[os.DirEntry[str]]:
        return iter(self._items)

    def __exit__(self, *exc: object) -> None:
        return None


def _capture(root: Path, *selections: str) -> TaskInputSnapshot:
    return capture_input_snapshot(root, list(selections))


def _file_content(snapshot: TaskInputSnapshot, path: str) -> bytes:
    entry = next(item for item in snapshot if item.path == path)
    assert isinstance(entry, TaskInputFile)
    return entry.content


def _log_records(
    caplog: pytest.LogCaptureFixture, message: str
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "yascheduler.infra.local_inputs"
        and record.getMessage() == message
    ]


def _find_log_record(
    caplog: pytest.LogCaptureFixture, message: str
) -> logging.LogRecord:
    records = _log_records(caplog, message)
    assert len(records) == 1
    return records[0]


class TestByteFidelity:
    def test_preserves_nul_and_binary_bytes(self, tmp_path: Path) -> None:
        content = b"\x00\x01\x02\xff\xfe binary payload \x00"
        (tmp_path / "INPUT").write_bytes(content)

        snapshot = _capture(tmp_path, "INPUT")

        assert snapshot == (TaskInputFile(path="INPUT", content=content),)

    def test_preserves_invalid_utf8_bytes(self, tmp_path: Path) -> None:
        content = b"\xff\xfe\x80\x81 invalid-utf8 \xc3\x28"
        (tmp_path / "INPUT").write_bytes(content)

        assert _file_content(_capture(tmp_path, "INPUT"), "INPUT") == content

    def test_preserves_crlf_without_newline_normalization(self, tmp_path: Path) -> None:
        content = b"first line\r\nsecond line\r\nmixed\nending\r"
        (tmp_path / "INPUT").write_bytes(content)

        assert _file_content(_capture(tmp_path, "INPUT"), "INPUT") == content

    def test_preserves_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / "INPUT").write_bytes(b"")

        snapshot = _capture(tmp_path, "INPUT")

        assert snapshot == (TaskInputFile(path="INPUT", content=b""),)

    def test_preserves_large_file_crossing_read_chunk(self, tmp_path: Path) -> None:
        content = (b"0123456789abcdef" * ((1 << 20) // 16)) + b"tail-after-chunk"
        assert len(content) > 1 << 20
        (tmp_path / "INPUT").write_bytes(content)

        captured = _file_content(_capture(tmp_path, "INPUT"), "INPUT")

        assert captured == content
        assert len(captured) == len(content)

    def test_preserves_non_utf8_filename(self, tmp_path: Path) -> None:
        raw_name = b"\xff-input.bin"
        raw_path = os.fsencode(tmp_path) + b"/" + raw_name
        try:
            fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            pytest.skip("filesystem does not allow a non-UTF-8 filename")
        try:
            os.write(fd, b"raw-name-content")
        finally:
            os.close(fd)

        snapshot = _capture(tmp_path, "*")

        assert len(snapshot) == 1
        entry = snapshot[0]
        assert isinstance(entry, TaskInputFile)
        assert os.fsencode(entry.path) == raw_name
        assert entry.content == b"raw-name-content"


class TestTreeShape:
    def test_explicit_file_is_captured_alone(self, tmp_path: Path) -> None:
        (tmp_path / "INPUT").write_bytes(b"body")
        (tmp_path / "unselected").write_bytes(b"other")

        assert _capture(tmp_path, "INPUT") == (
            TaskInputFile(path="INPUT", content=b"body"),
        )

    def test_explicit_directory_includes_complete_subtree(self, tmp_path: Path) -> None:
        basis = tmp_path / "basis"
        (basis / "deep").mkdir(parents=True)
        (basis / "root.orb").write_bytes(b"root")
        (basis / "deep" / "child.orb").write_bytes(b"child")

        snapshot = _capture(tmp_path, "basis")

        assert [entry.path for entry in snapshot] == [
            "basis",
            "basis/deep",
            "basis/deep/child.orb",
            "basis/root.orb",
        ]
        assert _file_content(snapshot, "basis/root.orb") == b"root"

    def test_explicit_directory_includes_hidden_descendants(
        self, tmp_path: Path
    ) -> None:
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / ".secret").write_bytes(b"hidden")
        (basis / ".hidden-dir").mkdir()
        (basis / ".hidden-dir" / "value").write_bytes(b"hidden-value")

        snapshot = _capture(tmp_path, "basis")

        assert _file_content(snapshot, "basis/.secret") == b"hidden"
        assert _file_content(snapshot, "basis/.hidden-dir/value") == b"hidden-value"

    def test_explicit_directory_preserves_empty_directories(
        self, tmp_path: Path
    ) -> None:
        basis = tmp_path / "basis"
        (basis / "empty" / "nested-empty").mkdir(parents=True)

        snapshot = _capture(tmp_path, "basis")

        assert snapshot == (
            TaskInputDirectory(path="basis"),
            TaskInputDirectory(path="basis/empty"),
            TaskInputDirectory(path="basis/empty/nested-empty"),
        )

    def test_nested_file_selection_materializes_parents(self, tmp_path: Path) -> None:
        (tmp_path / "basis" / "deep").mkdir(parents=True)
        (tmp_path / "basis" / "deep" / "Si.orb").write_bytes(b"orb")

        snapshot = _capture(tmp_path, "basis/deep/Si.orb")

        assert snapshot == (
            TaskInputDirectory(path="basis"),
            TaskInputDirectory(path="basis/deep"),
            TaskInputFile(path="basis/deep/Si.orb", content=b"orb"),
        )

    def test_snapshot_has_no_enclosing_root_directory(self, tmp_path: Path) -> None:
        (tmp_path / "INPUT").write_bytes(b"body")

        snapshot = _capture(tmp_path, "INPUT")

        assert all(entry.path not in ("", ".") for entry in snapshot)
        assert all(entry.path != tmp_path.name for entry in snapshot)

    def test_overlapping_selections_deduplicate_paths(self, tmp_path: Path) -> None:
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "Si.orb").write_bytes(b"orb")

        snapshot = _capture(tmp_path, "basis", "basis/Si.orb", "basis/*")

        assert [entry.path for entry in snapshot] == ["basis", "basis/Si.orb"]

    def test_duplicate_identical_selections_deduplicate_paths(
        self, tmp_path: Path
    ) -> None:
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "Si.orb").write_bytes(b"orb")

        snapshot = _capture(tmp_path, "basis", "basis")

        assert [entry.path for entry in snapshot] == ["basis", "basis/Si.orb"]


class TestGlobLanguageAtCapture:
    def test_star_matches_hidden_names(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").write_bytes(b"hidden")

        snapshot = _capture(tmp_path, "*")

        assert _file_content(snapshot, ".hidden") == b"hidden"

    def test_star_is_per_component(self, tmp_path: Path) -> None:
        root_file = tmp_path / "root.orb"
        root_file.write_bytes(b"root")
        (tmp_path / "basis").mkdir()
        (tmp_path / "basis" / "nested.orb").write_bytes(b"nested")

        snapshot = _capture(tmp_path, "*.orb")

        assert [entry.path for entry in snapshot] == ["root.orb"]

    def test_question_mark_matches_one_character(self, tmp_path: Path) -> None:
        (tmp_path / "result1.dat").write_bytes(b"one")
        (tmp_path / "result12.dat").write_bytes(b"two")

        assert _capture(tmp_path, "result?.dat") == (
            TaskInputFile(path="result1.dat", content=b"one"),
        )

    def test_character_class_matches_one_character(self, tmp_path: Path) -> None:
        (tmp_path / "result1.dat").write_bytes(b"one")
        (tmp_path / "resultx.dat").write_bytes(b"x")

        assert _capture(tmp_path, "result[0-9].dat") == (
            TaskInputFile(path="result1.dat", content=b"one"),
        )

    def test_character_class_escapes_metacharacter(self, tmp_path: Path) -> None:
        (tmp_path / "a?b").write_bytes(b"question")
        (tmp_path / "axb").write_bytes(b"other")

        assert _capture(tmp_path, "a[?]b") == (
            TaskInputFile(path="a?b", content=b"question"),
        )

    def test_double_star_matches_zero_and_more_levels(self, tmp_path: Path) -> None:
        basis = tmp_path / "basis"
        (basis / "deep").mkdir(parents=True)
        (basis / "a.orb").write_bytes(b"a")
        (basis / "deep" / "b.orb").write_bytes(b"b")

        snapshot = _capture(tmp_path, "basis/**/*.orb")

        assert [entry.path for entry in snapshot] == [
            "basis",
            "basis/a.orb",
            "basis/deep",
            "basis/deep/b.orb",
        ]

    def test_leading_double_star_matches_root_level_x(self, tmp_path: Path) -> None:
        (tmp_path / "x").write_bytes(b"root-x")
        (tmp_path / "deep").mkdir()
        (tmp_path / "deep" / "x").write_bytes(b"deep-x")

        snapshot = _capture(tmp_path, "**/x")

        assert _file_content(snapshot, "x") == b"root-x"
        assert _file_content(snapshot, "deep/x") == b"deep-x"

    def test_trailing_double_star_selects_directory_and_subtree(
        self, tmp_path: Path
    ) -> None:
        basis = tmp_path / "basis"
        (basis / "deep").mkdir(parents=True)
        (basis / "root.txt").write_bytes(b"root")
        (basis / "deep" / "child.txt").write_bytes(b"child")

        snapshot = _capture(tmp_path, "basis/**")

        assert [entry.path for entry in snapshot] == [
            "basis",
            "basis/deep",
            "basis/deep/child.txt",
            "basis/root.txt",
        ]

    def test_double_star_alone_excludes_root_level_files(self, tmp_path: Path) -> None:
        (tmp_path / "root.txt").write_bytes(b"root")
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "child.txt").write_bytes(b"child")
        (basis / "empty").mkdir()

        snapshot = _capture(tmp_path, "**")

        assert [entry.path for entry in snapshot] == [
            "basis",
            "basis/child.txt",
            "basis/empty",
        ]

    def test_deeper_glob_below_file_yields_required_error(self, tmp_path: Path) -> None:
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "x.orb").write_bytes(b"x")

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "basis/x.orb/**")

        assert info.value.path == "basis/x.orb/**"
        assert "matched no" in info.value.reason


class TestRequiredSelections:
    def test_missing_explicit_path_reports_raw_selection(self, tmp_path: Path) -> None:
        selection = "./missing/INPUT"

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, selection)

        assert info.value.path == selection
        assert "not found" in info.value.reason

    def test_glob_with_no_matches_reports_raw_selection(self, tmp_path: Path) -> None:
        selection = "missing/*.orb"

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, selection)

        assert info.value.path == selection
        assert "matched no" in info.value.reason

    def test_failure_happens_after_other_selections_matched(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "INPUT").write_bytes(b"body")

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "INPUT", "missing")

        assert info.value.path == "missing"
        assert "not found" in info.value.reason

    def test_empty_directory_matches_trailing_double_star(self, tmp_path: Path) -> None:
        (tmp_path / "basis" / "empty").mkdir(parents=True)

        snapshot = _capture(tmp_path, "basis/**")

        assert snapshot == (
            TaskInputDirectory(path="basis"),
            TaskInputDirectory(path="basis/empty"),
        )

    def test_empty_selections_returns_empty_snapshot(self, tmp_path: Path) -> None:
        assert capture_input_snapshot(tmp_path, []) == ()


class TestSymlinksNeverFollowed:
    def test_explicit_symlink_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_bytes(b"target")
        (tmp_path / "link").symlink_to(target)

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "link")

        assert info.value.path == "link"
        assert "symbolic link" in info.value.reason

    def test_dangling_symlink_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "link").symlink_to(tmp_path / "does-not-exist")

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "link")

        assert info.value.path == "link"
        assert "symbolic link" in info.value.reason

    def test_glob_matched_symlink_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_bytes(b"target")
        (tmp_path / "link").symlink_to(target)

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "*ink")

        assert info.value.path == "link"
        assert "symbolic link" in info.value.reason

    def test_symlink_in_selected_subtree_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_bytes(b"target")
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "link").symlink_to(target)

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "basis")

        assert info.value.path == "basis/link"
        assert "symbolic link" in info.value.reason

    def test_unselected_symlink_under_double_star_is_skipped(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "target"
        target.write_bytes(b"target")
        (tmp_path / "lnk").symlink_to(target)
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "a.orb").write_bytes(b"orb")

        snapshot = _capture(tmp_path, "**/*.orb")

        assert [entry.path for entry in snapshot] == ["basis", "basis/a.orb"]
        assert all(entry.path != "lnk" for entry in snapshot)


@pytest.mark.skipif(os.name != "posix", reason="mkfifo is POSIX-only")
class TestUnsupportedEntryTypes:
    def test_explicit_fifo_is_rejected(self, tmp_path: Path) -> None:
        os.mkfifo(tmp_path / "pipe")

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "pipe")

        assert info.value.path == "pipe"

    def test_fifo_inside_selected_subtree_is_rejected(self, tmp_path: Path) -> None:
        basis = tmp_path / "basis"
        basis.mkdir()
        os.mkfifo(basis / "pipe")

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "basis")

        assert info.value.path == "basis/pipe"

    def test_unmatched_fifo_under_traversed_branch_is_ignored(
        self, tmp_path: Path
    ) -> None:
        os.mkfifo(tmp_path / "pipe")
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "a.orb").write_bytes(b"orb")

        snapshot = _capture(tmp_path, "**/*.orb")

        assert [entry.path for entry in snapshot] == ["basis", "basis/a.orb"]


class TestSourceStability:
    def test_file_disappearing_between_scan_and_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "INPUT"
        target.write_bytes(b"body")
        real_scandir = os.scandir
        mutated = False

        def mutating_scandir(path: os.PathLike[str]) -> _ScandirResult:
            nonlocal mutated
            with real_scandir(path) as iterator:
                items = list(iterator)
            if not mutated and Path(path) == tmp_path:
                mutated = True
                target.unlink()
            return _ScandirResult(items)

        monkeypatch.setattr(local_inputs.os, "scandir", mutating_scandir)

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "INPUT")

        assert info.value.path == "INPUT"
        assert info.value.reason

    def test_file_swapped_to_symlink_between_scan_and_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "INPUT"
        target.write_bytes(b"body")
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"new")
        real_scandir = os.scandir
        mutated = False

        def mutating_scandir(path: os.PathLike[str]) -> _ScandirResult:
            nonlocal mutated
            with real_scandir(path) as iterator:
                items = list(iterator)
            if not mutated and Path(path) == tmp_path:
                mutated = True
                target.unlink()
                target.symlink_to(replacement)
            return _ScandirResult(items)

        monkeypatch.setattr(local_inputs.os, "scandir", mutating_scandir)

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "INPUT")

        assert info.value.path == "INPUT"
        assert "symbolic link" in info.value.reason

    def test_inode_replacement_between_scan_and_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "INPUT"
        target.write_bytes(b"original")
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"replacement")
        real_open = os.open
        replaced = False

        def replacing_open(
            path: os.PathLike[str],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if not replaced and Path(path) == target:
                replaced = True
                replacement.replace(target)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(local_inputs.os, "open", replacing_open)

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "INPUT")

        assert info.value.path == "INPUT"
        assert "replaced" in info.value.reason

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root ignores mode bits",
    )
    def test_unreadable_file_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "INPUT"
        target.write_bytes(b"secret")
        target.chmod(0o000)
        try:
            with pytest.raises(TaskInputCaptureError) as info:
                _capture(tmp_path, "INPUT")
        finally:
            target.chmod(0o600)

        assert info.value.path == "INPUT"


class TestRootErrors:
    def test_nonexistent_root_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path / "missing", "INPUT")

        assert info.value.path == "."

    def test_regular_file_root_is_rejected(self, tmp_path: Path) -> None:
        root_file = tmp_path / "root-file"
        root_file.write_bytes(b"not a directory")

        with pytest.raises(TaskInputCaptureError) as info:
            _capture(root_file, "INPUT")

        assert info.value.path == "."


class TestSelectionValidationOrdering:
    @pytest.mark.parametrize(
        "selection",
        ["../x", "/abs", "a\\b", "C:/x", "."],
    )
    def test_unsafe_selection_raises_before_missing_root(
        self, tmp_path: Path, selection: str
    ) -> None:
        with pytest.raises(UnsafeTaskPathError) as info:
            _capture(tmp_path / "missing-root", selection)

        assert info.value.path == selection

    def test_all_selections_validated_before_missing_root(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeTaskPathError) as info:
            _capture(tmp_path / "missing-root", "INPUT", "../x")

        assert info.value.path == "../x"


class TestFacadeAndExports:
    def test_infra_facade_exports_capture_input_snapshot(self, tmp_path: Path) -> None:
        (tmp_path / "INPUT").write_bytes(b"body")

        assert callable(capture_input_snapshot)
        assert capture_input_snapshot(tmp_path, ["INPUT"]) == (
            TaskInputFile(path="INPUT", content=b"body"),
        )

    def test_domain_facade_exports_capture_error(self) -> None:
        error = TaskInputCaptureError("INPUT", "reason")

        assert error.path == "INPUT"
        assert error.reason == "reason"

    def test_capture_error_carries_path_reason_and_message(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(TaskInputCaptureError) as info:
            _capture(tmp_path, "missing")

        error = info.value
        message = str(error)
        assert error.path == "missing"
        assert error.reason
        assert error.path in message
        assert error.reason in message


class TestSnapshotShape:
    def test_result_is_tuple(self, tmp_path: Path) -> None:
        (tmp_path / "INPUT").write_bytes(b"body")

        snapshot = _capture(tmp_path, "INPUT")

        assert isinstance(snapshot, tuple)

    def test_entries_are_sorted_by_path(self, tmp_path: Path) -> None:
        (tmp_path / "zeta").write_bytes(b"z")
        alpha = tmp_path / "alpha"
        alpha.mkdir()
        (alpha / "beta").write_bytes(b"b")

        snapshot = _capture(tmp_path, "*")

        paths = [entry.path for entry in snapshot]
        assert paths == ["alpha", "alpha/beta", "zeta"]
        assert paths == sorted(paths)

    def test_entries_are_known_value_objects_with_kinds(self, tmp_path: Path) -> None:
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "Si.orb").write_bytes(b"orb")

        snapshot = _capture(tmp_path, "basis")

        assert all(
            isinstance(entry, (TaskInputFile, TaskInputDirectory)) for entry in snapshot
        )
        file_entry = next(entry for entry in snapshot if entry.path == "basis/Si.orb")
        dir_entry = next(entry for entry in snapshot if entry.path == "basis")
        assert file_entry.kind is TaskInputKind.FILE
        assert dir_entry.kind is TaskInputKind.DIRECTORY

    def test_file_entry_is_frozen(self, tmp_path: Path) -> None:
        (tmp_path / "INPUT").write_bytes(b"body")
        entry = _capture(tmp_path, "INPUT")[0]

        with pytest.raises(AttributeError):
            entry.path = "other"  # type: ignore[misc]

    def test_directory_entry_is_frozen(self, tmp_path: Path) -> None:
        (tmp_path / "basis").mkdir()
        entry = _capture(tmp_path, "basis")[0]

        with pytest.raises(AttributeError):
            entry.path = "other"  # type: ignore[misc]


class TestStructuredLogs:
    def test_capture_emits_record_with_root_and_selection_count(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "one").write_bytes(b"1")
        (tmp_path / "two").write_bytes(b"2")

        with caplog.at_level(logging.DEBUG, logger="yascheduler.infra.local_inputs"):
            _capture(tmp_path, "one", "two")

        record = _find_log_record(caplog, "CAPTURE")
        fields = extra_fields(record)
        assert fields["root"] == str(tmp_path)
        assert fields["selections"] == 2
        assert fields["entry_count"] == 0

    def test_capture_done_emits_counts(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        basis = tmp_path / "basis"
        basis.mkdir()
        (basis / "Si.orb").write_bytes(b"orb")

        with caplog.at_level(logging.DEBUG, logger="yascheduler.infra.local_inputs"):
            snapshot = _capture(tmp_path, "basis")

        record = _find_log_record(caplog, "CAPTURE_DONE")
        fields = extra_fields(record)
        assert fields["root"] == str(tmp_path)
        assert fields["files"] == 1
        assert fields["directories"] == len(snapshot) - 1
        assert fields["entry_count"] == len(snapshot)

    def test_capture_starts_before_selection_validation(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger="yascheduler.infra.local_inputs"),
            pytest.raises(UnsafeTaskPathError),
        ):
            _capture(tmp_path, "../escape")

        assert caplog.records[0].getMessage() == "CAPTURE"
        start = extra_fields(caplog.records[0])
        assert start["root"] == str(tmp_path)
        assert start["selections"] == 1
        assert start["entry_count"] == 0

        failed = _find_log_record(caplog, "CAPTURE_FAILED")
        fields = extra_fields(failed)
        assert fields["failure"] == "UnsafeTaskPathError"
        assert fields["path"] == "../escape"
        assert fields["entry_count"] == 0

    def test_capture_failed_reports_missing_selection(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger="yascheduler.infra.local_inputs"),
            pytest.raises(TaskInputCaptureError),
        ):
            _capture(tmp_path, "basis/**/*.orb")

        failed = _find_log_record(caplog, "CAPTURE_FAILED")
        fields = extra_fields(failed)
        assert fields["path"] == "basis/**/*.orb"
        assert fields["selection"] == "basis/**/*.orb"
        assert fields["entry_count"] == 0
        assert fields["failure"] == "TaskInputCaptureError"

    def test_capture_failed_read_reports_progress_without_exception_text(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = "UNIQUE_CAPTURE_READ_SECRET_31d7"
        target = tmp_path / "INPUT"
        target.write_bytes(b"body")
        other = tmp_path / "other"
        other.write_bytes(b"other")
        real_open = local_inputs.os.open

        def failing_open(
            path: os.PathLike[str],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if Path(path) == target:
                raise PermissionError(f"payload={marker}")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(local_inputs.os, "open", failing_open)

        with (
            caplog.at_level(logging.DEBUG, logger="yascheduler.infra.local_inputs"),
            pytest.raises(TaskInputCaptureError),
        ):
            _capture(tmp_path, "INPUT", "other")

        failed = _find_log_record(caplog, "CAPTURE_FAILED")
        fields = extra_fields(failed)
        assert fields["path"] == "INPUT"
        assert fields["selection"] is None
        assert fields["entry_count"] == 2
        assert fields["failure"] == "TaskInputCaptureError"
        assert marker not in caplog.text
        assert marker not in repr([extra_fields(record) for record in caplog.records])
        assert all(record.exc_info is None for record in caplog.records)

    def test_logs_do_not_contain_file_contents(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        marker = b"UNIQUE_LOG_SECRET_MARKER_4f83a9"
        (tmp_path / "hidden-secret").write_bytes(marker)

        with caplog.at_level(logging.DEBUG, logger="yascheduler.infra.local_inputs"):
            _capture(tmp_path, "hidden-secret")

        marker_text = marker.decode()
        for record in caplog.records:
            assert marker_text not in record.getMessage()
            assert marker_text not in repr(extra_fields(record))
