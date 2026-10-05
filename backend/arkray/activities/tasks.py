"""Periodic attachment housekeeping (Celery beat, hourly). Safe to run late or twice."""

from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from . import attachments

logger = logging.getLogger(__name__)


@shared_task(name="activities.housekeeping", ignore_result=True)
def housekeeping() -> dict[str, int]:
    result = attachments.housekeeping(timezone.now())
    if any(result.values()):
        logger.info("attachment_housekeeping", extra=result)
    return result
