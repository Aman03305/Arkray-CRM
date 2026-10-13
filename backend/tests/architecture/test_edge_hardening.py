"""The reference edge after the adversarial review of 73ff1e3 (L2, L13 and its hardening
suggestions), checked on infrastructure/nginx/arkray.conf and frontend/next.config.ts; `nginx
-t` in the pinned image, and the limits live (docs/operations.md#edge-rate-limits).

- **L2**: `/api/` had a connection cap and no request-rate limit, and DRF checks permissions
  before throttles, so 401 and 403 answers were never limited: one client could keep the
  sync web workers busy with cheap refused requests. Every API location now has a request
  rate per signed-in session (the session cookie), per address for requests without one, and
  a generous per-address ceiling that an office of 100 behind one address (147 requests a
  second in the capacity test's headroom run, docs/capacity.md) stays well under. A refusal
  is the API's JSON 429 with the request id.
- **L13**: eight request bodies trickled a byte every 14 s held every body slot of their
  address, so one person on a shared office address blocked everyone's saves and sign-ins.
  Only a body that can reach the temp area takes a slot now (declared over the in-memory
  buffer, or undeclared): a sign-in or an ordinary save takes none. Slots are per session
  (per address without one), under a per-address ceiling; bodies stall for 10 s at most.
- **Hardening**: no version on any answer (`server_tokens off` for every server), TLS 1.2
  with forward-secret AEAD suites only and no session tickets, the plain-HTTP redirect to a
  fixed host (never the client's Host), the request headers only proxies or Next.js itself
  set stripped, Next.js's image optimizer unreachable, and the web tier's development
  rewrite limited to `/api/v1`. Behind a CDN, `real_ip` from its ranges only (documented in
  the file: otherwise every client shares the CDN's address and every per-client limit).
"""

from __future__ import annotations

import re

import pytest

from tests.architecture.test_compose_production import PRODUCTION, ROOT, UNITS, load, tmpfs_sizes
from tests.architecture.test_edge_errors import NGINX, _blocks, _https_server

NEXT_CONFIG = ROOT / "frontend" / "next.config.ts"
API_LOCATIONS = (
    "~* ^/api/v1/workspaces/[^/]+/activities/[^/]+/attachments$",
    "= /_arkray/session",
    "~* ^/api/v1/admin/privacy/exports/[^/]+/download$",
    "~* ^/api/v1/auth/(login|password-reset|invitations)",
    "~* ^/api/",
)
API_ZONES = ("arkray_api_session", "arkray_api_anonymous", "arkray_api_address")
# The capacity test's headroom run: 100 users from one address at twice the normal rate
# (docs/capacity.md#headroom).
OFFICE_PEAK_RPS = 147
# Django's own per-user and anonymous rates (config/settings/base.py defaults, R77).
DJANGO_USER_RPS = 600 / 60
DJANGO_ANON_RPS = 60 / 60
# Request headers only a proxy or Next.js itself sets (Next 16's own names).
INTERNAL_HEADERS = (
    "X-Forwarded-Host",
    "X-Forwarded-Port",
    "X-Forwarded-Prefix",
    "X-Middleware-Subrequest",
    "X-Middleware-Prefetch",
    "X-Middleware-Invoke",
    "X-Middleware-Next",
    "X-Middleware-Rewrite",
    "X-Middleware-Redirect",
    "X-Middleware-Refresh",
    "X-Middleware-Skip",
    "X-Middleware-Set-Cookie",
    "X-Middleware-Override-Headers",
    "X-Nextjs-Data",
    "X-Nextjs-Matched-Path",
    "X-Nextjs-Rewritten-Path",
    "X-Nextjs-Rewritten-Query",
    "X-Nextjs-Request-Id",
    "X-Nextjs-Html-Request-Id",
    "X-Nextjs-Stale-Time",
    "X-Nextjs-Prerender",
    "X-Nextjs-Postponed",
    "X-Nextjs-Cache",
    "X-Nextjs-Rewrite",
    "X-Nextjs-Redirect",
    "X-Nextjs-Action-Not-Found",
    "X-Nextjs-Deployment-Id",
    "X-Matched-Path",
    "X-Now-Route-Matches",
    "X-Invoke-Path",
    "X-Invoke-Query",
    "X-Invoke-Output",
    "X-Invoke-Status",
    "X-Invoke-Error",
    # Review round 1 (INFRA-7): more forwarding, rewrite and Next.js internal headers.
    "X-Forwarded-Ssl",
    "X-Forwarded-Protocol",
    "X-Forwarded-Server",
    "Forwarded",
    "X-Original-URL",
    "X-Rewrite-URL",
    "X-Middleware-Subrequest-Id",
    "Next-Resume",
    "X-Next-Resume-State-Length",
    "X-Prerender-Revalidate",
    "X-Prerender-Revalidate-If-Generated",
    "X-HTTP-Method-Override",
    "X-HTTP-Method",
    "X-Method-Override",
    "Next-Action",  # server actions: the app has none, and pages take no POST
)
SESSION_ID = "k3c9x0q2m8v7b1n5h4j6g2f8d0s9a7p1"  # 32 of [a-z0-9], as Django makes them


