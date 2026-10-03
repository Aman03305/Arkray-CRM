"""Password reset: enumeration-safe request, one-time expiring link, sessions ended."""

from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core.models import OutboxEvent
from arkray.identity.api.views import RESET_REQUEST_ACCEPTED
from arkray.identity.models import (
    AccountToken,
    AuthThrottleEvent,
    ThrottleKind,
    TokenPurpose,
    TokenStatus,
)
from arkray.identity.services import (
    AUDIT_PASSWORD_RESET_COMPLETED,
    TOPIC_PASSWORD_RESET_REQUESTED,
)
from tests.factories import DEFAULT_PASSWORD, InvitedUserFactory, UserFactory
from tests.helpers import drain_outbox, last_secret, signed_in, without_request_id

pytestmark = pytest.mark.django_db

REQUEST = "/api/v1/auth/password-reset"
CONFIRM = "/api/v1/auth/password-reset/confirm"
NEW_PASSWORD = "freshly-chosen-passphrase-7"


def request_reset(client, email):
    return client.post(REQUEST, {"email": email}, format="json")


def confirm(client, secret, new_password=NEW_PASSWORD):
    return client.post(CONFIRM, {"token": secret, "new_password": new_password}, format="json")


def sign_in(client, email, password):
    return client.post("/api/v1/auth/login", {"email": email, "password": password}, format="json")


class TestRequest:
    def test_the_response_is_identical_whatever_the_account_state(self, db):
        UserFactory(email="active@example.test")
        InvitedUserFactory(email="invited@example.test")
        UserFactory(email="gone@example.test", is_active=False)
        emails = ["active@example.test", "invited@example.test", "gone@example.test", "no@x.test"]

        responses = {email: request_reset(APIClient(), email) for email in emails}

        bodies = {email: (r.status_code, without_request_id(r)) for email, r in responses.items()}
        expected = (202, {"detail": RESET_REQUEST_ACCEPTED})
        assert bodies == dict.fromkeys(emails, expected)
        # Constant work per request: one queued job each, whether or not anyone matches.
        assert OutboxEvent.objects.filter(topic=TOPIC_PASSWORD_RESET_REQUESTED).count() == 4

    def test_only_an_active_account_receives_an_email(self, db):
        active = UserFactory(email="active@example.test")
        InvitedUserFactory(email="invited@example.test")
        UserFactory(email="gone@example.test", is_active=False)
        for email in (
            "active@example.test",
            "invited@example.test",
            "gone@example.test",
            "no@x.test",
        ):
            request_reset(APIClient(), email)
        drain_outbox()
        assert [m.to for m in mail.outbox] == [[active.email]]
        assert "Reset your Arkray CRM password" in mail.outbox[0].subject

    def test_the_email_is_matched_case_insensitively(self, api_client, user_a):
        request_reset(api_client, user_a.email.upper())
        drain_outbox()
        assert len(mail.outbox) == 1

    def test_at_most_three_emails_per_account_per_hour(self, api_client, user_a):
        for _ in range(settings.PASSWORD_RESET_ACCOUNT_LIMIT_PER_HOUR + 2):
            assert request_reset(api_client, user_a.email).status_code == 202
            drain_outbox()
        assert len(mail.outbox) == settings.PASSWORD_RESET_ACCOUNT_LIMIT_PER_HOUR

    def test_a_burst_of_requests_mails_only_the_newest_link(self, api_client, user_a):
        for _ in range(3):
            request_reset(api_client, user_a.email)
        drain_outbox()
        assert len(mail.outbox) == 1  # superseded links are never sent
        assert confirm(api_client, last_secret("reset-password")).status_code == 204

    def test_requests_per_source_are_limited_durably(self, api_client, user_a):
        AuthThrottleEvent.objects.bulk_create(
            AuthThrottleEvent(kind=ThrottleKind.PASSWORD_RESET_REQUEST, ip_address="127.0.0.1")
            for _ in range(settings.PASSWORD_RESET_SOURCE_LIMIT_PER_HOUR)
        )
        response = request_reset(api_client, user_a.email)
        assert response.status_code == 429
        assert int(response["Retry-After"]) > 0

    def test_requires_csrf(self, csrf_client, user_a):
        assert request_reset(csrf_client, user_a.email).status_code == 403


class TestConfirm:
    @pytest.fixture
    def secret(self, api_client, user_a):
        request_reset(api_client, user_a.email)
        drain_outbox()
        return last_secret("reset-password")

    def test_sets_the_new_password(self, api_client, user_a, secret):
        assert confirm(api_client, secret).status_code == 204
        assert sign_in(APIClient(), user_a.email, NEW_PASSWORD).status_code == 200
        assert sign_in(APIClient(), user_a.email, DEFAULT_PASSWORD).status_code == 400
        token = AccountToken.objects.get(user=user_a, purpose=TokenPurpose.PASSWORD_RESET)
        assert token.status == TokenStatus.USED
        event = AuditEvent.objects.get(action=AUDIT_PASSWORD_RESET_COMPLETED)
        assert event.actor_id == user_a.pk
        assert NEW_PASSWORD not in str(event.metadata)

    def test_ends_every_existing_session(self, api_client, user_a, secret):
        existing = signed_in(user_a)
        confirm(api_client, secret)
        assert existing.get("/api/v1/auth/me").status_code == 401

    def test_does_not_sign_the_user_in(self, api_client, secret):
        response = confirm(api_client, secret)
        assert settings.SESSION_COOKIE_NAME not in response.cookies
        assert api_client.get("/api/v1/auth/me").status_code == 401

    def test_a_link_works_only_once(self, api_client, secret):
        assert confirm(api_client, secret).status_code == 204
        replay = confirm(api_client, secret, "yet-another-passphrase-8")
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "invalid_token"

    def test_an_expired_link_is_refused(self, api_client, user_a, secret):
        past = timezone.now() - timedelta(seconds=1)
        AccountToken.objects.filter(user=user_a).update(
            created_at=past - timedelta(hours=1), expires_at=past
        )
        assert confirm(api_client, secret).status_code == 400

    def test_a_newer_request_supersedes_the_link(self, api_client, user_a, secret):
        request_reset(api_client, user_a.email)
        drain_outbox()
        assert confirm(api_client, secret).status_code == 400
        assert confirm(api_client, last_secret("reset-password")).status_code == 204

    def test_a_deactivated_user_cannot_use_an_earlier_link(self, admin, api_client, user_a, secret):
        from arkray.identity import services

        services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        assert confirm(api_client, secret).status_code == 400

    def test_a_weak_password_is_refused_with_reasons(self, api_client, secret):
        response = confirm(api_client, secret, "123456789012")
        assert response.status_code == 400
        assert response.json()["error"]["details"]["new_password"]

    def test_a_reset_lifts_a_sign_in_lockout(self, api_client, user_a, secret):
        for _ in range(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD):
            sign_in(APIClient(), user_a.email, "wrong-password-1")
        confirm(api_client, secret)
        assert sign_in(APIClient(), user_a.email, NEW_PASSWORD).status_code == 200

    def test_an_invitation_link_cannot_reset_a_password(self, admin, api_client, user_a):
        from arkray.identity import services

        services.create_user(
            actor_id=admin.pk,
            email="x@example.test",
            first_name="X",
            last_name="",
            role="sales_user",
        )
        drain_outbox()
        assert confirm(api_client, last_secret("activate")).status_code == 400
