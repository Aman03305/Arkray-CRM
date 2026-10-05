"""Administrator-chosen passwords (docs/authorization.md#admin-set-passwords) and the
password-change notification (#password-change-notification).

An administrator may create a user with an initial password, or set a new temporary one for
a user; the plaintext exists only in that request: hashed at once, never stored, logged,
audited, queued or returned; the user must replace it at first sign-in. When a user changes
their password, administrators learn *that* it happened (who, when) and nothing else."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.logging import JsonFormatter
from arkray.core.models import OutboxEvent
from arkray.identity.models import User, UserStatus
from tests.factories import DEFAULT_PASSWORD, AdminFactory, InvitedUserFactory, UserFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

INITIAL = "Initial-Blue-Lantern-2026!"
TEMPORARY = "Temporary-Reset-Pass-77#"
CHOSEN = "My-Own-Secret-Phrase-91$"
USERS = "/api/v1/admin/users"


def create_user(client, password=INITIAL, **extra):
    body = {
        "first_name": "Rahul",
        "last_name": "Sharma",
        "email": "rahul.sharma@example.test",
        "role": "sales_user",
        **extra,
    }
    if password is not None:
        body["password"] = password
    return client.post(USERS, body, format="json")


def sign_in(client, email, password):
    return client.post("/api/v1/auth/login", {"email": email, "password": password}, format="json")


@pytest.fixture
def records(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def everything_written(records):
    """Every response-independent place a secret could leak to: logs, audit, outbox, mail."""
    formatter = JsonFormatter()
    logged = "\n".join(formatter.format(r) for r in records.records)
    audited = json.dumps(list(AuditEvent.objects.values()), default=str)
    queued = json.dumps(list(OutboxEvent.objects.values("payload", "last_error")), default=str)
    mailed = "\n".join(str(m.body) for m in mail.outbox)
    return logged + audited + queued + mailed


class TestCreatingWithAPassword:
    def test_the_user_is_active_with_only_a_hash_and_must_change_it(self, admin_client, records):
        response = create_user(admin_client)
        assert response.status_code == 201, response.content
        body = response.content.decode()
        assert INITIAL not in body
        assert '"password"' not in body
        data = response.json()
        assert (data["status"], data["password_change_required"]) == ("active", True)
        user = User.objects.get(email="rahul.sharma@example.test")
        assert user.check_password(INITIAL)
        assert INITIAL not in user.password  # a hash
        assert user.password.startswith(("md5$", "argon2$", "pbkdf2"))
        assert INITIAL not in everything_written(records)
        assert not mail.outbox  # no invitation: the administrator hands the password over
        created = AuditEvent.objects.get(action="user.created")
        assert created.metadata == {"role": "sales_user", "activation": "set_by_admin"}

    @pytest.mark.parametrize(
        "weak",
        [
            "short1!",  # too short
            "password1234",  # common
            "123456789012345",  # numeric
            "rahul.sharma@example.test1",  # like the email
            "x" * 129,  # too long
        ],
    )
    def test_the_password_policy_applies(self, admin_client, weak):
        response = create_user(admin_client, password=weak)
        assert response.status_code == 400
        assert "password" in response.json()["error"]["details"]
        assert weak not in response.content.decode()
        assert not User.objects.filter(email="rahul.sharma@example.test").exists()

    def test_without_a_password_an_invitation_is_sent_as_before(self, admin_client):
        response = create_user(admin_client, password=None)
        assert response.status_code == 201
        assert response.json()["status"] == "invited"
        assert response.json()["password_change_required"] is False

    def test_a_sales_user_cant_create_users(self, user_a_client):
        assert create_user(user_a_client).status_code == 403


class TestFirstSignIn:
    def test_until_changed_only_who_am_i_and_change_password_work(self, admin_client, api_client):
        create_user(admin_client)
        signed = sign_in(api_client, "rahul.sharma@example.test", INITIAL)
        assert signed.status_code == 200
        assert signed.json()["password_change_required"] is True
        me = api_client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["password_change_required"] is True
        for path in (
            "/api/v1/workspaces/me/leads",
            "/api/v1/workspaces/me/dashboard",
            "/api/v1/workspaces/me/pipelines",
        ):
            blocked = api_client.get(path)
            assert blocked.status_code == 403
            assert blocked.json()["error"]["code"] == "password_change_required"
        changed = api_client.post(
            "/api/v1/auth/password/change",
            {"current_password": INITIAL, "new_password": CHOSEN},
            format="json",
        )
        assert changed.status_code == 204, changed.content
        assert api_client.get("/api/v1/workspaces/me/leads").status_code == 200
        user = User.objects.get(email="rahul.sharma@example.test")
        assert user.password_change_required is False
        assert user.check_password(CHOSEN)

    def test_a_temporary_password_expires(self, admin_client, api_client, settings):
        create_user(admin_client)
        User.objects.filter(email="rahul.sharma@example.test").update(
            password_changed_at=timezone.now()
            - timedelta(seconds=settings.TEMPORARY_PASSWORD_TTL_S + 60)
        )
        response = sign_in(api_client, "rahul.sharma@example.test", INITIAL)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "temporary_password_expired"
        # the wrong password still gets the generic answer
        wrong = sign_in(api_client, "rahul.sharma@example.test", "Wrong-Password-123!")
        assert wrong.json()["error"]["code"] == "invalid_credentials"


class TestAdminSetsAPassword:
    def url(self, user):
        return f"{USERS}/{user.pk}/set-password"

    def test_sessions_end_links_die_and_the_user_must_change_it(self, admin, admin_client, records):
        rahul = UserFactory()
        old_session = signed_in(rahul)
        response = admin_client.post(
            self.url(rahul), {"version": rahul.version, "new_password": TEMPORARY}, format="json"
        )
        assert response.status_code == 200, response.content
        assert response.json()["password_change_required"] is True
        assert TEMPORARY not in response.content.decode()
        assert old_session.get("/api/v1/auth/me").status_code == 401  # signed out everywhere
        rahul.refresh_from_db()
        assert rahul.check_password(TEMPORARY)
        event = AuditEvent.objects.get(action="auth.password_set_by_admin")
        assert (event.actor_id, event.subject_user_id, event.target_id) == (
            admin.pk,
            rahul.pk,
            str(rahul.pk),
        )
        assert TEMPORARY not in everything_written(records)

    def test_refused_for_oneself_another_administrator_and_inactive_users(
        self, admin, admin_client
    ):
        other_admin = AdminFactory()
        invited = InvitedUserFactory()
        gone = UserFactory(is_active=False)
        for user in (admin, other_admin, invited, gone):
            response = admin_client.post(
                self.url(user), {"version": user.version, "new_password": TEMPORARY}, format="json"
            )
            assert response.status_code == 422, (user.status, response.content)
            user.refresh_from_db()
            assert not user.check_password(TEMPORARY)

    def test_a_stale_version_conflicts(self, admin_client):
        rahul = UserFactory()
        response = admin_client.post(
            self.url(rahul),
            {"version": rahul.version + 1, "new_password": TEMPORARY},
            format="json",
        )
        assert response.status_code == 409

    def test_only_administrators(self, user_a_client):
        rahul = UserFactory()
        response = user_a_client.post(
            self.url(rahul), {"version": 1, "new_password": TEMPORARY}, format="json"
        )
        assert response.status_code == 403

    def test_the_user_detail_never_shows_a_password(self, admin_client):
        rahul = UserFactory()
        body = admin_client.get(f"{USERS}/{rahul.pk}").content.decode()
        assert '"password"' not in body
        assert DEFAULT_PASSWORD not in body
        assert "password_changed_at" in body  # when, never what


class TestPasswordChangeNotification:
    EVENTS = "/api/v1/admin/security-events"

    def test_administrators_learn_that_it_happened_and_nothing_else(
        self, admin_client, user_a, records
    ):
        client = signed_in(user_a)
        changed = client.post(
            "/api/v1/auth/password/change",
            {"current_password": DEFAULT_PASSWORD, "new_password": CHOSEN},
            format="json",
        )
        assert changed.status_code == 204, changed.content
        response = admin_client.get(self.EVENTS)
        assert response.status_code == 200
        body = response.content.decode()
        for secret in (CHOSEN, DEFAULT_PASSWORD, User.objects.get(pk=user_a.pk).password):
            assert secret not in body
        event = response.json()["results"][0]
        assert event["action"] == "auth.password_changed"
        assert event["actor"]["id"] == str(user_a.pk)
        assert event["user"]["full_name"] == "Rahul Sharma"
        assert event["details"] == {}
        assert CHOSEN not in everything_written(records)

    def test_only_allowlisted_events_and_details(self, admin_client, admin):
        rahul = UserFactory()
        admin_client.post(f"{USERS}/{rahul.pk}/deactivate")
        AuditEvent.objects.create(
            action="lead.created", actor_type="user", actor_id=admin.pk, metadata={}
        )
        actions = [e["action"] for e in admin_client.get(self.EVENTS).json()["results"]]
        assert "user.deactivated" in actions
        assert "lead.created" not in actions

    def test_sales_users_see_no_security_events(self, user_a_client):
        assert user_a_client.get(self.EVENTS).status_code == 403


def test_an_admin_created_users_status_matches_its_password(admin_client):
    """The database's lifecycle CHECKs hold: active with a usable password."""
    create_user(admin_client)
    user = User.objects.get(email="rahul.sharma@example.test")
    assert user.status == UserStatus.ACTIVE
    assert user.has_usable_password()
    assert user.activated_at is not None
    assert user.password_changed_at is not None


