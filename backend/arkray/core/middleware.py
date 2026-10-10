"""Request context + structured access log."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
import uuid
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.functional import SimpleLazyObject, empty

from .context import ExecutionContext, bind_context, get_context, reset_context

logger = logging.getLogger("arkray.access")

REQUEST_ID_HEADER = "X-Request-ID"
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# Caller-supplied IDs are considered only if short and boring, so nobody can inject content
# into logs through this header.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def _request_ids(request: HttpRequest) -> tuple[str, str | None]:
    """(correlation id, client-supplied id to log separately).

    An incoming X-Request-ID becomes the correlation id (which is stored in audit records)
    only when a trusted edge proxy sets it (TRUST_INCOMING_REQUEST_ID). Otherwise we always
    mint our own, so clients cannot spoof or replay ids in the audit trail.
    """
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    valid = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else None
    if valid and settings.TRUST_INCOMING_REQUEST_ID:
        return valid, None
    return uuid.uuid4().hex, valid


def _parse_address(value: str) -> str | None:
    """An IP address, tolerating the port suffixes some proxies append:
    "198.51.100.7:51234" and "[2001:db8::7]:443" (a bare IPv6 address has several colons)."""
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        value = value[1:end] if end > 0 else ""
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip(request: HttpRequest) -> str | None:
    """Client IP, trusting X-Forwarded-For only for the configured number of proxies.

    None when the address can't be determined; rate limits then share one bucket for all
    such requests (they fail closed, see identity.throttling)."""
    candidate: str = request.META.get("REMOTE_ADDR") or ""
    proxies = settings.TRUSTED_PROXY_COUNT
    if proxies > 0:
        forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
        forwarded = [p for p in forwarded if p]
        if len(forwarded) >= proxies:
            candidate = forwarded[-proxies]
    return _parse_address(candidate) if candidate else None


class RequestContextMiddleware:
    """Assigns a request ID, binds the execution context and writes one access-log line."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id, client_request_id = _request_ids(request)
        request.client_request_id = client_request_id  # type: ignore[attr-defined]
        token = bind_context(
            ExecutionContext(correlation_id=request_id, client_ip=client_ip(request))
        )
        started = time.perf_counter()
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            # The API serves JSON only: nothing in a response may run, load or be framed,
            # even if a browser were tricked into rendering one (Phase 9 review).
            response.headers.setdefault("Content-Security-Policy", API_CSP)
            self._log(request, response.status_code, started)
            return response
        finally:
            reset_context(token)

    @staticmethod
    def _user_id(request: HttpRequest) -> str | None:
        user = getattr(request, "user", None)
        # Only a user the request already resolved (the API's authentication replaces the
        # lazy one). Resolving it here, for the log line, read the session store again:
        # with the database down that was a second connection attempt, doubling every
        # request's time to its 503 (whole-software audit).
        if isinstance(user, SimpleLazyObject) and getattr(user, "_wrapped", None) is empty:
            return None
        try:
            return str(user.pk) if user is not None and user.is_authenticated else None
        except Exception:  # noqa: BLE001 — e.g. the session lookup hits a database that is down
            return None

    @classmethod
    def _log(cls, request: HttpRequest, status: int, started: float) -> None:
        match = request.resolver_match
        fields = {
            "method": request.method,
            "path": request.path,  # never the query string: it may carry search terms
            "route": match.route if match else None,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "user_id": cls._user_id(request),
            # The one line per request that carries the client address (core.logging).
            "client_ip": context.client_ip if (context := get_context()) else None,
        }
        client_request_id = getattr(request, "client_request_id", None)
        if client_request_id:
            fields["client_request_id"] = client_request_id
        level = logging.DEBUG if request.path.startswith("/health/") else logging.INFO
        logger.log(level, "http_request", extra=fields)
