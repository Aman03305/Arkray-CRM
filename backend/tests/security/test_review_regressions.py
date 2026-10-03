"""Regression tests for the defects found by the Phase 1 adversarial review.

Each test reproduces a confirmed finding and fails without its fix.
"""

from __future__ import annotations

import base64
import logging
from datetime import timedelta

import pytest
from django.conf import settings
from django.test import RequestFactory, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core.context import get_context
from arkray.core.middleware import client_ip
from arkray.core.models import OutboxEvent
from arkray.identity import services, throttling
from arkray.identity.models import (
    AccountToken,
    AuthThrottleEvent,
    Role,
    ThrottleKind,
    TokenPurpose,
    TokenStatus,
    User,
    UserStatus,
)
from arkray.identity.tasks import housekeeping
from arkray.identity.throttling import login_identifier
from tests.factories import DEFAULT_PASSWORD, AdminFactory, UserFactory
from tests.helpers import drain_outbox, last_secret, signed_in

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login"


def login(client, email, password=DEFAULT_PASSWORD, **extra):
    return client.post(LOGIN, {"email": email, "password": password}, format="json", **extra)


def seed_source_failures(ip, count=None):
    AuthThrottleEvent.objects.bulk_create(
        AuthThrottleEvent(
            kind=ThrottleKind.LOGIN_FAILURE,
            identifier_hash=login_identifier(f"sprayed{i}@example.test"),
            ip_address=ip,
        )
        for i in range(count or settings.LOGIN_SOURCE_FAILURE_THRESHOLD)
    )


class TestSourceLimits:
    def test_a_trusted_browser_is_exempt_from_a_blocked_source(self, user_a):
        """Review F1 (P2): a neighbour behind the same NAT could lock the owner out."""
        owner = APIClient()
        assert login(owner, user_a.email).status_code == 200
        owner.post("/api/v1/auth/logout")
        seed_source_failures("127.0.0.1")

        assert login(owner, user_a.email).status_code == 200  # trusted browser
        assert login(APIClient(), user_a.email).status_code == 429  # unrecognised browser

    def test_an_ipv6_source_is_its_64(self, user_a):
        """Review F5: rotating addresses inside one /64 must not reset the budget."""
        seed_source_failures(throttling.source_key("2001:db8:1:2::1"))
        response = login(APIClient(REMOTE_ADDR="2001:db8:1:2::9999"), user_a.email)
        assert response.status_code == 429
        assert login(APIClient(REMOTE_ADDR="2001:db8:1:3::1"), user_a.email).status_code == 200

    def test_an_unknown_source_shares_one_budget(self, user_a):
        """Review F6: an unparseable address used to disable source limits entirely."""
        seed_source_failures(None)
        with override_settings(TRUSTED_PROXY_COUNT=1):
            response = login(APIClient(), user_a.email, HTTP_X_FORWARDED_FOR="unknown")
        assert response.status_code == 429

    @pytest.mark.parametrize(
        ("forwarded", "expected"),
        [
            ("198.51.100.7:51234", "198.51.100.7"),
            ("[2001:db8::7]:443", "2001:db8::7"),
            ("2001:db8::7", "2001:db8::7"),
            ("unknown", None),
            ("[2001:db8::7", None),
        ],
    )
    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_forwarded_addresses_with_ports_are_parsed(self, forwarded, expected):
        request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR=forwarded)
        assert client_ip(request) == expected


class TestKeyRotation:
    def test_rotating_the_secret_key_keeps_lockouts_and_trusted_browsers(self, user_a):
        """Review F7: identifiers and device cookies made with a fallback key still count."""
        owner = APIClient()
        assert login(owner, user_a.email).status_code == 200
        owner.post("/api/v1/auth/logout")
        attacker = APIClient()
        for _ in range(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD):
            login(attacker, user_a.email, "wrong-password-1")

        rotated = {
            "SECRET_KEY": "rotated-" + "k" * 60,
            "SECRET_KEY_FALLBACKS": [settings.SECRET_KEY],
        }
        with override_settings(**rotated):
            assert login(attacker, user_a.email).status_code == 429  # lockout survives
            assert login(owner, user_a.email).status_code == 200  # still a trusted browser


