"""capacity_test.py's target (docs/staging-validation.md#prerequisites): the Host header, the
saved sessions' cookie domain and certificate checking follow --base, so one tool drives this
machine's production-shaped stack and a staging host; the local defaults are what they were.
And its monitor without a database: "not sampled", never a dead thread. No network.
"""

from __future__ import annotations

import argparse
import asyncio
import ssl
import subprocess
import time
from typing import Any

import certifi
import httpx
import pytest

from tests.performance import capacity_test as ct

LOCAL = "https://localhost:8443"
STAGING = "https://crm-staging.example.com"
ACCOUNTS = {
    "password": "not-a-password",
    "users": [{"index": 0, "id": "u0", "email": "u0@example.invalid", "role": "sales_user"}],
    "admins": [],
}


def options(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ct.common(parser)
    return parser.parse_args(list(argv))


def test_the_local_stack_keeps_its_target():
    """Byte for byte what the tool did before: Host localhost:8443, cookies under
    localhost.local, the self-signed certificate unchecked, metrics asked for as localhost."""
    local = options()
    assert local.base == LOCAL
    assert ct.target_host(local) == "localhost:8443"
    assert ct.cookie_domain(local.base) == "localhost.local"
    assert ct.tls_verify(local) is False
    assert local.metrics_host == "localhost"
    by_address = options("--base", "https://127.0.0.1:8443")
    assert ct.target_host(by_address) == "localhost:8443"
    assert ct.tls_verify(by_address) is False


def test_another_host_is_its_own_target_and_its_certificate_is_checked():
    staging = options("--base", STAGING)
    assert ct.target_host(staging) == "crm-staging.example.com"
    assert ct.cookie_domain(staging.base) == "crm-staging.example.com"
    assert ct.tls_verify(staging) is True
    assert ct.target_host(options("--base", "https://crm.example.com:8443")) == (
        "crm.example.com:8443"
    )


def test_explicit_options_override_the_derived_target():
    assert ct.target_host(options("--base", STAGING, "--host", "crm.internal")) == "crm.internal"
    assert ct.tls_verify(options("--base", STAGING, "--insecure")) is False
    context = ct.tls_verify(options("--base", STAGING, "--ca-file", certifi.where()))
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert options("--metrics-host", "api.internal").metrics_host == "api.internal"


@pytest.mark.parametrize("base", [LOCAL, "https://127.0.0.1:8443", STAGING])
def test_cookie_domain_is_where_the_client_files_a_sessions_cookies(base):
    jar = httpx.Cookies()
    response = httpx.Response(
        200,
        headers={"set-cookie": "arkray_sessionid=abc; Path=/; Secure; HttpOnly"},
        request=httpx.Request("POST", base + "/api/v1/auth/login"),
    )
    jar.extract_cookies(response)
    (cookie,) = list(jar.jar)
    assert cookie.domain == ct.cookie_domain(base)


@pytest.mark.parametrize("base", [LOCAL, "https://127.0.0.1:8443", STAGING])
def test_a_saved_session_is_sent_back_to_its_host(base):
    saved = httpx.Cookies()
    saved.set("arkray_sessionid", "abc", domain=ct.cookie_domain(base))
    request = httpx.Request("GET", base + "/api/v1/auth/me")
    saved.set_cookie_header(request)
    assert request.headers.get("cookie") == "arkray_sessionid=abc"


def test_the_old_fixed_domain_lost_every_saved_session_on_another_host():
    """The reason for cookie_domain: under "localhost.local" nothing is sent to staging."""
    saved = httpx.Cookies()
    saved.set("arkray_sessionid", "abc", domain="localhost.local")
    request = httpx.Request("GET", STAGING + "/api/v1/auth/me")
    saved.set_cookie_header(request)
    assert "cookie" not in request.headers


def test_virtual_users_carry_the_target():
    for base, host, tls in (
        (LOCAL, "localhost:8443", False),
        (STAGING, "crm-staging.example.com", True),
    ):
        run = ct.Run(options("--base", base), ACCOUNTS)
        vu = ct.VU(run, ACCOUNTS["users"][0], 0)
        try:
            assert vu.client.headers["Host"] == host
            assert vu.client.headers["Origin"] == base
            assert (run.host, run.tls, run.cookie_domain) == (host, tls, ct.cookie_domain(base))
        finally:
            asyncio.run(vu.client.aclose())


@pytest.mark.parametrize("pg", ["", "postgres://nobody@127.0.0.1:1/unreachable"])
def test_the_monitor_without_a_database_samples_the_rest(monkeypatch, pg):
    import psycopg

    attempts: list[dict[str, Any]] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(kwargs)
        raise psycopg.OperationalError("connection refused")

    def no_docker(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(ct.subprocess, "run", no_docker)
    monkeypatch.setattr(ct, "host_load", lambda: (12.5, 2048.0))
    monitor = ct.Monitor(options("--pg", pg, "--metrics-url", "", "--monitor-every", "0.02"))
    monitor.start()
    deadline = time.time() + 5
    while len(monitor.rows) < 3 and time.time() < deadline:
        time.sleep(0.01)
    monitor.finish()
    assert len(monitor.rows) >= 3, "the monitor thread died"
    assert all(row["db_error"] == "not_sampled" for row in monitor.rows)
    assert monitor.rows[0]["host_cpu"] == 12.5
    assert monitor.summarise()["sampling_errors"]["db_error"] == len(monitor.rows)
    if pg:
        assert attempts
        assert all(kwargs.get("connect_timeout") == 5 for kwargs in attempts)
    else:
        assert not attempts