def _text() -> str:
    return NGINX.read_text(encoding="utf-8")


def _http_level() -> str:
    text = _text()
    return text[: text.index("\nserver {")]


def _server_head() -> str:
    server = _https_server()
    return server[: server.index("    location")]


def _map(source: str, target: str) -> list[tuple[str, str]]:
    """A map block's (source, value) pairs, in order, comments skipped."""
    (body,) = re.findall(
        rf"^map {re.escape(source)} {re.escape(target)} \{{\n(.*?)^\}}", _http_level(), re.S | re.M
    )
    pairs = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        found = re.fullmatch(r'("[^"]*"|\S+)\s+("[^"]*"|\S+);', line)
        assert found, line
        pairs.append((found.group(1).strip('"'), found.group(2).strip('"')))
    return pairs


def _apply(pairs: list[tuple[str, str]], value: str, variables: dict[str, str]) -> str:
    """nginx's map: exact strings first, then the regexes in order, then default; `$1` and
    other variables filled in."""

    def fill(result: str, found: re.Match[str] | None) -> str:
        if found is not None:
            result = re.sub(r"\$(\d)", lambda m: found.group(int(m.group(1))) or "", result)
        for name, known in sorted(variables.items(), key=lambda item: -len(item[0])):
            result = result.replace(f"${name}", known)
        return result

    for source, result in pairs:
        if source != "default" and not source.startswith("~") and source == value:
            return fill(result, None)
    for source, result in pairs:
        if source.startswith("~"):
            flags = re.I if source.startswith("~*") else 0
            found = re.search(source.lstrip("~*"), value, flags)
            if found:
                return fill(result, found)
    (default,) = [result for source, result in pairs if source == "default"]
    return fill(default, None)


def session_of(cookie: str) -> str:
    return _apply(_map("$http_cookie", "$arkray_session"), cookie, {})


# --- L2: request rates on the API --------------------------------------------------------------
def _zones() -> dict[str, tuple[str, float]]:
    found = re.findall(r"^limit_req_zone (\S+) zone=(\w+):\d+m rate=(\d+)r/s;", _http_level(), re.M)
    return {zone: (key, float(rate)) for key, zone, rate in found}


def test_the_session_is_read_from_its_cookie_in_djangos_format_only():
    for cookie in (
        f"__Host-arkray_session={SESSION_ID}",
        f"__Host-arkray_csrftoken=abc; __Host-arkray_session={SESSION_ID}; theme=dark",
        f"arkray_session={SESSION_ID}",  # the plain-HTTP local stack's name
    ):
        assert session_of(cookie) == SESSION_ID, cookie
    for cookie in (
        "",
        "__Host-arkray_csrftoken=abc",
        "__Host-arkray_session=" + "a" * 4000,  # any length: never a 4 KB limit key
        "__Host-arkray_session=../../etc",
        f"x__Host-arkray_session={SESSION_ID}",
        f"my_arkray_session={SESSION_ID}",
    ):
        assert session_of(cookie) == "", cookie


def test_every_api_location_limits_requests_per_session_per_address_and_in_all():
    zones = _zones()
    assert zones["arkray_api_session"][0] == "$arkray_api_session_key"
    assert zones["arkray_api_anonymous"][0] == "$arkray_api_anonymous_key"
    assert zones["arkray_api_address"][0] == "$arkray_client_key"
    blocks = _blocks(_https_server())
    for name in API_LOCATIONS:
        if name == "= /_arkray/session":
            continue  # an internal subrequest: limits apply to the request it serves
        body = blocks[name]
        for zone in API_ZONES:
            assert re.search(rf"^\s*limit_req zone={zone} burst=\d+ nodelay;$", body, re.M), (
                name,
                zone,
            )
        assert "error_page 429 = @arkray_rate_limited;" in body, name
    # Pages aren't the API's: their own limits (limit_conn), not these.
    assert "limit_req" not in blocks["/"]


