"""Celery entry points for the outbox. Deliberately thin: all logic lives in `outbox`.

No Celery-level retries here — the outbox is the single retry layer.
"""

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from . import idempotency, outbox


@shared_task(name="core.outbox.relay", ignore_result=True)
def relay_outbox() -> int:
    return outbox.relay()


@shared_task(name="core.outbox.process", ignore_result=True)
def process_outbox_event(event_id: int, claim_token: str) -> None:
    outbox.process_event(event_id, claim_token)


@shared_task(name="core.housekeeping", ignore_result=True)
def housekeeping() -> dict[str, int]:
    """Hourly; safe to run late or twice."""
    retention = timezone.now() - timedelta(days=settings.OUTBOX_DONE_RETENTION_DAYS)
    return {
        "idempotency_records": idempotency.purge_expired(),
        "outbox_events": outbox.purge_done(finished_before=retention),
    }
