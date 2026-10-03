"""The OpenAPI schema is the API contract: generated without warnings, valid, and committed.

The frontend's TypeScript types are generated from the committed file
(frontend: `pnpm api:types`), so a drift here would silently break the client.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management import call_command

COMMITTED = Path(__file__).resolve().parents[2] / "openapi.yaml"
REGENERATE = "cd backend && uv run python manage.py spectacular --file openapi.yaml"


def test_schema_is_valid_warning_free_and_committed(tmp_path):
    generated = tmp_path / "openapi.yaml"
    call_command("spectacular", "--fail-on-warn", "--validate", "--file", str(generated))
    assert COMMITTED.exists(), f"Missing backend/openapi.yaml. Run: {REGENERATE}"
    assert COMMITTED.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8"), (
        f"backend/openapi.yaml is stale. Run: {REGENERATE} (then `pnpm api:types` in frontend)"
    )


def test_schema_never_describes_security_fields():
    text = COMMITTED.read_text(encoding="utf-8")
    for field in ("token_hash", "session_epoch", "is_superuser", "password_hash"):
        assert field not in text
