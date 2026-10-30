# region MODULE_CONTRACT
# PURPOSE: Pin the shared task-input contracts — entry value objects, kind discrimination, submission-record input defaults, canonical path and selection validation, the glob language and the traversal questions asked over it, missing-declaration reporting, and snapshot normalization — so capture, boundary normalization, submit validation, deployment, and legacy conversion cannot drift apart.
# SCOPE: TaskInputFile/TaskInputDirectory value semantics and kind discrimination; NewTask input_entries default and round-trip; submission-record contracts; facade exports; canonical path validation rejections; selection matching for literal/`*`/`?`/class/`**` patterns, hidden names, and kind rules; whether anything can still match below a directory and whether a selection component names an entry; normalize_input_snapshot materialization, deduplication, ordering, and conflict rejection; capture error shape.
# KEYWORDS: task inputs, snapshot, entry kinds, selection, glob, reachability, path validation
# endregion MODULE_CONTRACT

import pytest

from yascheduler.domain import (
    InvalidTaskSnapshotError,
    MissingInputFileError,
    NewTask,
    TaskInputDirectory,
    TaskInputEntry,
    TaskInputFile,
    TaskInputKind,
    UnsafeTaskPathError,
    ValidationError,
    normalize_input_snapshot,
    selection_matches,
    selection_names_entry,
    selection_reaches,
    validate_selection,
    validate_snapshot_selections,
    validate_task_path,
)