def test_the_keys_split_signed_in_from_anonymous_requests():
    variables = {"arkray_client_key": "203.0.113.7"}
    session_key = _map("$arkray_session", "$arkray_api_session_key")
    anonymous_key = _map("$arkray_session", "$arkray_api_anonymous_key")
    assert (
        _apply(session_key, SESSION_ID, {**variables, "arkray_session": SESSION_ID}) == SESSION_ID
    )
    assert _apply(anonymous_key, SESSION_ID, variables) == ""  # not counted twice
    assert _apply(session_key, "", variables) == ""
    assert _apply(anonymous_key, "", variables) == "203.0.113.7"


def test_the_rates_leave_an_office_behind_one_address_alone():
    """Generous on purpose: the limits stop floods of cheap refused requests, Django's own
    throttles (R77) stay the authority for everything it counts."""
    zones = _zones()
    blocks = _blocks(_https_server())
    bursts = {
        zone: int(burst)
        for zone, burst in re.findall(
            r"limit_req zone=(arkray_api_\w+) burst=(\d+) nodelay;", blocks["~* ^/api/"]
        )
    }
    # The ceiling: twice the busiest office the capacity test measured, all from one address.
    assert zones["arkray_api_address"][1] >= 2 * OFFICE_PEAK_RPS
    assert bursts["arkray_api_address"] >= 4 * OFFICE_PEAK_RPS
    # One person: never below what Django lets one user do; a page's burst of calls fits.
    assert zones["arkray_api_session"][1] >= 2 * DJANGO_USER_RPS
    assert bursts["arkray_api_session"] >= 100
    # Without a session: well above Django's anonymous rate, for an office's sign-ins.
    assert zones["arkray_api_anonymous"][1] >= 5 * DJANGO_ANON_RPS
    assert bursts["arkray_api_anonymous"] >= 50
    assert re.search(r"^limit_req_status 429;$", _http_level(), re.M)


# --- L13: request bodies ----------------------------------------------------------------------
def _spooled(method: str, length: str = "", encoding: str = "") -> str:
    pairs = _map(
        '"$request_method:$http_content_length$http_transfer_encoding"', "$arkray_spooled_body"
    )
    return _apply(pairs, f"{method}:{length}{encoding}", {})


def test_only_a_body_that_can_reach_the_temp_area_takes_a_slot():
    (buffer,) = re.findall(r"^\s*client_body_buffer_size (\d+)k;", _server_head(), re.M)
    limit = int(buffer) * 1024
    assert limit == 16384
    for method in ("GET", "HEAD", "OPTIONS"):
        assert _spooled(method) == ""
    for method in ("POST", "PUT", "PATCH", "DELETE", "GET"):
        for length in (
            "0",
            "1",
            "512",
            "9999",
            "10000",
            "15999",
            "16000",
            "16299",
            "16379",
            str(limit),
        ):
            assert _spooled(method, length) == "", (method, length)  # in memory: no slot
        for length in (str(limit + 1), "16390", "17000", "99999", "1048576", "00000016385"):
            assert _spooled(method, length) == "1", (method, length)
        assert _spooled(method, "", "chunked") == "1"  # undeclared
        assert _spooled(method, "10", "chunked") == "1"
    assert _spooled("POST") == "1"  # HTTP/2 without a Content-Length


