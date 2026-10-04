"""Liveness and readiness probes.

- /health/live  — the process can serve requests. No dependency checks (a DB outage must
  not cause the orchestrator to restart healthy web processes in a loop).
- /health/ready — the instance should receive traffic. PostgreSQL is required; Redis is
  optional (the CRM degrades gracefully without it), so a Redis failure reports
  "degraded" but stays ready.

Responses deliberately contain no hostnames, versions or error messages.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .metrics import bounded

logger = logging.getLogger(__name__)


@never_cache
@require_GET
def live(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def _select_one() -> bool:
    """In a probe thread, on its own connection, closed afterwards."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return bool(cursor.fetchone() == (1,))
    finally:
        connection.close()


def _database_ok() -> bool:
    # Bounded: a database that stalls (paused, failing over) instead of refusing made the
    # probe wait as long as the stall, so the instance never reported itself unready
    # (whole-software audit). Now: 503 within the probe deadline.
    try:
        return bounded(_select_one)
    except TimeoutError:
        logger.warning(
            "readiness_check_failed", extra={"dependency": "database", "reason": "timeout"}
        )
        return False
    except Exception:  # any failure at all means "not ready"
        logger.warning("readiness_check_failed", extra={"dependency": "database"}, exc_info=True)
        return False


def _cache_ok() -> bool:
    # The Redis cache backend swallows errors (IGNORE_EXCEPTIONS), so a failed round trip
    # shows up as a missing value rather than an exception.
    try:
        cache.set("health:probe", "1", timeout=5)
        return bool(cache.get("health:probe") == "1")
    except Exception:  # noqa: BLE001
        return False


@never_cache
@require_GET
def ready(request: HttpRequest) -> JsonResponse:
    if not _database_ok():
        return JsonResponse({"status": "unavailable"}, status=503)
    if not _cache_ok():
        logger.warning("readiness_degraded", extra={"dependency": "cache"})
        return JsonResponse({"status": "degraded"})
    return JsonResponse({"status": "ok"})
