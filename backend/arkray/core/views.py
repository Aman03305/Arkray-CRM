"""JSON replacements for Django's HTML error pages (this service only speaks JSON).

Deliberately independent of DRF so these still work if the API layer itself is broken.
"""

from __future__ import annotations

import sys

from django.http import HttpRequest, JsonResponse

from .errors import error_body


def bad_request(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return JsonResponse(error_body("bad_request", "Bad request."), status=400)


def permission_denied(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return JsonResponse(error_body("permission_denied", "Permission denied."), status=403)


def not_found(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return JsonResponse(error_body("not_found", "Not found."), status=404)


DATABASE_RETRY_AFTER_S = 30


def database_unavailable(exc: BaseException | None) -> bool:
    """The database can't be reached, as opposed to a query failing: a refused, dropped or
    unresolvable connection (psycopg's plain OperationalError), SQLSTATE class 08, the server
    shutting down or not yet accepting connections, or no connection slot left. A statement
    timeout, deadlock or lock timeout is an OperationalError too, but a fault to fix rather
    than an outage, so it stays a 500."""
    import psycopg
    from django.db import InterfaceError, OperationalError

    if isinstance(exc, InterfaceError):  # the connection is already closed
        return True
    if not isinstance(exc, OperationalError):
        return False
    cause = exc.__cause__
    if type(cause) is psycopg.OperationalError:
        return True
    # A host that never answers (connect_timeout) and an exhausted connection pool: the
    # commonest outages, and subclasses without a SQLSTATE (Phase 10 review, P2).
    from psycopg_pool import PoolTimeout

    if isinstance(cause, psycopg.errors.ConnectionTimeout | PoolTimeout):
        return True
    # By SQLSTATE: psycopg's class-08 errors don't share a connection base class.
    state = getattr(cause, "sqlstate", None) or ""
    return state.startswith("08") or state in _UNAVAILABLE_STATES


# Shutting down (admin, crash), not accepting connections yet, no connection slot left.
_UNAVAILABLE_STATES = frozenset({"57P01", "57P02", "57P03", "53300"})


def server_error(request: HttpRequest) -> JsonResponse:
    # Must not touch the database or anything else that might be the cause of the error.
    # Django calls this while handling the exception, so sys.exception() is the error.
    # Phase 10 drill: a database outage was a generic 500; it is a 503 with Retry-After, so
    # the UI can say "temporarily unavailable" and alerting can tell an outage from a bug.
    if database_unavailable(sys.exception()):
        response = JsonResponse(
            error_body(
                "service_unavailable",
                "The service is temporarily unavailable. Please try again shortly.",
            ),
            status=503,
        )
        response["Retry-After"] = str(DATABASE_RETRY_AFTER_S)
        return response
    return JsonResponse(error_body("server_error", "An unexpected error occurred."), status=500)


def csrf_failure(request: HttpRequest, reason: str = "") -> JsonResponse:
    return JsonResponse(
        error_body("csrf_failed", "CSRF verification failed. Refresh the page and try again."),
        status=403,
    )
