# region MODULE_CONTRACT
# PURPOSE: Pin submission-boundary input normalization — legacy literal/glob claims and encodings, exact fort.9 base64 compatibility, required declarations, typed/unsafe metadata separation, explicit-entry ambiguity, purity, immutable snapshot shape, and content-free trace logs — so public submit payloads become one typed domain snapshot exactly once.
# SCOPE: normalize_task_inputs legacy and explicit modes, UTF-8 and lenient base64 conversion, glob parent materialization, missing/ambiguous declaration errors, metadata preservation, no filesystem access, and NORMALIZE_INPUTS logging.
# KEYWORDS: task inputs, submission boundary, normalization, legacy metadata, base64, fort.9, snapshot, ADR-0019
# endregion MODULE_CONTRACT

from __future__ import annotations

import builtins
import logging

import pytest

from yascheduler.domain import (
    TYPED_METADATA_KEYS,
    Engine,
    InvalidTaskSnapshotError,
    MissingInputFileError,
    TaskInputDirectory,
    TaskInputFile,
    UnsafeTaskPathError,
)
from yascheduler.entrypoints import input_normalizer
from yascheduler.entrypoints.input_normalizer import normalize_task_inputs


def _engine(*input_files: str) -> Engine:
    return Engine(name="test_engine", spawn="run", input_files=input_files)


def _find_log_record(
    caplog: pytest.LogCaptureFixture, source: str
) -> logging.LogRecord:
    records = [
        record
        for record in caplog.records
        if record.name == "yascheduler.entrypoints.input_normalizer"
        and record.getMessage() == "NORMALIZE_INPUTS"
        and record.__dict__.get("source") == source
    ]
    assert len(records) == 1
    return records[0]


class TestLegacyLiteralDeclarations:
    def test_literal_value_is_utf8_encoded(self) -> None:
        snapshot, remaining = normalize_task_inputs(
            {"INPUT": "line1\nline2"}, _engine("INPUT"), None
        )

        assert snapshot == (TaskInputFile(path="INPUT", content=b"line1\nline2"),)
        assert remaining == {"INPUT": "line1\nline2"}

    def test_non_ascii_literal_value_is_utf8_encoded(self) -> None:
        snapshot, _ = normalize_task_inputs({"INPUT": "текст"}, _engine("INPUT"), None)

        assert snapshot == (TaskInputFile(path="INPUT", content="текст".encode()),)

    def test_fort9_standard_base64_is_decoded(self) -> None:
        snapshot, _ = normalize_task_inputs({"fort.9": "QUJD"}, _engine("fort.9"), None)

        assert snapshot == (TaskInputFile(path="fort.9", content=b"ABC"),)

    def test_fort9_whitespace_and_newlines_are_ignored(self) -> None:
        snapshot, _ = normalize_task_inputs(
            {"fort.9": " \nQUJD\n "}, _engine("fort.9"), None
        )

        assert snapshot == (TaskInputFile(path="fort.9", content=b"ABC"),)

    def test_fort9_missing_padding_is_repaired(self) -> None:
        snapshot, _ = normalize_task_inputs({"fort.9": "QUI"}, _engine("fort.9"), None)

        assert snapshot == (TaskInputFile(path="fort.9", content=b"AB"),)

    def test_fort9_invalid_base64_is_rejected(self) -> None:
        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_task_inputs({"fort.9": "a"}, _engine("fort.9"), None)

        assert info.value.path == "fort.9"
        assert "not valid base64" in info.value.reason

    @pytest.mark.parametrize("value", [b"ABC", 42, {"nested": 1}])
    def test_non_string_literal_value_is_rejected(self, value: object) -> None:
        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_task_inputs({"INPUT": value}, _engine("INPUT"), None)

        assert info.value.path == "INPUT"
        assert type(value).__name__ in info.value.reason

    def test_missing_literal_declaration_is_rejected(self) -> None:
        with pytest.raises(MissingInputFileError) as info:
            normalize_task_inputs({"other": "value"}, _engine("INPUT"), None)

        assert info.value.filename == "INPUT"
        assert info.value.engine_name == "test_engine"

    def test_typed_metadata_keys_are_never_claimed(self) -> None:
        typed_metadata: dict[str, object] = {
            "engine": "other-engine",
            "local_folder": "local",
            "webhook_url": "https://example.invalid/hook",
            "webhook_custom_params": {"token": "redacted"},
            "error": "previous-error",
            "remote_folder": "remote",
        }
        assert set(typed_metadata) == TYPED_METADATA_KEYS
        metadata = {**typed_metadata, "INPUT": "body"}

        snapshot, remaining = normalize_task_inputs(metadata, _engine("*"), None)

        assert snapshot == (TaskInputFile(path="INPUT", content=b"body"),)
        assert remaining == metadata

    def test_claimed_keys_stay_in_metadata_and_unrelated_metadata_preserved(
        self,
    ) -> None:
        metadata = {
            "INPUT": "body",
            "count": 42,
            "nested": {"items": [1, 2]},
        }

        snapshot, remaining = normalize_task_inputs(metadata, _engine("INPUT"), None)

        assert snapshot == (TaskInputFile(path="INPUT", content=b"body"),)
        # Transition: claimed keys stay in metadata until the deployer flip (4.1).
        assert remaining == metadata

    def test_empty_input_declarations_yield_empty_snapshot_and_metadata(self) -> None:
        metadata = {"engine": "x", "count": 42, "nested": {"a": 1}}

        snapshot, remaining = normalize_task_inputs(metadata, _engine(), None)

        assert snapshot == ()
        assert remaining == metadata

    def test_snapshot_is_sorted_tuple(self) -> None:
        snapshot, _ = normalize_task_inputs(
            {"z": "last", "a": "first"}, _engine("z", "a"), None
        )

        assert isinstance(snapshot, tuple)
        assert [entry.path for entry in snapshot] == ["a", "z"]

    def test_overlapping_declarations_share_one_claimed_key(self) -> None:
        snapshot, remaining = normalize_task_inputs(
            {"INPUT": "body"}, _engine("INPUT", "IN*"), None
        )

        assert snapshot == (TaskInputFile(path="INPUT", content=b"body"),)
        assert remaining == {"INPUT": "body"}


