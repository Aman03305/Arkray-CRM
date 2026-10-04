"""Ask Arkray's gauges for the metrics endpoint (Phase 10; docs/observability.md#metrics):
questions by outcome in the last hour, pending questions, the provider breaker. Labels are
statuses and error codes only. The index backlog is the outbox gauge of queue `ai_index`;
model latency and usage are in the `ai_model_call` log lines."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from arkray.core import metrics
from arkray.core.metrics import Gauge

from . import breaker
from .models import Question, QuestionStatus

logger = logging.getLogger(__name__)


def gauges() -> list[Gauge]:
    since = timezone.now() - timedelta(hours=1)
    recent = Gauge(
        "arkray_ai_questions_last_hour",
        "Ask Arkray questions finished in the last hour, by status, mode and error code.",
    )
    rows = (
        Question.objects.filter(finished_at__gte=since)
        .values("status", "mode", "error_code")
        .annotate(n=Count("id"))
        .order_by()
    )
    for row in rows:
        recent.add(
            row["n"], status=row["status"], mode=row["mode"] or "", error=row["error_code"] or ""
        )
    pending = Gauge("arkray_ai_questions_pending", "Ask Arkray questions waiting for a worker.")
    # Not yet expired: an overdue one is only marked failed when polled or by housekeeping.
    due = timezone.now() - timedelta(seconds=settings.AI_QUESTION_TIMEOUT_S)
    pending.add(Question.objects.filter(status=QuestionStatus.PENDING, created_at__gte=due).count())
    open_ = Gauge("arkray_ai_breaker_open", "1 while the model provider's circuit breaker is open.")
    # The breaker's state is in the cache: read under the probes' deadline, and left out
    # while the cache can't answer (Phase 10 review: it stalled scrapes in a Redis outage).
    try:
        open_.add(1 if metrics.bounded(breaker.is_open) else 0)
    except Exception:  # noqa: BLE001 — unknown, not closed
        logger.warning("metrics_ai_breaker_unknown")
    return [recent, pending, open_]
