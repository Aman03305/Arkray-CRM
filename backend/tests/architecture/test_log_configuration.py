"""What the edge and the containers log, and for how long (privacy remediation P2-2;
docs/observability.md#what-a-log-line-may-hold, docs/privacy.md#retention)."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from tests.architecture.test_compose_production import BASE, PRODUCTION, load

ROOT = Path(__file__).resolve().parents[3]
NGINX = ROOT / "infrastructure" / "nginx" / "arkray.conf"
BACKEND = Path(__file__).resolve().parents[2]


def _log_format() -> str:
    text = NGINX.read_text(encoding="utf-8")
    match = re.search(r"log_format arkray (.*?);", text, re.DOTALL)
    assert match
    return match.group(1)


def test_the_edge_access_log_holds_no_query_secret_referer_cookie_or_browser():
    fields = _log_format()
    for forbidden in (
        "$request_uri",
        "$args",
        "$query_string",
        "$request ",
        "$http_referer",
        "$http_cookie",
        "$http_user_agent",
        "$http_authorization",
    ):
        assert forbidden not in fields, forbidden
    assert "$arkray_log_path" in fields


def one_time_link_pages() -> list[str]:
    """Every page whose path carries an emailed token (`/<page>/[token]`): activation, password
    reset and, since the review of 73ff1e3 (L1), the sign-in unlock link."""
    app = ROOT / "frontend" / "src" / "app"
    pages = sorted({path.parent.name for path in app.rglob("[[]token[]]") if path.is_dir()})
    assert {"activate", "reset-password", "unlock-sign-in"} <= set(pages)
    return pages


def test_one_time_link_paths_are_redacted_whatever_their_case():
    """Each token page is redacted in the edge's access log, and Next serves it with
    `no-referrer` and `no-store` (next.config.ts): a new emailed link can't be missed (the
    unlock link of L1 was, until this test derived the list from the pages)."""
    text = NGINX.read_text(encoding="utf-8")
    config = (ROOT / "frontend" / "next.config.ts").read_text(encoding="utf-8")
    for page in one_time_link_pages():
        name = re.escape(page)
        assert re.search(rf"~\*\^/{name}/\s+/{name}/\[redacted\];", text), page
        assert re.search(rf'source: "/{name}/:token\*", headers: secretLinkHeaders', config), page
    assert "error_log /var/log/nginx/error.log crit;" in text


def _rotated(service: dict[str, Any]) -> bool:
    logging = service.get("logging") or {}
    options = logging.get("options") or {}
    return logging.get("driver") == "json-file" and bool(options.get("max-size"))


def test_every_container_log_is_rotated():
    base, production = load(BASE), load(PRODUCTION)
    for name, service in base["services"].items():
        assert _rotated(service), f"docker-compose.yml: {name}"
    for name, service in production["services"].items():
        merged = {**base["services"].get(name, {}), **service}
        assert _rotated(merged), f"compose.production.yml: {name}"


def _assigned(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not assigned in gunicorn.conf.py")


def test_gunicorns_error_log_goes_through_the_redacting_formatter():
    # Read, not imported: gunicorn needs fcntl, which Windows lacks.
    tree = ast.parse((BACKEND / "gunicorn.conf.py").read_text(encoding="utf-8"))
    config = _assigned(tree, "logconfig_dict")
    assert config["formatters"]["json"]["()"] == "arkray.core.logging.JsonFormatter"
    assert config["loggers"]["gunicorn.error"]["handlers"] == ["error_console"]
    assert config["handlers"]["error_console"]["filters"] == ["gunicorn"]
    assert _assigned(tree, "accesslog") is None