class TestLegacyGlobDeclarations:
    def test_glob_claims_matching_keys_and_materializes_parents(self) -> None:
        metadata = {
            "basis/Si.orb": "root-orb",
            "basis/deep/x.orb": "deep-orb",
            "unrelated": 42,
        }

        snapshot, remaining = normalize_task_inputs(
            metadata, _engine("basis/**/*.orb"), None
        )

        assert snapshot == (
            TaskInputDirectory(path="basis"),
            TaskInputFile(path="basis/Si.orb", content=b"root-orb"),
            TaskInputDirectory(path="basis/deep"),
            TaskInputFile(path="basis/deep/x.orb", content=b"deep-orb"),
        )
        assert remaining == metadata

    def test_glob_with_no_claimable_keys_is_rejected(self) -> None:
        with pytest.raises(MissingInputFileError) as info:
            normalize_task_inputs(
                {"text.txt": "not-an-orb", "count": 42},
                _engine("*.orb"),
                None,
            )

        assert info.value.filename == "*.orb"
        assert info.value.engine_name == "test_engine"

    def test_unsafe_keys_remain_unclaimed(self) -> None:
        metadata = {
            "../evil": "parent",
            "/abs": "absolute",
            "a\\b": "backslash",
            "safe": "body",
        }

        snapshot, remaining = normalize_task_inputs(metadata, _engine("*"), None)

        assert snapshot == (TaskInputFile(path="safe", content=b"body"),)
        # Unsafe keys are never claimed and stay in the metadata untouched.
        assert remaining == metadata

    def test_unsafe_glob_declaration_is_rejected(self) -> None:
        with pytest.raises(UnsafeTaskPathError) as info:
            normalize_task_inputs({"safe": "body"}, _engine("../**"), None)

        assert info.value.path == "../**"


