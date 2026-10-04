"""Celery entry points for Ask Arkray. Thin: logic lives in ai.service and ai.indexing.

`ai.answer_question` runs on queue "ai" (its own workers: a slow provider never occupies
web workers or CRM background work). No Celery retries: a question is claimed once and
either answered or failed; a crashed worker's question expires (AI_QUESTION_TIMEOUT_S).
"""

from __future__ import annotations

from uuid import UUID

from celery import shared_task
from django.conf import settings

from arkray.core.context import update_context

from . import indexing, service


@shared_task(
    name="ai.answer_question",
    ignore_result=True,
    soft_time_limit=int(settings.AI_QUESTION_BUDGET_S) + 30,
    time_limit=int(settings.AI_QUESTION_BUDGET_S) + 45,
)
def answer_question(question_id: str, correlation_id: str = "") -> None:
    if correlation_id:
        update_context(correlation_id=correlation_id)  # the asking request's id
    service.answer(UUID(question_id))


@shared_task(name="ai.housekeeping", ignore_result=True)
def housekeeping() -> dict[str, int]:
    """Hourly; safe to run late or twice."""
    return service.housekeeping()


@shared_task(name="ai.reconcile_index", ignore_result=True)
def reconcile_index() -> None:
    """Nightly: walk every source in bounded batches (outbox events) and re-index drift."""
    indexing.start_reconciliation()