class TestResetLinks:
    def test_changing_the_password_voids_outstanding_reset_links(self, user_a):
        """Review F2: a leaked reset link used to survive the owner's password change."""
        services.request_password_reset(user_a.email, ip=None)
        drain_outbox()
        leaked = last_secret("reset-password")

        client = signed_in(user_a)
        response = client.post(
            "/api/v1/auth/password/change",
            {"current_password": DEFAULT_PASSWORD, "new_password": "a-new-safe-passphrase-1"},
            format="json",
        )
        assert response.status_code == 204
        confirm = APIClient().post(
            "/api/v1/auth/password-reset/confirm",
            {"token": leaked, "new_password": "attacker-chosen-password-2"},
            format="json",
        )
        assert confirm.status_code == 400
        user_a.refresh_from_db()
        assert user_a.check_password("a-new-safe-passphrase-1")

    def test_the_reset_audit_records_where_the_request_came_from(self, user_a):
        """Review (admin) #7: reset forensics lacked the requester's address."""
        APIClient(REMOTE_ADDR="203.0.113.9").post(
            "/api/v1/auth/password-reset", {"email": user_a.email}, format="json"
        )
        drain_outbox()
        event = AuditEvent.objects.get(action=services.AUDIT_PASSWORD_RESET_ISSUED)
        assert event.metadata == {"requested_from": "203.0.113.9"}

    def test_suppressed_resets_are_audited(self, user_a):
        for _ in range(settings.PASSWORD_RESET_ACCOUNT_LIMIT_PER_HOUR + 1):
            services.request_password_reset(user_a.email, ip="203.0.113.9")
            drain_outbox()
        assert (
            AuditEvent.objects.filter(action=services.AUDIT_PASSWORD_RESET_SUPPRESSED).count() == 1
        )


class TestInvitationsOfRemovedManagers:
    @pytest.mark.parametrize("removal", ["deactivate", "demote"])
    def test_a_removed_admins_pending_invitations_die_with_their_access(self, admin, removal):
        """Review (admin) #2 (P2): a rogue admin could leave a backdoor admin invitation."""
        rogue = AdminFactory()
        backdoor = services.create_user(
            actor_id=rogue.pk,
            email="backdoor@evil.test",
            first_name="B",
            last_name="",
            role="admin",
        )
        drain_outbox()
        link = last_secret("activate")

        if removal == "deactivate":
            services.deactivate_user(actor_id=admin.pk, user_id=rogue.pk)
        else:
            services.update_user(
                actor_id=admin.pk, user_id=rogue.pk, version=1, changes={"role": "sales_user"}
            )

        accept = APIClient().post(
            "/api/v1/auth/invitations/accept",
            {"token": link, "password": "backdoor-passphrase-123"},
            format="json",
        )
        assert accept.status_code == 400
        assert User.objects.get(pk=backdoor.pk).status == UserStatus.INVITED
        action = "user.deactivated" if removal == "deactivate" else "user.role_changed"
        assert AuditEvent.objects.get(action=action).metadata["revoked_invitations"] == 1


