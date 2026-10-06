"""Session lifecycle: current user, logout, timeouts, and revocation on account changes."""

import time

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.identity import services
from arkray.identity.authentication import AUDIT_LOGOUT
from arkray.identity.models import User
from arkray.identity.sessions import AUTH_AT, SEEN_AT
from tests.factories import DEFAULT_PASSWORD, AdminFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"


def set_session_times(client, **times):
    session = client.session
    for key, value in times.items():
        session[key] = value
    session.save()


class TestCurrentUser:
    def test_anonymous_gets_401_with_a_challenge(self, api_client):
        response = api_client.get(ME)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"
        assert response["WWW-Authenticate"] == 'Session realm="arkray"'

    def test_returns_the_signed_in_user(self, user_a_client, user_a):
        body = user_a_client.get(ME).json()
        assert (body["id"], body["email"], body["role_label"]) == (
            str(user_a.pk),
            user_a.email,
            "User",
        )

    def test_is_never_cached(self, user_a_client):
        response = user_a_client.get(ME)
        assert "no-store" in response["Cache-Control"]

    def test_cannot_be_written(self, user_a_client):
        for method in ("post", "put", "patch", "delete"):
            assert getattr(user_a_client, method)(ME, {"role": "admin"}).status_code == 405


class TestLogout:
    def test_ends_the_session_and_audits(self, user_a_client, user_a):
        session_key = user_a_client.session.session_key
        assert user_a_client.post(LOGOUT).status_code == 204
        assert not Session.objects.filter(session_key=session_key).exists()
        assert user_a_client.get(ME).status_code == 401
        event = AuditEvent.objects.get(action=AUDIT_LOGOUT)
        assert event.actor_id == user_a.pk

    def test_is_idempotent_without_a_session(self, api_client):
        assert api_client.post(LOGOUT).status_code == 204
        assert not AuditEvent.objects.exists()

    def test_requires_csrf(self, user_a):
        client = signed_in(user_a, APIClient(enforce_csrf_checks=True))
        response = client.post(LOGOUT)
        assert response.status_code == 403
        assert client.get(ME).status_code == 200


class TestTimeouts:
    def test_idle_session_expires(self, user_a_client):
        key = user_a_client.session.session_key
        set_session_times(
            user_a_client, **{SEEN_AT: time.time() - settings.SESSION_IDLE_TIMEOUT_S - 1}
        )
        assert user_a_client.get(ME).status_code == 401
        assert not Session.objects.filter(session_key=key).exists()

    def test_absolute_lifetime_is_enforced_even_for_an_active_session(self, user_a_client):
        set_session_times(
            user_a_client,
            **{AUTH_AT: time.time() - settings.SESSION_COOKIE_AGE - 1, SEEN_AT: time.time()},
        )
        assert user_a_client.get(ME).status_code == 401

    def test_activity_is_recorded_at_most_once_per_interval(self, user_a_client):
        recent = time.time() - 10
        set_session_times(user_a_client, **{SEEN_AT: recent})
        user_a_client.get(ME)
        assert user_a_client.session[SEEN_AT] == recent  # no write for a fresh session

        stale = time.time() - settings.SESSION_ACTIVITY_REFRESH_S - 1
        set_session_times(user_a_client, **{SEEN_AT: stale})
        user_a_client.get(ME)
        assert user_a_client.session[SEEN_AT] > stale

    def test_activity_does_not_extend_the_absolute_expiry(self, api_client, user_a):
        api_client.post(
            "/api/v1/auth/login",
            {"email": user_a.email, "password": DEFAULT_PASSWORD},
            format="json",
        )
        key = api_client.cookies[settings.SESSION_COOKIE_NAME].value
        expiry = Session.objects.get(session_key=key).expire_date
        set_session_times(
            api_client, **{SEEN_AT: time.time() - settings.SESSION_ACTIVITY_REFRESH_S - 1}
        )
        api_client.get(ME)
        assert Session.objects.get(session_key=key).expire_date == expiry

    @pytest.mark.parametrize(
        "session_data",
        [{}, {AUTH_AT: "yesterday", SEEN_AT: 1.0}, {AUTH_AT: time.time() + 3600, SEEN_AT: 1.0}],
        ids=["missing", "malformed", "from-the-future"],
    )
    def test_sessions_without_valid_timestamps_are_rejected(self, user_a, session_data):
        client = APIClient()
        client.force_login(user_a)
        set_session_times(client, **session_data)
        assert client.get(ME).status_code == 401


class TestRevocation:
    def test_deactivation_ends_existing_sessions_immediately(self, admin, user_a):
        client = signed_in(user_a)
        services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        assert client.get(ME).status_code == 401

    def test_old_sessions_stay_dead_after_reactivation(self, admin, user_a):
        """A stolen laptop's session must not come back when the user is reactivated."""
        client = signed_in(user_a)
        services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        services.reactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        assert client.get(ME).status_code == 401
        user_a.refresh_from_db()
        assert signed_in(user_a).get(ME).status_code == 200  # signing in again works

    def test_email_change_ends_the_users_sessions(self, admin, user_a):
        client = signed_in(user_a)
        services.change_user_email(
            actor_id=admin.pk, user_id=user_a.pk, version=user_a.version, email="new@example.test"
        )
        assert client.get(ME).status_code == 401

    def test_demotion_takes_effect_on_the_next_request(self, admin):
        other_admin = AdminFactory()
        client = signed_in(other_admin)
        assert client.get("/api/v1/admin/users").status_code == 200
        # Another administrator can't demote them (R100); an operator can, in the database.
        User.objects.filter(pk=other_admin.pk).update(role="sales_user")
        assert client.get("/api/v1/admin/users").status_code == 403
        assert client.get(ME).json()["capabilities"] == ["ai.query", "crm.access_own"]


class TestPasswordChange:
    URL = "/api/v1/auth/password/change"
    NEW = "a-brand-new-passphrase-42"

    def change(self, client, current=DEFAULT_PASSWORD, new=NEW):
        return client.post(
            self.URL, {"current_password": current, "new_password": new}, format="json"
        )

    def test_keeps_this_session_on_a_new_key_and_ends_the_others(self, user_a):
        this, other = signed_in(user_a), signed_in(user_a)
        old_key = this.session.session_key
        assert self.change(this).status_code == 204
        assert this.get(ME).status_code == 200
        assert this.session.session_key != old_key
        assert other.get(ME).status_code == 401
        user_a.refresh_from_db()
        assert user_a.check_password(self.NEW)

    def test_wrong_current_password(self, user_a_client):
        response = self.change(user_a_client, current="not-my-password")
        assert response.status_code == 400
        assert "current_password" in response.json()["error"]["details"]

    def test_repeated_wrong_guesses_are_throttled(self, user_a_client):
        for _ in range(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD):
            self.change(user_a_client, current="not-my-password")
        assert self.change(user_a_client).status_code == 429

    def test_weak_new_password_is_rejected_with_reasons(self, user_a_client):
        response = self.change(user_a_client, new="short")
        assert response.status_code == 400
        assert response.json()["error"]["details"]["new_password"]

    def test_reusing_the_current_password_is_rejected(self, user_a_client):
        response = self.change(user_a_client, new=DEFAULT_PASSWORD)
        assert response.status_code == 400

    def test_requires_a_session(self, api_client):
        assert self.change(api_client).status_code == 401
