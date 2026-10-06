"""Support sessions (docs/admin-user-workspace.md#support-sessions): an administrator's
time-limited, audited access to one user's CRM, without the user's password and without
impersonation. Never recorded as the user's own act."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.identity import services as identity_services
from arkray.identity import support
from arkray.identity.models import SupportSession
from tests.factories import (
    DEFAULT_PASSWORD,
    AdminFactory,
    InvitedUserFactory,
    UserFactory,
)
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

START = "/api/v1/admin/support-sessions"
CURRENT = "/api/v1/admin/support-sessions/current"


def start(client, user, reason="Customer asked for help with a deal"):
    return client.post(START, {"user": str(user.pk), "reason": reason}, format="json")


def expire(minutes_ago: float = 0.02) -> None:
    now = timezone.now()
    SupportSession.objects.update(
        started_at=now - timedelta(minutes=30 + minutes_ago),
        expires_at=now - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def session(admin_client, user_a):
    response = start(admin_client, user_a)
    assert response.status_code == 201, response.content
    return response.json()


class TestStarting:
    def test_the_administrator_stays_themselves_and_the_target_is_explicit(
        self, admin, admin_client, user_a, session
    ):
        key = admin_client.session.session_key
        me = admin_client.get("/api/v1/auth/me").json()
        assert (me["id"], me["email"]) == (str(admin.pk), admin.email)  # never Rahul
        assert me["support_session"]["target"] == {
            "id": str(user_a.pk),
            "full_name": "Rahul Sharma",
        }
        assert admin_client.session.session_key == key  # no new identity, no login as
        row = SupportSession.objects.get()
        assert (row.admin_id, row.target_id, row.ended_at) == (admin.pk, user_a.pk, None)
        assert row.expires_at - row.started_at == timedelta(minutes=30)
        event = AuditEvent.objects.get(action="support_session.started")
        assert (event.actor_id, event.subject_user_id, event.support_session_id) == (
            admin.pk,
            user_a.pk,
            row.pk,
        )
        assert event.metadata["reason"] == "Customer asked for help with a deal"

    @pytest.mark.parametrize(
        "target",
        [
            lambda: AdminFactory(),
            lambda: UserFactory(is_active=False),
            lambda: InvitedUserFactory(),
        ],
        ids=["administrator", "deactivated", "invited"],
    )
    def test_only_for_active_crm_users(self, admin_client, target):
        response = start(admin_client, target())
        assert response.status_code == 422
        assert not SupportSession.objects.exists()

    def test_not_for_oneself_nor_an_unknown_user(self, admin, admin_client):
        assert start(admin_client, admin).status_code == 404
        unknown = admin_client.post(
            START, {"user": "5a1e4d2c-0000-4000-8000-00000000abcd"}, format="json"
        )
        assert unknown.status_code == 404

    def test_sales_users_cant(self, user_a_client, user_b):
        assert start(user_a_client, user_b).status_code == 403

    def test_the_reason_is_plain_and_bounded(self, admin_client, user_a):
        assert start(admin_client, user_a, reason="x" * 201).status_code == 400
        assert start(admin_client, user_a, reason="a" + chr(0x202E) + "b").status_code == 400

    def test_no_password_of_the_user_is_ever_involved(self, admin_client, user_a, session):
        user_a.refresh_from_db()
        password = user_a.password
        assert password not in admin_client.get("/api/v1/auth/me").content.decode()
        assert not AuditEvent.objects.filter(action__startswith="auth.").exists()


class TestDuringTheSession:
    def test_only_the_targets_workspace_opens(self, admin_client, user_a, user_b, session):
        assert admin_client.get(f"/api/v1/workspaces/{user_a.pk}/leads").status_code == 200
        for workspace in ("me", "all", str(user_b.pk)):
            response = admin_client.get(f"/api/v1/workspaces/{workspace}/leads")
            assert response.status_code == 403, workspace
            assert response.json()["error"]["code"] == "support_session_active"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v1/admin/users"),
            ("get", "/api/v1/admin/security-events"),
            ("post", "/api/v1/auth/password/change"),
            ("post", START),
        ],
    )
    def test_identity_and_security_operations_need_the_normal_admin_context(
        self, admin_client, user_a, session, method, path
    ):
        response = getattr(admin_client, method)(path, {}, format="json")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "support_session_active"

    def test_setting_the_users_password_is_refused(self, admin_client, user_a, session):
        response = admin_client.post(
            f"/api/v1/admin/users/{user_a.pk}/set-password",
            {"version": user_a.version, "new_password": "Temporary-Reset-Pass-77#"},
            format="json",
        )
        assert response.status_code == 403
        user_a.refresh_from_db()
        assert not user_a.check_password("Temporary-Reset-Pass-77#")


class TestEnding:
    def test_exit_ends_it_and_is_audited(self, admin, admin_client, user_a, session):
        assert admin_client.delete(CURRENT).status_code == 204
        assert admin_client.delete(CURRENT).status_code == 204  # idempotent
        assert admin_client.get("/api/v1/auth/me").json()["support_session"] is None
        assert admin_client.get("/api/v1/workspaces/all/leads").status_code == 200
        row = SupportSession.objects.get()
        assert row.end_reason == "exited"
        event = AuditEvent.objects.get(action="support_session.ended")
        assert (event.actor_id, event.metadata["end"]) == (admin.pk, "exited")

    def test_it_expires(self, admin_client, user_a, session):
        expire()
        assert admin_client.get("/api/v1/auth/me").json()["support_session"] is None
        assert SupportSession.objects.get().end_reason == "expired"
        event = AuditEvent.objects.get(action="support_session.ended")
        assert event.actor_id is None  # the system ended it
        assert admin_client.get(f"/api/v1/workspaces/{user_a.pk}/leads").status_code == 200

    def test_deactivating_the_user_ends_it(self, admin, admin_client, user_a, session):
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        assert admin_client.get("/api/v1/auth/me").json()["support_session"] is None
        assert SupportSession.objects.get().end_reason == "not_allowed"

    def test_promoting_the_user_to_administrator_ends_it(self, admin_client, user_a, session):
        type(user_a).objects.filter(pk=user_a.pk).update(role="admin")
        admin_client.get("/api/v1/auth/me")
        assert SupportSession.objects.get().end_reason == "not_allowed"

    def test_signing_out_ends_it(self, admin_client, user_a, session):
        admin_client.post("/api/v1/auth/logout")
        assert SupportSession.objects.get().end_reason == "signed_out"

    def test_it_belongs_to_one_browser_session(self, admin, admin_client, user_a, session):
        other_browser = signed_in(admin)
        assert other_browser.get("/api/v1/auth/me").json()["support_session"] is None
        # Copying the session's marker into another browser session (fixation) gets nothing.
        stolen = other_browser.session
        stolen[support.SESSION_KEY] = session["id"]
        stolen.save()
        assert other_browser.get("/api/v1/auth/me").json()["support_session"] is None
        assert SupportSession.objects.get().end_reason == "session_changed"
        assert admin_client.get("/api/v1/auth/me").json()["support_session"] is None

    def test_starting_elsewhere_ends_the_earlier_one(self, admin, admin_client, user_a, user_b):
        start(admin_client, user_a)
        assert start(signed_in(admin), user_b).status_code == 201
        ends = dict(SupportSession.objects.values_list("target_id", "end_reason"))
        assert ends == {user_a.pk: "session_changed", user_b.pk: ""}

    def test_the_hourly_sweep_closes_forgotten_ones(self, admin_client, user_a, session):
        expire(minutes_ago=5)
        assert support.sweep_expired(timezone.now()) == 1
        assert support.sweep_expired(timezone.now()) == 0
        assert SupportSession.objects.get().end_reason == "expired"


# --- attacks: the administrator is the ACTOR, the user only the SUBJECT ----------------------
# Identity routes a support session may use, and why. Every other identity route must refuse
# (enumerated from the URLconf below, so a new route is refused or listed here on purpose).
ALLOWED_DURING_SUPPORT = {
    "api/v1/auth/csrf": "the CSRF cookie",
    "api/v1/auth/login": "signing in starts a new browser session, which ends the support one",
    "api/v1/auth/logout": "ends it",
    "api/v1/auth/me": "says who is signed in, and which support session is live",
    "api/v1/auth/password-reset": "public: the link goes to the account's own address only",
    "api/v1/auth/password-reset/confirm": "needs the emailed one-time secret",
    "api/v1/auth/invitations/verify": "needs the emailed one-time secret",
    "api/v1/auth/invitations/accept": "needs the emailed one-time secret",
    "api/v1/admin/support-sessions/current": "exit",
    "api/v1/workspaces/<str:workspace>": "describes the target's workspace (others: 403)",
    "api/v1/assignees": "owner pickers in the target's CRM",
}
TEMPORARY = "Temporary-Reset-Pass-77#"


def identity_routes(patterns=None, prefix=""):
    """(route pattern, view class) of every identity API route, from the URLconf."""
    from django.urls import URLPattern, URLResolver, get_resolver

    from arkray.identity.api import views

    found = []
    for entry in get_resolver().url_patterns if patterns is None else patterns:
        full = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            found += identity_routes(entry.url_patterns, full)
        elif isinstance(entry, URLPattern) and full.startswith("api/"):
            view_class = getattr(entry.callback, "view_class", None)
            if view_class is not None and view_class.__module__ == views.__name__:
                found.append((full, view_class))
    return found


def methods_of(view_class):
    return sorted(
        m for m in view_class.http_method_names if hasattr(view_class, m) and m not in {"options"}
    )


def attack_body(route, target, other):
    """A request that would succeed without the support-session gate."""
    if route.endswith("/password/change"):
        return {"current_password": DEFAULT_PASSWORD, "new_password": "Admin-New-Passphrase-5!"}
    if route.endswith("/change-email"):
        return {"email": "attacker@evil.test", "version": target.version}
    if route.endswith("/set-password"):
        return {"version": target.version, "new_password": TEMPORARY}
    if route.endswith("/support-sessions"):
        return {"user": str(other.pk)}
    if route.endswith("/users"):
        return {"first_name": "Mole", "email": "mole@evil.test", "role": "admin"}
    if route.endswith("<uuid:user_id>"):
        return {"role": "admin", "version": target.version}  # role escalation of the user
    return {}


def account_state(user):
    row = type(user).objects.get(pk=user.pk)
    return (row.email, row.password, row.role, row.status, row.version, row.session_epoch)


class TestAttacksDuringASession:
    def test_the_allowlist_names_only_existing_identity_routes(self):
        assert set(ALLOWED_DURING_SUPPORT) <= {route for route, _ in identity_routes()}

    def test_every_other_identity_route_is_refused(self, admin, admin_client, user_a, session):
        """Password change, role escalation, takeover and security settings: none of it from
        inside a support session, against the user, another user, or the administrator."""
        other = UserFactory(first_name="Priya")
        other_admin = AdminFactory()
        before = {u.pk: account_state(u) for u in (admin, user_a, other, other_admin)}
        audited = AuditEvent.objects.count()
        refused = []
        for route, view_class in identity_routes():
            if route in ALLOWED_DURING_SUPPORT:
                continue
            for target in (user_a, other, other_admin, admin):
                path = "/" + route.replace("<uuid:user_id>", str(target.pk))
                for method in methods_of(view_class):
                    response = getattr(admin_client, method)(
                        path, attack_body(route, target, other), format="json"
                    )
                    assert response.status_code == 403, (method, path, response.content)
                    assert response.json()["error"]["code"] == "support_session_active"
                    refused.append((method, route))
        assert len({r for _, r in refused}) >= 9  # users, user, 5 actions, events, start, pw
        assert {u.pk: account_state(u) for u in (admin, user_a, other, other_admin)} == before
        assert AuditEvent.objects.count() == audited
        assert SupportSession.objects.get().ended_at is None  # refusals don't end it

    def test_the_audit_names_the_administrator_as_actor_and_the_user_as_subject(
        self, admin, admin_client, user_a, session
    ):
        response = admin_client.post(
            f"/api/v1/workspaces/{user_a.pk}/leads", {"first_name": "Asha"}, format="json"
        )
        assert response.status_code == 201, response.content
        for action in ("support_session.started", "lead.created"):
            event = AuditEvent.objects.get(action=action)
            assert (event.actor_id, event.subject_user_id, str(event.support_session_id)) == (
                admin.pk,
                user_a.pk,
                session["id"],
            ), action
        assert not AuditEvent.objects.filter(actor_id=user_a.pk).exists()

    def test_promoting_the_user_through_the_service_ends_it(self, admin_client, user_a, session):
        """Promotion by another administrator: the next request ends the session, audited."""
        promoter = AdminFactory()
        identity_services.update_user(
            actor_id=promoter.pk, user_id=user_a.pk, version=1, changes={"role": "admin"}
        )
        assert admin_client.get(f"/api/v1/workspaces/{user_a.pk}/leads").status_code in (200, 404)
        assert admin_client.get("/api/v1/auth/me").json()["support_session"] is None
        ended = AuditEvent.objects.get(action="support_session.ended")
        assert (ended.actor_id, ended.metadata["end"]) == (None, "not_allowed")
        assert admin_client.get("/api/v1/admin/users").status_code == 200  # normal context

    @pytest.mark.parametrize(
        "header",
        [
            "HTTP_X_SUPPORT_SESSION",
            "HTTP_X_ARKRAY_SUPPORT_SESSION",
            "HTTP_X_IMPERSONATE_USER",
            "HTTP_X_FORWARDED_USER",
            "HTTP_X_REMOTE_USER",
            "HTTP_X_USER_ID",
            "REMOTE_USER",
        ],
    )
    def test_headers_cant_claim_a_session_or_an_identity(
        self, admin, user_a, user_b, session, header
    ):
        """Cross-user tampering: the session lives only in the server-side browser session."""
        client = signed_in(user_b)
        for value in (session["id"], str(user_a.pk), str(admin.pk), admin.email):
            extra = {header: value}
            me = client.get("/api/v1/auth/me", **extra).json()
            assert (me["id"], me["support_session"]) == (str(user_b.pk), None)
            leads = client.get(f"/api/v1/workspaces/{user_a.pk}/leads", **extra)
            assert leads.status_code == 404
            users = client.get("/api/v1/admin/users", **extra)
            assert users.status_code == 403
        assert SupportSession.objects.get().ended_at is None

    @pytest.mark.parametrize("thief", ["sales_user", "other_admin"])
    def test_someone_elses_browser_session_cant_replay_the_id(
        self, admin_client, user_a, user_b, session, thief
    ):
        """Another account's browser session carrying the id gets nothing, and the
        administrator's own session carries on."""
        client = signed_in(user_b if thief == "sales_user" else AdminFactory())
        stolen = client.session
        stolen[support.SESSION_KEY] = session["id"]
        stolen.save()
        assert client.get("/api/v1/auth/me").json()["support_session"] is None
        assert support.SESSION_KEY not in client.session
        if thief == "sales_user":
            assert client.get(f"/api/v1/workspaces/{user_a.pk}/leads").status_code == 404
        else:
            # A full administrator anyway, but not "in" the session: identity work works and
            # nothing it does is stamped with the other administrator's session.
            assert client.get("/api/v1/admin/users").status_code == 200
        assert SupportSession.objects.get().ended_at is None
        assert admin_client.get("/api/v1/auth/me").json()["support_session"]["id"] == session["id"]

    @pytest.mark.parametrize("how", ["exited", "expired"])
    def test_an_ended_session_cant_be_revived(self, admin_client, user_a, session, how):
        if how == "exited":
            admin_client.delete(CURRENT)
        else:
            expire()
            admin_client.get("/api/v1/auth/me")
        revived = admin_client.session
        revived[support.SESSION_KEY] = session["id"]
        revived.save()
        assert admin_client.get("/api/v1/auth/me").json()["support_session"] is None
        assert admin_client.get("/api/v1/admin/users").status_code == 200
        assert AuditEvent.objects.filter(action="support_session.ended").count() == 1
