"""The metrics endpoint (Phase 10; docs/observability.md#metrics): `GET /health/metrics`.

Off unless METRICS_TOKEN is set; then a scraper must send `Authorization: Bearer <token>`.
Anything else (another method, no token, a wrong one) is a 404, exactly like an unknown
URL, so the endpoint isn't discoverable. It lives outside `arkray` because it composes
gauges from every module (the import layering allows nothing in `arkray` to sit above
`arkray.ai`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from django.conf import settings
from django.db import DatabaseError
from django.http import Http404, HttpRequest, HttpResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from arkray.activities import metrics as attachment_metrics
from arkray.ai import metrics as ai_metrics
from arkray.core import metrics
from arkray.core.views import database_unavailable

logger = logging.getLogger(__name__)


def _authorised(request: HttpRequest) -> bool:
    token = settings.METRICS_TOKEN
    supplied = request.headers.get("Authorization", "")
    scheme, _, credentials = supplied.partition(" ")
    if not token or request.method != "GET":  # no 405 to give the endpoint away
        return False
    if scheme.lower() == "bearer" and constant_time_compare(credentials.strip(), token):
        return True
    if supplied:
        # Someone tried a token: visible at the default level (health paths log at DEBUG);
        # never the value they sent.
        logger.warning("metrics_token_rejected")
    return False


# CSRF-exempt only so that unsafe methods get the same 404 as an unknown URL (the CSRF
# check's 403 gave the endpoint away); nothing but an authorised GET does anything.
@csrf_exempt
@never_cache
def metrics_view(request: HttpRequest) -> HttpResponse:
    if not _authorised(request):
        raise Http404
    # Phase 10: with PostgreSQL down the scrape was a 500, losing the broker and cache
    # gauges too. Only an outage reports the database down; any other failure (a statement
    # timeout, a table not migrated yet) drops just the gauge it broke and is logged.
    database_up = metrics.Gauge("arkray_db_up", "1 when the database answered this scrape.")
    from_database: list[metrics.Gauge] = []
    reachable = True
    collectors: tuple[Callable[[], list[metrics.Gauge]], ...] = (
        metrics.outbox,
        metrics.database,
        ai_metrics.gauges,
        attachment_metrics.gauges,
    )
    for collect in collectors:
        try:
            from_database.extend(collect())
        except DatabaseError as exc:
            if database_unavailable(exc):
                reachable, from_database = False, []
                break
            gauge = f"{collect.__module__}.{collect.__name__}"
            logger.warning(
                "metrics_gauge_failed", extra={"gauge": gauge, "exc_type": type(exc).__name__}
            )
    database_up.add(1 if reachable else 0)
    gauges = [database_up, *from_database, *metrics.broker(), *metrics.cache_up()]
    # Attachment storage: a degraded signal, like the cache (the CRM works without it).
    gauges += attachment_metrics.storage_up() + attachment_metrics.events()
    return HttpResponse(metrics.render(gauges), content_type="text/plain; version=0.0.4")
