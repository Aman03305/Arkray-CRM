"""The reference edge proxy's memory is bounded before anyone signs in (deep security audit H-1).

nginx buffered every request body over 16 KB in /var/cache/nginx, a tmpfs (memory) with no
size, with no per-client connection limit and nginx's 60 s body timeout: 24 connections from
one address, each sending 10 MB of an attachment and then a byte every few seconds, got the
container OOM-killed (256 MB) and cut every user off. Now (infrastructure/nginx/arkray.conf,
the tmpfs caps in test_compose_production.py):

- per client (the sign-in limit's key), at most so many requests in flight in every location,
  and uploads fewer, in a zone of their own;
- the refusal is the API's JSON 429 (a page: a minimal HTML 429), and a full temp area is the
  API's 503 instead of nginx's own 500 page;
- headers and bodies that stall for more than 30 s, and clients that stop reading, are dropped;
- no one response may take more than a quarter of the temp area, and the per-client upload
  limit times the upload size fits inside it;
- requests that carry a body have a per-client limit of their own, and JSON bodies an area of
  their own (independent review, P2-2: 80 slow 1 MB JSON bodies from one address had filled
  the uploads' area);
- data-export downloads (100 MB) are spooled whole on a disk volume, with a per-client and a
  global limit (independent review, P2-1: the 16 MB cap had a slow export cut short when
  gunicorn's 30 s timeout killed the worker, behind a 200);
- an upload's body is read only once Django has confirmed its session (auth_request, in the
  access phase, before the body): without one, 401 and nothing spooled.

Checked live in the pinned nginx with the compose limits (docs/security.md): the same attack
is held at 4 uploads per address (429 for the rest) and memory peaked at 72 MB; from six
addresses the temp area filled at 57 MB, the rest got 503, nothing was killed.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from tests.architecture.test_compose_production import (
    PRODUCTION,
    UNITS,
    load,
    tmpfs_sizes,
)
from tests.architecture.test_edge_errors import NGINX, _blocks, _https_server
from tests.helpers import signed_in

SERVER_ZONE = "arkray_conn"
BODY_ZONE = "arkray_body"
BODY_ADDRESS_ZONE = "arkray_body_address"
SMALL_BODY_ZONE = "arkray_small_body"
UPLOAD_ZONE = "arkray_upload"
EXPORT_ZONE, EXPORT_ALL_ZONE = "arkray_export", "arkray_export_all"
UPLOADS = "~* ^/api/v1/workspaces/[^/]+/activities/[^/]+/attachments$"
EXPORTS = "~* ^/api/v1/admin/privacy/exports/[^/]+/download$"
UPLOAD_AREA, BODY_AREA = "/var/cache/nginx", "/var/cache/nginx-body"
SESSION_CHECK = "= /_arkray/session"


def _nginx_service() -> dict[str, Any]:
    service: dict[str, Any] = load(PRODUCTION)["services"]["nginx"]
    return service


def _area(path: str) -> int:
    size = tmpfs_sizes(_nginx_service())[path]
    assert size is not None, path
    return size


def _named_volume_targets() -> set[str]:
    """Where the edge mounts a named volume (disk, not memory)."""
    production = load(PRODUCTION)
    targets = set()
    for entry in _nginx_service()["volumes"]:
        source, _, rest = str(entry).partition(":")
        if source in (production.get("volumes") or {}):
            targets.add(rest.split(":")[0])
    return targets


def _text() -> str:
    return NGINX.read_text(encoding="utf-8")


def _http_level() -> str:
    """Directives outside every server block (this file is included inside `http {}`)."""
    text = _text()
    return text[: text.index("\nserver {")]


def _server_head() -> str:
    """The HTTPS server's own directives, before its first location."""
    server = _https_server()
    return server[: server.index("    location")]


def _size(value: str) -> int:
    found = re.fullmatch(r"(\d+)([kmg]?)", value)
    assert found, value
    return int(found.group(1)) * (UNITS[found.group(2)] if found.group(2) else 1)


def _seconds(value: str) -> int:
    found = re.fullmatch(r"(\d+)(s|m)?", value)
    assert found, value
    return int(found.group(1)) * (60 if found.group(2) == "m" else 1)


def _limits(body: str) -> dict[str, int]:
    return {zone: int(n) for zone, n in re.findall(r"^\s*limit_conn (\w+) (\d+);", body, re.M)}


def _proxied() -> dict[str, str]:
    """The locations a client's request is proxied from (not the internal session check)."""
    return {
        name: body
        for name, body in _blocks(_https_server()).items()
        if "proxy_pass" in body and "internal;" not in body
    }


