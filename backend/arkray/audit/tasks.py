"""Audit housekeeping (Celery beat, hourly): expire the personal details of audit events
past AUDIT_DETAIL_RETENTION_DAYS (audit.retention). Safe to run late or twice."""

from __future__ import annotations

import logging

from celery import shared_task

from . import retention

logger = logging.getLogger(__name__)


@shared_task(name="audit.housekeeping", ignore_result=True)
def housekeeping() -> dict[str, int]:
    result = retention.expire_details()
    if any(result.values()):
        logger.info("audit_housekeeping", extra=result)
    return result