class _SensitiveRepr:
    """Non-input object whose repr must never enter user-facing diagnostics."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __repr__(self) -> str:
        return f"SENSITIVE_REPR<{self.marker}>"


@pytest.mark.parametrize(
    ("entry", "kind"),
    [
        (TaskInputFile(path="INPUT", content=b"\x00\xff"), TaskInputKind.FILE),
        (TaskInputDirectory(path="basis"), TaskInputKind.DIRECTORY),
    ],
    ids=["file", "directory"],
)
def test_entry_is_frozen_and_kind_discriminated(
    entry: TaskInputEntry, kind: TaskInputKind
) -> None:
    """Both entry kinds carry their own kind and resist mutation."""
    assert entry.path in {"INPUT", "basis"}
    assert entry.kind is kind
    with pytest.raises(AttributeError):
        entry.path = "other"  # type: ignore[misc]


def test_kind_values_are_the_persisted_wire_strings() -> None:
    """The enum values are what the DB CHECK constraint and legacy conversion spell."""
    assert (TaskInputKind.FILE, TaskInputKind.DIRECTORY) == ("file", "directory")


def test_file_keeps_binary_content_verbatim() -> None:
    """Input bytes reach the upload untouched, NUL bytes and CRLF included."""
    content = b"\x00\xff binary \n\r"
    assert TaskInputFile(path="INPUT", content=content).content is content


def test_new_task_round_trips_input_entries() -> None:
    """A submission record defaults to no entries and otherwise returns what it got."""
    entries: tuple[TaskInputEntry, ...] = (
        TaskInputDirectory(path="basis"),
        TaskInputFile(path="basis/Si.orb", content=b"\x00"),
    )
    assert NewTask(engine="cp2k").input_entries == ()
    assert NewTask(engine="cp2k", input_entries=entries).input_entries == entries


class TestValidateTaskPath:
    def test_normalizes_redundant_separators_and_dot_components(self) -> None:
        assert validate_task_path("a//b/./c/") == "a/b/c"

    def test_normalizes_leading_dot_and_trailing_separator(self) -> None:
        assert validate_task_path("./basis/") == "basis"

    def test_keeps_legal_lookalike_components(self) -> None:
        assert validate_task_path("...") == "..."
        assert validate_task_path("a..b/.hidden") == "a..b/.hidden"

    @pytest.mark.parametrize(
        "raw",
        [
            "/absolute/path",
            "//server/share",
            "C:",
            "C:/basis",
            "c:\\basis",
            "\\\\server\\share",
            "basis\\INPUT",
            "sub/C:",
            "sub/C:INPUT",
            "..",
            "basis/..",
            "basis/../INPUT",
            ".",
            "",
        ],
    )
    def test_rejects_unsafe_paths(self, raw: str) -> None:
        with pytest.raises(UnsafeTaskPathError) as info:
            validate_task_path(raw)
        assert info.value.path == raw

    @pytest.mark.parametrize("raw", [None, 5, b"INPUT"])
    def test_rejects_non_string_paths(self, raw: object) -> None:
        with pytest.raises(UnsafeTaskPathError):
            validate_task_path(raw)  # type: ignore[arg-type]

    def test_non_str_path_uses_placeholder_instead_of_object_repr(self) -> None:
        marker = "UNIQUE_TASK_PATH_SECRET_2f9a"
        with pytest.raises(UnsafeTaskPathError) as info:
            validate_task_path(_SensitiveRepr(marker))  # type: ignore[arg-type]

        assert info.value.path == "<task path>"
        assert info.value.reason == "task path must be a str"
        assert marker not in str(info.value)


class TestValidateSelection:
    def test_preserves_glob_metacharacters(self) -> None:
        assert validate_selection("basis/**/*.orb") == "basis/**/*.orb"
        assert validate_selection("*") == "*"
        assert validate_selection("**") == "**"
        assert validate_selection("result[?].dat") == "result[?].dat"
        assert validate_selection(".hidden/*") == ".hidden/*"

    def test_normalizes_safe_components(self) -> None:
        assert validate_selection("./basis//*.orb") == "basis/*.orb"
        assert validate_selection("a/./b/?") == "a/b/?"

    def test_preserves_shell_syntax_components(self) -> None:
        assert validate_selection("$HOME/INPUT") == "$HOME/INPUT"
        assert validate_selection("~/INPUT") == "~/INPUT"
        assert validate_selection("basis/{a,b}.orb") == "basis/{a,b}.orb"

    @pytest.mark.parametrize(
        "selection",
        [
            "/INPUT",
            "..",
            "basis/..",
            "C:/x",
            "sub/C:",
            "sub/C:*.orb",
            "basis\\INPUT",
            ".",
            "",
        ],
    )
    def test_rejects_unsafe_selections(self, selection: str) -> None:
        with pytest.raises(UnsafeTaskPathError):
            validate_selection(selection)

    def test_rejects_non_string_selections(self) -> None:
        with pytest.raises(UnsafeTaskPathError):
            validate_selection(None)  # type: ignore[arg-type]

    def test_non_str_selection_uses_placeholder_instead_of_object_repr(self) -> None:
        marker = "UNIQUE_SELECTION_SECRET_4b1c"
        with pytest.raises(UnsafeTaskPathError) as info:
            validate_selection(_SensitiveRepr(marker))  # type: ignore[arg-type]

        assert info.value.path == "<selection>"
        assert info.value.reason == "selection must be a str"
        assert marker not in str(info.value)


class TestSelectionMatchesExplicit:
    def test_explicit_file_matches_itself(self) -> None:
        assert selection_matches("INPUT", "INPUT", TaskInputKind.FILE)

    def test_explicit_directory_matches_itself(self) -> None:
        assert selection_matches("basis", "basis", TaskInputKind.DIRECTORY)

    def test_explicit_nested_path_matches(self) -> None:
        assert selection_matches("basis/Si.orb", "basis/Si.orb", TaskInputKind.FILE)

    def test_matching_is_case_sensitive(self) -> None:
        assert not selection_matches("input", "INPUT", TaskInputKind.FILE)

    def test_explicit_path_does_not_match_children(self) -> None:
        assert not selection_matches("basis", "basis/Si.orb", TaskInputKind.FILE)


class TestSelectionMatchesWildcards:
    def test_star_matches_one_component_including_hidden(self) -> None:
        assert selection_matches("*", ".hidden", TaskInputKind.FILE)
        assert selection_matches("*", "basis", TaskInputKind.DIRECTORY)

    def test_star_does_not_cross_separators(self) -> None:
        assert not selection_matches("*", "basis/Si.orb", TaskInputKind.FILE)
        assert not selection_matches("*.orb", "basis/Si.orb", TaskInputKind.FILE)

    def test_question_mark_matches_single_character(self) -> None:
        assert selection_matches("result?.dat", "result1.dat", TaskInputKind.FILE)
        assert not selection_matches("result?.dat", "result12.dat", TaskInputKind.FILE)

    def test_character_class_matches_members(self) -> None:
        assert selection_matches("result[0-9].dat", "result1.dat", TaskInputKind.FILE)
        assert not selection_matches(
            "result[0-9].dat", "resultx.dat", TaskInputKind.FILE
        )

    def test_negated_character_class(self) -> None:
        assert selection_matches("result[!1].dat", "result2.dat", TaskInputKind.FILE)
        assert not selection_matches(
            "result[!1].dat", "result1.dat", TaskInputKind.FILE
        )

    def test_class_escapes_glob_metacharacters(self) -> None:
        assert selection_matches("file[?]name", "file?name", TaskInputKind.FILE)
        assert not selection_matches("file[?]name", "fileXname", TaskInputKind.FILE)
        assert selection_matches("a[*]b", "a*b", TaskInputKind.FILE)
        assert not selection_matches("a[*]b", "axb", TaskInputKind.FILE)

    def test_unmatched_pattern_selects_nothing(self) -> None:
        """Required-selection failure is the caller's error; matching stays total."""
        entries = [
            ("INPUT", TaskInputKind.FILE),
            ("basis/Si.orb", TaskInputKind.FILE),
            ("basis", TaskInputKind.DIRECTORY),
        ]
        for path, kind in entries:
            assert not selection_matches("missing/*.dat", path, kind)


