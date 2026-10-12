"""R126 (ADR-0033): an administrator reactivated by another administrator comes back without
the password they were deactivated with. And ArkrayAdministratorDeactivated's gauge."""

from __future__ import annotations

import pytest
from django.core import mail
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.identity import metrics, services
from arkray.identity.models import AccountToken, MfaDevice, TokenPurpose, TokenStatus
from tests.factories import DEFAULT_PASSWORD, AdminFactory, UserFactory
from tests.helpers import drain_outbox, last_secret, signed_in
from tests.mfa_helpers import enrol, password_step

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "A-Fresh-Owner-Chosen-Phrase-31"


def url(user, action):
    return f"/api/v1/admin/users/{user.pk}/{action}"


def sign_in(user, password=DEFAULT_PASSWORD):
    return password_step(APIClient(), user, password)


class TestAnotherAdministratorReactivated:
    @pytest.fixture
    def returned(self, admin):
        """Admin B, signed in somewhere with a pending reset link, deactivated and then
        reactivated by admin A."""
        b = AdminFactory(first_name="Bina", email="bina.admin@example.test")
        stale = signed_in(b)
        services.issue_password_reset(b.email)  # a link B asked for earlier, still pending
        drain_outbox()
        mail.outbox.clear()
        client = signed_in(admin)
        assert client.post(url(b, "deactivate")).status_code == 200
        response = client.post(url(b, "activate"))
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "active"
        return b, stale

    def test_the_old_password_never_works_again(self, returned):
        b, _ = returned
        response = sign_in(b)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_credentials"

    def test_sessions_from_before_stay_dead(self, returned):
        _, stale = returned
        assert stale.get("/api/v1/auth/me").status_code == 401

    def test_a_reset_link_goes_to_their_own_address_and_the_old_one_is_void(self, returned):
        b, _ = returned
        links = AccountToken.objects.filter(user=b, purpose=TokenPurpose.PASSWORD_RESET)
        assert links.filter(status=TokenStatus.PENDING).count() == 1
        assert links.filter(status=TokenStatus.REVOKED).count() >= 1
        drain_outbox()
        assert [m.to for m in mail.outbox] == [["bina.admin@example.test"]]
        secret = last_secret("reset-password")
        done = APIClient().post(
            "/api/v1/auth/password-reset/confirm",
            {"token": secret, "new_password": NEW_PASSWORD},
            format="json",
        )
        assert done.status_code == 204, done.content
        assert sign_in(b, NEW_PASSWORD).status_code == 200

    def test_audited_with_the_reset(self, admin, returned):
        assert returned
        reactivated = AuditEvent.objects.get(action=services.AUDIT_USER_REACTIVATED)
        assert reactivated.actor_id == admin.pk
        assert reactivated.metadata == {"status": "active", "reset_link": True}
        issued = AuditEvent.objects.get(
            action=services.AUDIT_PASSWORD_RESET_ISSUED, actor_id=admin.pk
        )
        assert issued.metadata == {"reason": "reactivated"}
        events = signed_in(admin).get("/api/v1/admin/security-events").json()["results"]
        shown = next(e for e in events if e["action"] == "user.reactivated")
        assert shown["details"] == {"reset_link": "True"}

    def test_the_second_factor_is_kept(self, admin):
        b = AdminFactory()
        enrol(b)
        client = signed_in(admin)
        client.post(url(b, "deactivate"))
        client.post(url(b, "activate"))
        assert MfaDevice.objects.filter(user=b, confirmed_at__isnull=False).exists()
        drain_outbox()
        secret = last_secret("reset-password")
        APIClient().post(
            "/api/v1/auth/password-reset/confirm",
            {"token": secret, "new_password": NEW_PASSWORD},
            format="json",
        )
        assert sign_in(b, NEW_PASSWORD).status_code == 202  # the code is still needed


def test_an_ordinary_user_comes_back_with_their_own_password(admin, user_a):
    client = signed_in(admin)
    client.post(url(user_a, "deactivate"))
    mail.outbox.clear()
    assert client.post(url(user_a, "activate")).json()["status"] == "active"
    assert sign_in(user_a).status_code == 200
    drain_outbox()
    assert mail.outbox == []
    event = AuditEvent.objects.get(action=services.AUDIT_USER_REACTIVATED)
    assert event.metadata == {"status": "active"}


def test_the_deactivation_gauge_counts_administrators_only(admin):
    def value():
        by_name = {gauge.name: gauge for gauge in metrics.gauges()}
        return by_name["arkray_administrator_deactivations_last_hour"].samples[0][1]

    assert value() == 0
    services.deactivate_user(actor_id=admin.pk, user_id=UserFactory().pk)
    assert value() == 0
    services.deactivate_user(actor_id=admin.pk, user_id=AdminFactory().pk)
    assert value() == 1
    event = AuditEvent.objects.filter(action=services.AUDIT_USER_DEACTIVATED).latest("id")
    assert event.metadata["role"] == "admin"
