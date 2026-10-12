"""The edge's emergency maintenance switch (docs/runbooks.md#emergency-maintenance).

An operator closes the site at the reference edge without a reload: a file named `on` in the
directory mounted read-only at /etc/nginx/maintenance (infrastructure/nginx/maintenance/ on
the host, compose.production.yml). infrastructure/nginx/arkray.conf then answers every
request on the HTTPS server itself, in its server rewrite phase (before any location, limit
or upstream): 503 with Retry-After and no-store, the API's JSON envelope with the request id
on /api/, a minimal page loading nothing elsewhere. The plain-HTTP server, with the
container's health check, is outside the switch, so the orchestrator never restarts the edge
for it. Single addresses may be let through to try the application (a geo map on
$remote_addr, empty in the repository).

Checked live in the pinned nginx (nginx -t, then requests with the flag created and removed
on the host through the read-only bind mount, no reload): off, the edge's usual answers for
an absent API and web tier; on, every route (the API in any case, pages, /health/*, sign-in,
a 2 MB upload answered before its session check, HEAD) the maintenance answer with
`upstream=-` in the access log, the health check 200, plain HTTP still 308; an allowlisted
address reached the upstreams while another, sending X-Forwarded-For with the allowlisted
address, did not; removing the file reopened at once.
"""

from __future__ import annotations

import ipaddress
import json
import re

from tests.architecture.test_compose_production import PRODUCTION, ROOT, load
from tests.architecture.test_edge_errors import NGINX, _blocks, _https_server

FLAG_DIRECTORY = "/etc/nginx/maintenance"
FLAG = f"{FLAG_DIRECTORY}/on"
HOST_DIRECTORY = ROOT / "infrastructure" / "nginx" / "maintenance"
API, PAGE = "@arkray_maintenance_api", "@arkray_maintenance_page"
SWITCH = re.compile(
    r"^    set \$arkray_maintenance 0;\n"
    rf"    if \(-f {re.escape(FLAG)}\) \{{\n"
    r"        set \$arkray_maintenance \$arkray_maintenance_hold;\n"
    r"    \}\n"
    r"    if \(\$arkray_maintenance\) \{\n"
    r"        return 503;\n"
    r"    \}\n"
    r"    error_page 503 = \$arkray_maintenance_answer;\n",
    re.M,
)


def _text() -> str:
    return NGINX.read_text(encoding="utf-8")


def _server_head() -> str:
    server = _https_server()
    return server[: server.index("    location")]


def _directives(body: str) -> list[str]:
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _http_block(header: str) -> str:
    (body,) = re.findall(rf"^{re.escape(header)} \{{\n(.*?)^\}}", _text(), re.S | re.M)
    return str(body)


def _answer_for(uri: str) -> str:
    """What `map $uri $arkray_maintenance_answer` gives, applied the way nginx does (the
    regexes in order, `~*` case-insensitive, else the default)."""
    default = ""
    for line in _directives(_http_block("map $uri $arkray_maintenance_answer")):
        source, value = re.fullmatch(r'("[^"]*"|\S+)\s+(\S+);', line).groups()  # type: ignore[union-attr]
        source = source.strip('"')
        if source == "default":
            default = value
        elif re.search(source.removeprefix("~*"), uri, re.I if source.startswith("~*") else 0):
            return value
    return default


def test_the_switch_answers_every_https_request_before_any_location():
    """In the server's rewrite phase, so no location, limit_conn/limit_req, auth_request or
    upstream is involved, and every route is covered, routes added later included. The
    `return 503` is bare, so the error_page beside it picks the route's answer."""
    head = _server_head()
    assert SWITCH.search(head), "the maintenance switch isn't in the HTTPS server's head"
    # The one error_page at the server level: nothing else there is sent to the answers.
    assert re.findall(r"^[ \t]*error_page .*;$", head, re.M) == [
        "    error_page 503 = $arkray_maintenance_answer;"
    ]
    # Only there: the plain-HTTP server (redirects and the health check) is outside it.
    assert _text().count(f"-f {FLAG}") == 1
    assert _text().count("$arkray_maintenance_answer;") == 1


def test_the_health_check_stays_outside_the_switch():
    """The orchestrator's probe of the edge (compose.production.yml) is the plain-HTTP
    server's /edge-health, which no maintenance check precedes."""
    text = _text()
    plain = text[text.index("listen 8080;") : text.index("listen 8443 ssl default_server;")]
    assert "location = /edge-health {" in plain
    assert "maintenance" not in plain
    nginx = load(PRODUCTION)["services"]["nginx"]
    assert "http://127.0.0.1:8080/edge-health" in nginx["healthcheck"]["test"]


