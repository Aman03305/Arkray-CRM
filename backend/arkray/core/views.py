"""JSON replacements for Django's HTML error pages (this service only speaks JSON).

Deliberately independent of DRF so these still work if the API layer itself is broken.
"""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse

from .errors import error_body


def bad_request(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return JsonResponse(error_body("bad_request", "Bad request."), status=400)


def permission_denied(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return JsonResponse(error_body("permission_denied", "Permission denied."), status=403)


def not_found(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return JsonResponse(error_body("not_found", "Not found."), status=404)


def server_error(request: HttpRequest) -> JsonResponse:
    # Must not touch the database or anything else that might be the cause of the error.
    return JsonResponse(error_body("server_error", "An unexpected error occurred."), status=500)


def csrf_failure(request: HttpRequest, reason: str = "") -> JsonResponse:
    return JsonResponse(
        error_body("csrf_failed", "CSRF verification failed. Refresh the page and try again."),
        status=403,
    )