class TestExplicitEntries:
    def test_parents_are_materialized(self) -> None:
        entries = [TaskInputFile(path="basis/deep/Si.orb", content=b"orb")]

        snapshot, remaining = normalize_task_inputs(
            {"unrelated": 42}, _engine("INPUT"), entries
        )

        assert snapshot == (
            TaskInputDirectory(path="basis"),
            TaskInputDirectory(path="basis/deep"),
            TaskInputFile(path="basis/deep/Si.orb", content=b"orb"),
        )
        assert remaining == {"unrelated": 42}

    def test_duplicate_path_is_rejected(self) -> None:
        entries = [
            TaskInputFile(path="a", content=b"first"),
            TaskInputFile(path="a", content=b"second"),
        ]

        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_task_inputs({}, _engine(), entries)

        assert info.value.path == "a"

    def test_file_ancestor_conflict_is_rejected(self) -> None:
        entries = [
            TaskInputFile(path="a", content=b""),
            TaskInputFile(path="a/b", content=b""),
        ]

        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_task_inputs({}, _engine(), entries)

        assert info.value.path == "a"

    def test_unsafe_path_is_rejected(self) -> None:
        with pytest.raises(UnsafeTaskPathError) as info:
            normalize_task_inputs(
                {}, _engine(), [TaskInputFile(path="/abs", content=b"")]
            )

        assert info.value.path == "/abs"

    def test_empty_sequence_is_empty_snapshot_without_legacy_fallback(self) -> None:
        metadata = {"unrelated": "kept", "local_folder": "local"}

        snapshot, remaining = normalize_task_inputs(metadata, _engine("INPUT"), [])

        assert snapshot == ()
        assert remaining == metadata

    def test_literal_payload_ambiguity_is_rejected(self) -> None:
        entries = [TaskInputFile(path="explicit", content=b"x")]

        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_task_inputs({"INPUT": "legacy-body"}, _engine("INPUT"), entries)

        assert info.value.path == "INPUT"

    def test_glob_payload_ambiguity_names_sorted_first_key(self) -> None:
        entries = [TaskInputFile(path="explicit", content=b"x")]
        metadata = {
            "basis/z.orb": "z",
            "basis/a.orb": "a",
        }

        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_task_inputs(metadata, _engine("basis/**/*.orb"), entries)

        assert info.value.path == "basis/a.orb"

    def test_unrelated_metadata_is_preserved_verbatim(self) -> None:
        metadata = {
            "count": 42,
            "nested": {"items": [1, 2]},
            "local_folder": "local",
        }

        snapshot, remaining = normalize_task_inputs(
            metadata, _engine("INPUT"), [TaskInputDirectory(path="empty")]
        )

        assert snapshot == (TaskInputDirectory(path="empty"),)
        assert remaining == metadata


class TestPurity:
    def test_normalization_does_not_open_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_open(*args: object, **kwargs: object) -> object:
            raise AssertionError("normalization must not read the filesystem")

        monkeypatch.setattr(builtins, "open", fail_open)
        engine = _engine("INPUT")

        legacy_snapshot, _ = normalize_task_inputs({"INPUT": "legacy"}, engine, None)
        explicit_snapshot, _ = normalize_task_inputs(
            {}, engine, [TaskInputFile(path="explicit", content=b"data")]
        )

        assert legacy_snapshot == (TaskInputFile(path="INPUT", content=b"legacy"),)
        assert explicit_snapshot == (TaskInputFile(path="explicit", content=b"data"),)

    def test_module_imports_no_filesystem_modules(self) -> None:
        assert not hasattr(input_normalizer, "os")
        assert not hasattr(input_normalizer, "Path")


class TestLogs:
    def test_legacy_record_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = _engine("INPUT")

        with caplog.at_level(
            logging.DEBUG, logger="yascheduler.entrypoints.input_normalizer"
        ):
            normalize_task_inputs({"INPUT": "body"}, engine, None)

        record = _find_log_record(caplog, "legacy")
        assert record.__dict__["engine"] == engine.name
        assert record.__dict__["entries"] == 1

    def test_explicit_record_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = _engine("INPUT")

        with caplog.at_level(
            logging.DEBUG, logger="yascheduler.entrypoints.input_normalizer"
        ):
            normalize_task_inputs(
                {}, engine, [TaskInputFile(path="basis/x", content=b"x")]
            )

        record = _find_log_record(caplog, "explicit")
        assert record.__dict__["engine"] == engine.name
        assert record.__dict__["entries"] == 2

    def test_logs_do_not_contain_file_contents(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        marker = "UNIQUE_NORMALIZER_SECRET_MARKER_6d4c2e"

        with caplog.at_level(
            logging.DEBUG, logger="yascheduler.entrypoints.input_normalizer"
        ):
            normalize_task_inputs({"INPUT": marker}, _engine("INPUT"), None)
            normalize_task_inputs(
                {},
                _engine("INPUT"),
                [TaskInputFile(path="explicit", content=marker.encode())],
            )

        for record in caplog.records:
            assert marker not in record.getMessage()
            assert marker not in repr(record.__dict__)
