"""Periodic identity housekeeping (Celery beat, hourly). Safe to run late or twice."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.sessions.models import Session
from django.utils import timezone

from arkray.core import outbox

from . import services, throttling

# Outbox topics whose payload holds personal data (submitted or previous email addresses).
PERSONAL_DATA_TOPICS = (services.TOPIC_PASSWORD_RESET_REQUESTED, services.TOPIC_EMAIL_CHANGED)

logger = logging.getLogger(__name__)


@shared_task(name="identity.housekeeping", ignore_result=True)
def housekeeping() -> dict[str, int]:
    now = timezone.now()
    purged_events = throttling.purge_expired(now)
    purged_sessions, _ = Session.objects.filter(expire_date__lt=now).delete()
    retention = now - timedelta(seconds=settings.AUTH_THROTTLE_RETENTION_S)
    redacted = sum(
        outbox.redact_finished_payloads(topic, finished_before=retention)
        for topic in PERSONAL_DATA_TOPICS
    )
    result = {"throttle_events": purged_events, "sessions": purged_sessions, "redacted": redacted}
    if any(result.values()):
        logger.info("identity_housekeeping", extra=result)
    return result
