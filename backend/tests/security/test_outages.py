"""Degradation under dependency outages (docs/reliability.md): the CRM keeps working and
authentication protections never switch off."""

from __future__ import annotations

import smtplib
import time

import pytest
from django.conf import settings
from django.core import mail
from django.core.mail import EmailMessage
from django.test import override_settings
from kombu.exceptions import OperationalError
from rest_framework.test import APIClient

from arkray.core import tasks
from arkray.core.models import OutboxEvent, OutboxStatus
from arkray.core.outbox import relay
from arkray.core.redis import cache_breaker, fail_fast_pool_kwargs
from arkray.identity.models import AuthThrottleEvent
from tests.factories import DEFAULT_PASSWORD
from tests.helpers import drain_outbox, last_secret, signed_in

pytestmark = pytest.mark.django_db

DEAD_REDIS_URL = "redis://127.0.0.1:1/0"  # nothing listens on port 1
DEAD_REDIS_CACHE = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": DEAD_REDIS_URL,
        "OPTIONS": {
            "SOCKET_CONNECT_TIMEOUT": 1,
            "SOCKET_TIMEOUT": 1,
            "IGNORE_EXCEPTIONS": True,
            "CONNECTION_POOL_KWARGS": fail_fast_pool_kwargs(DEAD_REDIS_URL),
        },
    }
}


def login(client, email, password=DEFAULT_PASSWORD):
    return client.post("/api/v1/auth/login", {"email": email, "password": password}, format="json")


@pytest.fixture
def redis_down():
    cache_breaker.reset()
    with override_settings(CACHES=DEAD_REDIS_CACHE):
        yield
    cache_breaker.reset()


@pytest.mark.usefixtures("redis_down")
class TestRedisCacheOutage:
    def test_sign_in_and_sessions_keep_working(self, user_a):
        client = APIClient()
        started = time.monotonic()
        assert login(client, user_a.email).status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 200
        assert time.monotonic() - started < 5  # fail-fast cache: no multi-second stalls

    def test_brute_force_protection_does_not_switch_off(self, user_a):
        """DRF's Redis-backed rate limits fail open; the PostgreSQL throttle does not."""
        client = APIClient()
        for _ in range(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD):
            assert login(client, user_a.email, "wrong-password-1").status_code == 400
        assert login(client, user_a.email).status_code == 429
        assert AuthThrottleEvent.objects.count() == settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD

    def test_user_administration_keeps_working(self, admin):
        response = signed_in(admin).post(
            "/api/v1/admin/users",
            {"first_name": "N", "email": "n@example.test", "role": "sales_user"},
            format="json",
        )
        assert response.status_code == 201


class TestBrokerOutage:
    def test_user_creation_succeeds_and_the_invitation_waits_durably(self, admin, monkeypatch):
        def broker_down(*args, **kwargs):
            raise OperationalError("broker unavailable")

        monkeypatch.setattr(tasks.process_outbox_event, "apply_async", broker_down)
        response = signed_in(admin).post(
            "/api/v1/admin/users",
            {"first_name": "N", "email": "n@example.test", "role": "sales_user"},
            format="json",
        )
        assert response.status_code == 201
        assert relay() == 0  # dispatch failed: the event is released, not lost
        assert OutboxEvent.objects.get().status == OutboxStatus.PENDING

        monkeypatch.undo()
        drain_outbox()
        assert mail.outbox[0].to == ["n@example.test"]

    def test_password_reset_requests_are_accepted_while_workers_are_down(self, user_a):
        response = APIClient().post(
            "/api/v1/auth/password-reset", {"email": user_a.email}, format="json"
        )
        assert response.status_code == 202
        assert OutboxEvent.objects.filter(status=OutboxStatus.PENDING).count() == 1


