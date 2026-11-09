"""checks.py and decide.py must not use float at all."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _float_nodes(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "float":
            yield node
        elif isinstance(node, ast.Attribute) and node.attr == "float":
            yield node
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            yield node


@pytest.mark.parametrize("module_name", ["checks.py", "decide.py"])
def test_no_float_in_module(module_name: str):
    path = ROOT / "src" / module_name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = sorted(node.lineno for node in _float_nodes(tree))
    assert not offenders, f"{module_name} uses float at lines {offenders}"
