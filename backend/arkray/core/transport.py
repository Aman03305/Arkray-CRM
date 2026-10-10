"""Transport security of the backend's own connections (docs/security.md#backend-tls).

Production refuses to start when a connection that leaves the machine could travel without
verified TLS (privacy remediation, P2-1):

- PostgreSQL: `sslmode=verify-full` and a CA (`sslrootcert=<file>`, or `system` for a
  publicly trusted certificate). libpq's default (`prefer`) falls back to plain text when
  the server (or anyone in the middle) says it has no TLS, and checks no certificate.
- Redis (broker and cache): `rediss://` with `ssl_cert_reqs=required`. Without that
  parameter kombu connects to a `rediss://` broker with CERT_NONE: encrypted, but to
  whoever answers.
- SMTP: STARTTLS (`smtp+tls://`) or implicit TLS (`smtps://`), checked against the system
  CA store or EMAIL_TLS_CA_FILE (arkray.core.mail). The emails carry one-time account links.
- S3: an https endpoint (or none: AWS's own endpoints are https).
- ClamAV (clamd has no TLS): a loopback or private host only.

Exceptions, never silent:
- loopback addresses (the traffic never leaves the machine: a local TLS proxy, a sidecar);
- the names listed in BACKEND_TLS_PRIVATE_HOSTS: single-label container or service names
  (`postgres`, `redis`), which only a private network resolves. A dotted name or an IP
  address is refused there: it may be routed anywhere;
- DJANGO_ALLOW_INSECURE_LOCAL_HTTP=true (the local development stack only; production
  settings refuse it for any public host name).

Pure functions of the configuration: production settings call them, and the tests call them
directly as well as through a fresh interpreter.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def is_loopback(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host in LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_single_label(host: str) -> bool:
    """A name without dots that isn't an IP address: a container or service name."""
    host = host.strip().lower()
    if not host or "." in host or ":" in host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def private_host_problems(names: Collection[str]) -> list[str]:
    """BACKEND_TLS_PRIVATE_HOSTS may name container/service names only."""
    return [
        f"BACKEND_TLS_PRIVATE_HOSTS: {name!r} is not a single-label container or service name"
        " (a dotted name or an IP address may be routed outside the private network)."
        for name in names
        if not is_single_label(name)
    ]


def _exempt(host: str, private_hosts: Collection[str]) -> bool:
    return is_loopback(host) or host.strip().lower() in {h.lower() for h in private_hosts}


def database_problem(
    database: Mapping[str, Any], private_hosts: Collection[str] = ()
) -> str | None:
    """DATABASES["default"] must use verified TLS to anything but loopback or a listed
    private host."""
    host = str(database.get("HOST") or "")
    if not host or host.startswith("/") or _exempt(host, private_hosts):
        return None  # a Unix socket, the machine itself, or an explicitly private name
    options = database.get("OPTIONS") or {}
    sslmode = str(options.get("sslmode", "")).lower()
    if sslmode != "verify-full":
        return (
            f"DATABASE_URL: the connection to {host} must use sslmode=verify-full (got"
            f" {sslmode or 'no sslmode, i.e. prefer'}): anything less accepts plain text or an"
            " unverified certificate."
        )
    root = str(options.get("sslrootcert", ""))
    if not root:
        return (
            "DATABASE_URL: sslmode=verify-full needs sslrootcert (the provider's CA file, or"
            " 'system' for a publicly trusted certificate)."
        )
    if root != "system" and not Path(root).is_file():
        return f"DATABASE_URL: the sslrootcert file {root} does not exist."
    return None


def redis_problem(name: str, url: str, private_hosts: Collection[str] = ()) -> str | None:
    """A Redis URL must be rediss:// with certificate and host name verification, unless it
    is a Unix socket, loopback, or a listed private host."""
    # Exactly as the clients read it (backend review P3): redis-py matches the scheme and the
    # parameter names and values case-sensitively, so `REDISS://` would connect in clear,
    # and `ssl_cert_reqs=CERT_REQUIRED` would fail every connection, silently for the cache.
    parts = urlsplit(url)
    scheme = url.split(":", 1)[0]
    if scheme in {"unix", "redis+socket", "socket"}:
        return None
    host = parts.hostname or ""
    if _exempt(host, private_hosts):
        return None
    if scheme != "rediss":
        return f"{name}: the connection to {host or 'Redis'} must use rediss:// (TLS, lowercase)."
    query = dict(parse_qs(parts.query))
    if query.get("ssl_cert_reqs") != ["required"]:
        return (
            f"{name}: rediss:// needs ssl_cert_reqs=required, once and in lowercase (without"
            " it the Celery broker accepts any certificate)."
        )
    if query.get("ssl_check_hostname", ["true"]) != ["true"]:
        return f"{name}: ssl_check_hostname must be left out or true."
    return None


def smtp_problem(
    host: str, *, use_tls: bool, use_ssl: bool, private_hosts: Collection[str] = ()
) -> str | None:
    if use_tls or use_ssl or _exempt(host, private_hosts):
        return None
    return (
        f"EMAIL_URL: the connection to {host} must use STARTTLS (smtp+tls:// or smtps://) or"
        " implicit TLS (smtp+ssl://, port 465): the emails carry one-time account links."
    )


def s3_problem(endpoint_url: str, private_hosts: Collection[str] = ()) -> str | None:
    if not endpoint_url:
        return None  # AWS's own endpoints: https
    parts = urlsplit(endpoint_url)
    if parts.scheme.lower() == "https" or _exempt(parts.hostname or "", private_hosts):
        return None
    return "ATTACHMENT_S3_ENDPOINT_URL must be https:// (attachment files travel over it)."


def scanner_problem(scanner_url: str, private_hosts: Collection[str] = ()) -> str | None:
    """clamd speaks plain TCP: it must sit on the machine or the private network."""
    if not scanner_url:
        return None
    host = urlsplit(scanner_url).hostname or ""
    if _exempt(host, private_hosts):
        return None
    return (
        f"ATTACHMENT_SCANNER: clamd at {host} has no TLS; run it on loopback or a private host"
        " listed in BACKEND_TLS_PRIVATE_HOSTS."
    )


def redis_ssl_options(url: str) -> dict[str, Any] | None:
    """The broker's `broker_use_ssl` for a rediss:// URL: certificate and host name checks,
    instead of kombu's CERT_NONE fallback. kombu takes its TLS options from here only (it
    doesn't read ssl_* parameters in a rediss:// URL), so the URL's CA file and, for mutual
    TLS, its client certificate and key are carried over. None for any other scheme."""
    import ssl

    parts = urlsplit(url)
    if parts.scheme.lower() != "rediss":
        return None
    query = {key.lower(): values[-1] for key, values in parse_qs(parts.query).items()}
    options: dict[str, Any] = {"ssl_cert_reqs": ssl.CERT_REQUIRED, "ssl_check_hostname": True}
    for key in ("ssl_ca_certs", "ssl_certfile", "ssl_keyfile"):
        if query.get(key):
            options[key] = query[key]
    return options
