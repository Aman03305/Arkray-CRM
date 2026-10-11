"""The reference edge proxy names every response, its own errors included.

Production readiness review: nginx minted a request id and passed it to Django, but never
returned it; the 429 it answers itself had no `request_id`, and its 502/504/413 were HTML
pages, so a user saw "HTTP 502" with no reference to quote. Checked live with nginx -t and
curl against an unreachable backend (503 JSON, 413 JSON, 429 JSON, all with the id).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

NGINX = Path(__file__).resolve().parents[3] / "infrastructure" / "nginx" / "arkray.conf"


def _https_server() -> str:
    text = NGINX.read_text(encoding="utf-8")
    start = text.index("listen 8443 ssl;")
    return text[start:]


def _blocks(server: str) -> dict[str, str]:
    """location name -> its body. Braces inside quoted strings (the JSON bodies) don't count."""
    blocks = {}
    for match in re.finditer(r"location\s+([^{]+)\{", server):
        depth, quote, i = 1, "", match.end()
        while depth:
            char = server[i]
            if quote:
                quote = "" if char == quote else quote
            elif char in "'\"":
                quote = char
            elif char in "{}":
                depth += 1 if char == "{" else -1
            i += 1
        blocks[match.group(1).strip()] = server[match.end() : i - 1]
    return blocks


def test_every_response_carries_the_request_id():
    server = _https_server()
    head = server[: server.index("    location")]
    assert "add_header X-Request-ID $request_id always;" in head
    assert "proxy_hide_header X-Request-ID;" in head  # Django's copy: sent once
    # nginx doesn't inherit add_header into a location that has its own: each repeats it.
    for name, body in _blocks(server).items():
        if "add_header" in body:
            assert "add_header X-Request-ID $request_id always;" in body, name
            assert "Strict-Transport-Security" in body, name


def test_the_edges_own_api_errors_are_the_apis_envelope_with_the_id():
    blocks = _blocks(_https_server())
    for name in ("@arkray_rate_limited", "@arkray_api_unavailable", "@arkray_too_large"):
        body = blocks[name]
        (literal,) = re.findall(r"return \d{3} '([^']*)';", body)
        envelope = json.loads(literal.replace("$request_id", "abc"))["error"]
        assert set(envelope) == {"code", "message", "details", "request_id"}, name
        assert envelope["request_id"] == "abc", name
        assert "default_type application/json;" in body, name
    for name, body in blocks.items():
        if name.startswith("~* ^/api"):
            assert "error_page 502 504 = @arkray_api_unavailable;" in body, name
            assert "error_page 413 = @arkray_too_large;" in body, name


def test_the_web_tiers_own_errors_are_a_minimal_page_with_the_id():
    """R128: the web tier (Next.js) down answered with nginx's own 502 page, without the id
    or the security headers."""
    blocks = _blocks(_https_server())
    assert "error_page 502 504 = @arkray_web_unavailable;" in blocks["/"]
    body = blocks["@arkray_web_unavailable"]
    assert "default_type text/html;" in body
    (page,) = re.findall(r"return 503 '([^']*)';", body)
    assert "$request_id" in page
    for loads_something in ("<script", "style", "http"):
        assert loads_something not in page, loads_something
    for header in (
        "Content-Security-Policy \"default-src 'none'",
        "X-Content-Type-Options nosniff",
        "X-Frame-Options DENY",
        "Cache-Control no-store",
    ):
        assert f"add_header {header}" in body, header


def test_the_health_probe_answers_loopback_only():
    """`return` runs in nginx's rewrite phase, before allow/deny (the access phase): an
    allow/deny pair beside it restricted nothing (found live: 200 from the host)."""
    text = NGINX.read_text(encoding="utf-8")
    start = text.index("location = /edge-health {")
    block = text[start : text.index("}", start)]
    assert "allow " not in block
    assert "deny " not in block
    assert 'if ($remote_addr != "127.0.0.1") {' in block


def test_every_answer_the_edge_writes_itself_is_nosniff():
    """Adversarial review P3: the JSON errors lacked it (Django's own responses have it)."""
    for name, body in _blocks(_https_server()).items():
        if name.startswith("@"):
            assert "add_header X-Content-Type-Options nosniff always;" in body, name
