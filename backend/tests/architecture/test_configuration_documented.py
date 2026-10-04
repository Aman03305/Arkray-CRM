"""Every environment variable the backend reads is documented by name in the deployment
guide's configuration tables (Phase 11: 27 of 71 were missing or only covered by a wildcard,
the rate limits among them)."""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
GUIDE = BACKEND.parent / "docs" / "deployment.md"
SOURCES = [*sorted((BACKEND / "config").rglob("*.py")), BACKEND / "gunicorn.conf.py"]
READ = re.compile(
    r"""(?:\benv(?:\.\w+)?\(|os\.environ(?:\.get)?\(|os\.environ\[)\s*["']([A-Z][A-Z0-9_]+)["']"""
    r"""|["']([A-Z][A-Z0-9_]+)["']\s+(?:not\s+)?in\s+os\.environ""",
    re.S,
)
# Read by the platform rather than configured per deployment.
NOT_CONFIGURATION = {"DJANGO_SETTINGS_MODULE_FOR_TESTS"}


def variables_read() -> set[str]:
    found: set[str] = set()
    for path in SOURCES:
        for match in READ.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group(1) or match.group(2))
    return found - NOT_CONFIGURATION


def test_the_scan_finds_the_variables_it_should():
    found = variables_read()
    # Multi-line, single-quoted and membership reads are all seen.
    assert {"AI_EMBEDDING_MODEL_DIR", "DB_STATEMENT_TIMEOUT_MS", "PORT", "ANTHROPIC_LOG"} <= found
    assert len(found) > 60


def test_every_variable_is_named_in_the_deployment_guide():
    guide = GUIDE.read_text(encoding="utf-8")
    missing = sorted(name for name in variables_read() if f"`{name}`" not in guide)
    assert missing == []
