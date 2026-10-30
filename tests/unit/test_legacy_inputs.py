"""One flat legacy payload conversion, shared by submission and migration 014."""
# region MODULE_CONTRACT
# PURPOSE: Pin the single rule for reading a stored legacy metadata payload, so the submission boundary and the forward migration cannot claim different keys or encode different bytes.
# SCOPE: Literal and glob claims over a flat payload, the recorded UTF-8/`fort.9` value meanings, required declarations, unsafe keys and declarations, and non-mutating failure behavior.
# KEYWORDS: legacy inputs, flat payload, claim, snapshot, fort.9, base64
# endregion MODULE_CONTRACT

from __future__ import annotations

import pytest

from yascheduler.application import legacy_input_snapshot
from yascheduler.domain import (
    InvalidTaskSnapshotError,
    MissingInputFileError,
    TaskInputDirectory,
    TaskInputFile,
    UnsafeTaskPathError,
)

pytestmark = pytest.mark.unit

__all__: list[str] = []


# region TEST_literal_claim
def test_literal_declaration_claims_only_its_own_key() -> None:
    """A key that also has children stays a file; the children are not its subtree."""
    payload: dict[str, object] = {
        "INPUT": "text",
        "basis": "placeholder",
        "basis/Si.orb": "orb",
    }

    snapshot, remaining = legacy_input_snapshot("abacus", ("INPUT", "basis"), payload)

    assert snapshot == (
        TaskInputFile(path="INPUT", content=b"text"),
        TaskInputFile(path="basis", content=b"placeholder"),
    )
    assert remaining == {"basis/Si.orb": "orb"}
    assert payload["basis"] == "placeholder"


def test_literal_declaration_without_its_key_is_missing() -> None:
    """Children never satisfy a literal declaration: no directory is inferred."""
    payload: dict[str, object] = {"INPUT": "text", "basis/Si.orb": "orb"}

    with pytest.raises(MissingInputFileError) as info:
        legacy_input_snapshot("abacus", ("INPUT", "basis"), payload)

    assert info.value.filename == "basis"
    assert payload == {"INPUT": "text", "basis/Si.orb": "orb"}


# endregion TEST_literal_claim


# region TEST_glob_claim
def test_glob_selects_one_level_and_materializes_parents() -> None:
    payload: dict[str, object] = {
        "basis/one.orb": "one",
        "basis/nested/two.orb": "two",
    }

    snapshot, remaining = legacy_input_snapshot("abacus", ("basis/*",), payload)

    assert snapshot == (
        TaskInputDirectory(path="basis"),
        TaskInputFile(path="basis/one.orb", content=b"one"),
    )
    assert remaining == {"basis/nested/two.orb": "two"}


def test_trailing_double_star_selects_every_key_below() -> None:
    payload: dict[str, object] = {
        "basis": "not a directory",
        "basis/one.orb": "one",
        "basis/nested/two.orb": "two",
    }

    snapshot, _ = legacy_input_snapshot("abacus", ("basis/**",), payload)

    assert snapshot == (
        TaskInputDirectory(path="basis"),
        TaskInputDirectory(path="basis/nested"),
        TaskInputFile(path="basis/nested/two.orb", content=b"two"),
        TaskInputFile(path="basis/one.orb", content=b"one"),
    )


def test_double_star_component_selects_at_any_depth() -> None:
    payload: dict[str, object] = {
        "basis/one.orb": "one",
        "basis/nested/two.orb": "two",
        "basis/nested/two.dat": "keep",
    }

    snapshot, remaining = legacy_input_snapshot("abacus", ("basis/**/*.orb",), payload)

    assert [entry.path for entry in snapshot] == [
        "basis",
        "basis/nested",
        "basis/nested/two.orb",
        "basis/one.orb",
    ]
    assert remaining == {"basis/nested/two.dat": "keep"}


def test_overlapping_declarations_claim_each_key_once() -> None:
    payload: dict[str, object] = {"basis/one.orb": "one"}

    snapshot, remaining = legacy_input_snapshot(
        "abacus", ("basis/one.orb", "basis/*.orb", "basis/**"), payload
    )

    assert [entry.path for entry in snapshot] == ["basis", "basis/one.orb"]
    assert remaining == {}


def test_glob_without_matches_is_missing() -> None:
    with pytest.raises(MissingInputFileError):
        legacy_input_snapshot("abacus", ("basis/*.orb",), {"basis/x.dat": "x"})


# endregion TEST_glob_claim


# region TEST_encoding
@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("INPUT", "line one\nline two — ü", "line one\nline two — ü".encode()),
        ("empty.orb", "", b""),
        ("fort.9", "AAEC\n/w", b"\x00\x01\x02\xff"),
        ("looks-like-base64.txt", "SGVsbG8=", b"SGVsbG8="),
    ],
)
def test_recorded_encoding_meaning_is_preserved(
    path: str, value: str, expected: bytes
) -> None:
    snapshot, _ = legacy_input_snapshot("abacus", (path,), {path: value})

    assert snapshot == (TaskInputFile(path=path, content=expected),)


def test_fort9_decoder_keeps_historical_lenient_base64() -> None:
    """Pin deployment compatibility; invalid characters are deliberately discarded."""
    snapshot, _ = legacy_input_snapshot("abacus", ("fort.9",), {"fort.9": "QUJD$"})

    assert snapshot == (TaskInputFile(path="fort.9", content=b"ABC"),)


def test_malformed_fort9_names_the_key_and_not_the_value() -> None:
    malformed = "c2VjcmV0Q"

    with pytest.raises(InvalidTaskSnapshotError) as info:
        legacy_input_snapshot("abacus", ("fort.9",), {"fort.9": malformed})

    assert info.value.path == "fort.9"
    assert "base64" in str(info.value)
    assert malformed not in str(info.value)


def test_non_string_value_names_the_key_and_not_the_value() -> None:
    secret: dict[str, object] = {"secret": "do-not-log"}

    with pytest.raises(InvalidTaskSnapshotError) as info:
        legacy_input_snapshot("abacus", ("INPUT",), {"INPUT": secret})

    assert info.value.path == "INPUT"
    assert "got dict" in str(info.value)
    assert "do-not-log" not in str(info.value)


# endregion TEST_encoding


# region TEST_unrelated_payload
def test_unsafe_key_stays_unrelated_payload() -> None:
    payload: dict[str, object] = {"good.orb": "good", "../evil.orb": "payload"}

    snapshot, remaining = legacy_input_snapshot("abacus", ("*.orb",), payload)

    assert snapshot == (TaskInputFile(path="good.orb", content=b"good"),)
    assert remaining == {"../evil.orb": "payload"}


def test_typed_metadata_keys_are_never_claimed() -> None:
    payload: dict[str, object] = {
        "INPUT": "body",
        "remote_folder": "/keep/typed-metadata",
        "priority": 7,
    }

    snapshot, remaining = legacy_input_snapshot("abacus", ("INPUT",), payload)

    assert snapshot == (TaskInputFile(path="INPUT", content=b"body"),)
    assert remaining == {"remote_folder": "/keep/typed-metadata", "priority": 7}


def test_unsafe_declaration_is_a_configuration_error() -> None:
    with pytest.raises(UnsafeTaskPathError) as info:
        legacy_input_snapshot("abacus", ("../outside",), {"INPUT": "body"})

    assert info.value.path == "../outside"


# endregion TEST_unrelated_payload