def test_connections_are_counted_per_client_with_the_sign_in_limits_key():
    """The same key as limit_req (an IPv6 client by its /64, R127): keyed by the raw
    address, one host would get 2**64 budgets."""
    http = _http_level()
    found = re.findall(r"^limit_conn_zone (\S+) zone=(\w+):\d+m;", http, re.M)
    zones = {zone: key for key, zone in found}
    assert zones == {
        SERVER_ZONE: "$arkray_client_key",
        # Per session, or the address without one; and per address over all of them
        # (adversarial review of 73ff1e3, L13: test_edge_hardening.py).
        BODY_ZONE: "$arkray_body_client_key",
        BODY_ADDRESS_ZONE: "$arkray_body_address_key",
        # Small declared bodies, per address under the request slots (review round 1, INFRA-3).
        SMALL_BODY_ZONE: "$arkray_small_body_key",
        UPLOAD_ZONE: "$arkray_client_key",
        EXPORT_ZONE: "$arkray_client_key",
        # One key for the whole server: every client together.
        EXPORT_ALL_ZONE: "$server_name",
    }
    assert re.search(r"^limit_conn_status 429;$", http, re.M)


def test_requests_with_a_body_count_by_the_same_key():
    """P2-2: a GET, HEAD or OPTIONS that declares no body (no Content-Length, no
    Transfer-Encoding) is left out; since the adversarial review of 73ff1e3 (L13) so is a
    declared body that stays in memory (16 KB at most: it never reaches a temp area, and a
    sign-in or a save no longer waits behind a trickler's bodies). Everything else counts:
    per session, the address without one (test_edge_hardening.py has the whole rule)."""
    (body,) = re.findall(
        r'^map "\$request_method:\$http_content_length\$http_transfer_encoding" '
        r"\$arkray_spooled_body \{\n(.*?)^\}",
        _http_level(),
        re.S | re.M,
    )
    rules = [line.split() for line in body.strip().splitlines() if not line.strip().startswith("#")]
    assert rules[0] == ['"~^(GET|HEAD|OPTIONS):$"', '"";']
    assert rules[-1] == ["default", "1;"]
    assert len(rules) == 3  # and the small declared bodies between them
    skipped = re.compile(rules[0][0].strip('"').removeprefix("~"))
    for method in ("GET", "HEAD", "OPTIONS"):
        assert skipped.search(f"{method}:")
        assert not skipped.search(f"{method}:chunked"), method
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert not skipped.search(f"{method}:"), method


def test_every_location_counts_against_the_per_client_limit():
    """In the server block, so every location has it. nginx doesn't inherit limit_conn into a
    location that has its own: such a location repeats the server's line. Uploads leave out the
    JSON bodies' zones on purpose (review round 1, INFRA-3: counted before the session check,
    forged sessions' JSON trickles held them and blocked co-workers' uploads); their own area
    and zone bound them."""
    server = _limits(_server_head())
    assert set(server) == {SERVER_ZONE, BODY_ZONE, BODY_ADDRESS_ZONE, SMALL_BODY_ZONE}
    # Bounds one address's share of nginx's memory, and still lets an office behind one
    # address work (docs/capacity.md: 100 users from one address).
    assert 64 <= server[SERVER_ZONE] <= 256
    proxied = _proxied()
    assert UPLOADS in proxied
    assert EXPORTS in proxied
    json_body_zones = {BODY_ZONE, BODY_ADDRESS_ZONE}
    for name, body in proxied.items():
        own = _limits(body)
        if own:
            for zone, limit in server.items():
                if name == UPLOADS and zone in json_body_zones:
                    assert zone not in own, (name, zone)
                else:
                    assert own.get(zone) == limit, (name, zone)


def test_json_bodies_are_bounded_per_client_in_an_area_of_their_own():
    """P2-2: 8 bodies of 1 MB per client take at most half of the JSON bodies' area. That
    area is not the uploads' one, so a flood of bodies an HTTP/2 client doesn't declare
    (not counted above) can stop JSON writes over 16 KB, never uploads."""
    head = _server_head()
    server = _limits(head)
    (body_size,) = re.findall(r"^\s*client_max_body_size (\d+[kmg]?);", head, re.M)
    assert 1 <= server[BODY_ZONE] <= 8
    assert server[BODY_ZONE] * _size(body_size) <= _area(BODY_AREA) // 2
    (temp,) = re.findall(r"^\s*client_body_temp_path (\S+);", head, re.M)
    assert temp == BODY_AREA
    # Uploads spool to the 64 MB area (the per-client upload check below), nothing else does.
    for name, body in _proxied().items():
        paths = re.findall(r"client_body_temp_path (\S+);", body)
        if name == UPLOADS:
            assert paths == [f"{UPLOAD_AREA}/client_temp"], name
        else:
            assert not paths, name


