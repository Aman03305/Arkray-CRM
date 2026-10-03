"""Invitation flow: admin creates a user -> outbox -> one-time link -> user sets password."""

import smtplib
from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.core.mail import EmailMessage
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.models import OutboxEvent, OutboxStatus
from arkray.identity import services, tokens
from arkray.identity.models import AccountToken, TokenPurpose, TokenStatus, User, UserStatus
from arkray.identity.services import (
    AUDIT_INVITATION_CREATED,
    AUDIT_USER_ACTIVATED,
    AUDIT_USER_CREATED,
    TOPIC_DELIVER_ACCOUNT_TOKEN,
)
from tests.helpers import drain_outbox, last_secret, without_request_id

pytestmark = pytest.mark.django_db

VERIFY = "/api/v1/auth/invitations/verify"
ACCEPT = "/api/v1/auth/invitations/accept"
PASSWORD = "a-long-and-unusual-passphrase"


@pytest.fixture
def invite(admin):
    def create(
        email="new.hire@example.test", first_name="Neha", last_name="Verma", role="sales_user"
    ):
        return services.create_user(
            actor_id=admin.pk, email=email, first_name=first_name, last_name=last_name, role=role
        )

    return create


def accept(client, secret, password=PASSWORD):
    return client.post(ACCEPT, {"token": secret, "password": password}, format="json")


def invitation_of(user):
    return AccountToken.objects.get(
        user=user, purpose=TokenPurpose.INVITATION, status=TokenStatus.PENDING
    )


def backdate_latest_invitation(user, seconds=settings.INVITATION_RESEND_COOLDOWN_S + 1):
    token = AccountToken.objects.filter(user=user).latest("created_at")
    AccountToken.objects.filter(pk=token.pk).update(
        created_at=token.created_at - timedelta(seconds=seconds)
    )


class TestCreation:
    def test_creates_an_invited_user_without_a_password(self, invite):
        user = invite()
        stored = User.objects.get(pk=user.pk)
        assert (stored.status, stored.is_active) == (UserStatus.INVITED, False)
        assert not stored.has_usable_password()

    def test_queues_the_invitation_without_minting_a_secret_in_the_request(self, invite):
        user = invite()
        token = invitation_of(user)
        assert token.token_hash is None  # minted by the email job, never by the web request
        assert token.expires_at - token.created_at == timedelta(
            seconds=settings.ACCOUNT_INVITATION_TTL_S
        )
        event = OutboxEvent.objects.get(topic=TOPIC_DELIVER_ACCOUNT_TOKEN)
        assert event.payload == {"token_id": str(token.pk)}
        assert event.queue == "email"
        assert mail.outbox == []  # nothing is sent inside the request

    def test_is_audited(self, invite, admin):
        user = invite()
        actions = set(
            AuditEvent.objects.filter(target_id=str(user.pk)).values_list("action", "actor_id")
        )
        assert actions == {(AUDIT_USER_CREATED, admin.pk), (AUDIT_INVITATION_CREATED, admin.pk)}


class TestDelivery:
    def test_emails_a_one_time_link_and_stores_only_its_digest(self, invite):
        user = invite()
        drain_outbox()
        (message,) = mail.outbox
        assert message.to == [user.email]
        assert message.subject == "You're invited to Arkray CRM"
        assert f"{settings.APP_BASE_URL}/activate/" in message.body
        assert "Anita Admin has invited you" in message.body
        assert "IST" in message.body  # expiry shown in the business time zone

        secret = last_secret("activate")
        token = invitation_of(user)
        assert token.token_hash == tokens.digest(secret)
        assert token.sent_at is not None
        stored = str(list(AccountToken.objects.values())) + str(
            list(OutboxEvent.objects.values("payload", "last_error"))
        )
        assert secret not in stored

    def test_mail_outage_does_not_affect_user_creation_and_the_outbox_retries(
        self, invite, monkeypatch, api_client
    ):
        def smtp_down(self, fail_silently=False):
            raise smtplib.SMTPConnectError(421, "mail server unavailable")

        monkeypatch.setattr(EmailMessage, "send", smtp_down)
        user = invite()
        drain_outbox()

        event = OutboxEvent.objects.get(topic=TOPIC_DELIVER_ACCOUNT_TOKEN)
        assert (event.status, event.attempts) == (OutboxStatus.PENDING, 1)
        assert event.available_at > timezone.now()  # backing off: the single retry layer
        assert "SMTPConnectError" in event.last_error
        assert User.objects.filter(pk=user.pk, status=UserStatus.INVITED).exists()
        assert invitation_of(user).sent_at is None

        monkeypatch.undo()
        OutboxEvent.objects.filter(pk=event.pk).update(available_at=timezone.now())
        drain_outbox()
        assert OutboxEvent.objects.get(pk=event.pk).status == OutboxStatus.DONE
        assert accept(api_client, last_secret("activate")).status_code == 200

    def test_redelivery_is_idempotent(self, invite):
        user = invite()
        drain_outbox()
        token = invitation_of(user)
        services.deliver_account_token(token.pk)  # e.g. a duplicate broker message
        assert len(mail.outbox) == 1
        assert invitation_of(user).token_hash == token.token_hash

    def test_nothing_is_sent_for_a_user_deactivated_before_delivery(self, invite, admin):
        user = invite()
        services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
        drain_outbox()
        assert mail.outbox == []


