"""Maps every API error to one envelope:

    {"error": {"code": "...", "message": "...", "details": ..., "request_id": "..."}}

Unexpected exceptions are not handled here: they propagate to Django, get logged with a
traceback, and the client receives a generic 500 envelope (arkray.core.views.server_error).
Stack traces never reach clients.
"""

from __future__ import annotations

from typing import Any

from rest_framework import exceptions as drf_exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .csrf import CsrfFailed
from .errors import DomainError, RateLimitedError, error_body

_DRF_CODES: dict[type[drf_exceptions.APIException], str] = {
    CsrfFailed: "csrf_failed",
    drf_exceptions.ValidationError: "validation_error",
    drf_exceptions.ParseError: "malformed_request",
    drf_exceptions.NotAuthenticated: "not_authenticated",
    drf_exceptions.AuthenticationFailed: "not_authenticated",
    drf_exceptions.PermissionDenied: "permission_denied",
    drf_exceptions.NotFound: "not_found",
    drf_exceptions.MethodNotAllowed: "method_not_allowed",
    drf_exceptions.NotAcceptable: "not_acceptable",
    drf_exceptions.UnsupportedMediaType: "unsupported_media_type",
    drf_exceptions.Throttled: "rate_limited",
}


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, DomainError):
        domain = Response(error_body(exc.code, exc.message, exc.details), status=exc.http_status)
        if isinstance(exc, RateLimitedError):
            domain["Retry-After"] = str(exc.retry_after)
        return domain

    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # unexpected -> Django's 500 handling (logged, generic body)

    if isinstance(exc, drf_exceptions.ValidationError):
        body = error_body("validation_error", "Some fields are invalid.", response.data)
    elif isinstance(exc, drf_exceptions.APIException):
        code = next((c for t, c in _DRF_CODES.items() if isinstance(exc, t)), "error")
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        body = error_body(code, str(detail))
    else:  # Django Http404 / PermissionDenied converted by DRF
        code = "not_found" if response.status_code == 404 else "permission_denied"
        body = error_body(code, str(response.data.get("detail", "")) or code)
    response.data = body
    return response
