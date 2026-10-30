# region MODULE_CONTRACT
# PURPOSE: Regression guard against reintroducing typing.cast in the composition root.
# SCOPE: Parses yascheduler/entrypoints/di.py and rejects a cast import plus both call spellings.
# KEYWORDS: typing.cast, composition root, AST walk
# endregion MODULE_CONTRACT

from __future__ import annotations

import ast
from pathlib import Path

DI_MODULE = (
    Path(__file__).resolve().parents[2] / "yascheduler" / "entrypoints" / "di.py"
)


def _cast_mentions(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            if any(alias.name == "cast" for alias in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            is_cast = (isinstance(func, ast.Name) and func.id == "cast") or (
                isinstance(func, ast.Attribute) and func.attr == "cast"
            )
            if is_cast:
                lines.append(node.lineno)
    return lines


def test_composition_root_does_not_reach_for_cast() -> None:
    """Config.clouds is typed Sequence[ConfigCloud]; a cast in the wiring is a symptom, not a fix."""
    tree = ast.parse(DI_MODULE.read_text(), filename=str(DI_MODULE))
    assert _cast_mentions(tree) == []