def test_uploads_have_a_tighter_limit_of_their_own():
    """Its own zone: in the server's zone, every request the client has in flight anywhere
    would count against the upload limit (an office with 4 page loads in flight could not
    upload). And the uploads one client may have in flight fit the capped temp area with
    room for everyone else."""
    proxied = _proxied()
    uploads = _limits(proxied[UPLOADS])
    assert 1 <= uploads[UPLOAD_ZONE] <= 4
    for name, body in proxied.items():
        if name != UPLOADS:
            assert UPLOAD_ZONE not in _limits(body), name
    (body_size,) = re.findall(r"client_max_body_size (\d+[kmg]?);", proxied[UPLOADS])
    cache = tmpfs_sizes(load(PRODUCTION)["services"]["nginx"])["/var/cache/nginx"]
    assert cache is not None
    assert uploads[UPLOAD_ZONE] * _size(body_size) <= cache * 3 // 4


def test_every_refusal_is_the_apps_own_shape():
    """limit_conn's 429 (503 by default, which the web app would show as an outage) and the
    500 nginx answers when its temp area is full: in the API's JSON envelope with the request
    id on API routes, a minimal page on pages (test_edge_errors.py checks those)."""
    assert re.search(r"^limit_conn_status 429;$", _http_level(), re.M)
    for name, body in _proxied().items():
        if name.startswith("~* ^/api"):
            assert "error_page 429 = @arkray_rate_limited;" in body, name
            assert "error_page 500 502 504 = @arkray_api_unavailable;" in body, name
        else:
            assert "error_page 429 = @arkray_web_rate_limited;" in body, name
            assert "error_page 500 502 504 = @arkray_web_unavailable;" in body, name
    rate_limited = _blocks(_https_server())["@arkray_rate_limited"]
    assert "add_header Retry-After" in rate_limited


def test_stalled_clients_are_dropped_within_30_seconds():
    """nginx's defaults are 60 s; set once, at the http level, never overridden in a server
    or location.
    (client_body_timeout runs between two reads: a client trickling a byte now and then
    keeps its connection, which is what the per-client limits are for.)"""
    text, http = _text(), _http_level()
    for directive in ("client_header_timeout", "client_body_timeout", "send_timeout"):
        (value,) = re.findall(rf"^{directive} (\S+);", http, re.M)
        assert _seconds(value) <= 30, directive
        assert len(re.findall(rf"^\s*{directive}\s", text, re.M)) == 1, directive


def test_no_response_can_take_the_temp_area():
    """A response the client reads slowly is spooled to the temp area: at most a quarter of
    it each. Not 0 (the audit's suggestion): every attachment must still fit whole, so a slow
    download frees its gunicorn sync worker at once instead of holding it for the whole
    transfer (measured: 20 s held for a 20 s read with 0, 0.01-0.1 s with this)."""
    from django.conf import settings

    head = _server_head()
    (value,) = re.findall(r"^\s*proxy_max_temp_file_size (\S+);", head, re.M)
    cache = tmpfs_sizes(load(PRODUCTION)["services"]["nginx"])["/var/cache/nginx"]
    assert cache is not None
    assert settings.ATTACHMENT_MAX_BYTES <= _size(value) <= cache // 4
    # Raised only where the spool is on disk (the export downloads, below); never turned off.
    for name, body in _proxied().items():
        if "proxy_max_temp_file_size" in body:
            assert name == EXPORTS, name
    assert "proxy_buffering off" not in _text()


def test_data_exports_are_spooled_whole_on_disk_and_bounded():
    """P2-1: a whole export (EXPORT_MAX_BYTES and the ZIP's own overhead) is spooled, so a
    slow download frees its gunicorn worker at once; on a named volume (disk), never one of
    the tmpfs areas; at most 2 per client and 4 in all; no request body (refused 413 from its
    Content-Length, before it is counted); before the generic /api/ location (regex
    locations match in order). Measured: an 80 MB export at 1 MB/s arrived whole in 80 s
    (upstream time 0.07 s, no worker killed); 41 of 60 MB, behind a 200, with the 16 MB cap."""
    from django.conf import settings

    proxied = _proxied()
    names = list(proxied)
    assert names.index(EXPORTS) < names.index("~* ^/api/")
    body = proxied[EXPORTS]
    (size,) = re.findall(r"proxy_max_temp_file_size (\S+);", body)
    assert _size(size) >= settings.EXPORT_MAX_BYTES * 105 // 100
    (path,) = re.findall(r"proxy_temp_path (\S+);", body)
    assert path in _named_volume_targets()
    assert path not in tmpfs_sizes(_nginx_service())
    limits = _limits(body)
    assert 1 <= limits[EXPORT_ZONE] <= limits[EXPORT_ALL_ZONE] <= 4
    # The most the volume holds at once.
    assert limits[EXPORT_ALL_ZONE] * _size(size) <= 512 * UNITS["m"]
    assert "client_max_body_size 1;" in body
    for name, other in proxied.items():
        if name != EXPORTS:
            assert EXPORT_ZONE not in _limits(other), name
            assert EXPORT_ALL_ZONE not in _limits(other), name