class TestMailOutage:
    def test_reset_requests_succeed_and_delivery_retries_later(self, user_a, monkeypatch):
        def smtp_down(self, fail_silently=False):
            raise smtplib.SMTPServerDisconnected("mail server unavailable")

        monkeypatch.setattr(EmailMessage, "send", smtp_down)
        client = APIClient()
        assert (
            client.post(
                "/api/v1/auth/password-reset", {"email": user_a.email}, format="json"
            ).status_code
            == 202
        )
        drain_outbox()
        delivery = OutboxEvent.objects.get(topic="identity.deliver_account_token")
        assert (delivery.status, delivery.attempts) == (OutboxStatus.PENDING, 1)

        monkeypatch.undo()
        OutboxEvent.objects.filter(pk=delivery.pk).update(available_at=delivery.created_at)
        drain_outbox()
        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            {"token": last_secret("reset-password"), "new_password": "after-the-outage-42"},
            format="json",
        )
        assert response.status_code == 204


class TestActivitiesKeepWorking:
    """Activities depend on PostgreSQL only: no cache, broker, worker or provider is on the
    path of a create, a completion, a list or a timeline, and nothing is queued."""

    def journey(self, owner):
        from datetime import timedelta

        from django.utils import timezone

        from tests.factories import LeadFactory, MeetingFactory

        client = signed_in(owner)
        lead = LeadFactory(owner=owner)
        me = "/api/v1/workspaces/me"
        created = client.post(
            f"{me}/activities", {"type": "task", "lead": str(lead.pk), "title": "x"}, format="json"
        )
        assert created.status_code == 201
        start = timezone.now() - timedelta(hours=2)
        meeting = MeetingFactory(lead=lead, starts_at=start, ends_at=start + timedelta(hours=1))
        assert (
            client.post(
                f"{me}/activities/{meeting.pk}/complete", {"version": 1}, format="json"
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{me}/activities",
                {"type": "note", "lead": str(lead.pk), "description": "y"},
                format="json",
            ).status_code
            == 201
        )
        assert client.get(f"{me}/activities").status_code == 200
        assert client.get(f"{me}/activity-summary").status_code == 200
        assert len(client.get(f"{me}/leads/{lead.pk}/timeline").json()["results"]) == 3
        assert not OutboxEvent.objects.exists()

    @pytest.mark.usefixtures("redis_down")
    def test_during_a_redis_outage(self, user_a):
        started = time.monotonic()
        self.journey(user_a)
        assert time.monotonic() - started < 10  # fail-fast cache: no multi-second stalls

    def test_during_a_broker_outage(self, user_a, monkeypatch):
        def broker_down(*args, **kwargs):
            raise OperationalError("broker unavailable")

        monkeypatch.setattr(tasks.process_outbox_event, "apply_async", broker_down)
        self.journey(user_a)


class TestDashboardKeepsWorking:
    """The dashboard reads PostgreSQL only (no analytics cache, no worker, no AI): with Redis
    or the broker down it still answers, and an administrator's delegated viewing is then
    audited on every request (the audit window lives in the cache; fail towards more
    auditing)."""

    def journey(self, owner, admin):
        from arkray.audit.models import AuditEvent
        from tests.factories import LeadFactory, OpportunityFactory, TaskFactory

        lead = LeadFactory(owner=owner)
        OpportunityFactory(lead=lead, stage_key="proposal")
        TaskFactory(lead=lead)
        mine = signed_in(owner).get("/api/v1/workspaces/me/dashboard")
        assert mine.status_code == 200
        assert mine.json()["pipeline"]["pipeline_value"] == "100000.00"
        assert mine.json()["activities"]["open_tasks"] == 1
        admin_client = signed_in(admin)
        for _ in range(2):
            for workspace in (str(owner.pk), "all"):
                response = admin_client.get(f"/api/v1/workspaces/{workspace}/dashboard")
                assert response.status_code == 200
                assert response.json()["leads"]["total"] == 1
        assert not OutboxEvent.objects.exists()
        return AuditEvent.objects.filter(action="workspace.accessed").count()

    @pytest.mark.usefixtures("redis_down")
    def test_during_a_redis_outage(self, user_a, admin):
        started = time.monotonic()
        assert self.journey(user_a, admin) == 4  # every delegated view audited
        assert time.monotonic() - started < 10  # fail-fast cache: no multi-second stalls

    def test_during_a_broker_outage(self, user_a, admin, monkeypatch):
        def broker_down(*args, **kwargs):
            raise OperationalError("broker unavailable")

        monkeypatch.setattr(tasks.process_outbox_event, "apply_async", broker_down)
        self.journey(user_a, admin)
