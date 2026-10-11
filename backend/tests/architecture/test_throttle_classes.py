"""Every API rate limit is one of core.throttling's (R127): keyed by the trusted client
address (an IPv6 client by its /64) and bounded while Redis is down. DRF's own classes key
by their own reading of X-Forwarded-For and the full address, and fail open without Redis.

BaseThrottle (a type) may still be imported from rest_framework.throttling.
"""

from __future__ import annotations

import ast
from pathlib import Path

ARKRAY = Path(__file__).resolve().parents[2] / "arkray"
CONCRETE = {"AnonRateThrottle", "UserRateThrottle", "ScopedRateThrottle", "SimpleRateThrottle"}


def _drf_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "rest_framework.throttling":
            found |= {alias.name for alias in node.names} & CONCRETE
        elif isinstance(node, ast.Attribute) and node.attr in CONCRETE:
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "throttling":
                found.add(node.attr)
    return found


def _offenders() -> dict[str, set[str]]:
    offenders = {}
    for path in sorted(ARKRAY.rglob("*.py")):
        relative = path.relative_to(ARKRAY).as_posix()
        if relative == "core/throttling.py" or "/tests/" in f"/{relative}":
            continue
        if found := _drf_imports(path):
            offenders[relative] = found
    return offenders


def test_views_use_the_core_throttles():
    assert _offenders() == {}, "import these from arkray.core.throttling instead"