def test_body_slots_belong_to_the_session_under_a_per_address_ceiling():
    variables = {"arkray_client_key": "203.0.113.7", "arkray_session": SESSION_ID}
    client = _map('"$arkray_spooled_body:$arkray_session"', "$arkray_body_client_key")
    assert _apply(client, f"1:{SESSION_ID}", variables) == SESSION_ID
    assert _apply(client, "1:", variables) == "203.0.113.7"
    assert _apply(client, f":{SESSION_ID}", variables) == ""
    assert _apply(client, ":", variables) == ""
    address = _map("$arkray_spooled_body", "$arkray_body_address_key")
    assert _apply(address, "1", variables) == "203.0.113.7"
    assert _apply(address, "", variables) == ""
    zones = {
        zone: key
        for key, zone in re.findall(r"^limit_conn_zone (\S+) zone=(\w+):\d+m;", _http_level(), re.M)
    }
    assert zones["arkray_body"] == "$arkray_body_client_key"
    assert zones["arkray_body_address"] == "$arkray_body_address_key"
    head = _server_head()
    (per_client,) = re.findall(r"^\s*limit_conn arkray_body (\d+);", head, re.M)
    (per_address,) = re.findall(r"^\s*limit_conn arkray_body_address (\d+);", head, re.M)
    # A co-worker keeps slots while one session holds its own; the address's together are
    # still bounded, and fit the JSON-body area with room for every other address.
    assert int(per_address) >= 2 * int(per_client)
    (body_size,) = re.findall(r"^\s*client_max_body_size (\d+)m;", head, re.M)
    area = tmpfs_sizes(load(PRODUCTION)["services"]["nginx"])["/var/cache/nginx-body"]
    assert area is not None
    assert int(per_address) * int(body_size) * UNITS["m"] <= area // 2


def test_bodies_that_stall_are_dropped_within_10_seconds():
    (value,) = re.findall(r"^client_body_timeout (\d+)s;", _http_level(), re.M)
    assert int(value) <= 10


def test_the_cdn_requirement_is_written_where_the_key_is_made():
    """Behind a CDN or load balancer every client has its address unless real_ip takes it
    from the proxy: then every per-client limit is the whole site's."""
    http = _http_level()
    block = http[: http.index("map $remote_addr $arkray_client_key")]
    assert re.search(r"^#\s+set_real_ip_from \S+;", block, re.M)
    assert re.search(r"^#\s+real_ip_header CF-Connecting-IP;", block, re.M)
    assert "never 0.0.0.0/0" in block


# --- hardening -----------------------------------------------------------------------------------
def test_no_answer_names_the_nginx_version():
    http = _http_level()
    assert re.search(r"^server_tokens off;$", http, re.M)
    assert "server_tokens on" not in _text()


def test_tls_is_forward_secret_aead_only_without_session_tickets():
    http = _http_level()
    assert re.search(r"^ssl_protocols TLSv1\.2 TLSv1\.3;$", http, re.M)
    assert re.search(r"^ssl_session_tickets off;$", http, re.M)
    (ciphers,) = re.findall(r"^ssl_ciphers (\S+);$", http, re.M)
    for suite in ciphers.split(":"):
        assert suite.startswith(("ECDHE-ECDSA-", "ECDHE-RSA-")), suite
        assert suite.endswith(("-GCM-SHA256", "-GCM-SHA384", "-CHACHA20-POLY1305")), suite
    # Set once, for every server (the default one included).
    assert "ssl_protocols" not in _https_server()


def test_the_plain_http_redirect_goes_to_a_fixed_host():
    text = _text()
    plain = text[text.index("listen 8080;") : text.index("listen 8443 ssl default_server;")]
    (target,) = re.findall(r"return 308 (\S+);", plain)
    assert target == "https://localhost:8443$request_uri"
    assert "$host" not in target
    assert "$http_host" not in text


def test_client_supplied_internal_headers_never_reach_the_upstreams():
    head = _server_head()
    check = _blocks(_https_server())["= /_arkray/session"]
    for header in INTERNAL_HEADERS:
        line = f'proxy_set_header {header} "";'
        assert f"    {line}" in head, header
        assert line in check, header
    # No location replaces the server's lines without repeating them.
    for name, body in _blocks(_https_server()).items():
        if "proxy_set_header" in body and name != "= /_arkray/session":
            raise AssertionError(f"{name} replaces the server's proxy_set_header lines")


def test_nextjs_image_optimizer_is_unreachable():
    """In any case (`/_NEXT/image` too: review round 1), before every other location."""
    blocks = _blocks(_https_server())
    assert blocks["~* ^/_next/image"].strip() == "return 404;"
    names = list(blocks)
    regexes = [name for name in names if name.startswith("~")]
    assert regexes[0] == "~* ^/_next/image"
    assert "^~ /_next/image" not in blocks
    config = NEXT_CONFIG.read_text(encoding="utf-8")
    assert re.search(r"images: \{ unoptimized: true \}", config)


def test_the_web_tiers_rewrite_reaches_api_v1_only():
    config = NEXT_CONFIG.read_text(encoding="utf-8")
    rewrites = re.findall(r'\{ source: "([^"]+)", destination: `\$\{apiOrigin\}([^`]+)` \}', config)
    assert rewrites == [("/api/v1/:path*", "/api/v1/:path*")]