class TestSelectionMatchesShellSyntax:
    def test_shell_variable_is_literal(self) -> None:
        assert selection_matches("$HOME/INPUT", "$HOME/INPUT", TaskInputKind.FILE)
        assert not selection_matches("$HOME/INPUT", "home/INPUT", TaskInputKind.FILE)

    def test_home_directory_syntax_is_literal(self) -> None:
        assert selection_matches("~/INPUT", "~/INPUT", TaskInputKind.FILE)
        assert not selection_matches("~/INPUT", "home/user/INPUT", TaskInputKind.FILE)

    def test_brace_syntax_is_literal(self) -> None:
        assert selection_matches("{a,b}.orb", "{a,b}.orb", TaskInputKind.FILE)
        assert not selection_matches("{a,b}.orb", "a.orb", TaskInputKind.FILE)
        assert not selection_matches("{a,b}.orb", "b.orb", TaskInputKind.FILE)


class TestSelectionMatchesDoubleStar:
    def test_zero_level_match_between_components(self) -> None:
        assert selection_matches("a/**/b", "a/b", TaskInputKind.FILE)
        assert selection_matches("basis/**/*.orb", "basis/Si.orb", TaskInputKind.FILE)

    def test_root_level_file_matched_by_leading_double_star(self) -> None:
        assert selection_matches("**/*.orb", "Si.orb", TaskInputKind.FILE)

    def test_nested_levels_matched(self) -> None:
        assert selection_matches(
            "basis/**/*.orb", "basis/sub/deep/Si.orb", TaskInputKind.FILE
        )
        assert selection_matches(
            "**/result?.dat", "a/b/result1.dat", TaskInputKind.FILE
        )

    def test_double_star_alone_matches_directories_only(self) -> None:
        assert selection_matches("**", "basis", TaskInputKind.DIRECTORY)
        assert selection_matches("**", "a/b", TaskInputKind.DIRECTORY)
        assert not selection_matches("**", "INPUT", TaskInputKind.FILE)

    def test_trailing_double_star_matches_directories_only(self) -> None:
        assert selection_matches("a/**", "a", TaskInputKind.DIRECTORY)
        assert selection_matches("a/**", "a/b", TaskInputKind.DIRECTORY)
        assert not selection_matches("a/**", "a", TaskInputKind.FILE)
        assert not selection_matches("a/**", "a/b", TaskInputKind.FILE)

    def test_files_under_matched_directories_come_from_subtree_expansion(self) -> None:
        """`**` matches directory entries; the caller expands their subtrees."""
        assert selection_matches("**", "basis", TaskInputKind.DIRECTORY)
        assert not selection_matches("basis/**", "basis/Si.orb", TaskInputKind.FILE)


class TestSelectionReaches:
    """Descent asks whether anything below a directory can still match."""

    def test_literal_selection_reaches_below_its_parent_only(self) -> None:
        assert selection_reaches("a/b", "")
        assert selection_reaches("a/b", "a")
        assert not selection_reaches("a/b", "a/b")
        assert not selection_reaches("a/b", "c")

    def test_glob_component_must_cover_the_prefix(self) -> None:
        assert selection_reaches("*.txt", "")
        assert not selection_reaches("*.txt", "sub")
        assert selection_reaches("dir/*/x", "dir")

    def test_double_star_reaches_everywhere(self) -> None:
        assert selection_reaches("**", "")
        assert selection_reaches("**", "a/b")
        assert selection_reaches("a/**", "a")
        assert selection_reaches("a/**", "a/b/c")

    def test_middle_double_star_reaches_below_any_level(self) -> None:
        assert selection_reaches("a/**/b", "a")
        assert selection_reaches("a/**/b", "a/x/y")
        assert not selection_reaches("a/**/b", "z")

    def test_reaches_differs_from_matching_the_path_itself(self) -> None:
        assert selection_matches("a", "a", TaskInputKind.DIRECTORY)
        assert not selection_reaches("a", "a")


