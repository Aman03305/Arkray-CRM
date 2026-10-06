"""Periodic attachment work (Celery beat): the hourly housekeeping, and the daily read-only
reconciliation of rows against storage. Safe to run late or twice."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from . import attachments, reconcile

logger = logging.getLogger(__name__)

# The daily check stops after this long (outcome "errors": the metrics show it) rather than
# hold a CRM worker; a full pass lists 1,000 objects a call and reads 500 rows a query, so
# 300,000 files take a few minutes.
RECONCILE_MAX_SECONDS = 15 * 60


@shared_task(name="activities.housekeeping", ignore_result=True)
def housekeeping() -> dict[str, int]:
    result = attachments.housekeeping(timezone.now())
    if any(result.values()):
        logger.info("attachment_housekeeping", extra=result)
    return result


@shared_task(
    name="activities.reconcile_check",
    ignore_result=True,
    soft_time_limit=RECONCILE_MAX_SECONDS + 60,
    time_limit=RECONCILE_MAX_SECONDS + 120,
)
def reconcile_check() -> dict[str, int] | None:
    """`reconcile_attachments --check`, daily: never repairs (an operator runs --repair,
    docs/runbooks.md#attachment-reconciliation). Its outcome is published for the metrics
    endpoint. Off with ATTACHMENT_RECONCILE_DAILY=false, or without attachment storage."""
    if not settings.ATTACHMENT_RECONCILE_DAILY or "attachments" not in settings.STORAGES:
        logger.info("attachment_reconcile_skipped")
        return None
    report = reconcile.run(
        reconcile.Options(repair=False, max_seconds=RECONCILE_MAX_SECONDS, batch_size=1000)
    )
    return report.counts()
