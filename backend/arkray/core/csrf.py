"""CSRF enforcement for API views.

DRF exempts its views from Django's CSRF middleware and re-checks CSRF only for requests
that are already authenticated. That leaves anonymous endpoints (login, password reset,
invitation acceptance) open to login-CSRF and similar attacks, so `ApiView` enforces the
same double-submit + Origin check on *every* unsafe request, signed in or not.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest
from django.middleware.csrf import CsrfViewMiddleware
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.request import Request

# Django's own CSRF logger, so rejections land where operators already look.
logger = logging.getLogger("django.security.csrf")


class CsrfFailed(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "CSRF verification failed. Refresh the page and try again."
    default_code = "csrf_failed"


class _CsrfCheck(CsrfViewMiddleware):
    def _reject(self, request: HttpRequest, reason: str) -> str:
        return reason


def _no_response(request: HttpRequest) -> None:  # pragma: no cover - never called
    return None


def enforce_csrf(request: Request | HttpRequest) -> None:
    """Raise CsrfFailed unless the request passes Django's CSRF checks."""
    django_request = request._request if isinstance(request, Request) else request
    check = _CsrfCheck(_no_response)  # type: ignore[arg-type]
    check.process_request(django_request)  # loads the CSRF cookie into request.META
    reason = check.process_view(django_request, None, (), {})  # type: ignore[arg-type]
    if reason:
        logger.warning("csrf_rejected", extra={"reason": str(reason)[:200]})
        raise CsrfFailed()