class TestSelectionNamesEntry:
    """An untransferable entry is rejected only when a component names it."""

    def test_literal_component_names_the_entry(self) -> None:
        assert selection_names_entry("a", "", "a")
        assert not selection_names_entry("a", "", "b")

    def test_glob_component_names_the_entry(self) -> None:
        assert selection_names_entry("*.txt", "", "x.txt")
        assert selection_names_entry("dir/*", "dir", "x")
        assert not selection_names_entry("dir/*", "other", "x")

    def test_double_star_names_nothing(self) -> None:
        assert not selection_names_entry("**", "", "anything")
        assert not selection_names_entry("a/**", "a", "anything")
        assert not selection_names_entry("**", "a", "anything")

    def test_nested_component_names_at_its_depth(self) -> None:
        assert selection_names_entry("a/**/b", "a/x", "b")
        assert not selection_names_entry("a/**/b", "a/x", "c")
        assert selection_names_entry("**/x", "", "x")


class TestValidateSnapshotSelections:
    def test_empty_declarations_are_trivially_satisfied(self) -> None:
        validate_snapshot_selections("engine", (), ())

    def test_literal_file_declaration_is_satisfied(self) -> None:
        snapshot = (TaskInputFile("INPUT", b"body"),)

        validate_snapshot_selections("engine", ("INPUT",), snapshot)

    def test_literal_directory_declaration_is_satisfied(self) -> None:
        snapshot = (TaskInputDirectory("basis"),)

        validate_snapshot_selections("engine", ("basis",), snapshot)

    def test_glob_declaration_is_satisfied(self) -> None:
        snapshot = (TaskInputFile("basis/Si.orb", b"orb"),)

        validate_snapshot_selections("engine", ("basis/*.orb",), snapshot)

    def test_trailing_double_star_is_satisfied_by_directory_entries(self) -> None:
        snapshot = (
            TaskInputDirectory("basis"),
            TaskInputDirectory("basis/empty"),
        )

        validate_snapshot_selections("engine", ("basis/**",), snapshot)

    def test_zero_level_double_star_is_satisfied_by_direct_file(self) -> None:
        snapshot = (TaskInputFile("basis/a.orb", b"x"),)

        validate_snapshot_selections("engine", ("basis/**/*.orb",), snapshot)

    def test_first_unsatisfied_declaration_is_reported(self) -> None:
        with pytest.raises(MissingInputFileError) as info:
            validate_snapshot_selections(
                "engine",
                ("missing-first", "missing-second"),
                (),
            )

        assert info.value.engine_name == "engine"
        assert info.value.filename == "missing-first"

    def test_trailing_double_star_requires_directory_kind(self) -> None:
        snapshot = (TaskInputFile("basis/x.orb", b"x"),)

        with pytest.raises(MissingInputFileError) as info:
            validate_snapshot_selections("engine", ("basis/**",), snapshot)

        assert info.value.filename == "basis/**"

    def test_is_exported_from_domain_facade(self) -> None:
        from yascheduler import domain

        assert "validate_snapshot_selections" in domain.__all__
        assert domain.validate_snapshot_selections is validate_snapshot_selections