class TestNoAdministratorTakeover:
    """Enhancement security review: an administrator must never end up knowing another
    administrator's password, and a sign-in with a password an administrator chose must
    be visible to administrators."""

    def test_demote_set_promote_is_refused(self, admin, admin_client, api_client):
        other = AdminFactory()
        demoted = admin_client.patch(
            f"{USERS}/{other.pk}", {"version": other.version, "role": "sales_user"}, format="json"
        )
        assert demoted.status_code == 200, demoted.content
        other.refresh_from_db()
        assert (
            admin_client.post(
                f"{USERS}/{other.pk}/set-password",
                {"version": other.version, "new_password": TEMPORARY},
                format="json",
            ).status_code
            == 200
        )
        other.refresh_from_db()
        promoted = admin_client.patch(
            f"{USERS}/{other.pk}", {"version": other.version, "role": "admin"}, format="json"
        )
        assert promoted.status_code == 422
        assert "choose their own password" in promoted.json()["error"]["message"]
        other.refresh_from_db()
        assert other.role == "sales_user"
        # Once the user has chosen their own password, promotion works as before.
        assert sign_in(api_client, other.email, TEMPORARY).status_code == 200
        changed = api_client.post(
            "/api/v1/auth/password/change",
            {"current_password": TEMPORARY, "new_password": CHOSEN},
            format="json",
        )
        assert changed.status_code == 204, changed.content
        other.refresh_from_db()
        promoted = admin_client.patch(
            f"{USERS}/{other.pk}", {"version": other.version, "role": "admin"}, format="json"
        )
        assert promoted.status_code == 200, promoted.content

    def test_an_administrator_is_invited_never_given_a_password(self, admin_client):
        response = create_user(admin_client, role="admin")
        assert response.status_code == 400
        assert "invitation" in response.json()["error"]["details"]["password"][0]
        assert not User.objects.filter(email="rahul.sharma@example.test").exists()
        assert create_user(admin_client, password=None, role="admin").status_code == 201

    def test_a_sign_in_with_a_temporary_password_shows_in_security_events(
        self, admin_client, api_client
    ):
        create_user(admin_client)
        assert sign_in(api_client, "rahul.sharma@example.test", INITIAL).status_code == 200
        api_client.post(
            "/api/v1/auth/password/change",
            {"current_password": INITIAL, "new_password": CHOSEN},
            format="json",
        )
        actions = [
            e["action"] for e in admin_client.get("/api/v1/admin/security-events").json()["results"]
        ]
        # newest first: created, signed in with the temporary password, then changed it
        assert actions[:3] == [
            "auth.password_changed",
            "auth.login_with_temporary_password",
            "user.created",
        ]
        # An ordinary sign-in stays out of the feed.
        sign_in(api_client, "rahul.sharma@example.test", CHOSEN)
        assert (
            admin_client.get("/api/v1/admin/security-events").json()["results"][0]["action"]
            == "auth.password_changed"
        )