def test_each_route_gets_its_answer():
    """The API's routes (`~* ^/api/`, any case, as the API location matches them) get the
    JSON envelope; everything else, the page."""
    blocks = _blocks(_https_server())
    assert {API, PAGE} <= set(blocks)
    assert "~* ^/api/" in blocks
    for uri in ("/api/v1/auth/me", "/API/V1/leads", "/Api/", "/api/v1/workspaces/me/x"):
        assert _answer_for(uri) == API, uri
    for uri in ("/", "/pipelines", "/api", "/apix/", "/health/ready", "/_next/static/a.js"):
        assert _answer_for(uri) == PAGE, uri
    routed = re.compile("~* ^/api/".removeprefix("~* "), re.I)  # that location's own regex
    for uri in ("/api/v1/x", "/API/x", "/api", "/pipelines", "/x/api/"):
        assert (_answer_for(uri) == API) == (routed.search(uri) is not None), uri


def _retry_after(body: str) -> int:
    (value,) = re.findall(r"add_header Retry-After (\d+) always;", body)
    return int(value)


def _web_app_retries_up_to() -> int:
    """The web app retries a GET on its own only when Retry-After is at most this
    (frontend/src/lib/query-client.ts)."""
    source = (ROOT / "frontend" / "src" / "lib" / "query-client.ts").read_text(encoding="utf-8")
    (value,) = re.findall(r"^const MAX_RETRY_AFTER_S = (\d+);$", source, re.M)
    return int(value)


def test_the_api_answer_is_the_envelope_with_the_id():
    """`service_unavailable`, a code the API documents and the web app shows as "temporarily
    unavailable"; Retry-After long enough that the web app doesn't retry every page's
    queries against a closed site; never cached."""
    body = _blocks(_https_server())[API]
    (literal,) = re.findall(r"return 503 '([^']*)';", body)
    envelope = json.loads(literal.replace("$request_id", "abc"))["error"]
    assert set(envelope) == {"code", "message", "details", "request_id"}
    assert envelope["code"] == "service_unavailable"
    assert envelope["request_id"] == "abc"
    assert "maintenance" in envelope["message"]
    assert "default_type application/json;" in body
    assert _retry_after(body) > _web_app_retries_up_to()
    for header in (
        "X-Content-Type-Options nosniff",
        "Cache-Control no-store",
        'Strict-Transport-Security "max-age=31536000"',
        "X-Request-ID $request_id",
    ):
        assert f"add_header {header} always;" in body, header


def test_the_page_loads_nothing_and_names_the_request():
    """Like the edge's other pages: no script, style, image or link (the CSP lets nothing
    load, inline included), the request id as the reference a user quotes to support."""
    body = _blocks(_https_server())[PAGE]
    (page,) = re.findall(r"return 503 '([^']*)';", body)
    assert "Reference: $request_id" in page
    for loads_something in ("<script", "style", "<img", "<link", "http", "src="):
        assert loads_something not in page.lower(), loads_something
    assert "default_type text/html;" in body
    assert "charset utf-8;" in body
    assert _retry_after(body) > _web_app_retries_up_to()
    for header in (
        "Content-Security-Policy \"default-src 'none'; base-uri 'none'; form-action 'none';"
        " frame-ancestors 'none'\"",
        "X-Content-Type-Options nosniff",
        "X-Frame-Options DENY",
        "Referrer-Policy no-referrer",
        "Cache-Control no-store",
        'Strict-Transport-Security "max-age=31536000"',
        "X-Request-ID $request_id",
    ):
        assert f"add_header {header} always;" in body, header


def test_the_allowlist_is_empty_and_keyed_by_the_connections_own_address():
    """geo without a source variable reads $remote_addr, the address of the connection the
    edge terminates, and has no `proxy` entries: a client's X-Forwarded-For can't claim an
    allowlisted address. Shipped empty; the example is one host. Behind Cloudflare or a load
    balancer, real_ip may set $remote_addr from the proxy's header, but only for requests
    from named proxy ranges (docs/operations.md#edge-rate-limits): trusting every address
    would let any client name its own."""
    rules = _directives(_http_block("geo $arkray_maintenance_hold"))
    assert rules == ["default 1;"]
    for source in re.findall(r"^[ \t]*set_real_ip_from\s+(\S+);", _text(), re.M):
        if not source.startswith("unix:"):
            assert ipaddress.ip_network(source, strict=False).prefixlen > 0, source
    examples = re.findall(
        r"^\s*#\s*(\S+/\d+) 0;", _http_block("geo $arkray_maintenance_hold"), re.M
    )
    assert examples
    for example in examples:
        network = ipaddress.ip_network(example)
        assert network.num_addresses == 1, example


def test_the_flag_directory_is_mounted_read_only_and_the_flag_never_committed():
    """nginx only looks for the file. Committed, it would close every deployment of that
    commit: the directory's own .gitignore keeps it out."""
    services = load(PRODUCTION)["services"]
    mounts = [str(v) for v in services["nginx"]["volumes"]]
    assert f"./infrastructure/nginx/maintenance:{FLAG_DIRECTORY}:ro" in mounts
    for name, service in services.items():
        if name != "nginx":
            assert not [v for v in service.get("volumes") or [] if "maintenance" in str(v)], name
    assert HOST_DIRECTORY.is_dir()
    ignored = (HOST_DIRECTORY / ".gitignore").read_text(encoding="utf-8")
    assert "/on" in _directives(ignored)
