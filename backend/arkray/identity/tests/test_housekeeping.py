from datetime import timedelta

import pytest
from celery.schedules import crontab
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone

from arkray.identity.models import AuthThrottleEvent, ThrottleKind
from arkray.identity.tasks import housekeeping

pytestmark = pytest.mark.django_db


def test_purges_old_throttle_evidence_and_expired_sessions():
    old = timezone.now() - timedelta(seconds=settings.AUTH_THROTTLE_RETENTION_S + 60)
    AuthThrottleEvent.objects.create(kind=ThrottleKind.LOGIN_FAILURE, occurred_at=old)
    AuthThrottleEvent.objects.create(kind=ThrottleKind.LOGIN_FAILURE)
    live, expired = SessionStore(), SessionStore()
    live.create()
    expired.set_expiry(-1)
    expired.create()

    assert housekeeping() == {
        "throttle_events": 1,
        "sessions": 1,
        "redacted": 0,
        "access_windows": 0,
        "support_sessions_expired": 0,
    }
    assert AuthThrottleEvent.objects.count() == 1
    assert list(Session.objects.values_list("session_key", flat=True)) == [live.session_key]


def test_is_scheduled_hourly():
    entry = settings.CELERY_BEAT_SCHEDULE["identity-housekeeping"]
    assert entry["task"] == "identity.housekeeping"
    assert entry["schedule"] == crontab(minute=15)  # hourly, at a wall-clock time