@pytest.mark.parametrize(
    "header", ["X-Forwarded-For", "X-Forwarded-Proto", "X-Request-ID", "Host", "X-Real-IP"]
)
def test_the_forwarding_headers_are_still_overwritten(header):
    assert re.search(rf"^    proxy_set_header {header} \S+;$", _server_head(), re.M), header


def test_x_real_ip_is_the_connections_own_address():
    """Set, never passed through (INFRA-7): the same address as X-Forwarded-For."""
    head = _server_head()
    assert "    proxy_set_header X-Real-IP $remote_addr;" in head
    assert (
        "proxy_set_header X-Real-IP $remote_addr;" in _blocks(_https_server())["= /_arkray/session"]
    )


# --- review round 1 (INFRA-3): what one client on a shared address can still hold ------------
def test_pages_take_only_a_bodiless_get_or_head_before_any_slot():
    """256 small trickled `POST /login` bodies from one client held every request slot of its
    address (pages had no body rule). Pages need GET and HEAD without a body: anything else is
    answered 405 in the rewrite phase, before limit_conn counts it or a body is read; and a
    page body can't exceed one byte (an HTTP/2 body undeclared)."""
    pairs = _map(
        '"$request_method:$http_content_length$http_transfer_encoding"', "$arkray_page_refused"
    )
    for allowed in ("GET:", "HEAD:", "GET:0"):
        assert _apply(pairs, allowed, {}) == "0", allowed
    for refused in ("POST:", "POST:1000", "GET:10", "GET:chunked", "PUT:0", "OPTIONS:", "DELETE:"):
        assert _apply(pairs, refused, {}) == "1", refused
    page = _blocks(_https_server())["/"]
    lines = [
        line.strip()
        for line in page.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines[:3] == ["if ($arkray_page_refused) {", "return 405;", "}"]
    assert "client_max_body_size 1;" in page


def test_small_bodies_have_a_per_address_ceiling_under_the_request_slots():
    """Small declared bodies (16 KB or less) take no JSON-body slot (L13) but did count against
    the 256 request slots alone: a ceiling of their own, at most half of those, leaves the
    address's reads and page loads room whatever its small bodies hold."""
    variables = {"arkray_client_key": "203.0.113.7"}
    pairs = _map('"$request_method:$http_content_length"', "$arkray_small_body_key")
    for counted in ("POST:1", "PATCH:200", "POST:16384", "GET:10"):
        assert _apply(pairs, counted, variables) == "203.0.113.7", counted
    for skipped in ("GET:", "POST:", "POST:0", "POST:16385", "POST:1048576"):
        assert _apply(pairs, skipped, variables) == "", skipped
    zones = {
        zone: key
        for key, zone in re.findall(r"^limit_conn_zone (\S+) zone=(\w+):\d+m;", _http_level(), re.M)
    }
    assert zones["arkray_small_body"] == "$arkray_small_body_key"
    head = _server_head()
    (small,) = re.findall(r"^\s*limit_conn arkray_small_body (\d+);", head, re.M)
    (requests,) = re.findall(r"^\s*limit_conn arkray_conn (\d+);", head, re.M)
    # An office's writes in flight at once are a handful (capacity test: 89 requests a second
    # at a 38 ms median from one address); the rest of the slots stay for reads.
    assert 32 <= int(small) <= int(requests) // 2


def test_uploads_dont_wait_for_json_body_slots():
    """`arkray_body_address` was counted before the upload's session check: 16 chunked JSON
    bodies under two forged sessions blocked co-workers' uploads. Uploads have their own temp
    area and their own per-address zone; the JSON bodies' zones don't apply to them."""
    uploads = _blocks(_https_server())["~* ^/api/v1/workspaces/[^/]+/activities/[^/]+/attachments$"]
    zones = set(re.findall(r"^\s*limit_conn (\w+) \d+;", uploads, re.M))
    assert zones == {"arkray_conn", "arkray_small_body", "arkray_upload"}


def test_headers_must_arrive_within_10_seconds():
    """nginx sets the header timer once (a whole-header deadline, unlike the body's between
    reads): a client trickling its headers is dropped within 10 s."""
    (value,) = re.findall(r"^client_header_timeout (\d+)s;", _http_level(), re.M)
    assert int(value) <= 10