class TestActivation:
    def test_verify_shows_who_the_link_is_for(self, invite, api_client):
        user = invite()
        drain_outbox()
        response = api_client.post(VERIFY, {"token": last_secret("activate")}, format="json")
        assert response.json() == {"email": user.email, "first_name": "Neha"}

    def test_accepting_sets_the_password_and_activates(self, invite, api_client):
        user = invite()
        drain_outbox()
        response = accept(api_client, last_secret("activate"))
        assert response.status_code == 200
        assert response.json() == {"email": user.email, "first_name": "Neha"}

        user.refresh_from_db()
        assert (user.status, user.is_active) == (UserStatus.ACTIVE, True)
        assert user.activated_at is not None
        assert user.check_password(PASSWORD)
        assert AccountToken.objects.get(user=user).status == TokenStatus.USED
        event = AuditEvent.objects.get(action=AUDIT_USER_ACTIVATED)
        assert event.actor_id == user.pk

        login = api_client.post(
            "/api/v1/auth/login", {"email": user.email, "password": PASSWORD}, format="json"
        )
        assert login.status_code == 200

    def test_a_link_works_only_once(self, invite, api_client):
        invite()
        drain_outbox()
        secret = last_secret("activate")
        assert accept(api_client, secret).status_code == 200
        replay = accept(api_client, secret, "another-long-passphrase-9")
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "invalid_token"
        assert api_client.post(VERIFY, {"token": secret}, format="json").status_code == 400

    def test_an_expired_link_is_refused(self, invite, api_client):
        user = invite()
        drain_outbox()
        past = timezone.now() - timedelta(minutes=1)
        AccountToken.objects.filter(user=user).update(
            created_at=past - timedelta(hours=72), expires_at=past
        )
        assert accept(api_client, last_secret("activate")).status_code == 400

    def test_resending_supersedes_the_previous_link(self, invite, admin, api_client):
        user = invite()
        drain_outbox()
        first = last_secret("activate")
        backdate_latest_invitation(user)
        services.resend_invitation(actor_id=admin.pk, user_id=user.pk)
        drain_outbox()
        second = last_secret("activate")

        assert first != second
        assert accept(api_client, first).status_code == 400
        assert accept(api_client, second).status_code == 200

    def test_deactivating_an_invited_user_voids_the_link(self, invite, admin, api_client):
        user = invite()
        drain_outbox()
        services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
        assert accept(api_client, last_secret("activate")).status_code == 400

    def test_a_weak_password_is_refused_and_the_link_stays_usable(self, invite, api_client):
        invite(email="neha.verma@example.test")
        drain_outbox()
        secret = last_secret("activate")
        response = accept(api_client, secret, "neha.verma")
        assert response.status_code == 400
        assert response.json()["error"]["details"]["password"]
        assert accept(api_client, secret).status_code == 200

    @pytest.mark.parametrize(
        "secret",
        ["", "short", "x" * 43, tokens.generate_secret(), "'; DROP TABLE users; --" + "a" * 20],
    )
    def test_unknown_or_malformed_links_get_one_uniform_answer(self, api_client, secret):
        response = accept(api_client, secret)
        assert response.status_code == 400
        if secret:
            assert without_request_id(response) == {
                "error": {
                    "code": "invalid_token",
                    "message": "This link is invalid or has expired. Ask for a new one.",
                    "details": None,
                }
            }

    def test_a_password_reset_link_cannot_activate_an_account(self, invite, user_a, api_client):
        invite()
        services.request_password_reset(user_a.email, ip="127.0.0.1")
        drain_outbox()
        assert accept(api_client, last_secret("reset-password")).status_code == 400


class TestResend:
    def test_only_for_invited_users(self, admin, user_a, admin_client):
        response = admin_client.post(f"/api/v1/admin/users/{user_a.pk}/resend-invitation")
        assert response.status_code == 422

    def test_is_rate_limited_per_user(self, invite, admin_client):
        user = invite()
        response = admin_client.post(f"/api/v1/admin/users/{user.pk}/resend-invitation")
        assert response.status_code == 429
        assert int(response["Retry-After"]) <= settings.INVITATION_RESEND_COOLDOWN_S + 1

    def test_revokes_the_old_invitation_and_queues_a_new_one(self, invite, admin_client):
        user = invite()
        old = invitation_of(user)
        backdate_latest_invitation(user)
        response = admin_client.post(f"/api/v1/admin/users/{user.pk}/resend-invitation")
        assert response.status_code == 200
        assert AccountToken.objects.get(pk=old.pk).status == TokenStatus.REVOKED
        assert invitation_of(user).pk != old.pk
        assert response.json()["invitation"]["sent_at"] is None