def test_an_upload_body_is_read_only_for_a_signed_in_session():
    """auth_request runs in the access phase, before the body is read: without a valid
    session the upload is refused (401, the API's envelope) and its body discarded, never
    spooled. Measured: 30 unauthenticated slow 10.5 MB uploads from one address and 60 from
    six more all got 401 at once, the upload area stayed at 0 KB, memory at 32 MB; a signed-in
    10 MB upload still arrived whole (201), over HTTP/1.1 and HTTP/2."""
    uploads = _proxied()[UPLOADS]
    assert "auth_request /_arkray/session;" in uploads
    assert "error_page 401 = @arkray_not_authenticated;" in uploads
    assert "error_page 403 = @arkray_permission_denied;" in uploads
    # Any other answer from the check is an error to auth_request (500): fails closed, 503.
    assert "error_page 500 502 504 = @arkray_api_unavailable;" in uploads
    # Only the upload route waits for the check.
    for name, body in _blocks(_https_server()).items():
        if name != UPLOADS:
            assert "auth_request" not in body, name


def test_the_session_check_is_internal_and_sends_no_body():
    """GET /api/v1/auth/me with the browser's headers and none of its body. The location's
    own proxy_set_header lines replace the server's, so it repeats every one of them (without
    X-Forwarded-Proto Django would redirect to https, the check would fail, and every upload
    with it). Its client_max_body_size is off: the server's 1 MB, checked against the
    upload's Content-Length, refused every real upload's check with 413 (found live)."""
    blocks = _blocks(_https_server())
    check = blocks[SESSION_CHECK]
    assert check.strip().startswith("internal;") or "\n        internal;\n" in check
    for line in (
        "proxy_method GET;",
        "proxy_pass_request_body off;",
        'proxy_set_header Content-Length "";',
        "client_max_body_size 0;",
        "proxy_pass http://arkray_api/api/v1/auth/me;",
    ):
        assert line in check, line
    server = re.findall(r"^    proxy_set_header .+;$", _server_head(), re.M)
    assert server
    for line in server:
        assert "    " + line.strip() in check, line
    (timeout,) = re.findall(r"proxy_read_timeout (\d+)s;", check)
    assert int(timeout) <= 35
    # The refusals are the API's own envelope, with the request id.
    for name, status, code in (
        ("@arkray_not_authenticated", 401, "not_authenticated"),
        ("@arkray_permission_denied", 403, "permission_denied"),
    ):
        body = blocks[name]
        assert f"return {status} '" in body, name
        assert f'"code":"{code}"' in body, name
        assert '"request_id":"$request_id"' in body, name
    assert "WWW-Authenticate 'Session realm=\"arkray\"'" in blocks["@arkray_not_authenticated"]


def _as_the_session_check(client: APIClient, **headers: str):
    """What nginx's subrequest sends for a browser's upload: a GET with the upload's headers,
    no body, no Content-Length or Content-Type."""
    return client.get(
        "/api/v1/auth/me",
        HTTP_SEC_FETCH_SITE=headers.get("site", "same-origin"),
        HTTP_ORIGIN="https://testserver",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        HTTP_X_FORWARDED_PROTO="https",
    )


@pytest.mark.django_db
def test_the_session_check_answers_as_auth_request_needs(user_a):
    """200 for a signed-in session, CSRF enforced as for the browser (a GET passes), cheap
    and without a write; 401 without a session; a cross-site request (deep security audit
    L-3) is a 404, which auth_request treats as an error: the upload fails closed (503)."""
    client = signed_in(user_a, APIClient(enforce_csrf_checks=True))
    with CaptureQueriesContext(connection) as queries:
        response = _as_the_session_check(client)
    assert response.status_code == 200, response.content
    statements = [q["sql"].lstrip().split()[0].upper() for q in queries.captured_queries]
    assert not {"INSERT", "UPDATE", "DELETE"} & set(statements), statements
    assert len(statements) <= 5, statements  # 3 SELECTs, measured
    assert _as_the_session_check(APIClient()).status_code == 401
    assert _as_the_session_check(client, site="cross-site").status_code == 404