class TestNormalizeInputSnapshot:
    def test_empty_sequence_is_valid_empty_snapshot(self) -> None:
        assert normalize_input_snapshot(()) == ()

    def test_single_file_without_parents_is_unchanged(self) -> None:
        entry = TaskInputFile(path="INPUT", content=b"body")
        assert normalize_input_snapshot([entry]) == (entry,)

    def test_keeps_canonical_entry_object_identity(self) -> None:
        entry = TaskInputFile(path="INPUT", content=b"body")
        assert normalize_input_snapshot([entry])[0] is entry

    def test_materializes_missing_parents(self) -> None:
        snap = normalize_input_snapshot([TaskInputFile("a/b/c/d.txt", b"")])
        assert [entry.path for entry in snap] == ["a", "a/b", "a/b/c", "a/b/c/d.txt"]
        assert all(isinstance(entry, TaskInputDirectory) for entry in snap[:3])

    def test_keeps_explicit_empty_directory(self) -> None:
        snap = normalize_input_snapshot([TaskInputDirectory("empty")])
        assert snap == (TaskInputDirectory("empty"),)

    def test_explicit_directory_deduplicates_with_materialized_parent(
        self,
    ) -> None:
        """Overlapping explicit and implied parents yield each path once."""
        snap = normalize_input_snapshot(
            [TaskInputDirectory("basis"), TaskInputFile("basis/Si.orb", b"x")]
        )
        assert [entry.path for entry in snap] == ["basis", "basis/Si.orb"]

    def test_result_is_sorted_by_canonical_path(self) -> None:
        snap = normalize_input_snapshot(
            [
                TaskInputFile("z.txt", b"z"),
                TaskInputFile("a/x", b"x"),
                TaskInputDirectory("a"),
            ]
        )
        assert [entry.path for entry in snap] == ["a", "a/x", "z.txt"]

    def test_canonicalizes_entry_paths(self) -> None:
        snap = normalize_input_snapshot([TaskInputFile("a//b/./c", b"z")])
        assert [entry.path for entry in snap] == ["a", "a/b", "a/b/c"]
        leaf = snap[2]
        assert isinstance(leaf, TaskInputFile)
        assert leaf.content == b"z"

    def test_mixed_tree_snapshot(self) -> None:
        snap = normalize_input_snapshot(
            [
                TaskInputFile("INPUT", b"plan\n"),
                TaskInputDirectory("basis"),
                TaskInputFile("basis/.hidden", b"\x00\xff"),
                TaskInputDirectory("basis/empty"),
                TaskInputFile("basis/deep/Si.orb", b"orb"),
            ]
        )
        assert [(entry.path, type(entry).__name__) for entry in snap] == [
            ("INPUT", "TaskInputFile"),
            ("basis", "TaskInputDirectory"),
            ("basis/.hidden", "TaskInputFile"),
            ("basis/deep", "TaskInputDirectory"),
            ("basis/deep/Si.orb", "TaskInputFile"),
            ("basis/empty", "TaskInputDirectory"),
        ]

    def test_rejects_duplicate_explicit_paths(self) -> None:
        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_input_snapshot(
                [TaskInputFile("a", b""), TaskInputFile("a", b"x")]
            )
        assert info.value.path == "a"

    def test_rejects_duplicate_paths_that_canonicalize_equal(self) -> None:
        with pytest.raises(InvalidTaskSnapshotError):
            normalize_input_snapshot(
                [TaskInputFile("a//b", b""), TaskInputFile("a/b", b"")]
            )

    def test_rejects_same_path_with_conflicting_kinds(self) -> None:
        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_input_snapshot([TaskInputFile("a", b""), TaskInputDirectory("a")])
        assert info.value.path == "a"

    @pytest.mark.parametrize(
        "entries",
        [
            [TaskInputFile("a", b""), TaskInputFile("a/b", b"")],
            [TaskInputFile("a", b""), TaskInputDirectory("a/b")],
        ],
    )
    def test_rejects_file_ancestor_conflicts(
        self, entries: list[TaskInputEntry]
    ) -> None:
        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_input_snapshot(entries)
        assert info.value.path == "a"

    def test_rejects_non_entry_objects(self) -> None:
        with pytest.raises(InvalidTaskSnapshotError):
            normalize_input_snapshot(["INPUT"])  # type: ignore[list-item]
        with pytest.raises(InvalidTaskSnapshotError):
            normalize_input_snapshot([None])  # type: ignore[list-item]

    def test_rejects_non_bytes_file_content(self) -> None:
        with pytest.raises(InvalidTaskSnapshotError):
            normalize_input_snapshot(
                [TaskInputFile("a", "text")]  # type: ignore[arg-type]
            )

    def test_rejects_unsafe_entry_paths(self) -> None:
        with pytest.raises(UnsafeTaskPathError):
            normalize_input_snapshot([TaskInputFile("/abs", b"")])
        with pytest.raises(UnsafeTaskPathError):
            normalize_input_snapshot([TaskInputDirectory("..")])

    def test_rejects_non_iterable_entries(self) -> None:
        with pytest.raises(InvalidTaskSnapshotError):
            normalize_input_snapshot(5)  # type: ignore[arg-type]

    def test_non_iterable_entries_use_placeholder_instead_of_object_repr(self) -> None:
        marker = "UNIQUE_ENTRIES_SECRET_7d3e"
        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_input_snapshot(_SensitiveRepr(marker))  # type: ignore[arg-type]

        assert info.value.path == "<entries>"
        assert info.value.reason == "entries must be an iterable of task input entries"
        assert marker not in str(info.value)

    def test_non_entry_uses_positional_placeholder_instead_of_object_repr(
        self,
    ) -> None:
        marker = "UNIQUE_ENTRY_SECRET_8c5f"
        with pytest.raises(InvalidTaskSnapshotError) as info:
            normalize_input_snapshot([_SensitiveRepr(marker)])  # type: ignore[list-item]

        assert info.value.path == "<entry 0>"
        assert info.value.reason == "item 0 is not a task input entry"
        assert marker not in str(info.value)


class TestErrorContracts:
    def test_snapshot_errors_are_validation_errors(self) -> None:
        assert issubclass(UnsafeTaskPathError, ValidationError)
        assert issubclass(InvalidTaskSnapshotError, ValidationError)
