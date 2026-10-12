"""Session and request boundaries from the deep security audit: a role change signs the user
out everywhere (L-2), another site can't make an administrator's browser record a workspace
visit (L-3), and signing in again ends a support session bound to the browser (I-1)."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.identity import services
from arkray.identity.models import Role, SupportEnd, SupportSession
from arkray.identity.workspaces import AUDIT_ACTION_WORKSPACE_ACCESSED
from tests.factories import DEFAULT_PASSWORD, AdminFactory, UserFactory
from tests.helpers import signed_in
from tests.mfa_helpers import enrol, password_step

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login"
SUPPORT = "/api/v1/admin/support-sessions"


# --- L-2: a role change ends the user's sessions ---------------------------------------------
def test_a_promotion_signs_the_user_out_everywhere(admin):
    sales = UserFactory()
    stale = signed_in(sales)  # a session on a shared computer, or a stolen cookie
    assert stale.get("/api/v1/admin/users").status_code == 403
    services.update_user(
        actor_id=admin.pk, user_id=sales.pk, version=sales.version, changes={"role": Role.ADMIN}
    )
    assert stale.get("/api/v1/admin/users").status_code == 401
    assert stale.get("/api/v1/auth/me").status_code == 401
    # Signing in again (as an administrator now) works.
    assert password_step(APIClient(), sales).status_code == 200


def test_a_name_change_keeps_the_sessions(admin):
    sales = UserFactory()
    client = signed_in(sales)
    services.update_user(
        actor_id=admin.pk, user_id=sales.pk, version=sales.version, changes={"first_name": "Ravi"}
    )
    assert client.get("/api/v1/auth/me").status_code == 200


# --- L-3: reads another site makes the browser send ------------------------------------------
@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
def test_a_workspace_opened_from_another_site_is_404_and_not_audited(admin, user_a, site):
    response = signed_in(admin).get(f"/api/v1/workspaces/{user_a.pk}", HTTP_SEC_FETCH_SITE=site)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert not AuditEvent.objects.filter(action=AUDIT_ACTION_WORKSPACE_ACCESSED).exists()


@pytest.mark.usefixtures("crm_configuration")
def test_every_api_read_from_another_site_is_refused(user_a):
    client = signed_in(user_a)
    for path in ("/api/v1/auth/me", "/api/v1/workspaces/me", "/api/v1/workspaces/me/opportunities"):
        assert client.get(path).status_code == 200, path
        assert client.get(path, HTTP_SEC_FETCH_SITE="cross-site").status_code == 404, path


@pytest.mark.parametrize("site", ["same-origin", "none", None])
def test_the_app_typed_addresses_and_api_clients_still_read(admin, user_a, site):
    """`same-origin`: the app's own fetches and download links; `none`: an address typed or
    bookmarked; no header: API clients, tests, older browsers (no other site makes those
    send the user's cookies)."""
    headers = {} if site is None else {"HTTP_SEC_FETCH_SITE": site}
    response = signed_in(admin).get(f"/api/v1/workspaces/{user_a.pk}", **headers)
    assert response.status_code == 200, response.content
    assert AuditEvent.objects.filter(action=AUDIT_ACTION_WORKSPACE_ACCESSED).count() == 1


# --- I-1: signing in again ends a support session ---------------------------------------------
@pytest.fixture
def in_support(admin, user_a):
    client = signed_in(admin)
    started = client.post(SUPPORT, {"user": str(user_a.pk), "reason": "help"}, format="json")
    assert started.status_code == 201, started.content
    return client


def ended(reason: SupportEnd) -> None:
    session = SupportSession.objects.get()
    assert session.ended_at is not None
    assert session.end_reason == reason
    event = AuditEvent.objects.get(action="support_session.ended")
    assert event.metadata["end"] == reason.value


def test_signing_in_again_ends_the_support_session(admin, in_support):
    response = in_support.post(
        LOGIN, {"email": admin.email, "password": DEFAULT_PASSWORD}, format="json"
    )
    assert response.status_code == 200, response.content
    ended(SupportEnd.SESSION_CHANGED)
    assert in_support.get("/api/v1/auth/me").json()["support_session"] is None


def test_signing_in_as_someone_else_ends_it_too(in_support):
    other = AdminFactory()
    assert password_step(in_support, other).status_code == 200
    ended(SupportEnd.SESSION_CHANGED)


def test_a_second_factor_challenge_ends_it_too(admin, in_support):
    enrol(admin)
    assert password_step(in_support, admin).status_code == 202
    ended(SupportEnd.SESSION_CHANGED)


def test_a_wrong_password_leaves_it_running(admin, in_support):
    response = in_support.post(
        LOGIN, {"email": admin.email, "password": "not-the-password-1"}, format="json"
    )
    assert response.status_code == 400
    session = SupportSession.objects.get()
    assert session.ended_at is None
    assert in_support.get("/api/v1/auth/me").json()["support_session"] is not None
