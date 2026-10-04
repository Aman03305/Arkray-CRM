"""Every list the API pages binds its cursors (Phase 9, core/keyset.py): in application code a
`KeysetPaginator` always gets a binding that is not literally None, and the board view
binds its columns' cursors. Only tests and benchmarks page unbound."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

APPLICATION = Path(__file__).resolve().parents[2] / "arkray"


def calls(name: str) -> Iterator[tuple[Path, ast.Call]]:
    for path in sorted(APPLICATION.rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called == name:
                    yield path, node


def keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def is_none(value: ast.expr | None) -> bool:
    return isinstance(value, ast.Constant) and value.value is None


def test_application_code_never_pages_unbound():
    found = list(calls("KeysetPaginator"))
    assert len(found) >= 8  # seven list views and the board
    for path, call in found:
        binding = keyword(call, "binding")
        assert binding is not None, f"{path}:{call.lineno}"
        assert not is_none(binding), f"{path}:{call.lineno}"


def test_the_board_view_binds_its_columns():
    views = [(p, c) for p, c in calls("board") if p.parent.name == "api"]
    assert views
    for path, call in views:
        binding_for = keyword(call, "binding_for")
        assert binding_for is not None, f"{path}:{call.lineno}"
        assert not is_none(binding_for), f"{path}:{call.lineno}"