class TestInvitationAudit:
    def invite(self, admin):
        return services.create_user(
            actor_id=admin.pk,
            email="i@example.test",
            first_name="I",
            last_name="",
            role="sales_user",
        )

    def test_reinvitation_on_reactivation_is_audited(self, admin):
        """Review (admin) #7: two paths issued invitations without an audit event."""
        user = self.invite(admin)
        services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
        services.reactivate_user(actor_id=admin.pk, user_id=user.pk)
        reasons = list(
            AuditEvent.objects.filter(action=services.AUDIT_INVITATION_CREATED)
            .order_by("id")
            .values_list("metadata__reason", flat=True)
        )
        assert reasons == [None, "reactivated"]

    def test_reinvitation_on_email_change_is_audited(self, admin):
        user = self.invite(admin)
        services.change_user_email(
            actor_id=admin.pk, user_id=user.pk, version=1, email="j@example.test"
        )
        assert AuditEvent.objects.filter(
            action=services.AUDIT_INVITATION_CREATED, metadata__reason="email_changed"
        ).exists()
        changed = AuditEvent.objects.get(action=services.AUDIT_EMAIL_CHANGED)
        assert changed.metadata["revoked_links"] == 1

    def test_reactivating_to_invited_clears_the_activation_date(self, admin):
        """Review (admin): a deactivated user with an activation date but no usable password
        used to hit the lifecycle CHECK (a 500) when reactivated."""
        user = UserFactory(is_active=False)
        User.objects.filter(pk=user.pk).update(password="!unusable")
        reactivated = services.reactivate_user(actor_id=admin.pk, user_id=user.pk)
        assert (reactivated.status, reactivated.activated_at) == (UserStatus.INVITED, None)


class TestErrorHandling:
    @pytest.mark.parametrize("position", ["not-a-date", "2026-13-45", "9" * 60])
    def test_a_forged_cursor_is_a_404_not_a_500(self, admin_client, position):
        """Review (admin) #6: forged cursor positions raised an unhandled 500."""
        cursor = base64.b64encode(f"p={position}".encode()).decode()
        response = admin_client.get("/api/v1/admin/users", {"cursor": cursor})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_csrf_rejections_are_logged_with_a_reason(self, csrf_client, user_a, caplog):
        """Review F10: rejections left no trace for operators."""
        with caplog.at_level(logging.WARNING, logger="django.security.csrf"):
            login(csrf_client, user_a.email)
        (record,) = [r for r in caplog.records if r.getMessage() == "csrf_rejected"]
        assert "CSRF" in record.reason


class TestLogAttribution:
    def test_a_revoked_session_does_not_label_log_lines_with_its_user(self, admin, user_a):
        """Review F8: the raw session user id used to reach the log context."""
        from django.contrib.auth.middleware import AuthenticationMiddleware
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.http import HttpResponse

        from arkray.core.middleware import RequestContextMiddleware
        from arkray.identity.sessions import SessionPolicyMiddleware

        session_key = signed_in(user_a).session.session_key
        services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        seen = {}

        def view(request):
            seen["user_id"] = get_context().user_id
            return HttpResponse()

        chain = RequestContextMiddleware(
            SessionMiddleware(AuthenticationMiddleware(SessionPolicyMiddleware(view)))
        )
        request = RequestFactory().get("/api/v1/auth/me")
        request.COOKIES[settings.SESSION_COOKIE_NAME] = session_key
        chain(request)
        assert seen == {"user_id": None}


class TestPersonalDataRetention:
    def test_submitted_reset_emails_do_not_linger_in_the_outbox(self, db):
        """Review (admin) #8: submitted emails, even for non-accounts, were kept forever."""
        services.request_password_reset("not.a.customer@somewhere.test", ip=None)
        drain_outbox()
        OutboxEvent.objects.update(finished_at=timezone.now() - timedelta(days=2))
        assert housekeeping()["redacted"] == 1
        assert OutboxEvent.objects.get().payload == {}


class TestLastAdministrator:
    def test_an_invited_admin_is_not_counted_as_a_remaining_administrator(self, admin):
        invited_admin = services.create_user(
            actor_id=admin.pk, email="a2@example.test", first_name="A", last_name="", role="admin"
        )
        assert invited_admin.role == Role.ADMIN
        assert not services._another_manager_remains(admin.pk)


def test_revoked_tokens_record_when(admin):
    user = services.create_user(
        actor_id=admin.pk, email="t@example.test", first_name="T", last_name="", role="sales_user"
    )
    services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
    token = AccountToken.objects.get(user=user, purpose=TokenPurpose.INVITATION)
    assert (token.status, token.revoked_at is not None) == (TokenStatus.REVOKED, True)
