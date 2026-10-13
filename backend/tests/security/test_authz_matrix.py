"""Every route in the authorization matrix, exercised as anonymous, sales user, super
administrator and team administrator (ADR-0035).

Complements tests/architecture (which checks what each view *declares*) by checking what
each route *does*: the backend refuses, whatever the frontend shows.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from arkray.identity.models import Role, User
from arkray.identity.policy import ROLE_CAPABILITIES, Capability
from tests.authz_matrix import AUTHZ_MATRIX, PIPELINE
from tests.factories import UserFactory
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

CASES = sorted((route, method) for route, rule in AUTHZ_MATRIX.items() for method in rule.methods)


# A record id that exists nowhere: record routes answer 404 to everyone allowed past the
# permission layer, which is exactly what these tests must tell apart from 401/403.
MISSING_RECORD = "5a1e4d2c-0000-4000-8000-00000000abcd"


def concrete(route: str, target: User) -> str:
    return "/" + (
        route.replace("<uuid:user_id>", str(target.pk))
        .replace("<str:workspace>", "me")
        .replace("<uuid:lead_id>", MISSING_RECORD)
        .replace("<uuid:opportunity_id>", MISSING_RECORD)
        .replace("<uuid:activity_id>", MISSING_RECORD)
        .replace("<uuid:question_id>", MISSING_RECORD)
        .replace("<uuid:conversation_id>", MISSING_RECORD)
        .replace("<uuid:pipeline_id>", MISSING_RECORD)
        .replace("<uuid:attachment_id>", MISSING_RECORD)
        .replace("<uuid:export_id>", MISSING_RECORD)
        .replace("<uuid:field_id>", MISSING_RECORD)
    )


def call(client: APIClient, method: str, path: str, body=None):
    return getattr(client, method.lower())(path, body or {}, format="json")


def holds_any(capabilities: Iterable[Capability], access: str) -> bool:
    """Whether holders of `capabilities` pass a `capability:` rule: one capability, or
    several of which any one will do (`a|b`, ADR-0035)."""
    held = {capability.value for capability in capabilities}
    _, _, wanted = access.partition(":")
    return any(capability in held for capability in wanted.split("|"))


@pytest.mark.parametrize(("route", "method"), CASES)
def test_anonymous_callers_reach_only_public_routes(route, method, user_b):
    response = call(APIClient(), method, concrete(route, user_b))
    if AUTHZ_MATRIX[route].access == "public":
        assert response.status_code not in (401, 403), response.content
    else:
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"


@pytest.mark.parametrize(("route", "method"), CASES)
def test_sales_users_never_reach_user_administration(route, method, user_a, user_b):
    client = signed_in(user_a)
    response = call(client, method, concrete(route, user_b))
    access = AUTHZ_MATRIX[route].access
    # Capabilities a sales user holds (ai.query: Ask Arkray in their own workspace) pass.
    if access.startswith("capability:") and not holds_any(
        ROLE_CAPABILITIES[Role.SALES_USER], access
    ):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"
    else:
        assert response.status_code not in (401, 403), response.content


@pytest.mark.parametrize(("route", "method"), CASES)
def test_admins_reach_every_route(route, method, admin, user_b):
    response = call(signed_in(admin), method, concrete(route, user_b))
    assert response.status_code not in (401, 403), response.content


@pytest.mark.parametrize(
    ("route", "method"),
    [
        (r, m)
        for r, m in CASES
        if AUTHZ_MATRIX[r].access.startswith("capability:") and "<uuid:user_id>" in r
    ],
)
def test_a_403_reveals_nothing_about_whether_the_target_exists(route, method, user_a, user_b):
    client = signed_in(user_a)
    ghost = UserFactory.build()  # never saved
    existing = call(client, method, concrete(route, user_b))
    missing = call(client, method, concrete(route, ghost))
    assert existing.status_code == missing.status_code == 403


class TestNoSelfEscalation:
    PAYLOAD = {"first_name": "A", "role": "admin", "is_superuser": True, "capabilities": ["*"]}

    def test_a_user_cannot_patch_themselves_into_an_admin(self, user_a):
        client = signed_in(user_a)
        response = client.patch(
            f"/api/v1/admin/users/{user_a.pk}", {**self.PAYLOAD, "version": 1}, format="json"
        )
        assert response.status_code == 403
        user_a.refresh_from_db()
        assert (user_a.role, user_a.first_name) == (Role.SALES_USER, "Rahul")
        assert "users.manage" not in client.get("/api/v1/auth/me").json()["capabilities"]

    def test_the_exact_malicious_payload_is_refused_even_for_an_admin(self, admin, user_a):
        """The mass-assignment payload from the Phase 1 brief: unknown keys -> 400."""
        response = signed_in(admin).patch(
            f"/api/v1/admin/users/{user_a.pk}", {**self.PAYLOAD, "version": 1}, format="json"
        )
        assert response.status_code == 400
        user_a.refresh_from_db()
        assert (user_a.role, user_a.first_name, user_a.version) == (Role.SALES_USER, "Rahul", 1)

    def test_a_user_cannot_modify_another_user(self, user_a, user_b):
        client = signed_in(user_a)
        for action, body in [
            ("", {"first_name": "Hacked", "version": 1}),
            ("/deactivate", {}),
            ("/change-email", {"email": "mine@example.test", "version": 1}),
            ("/resend-invitation", {}),
        ]:
            method = client.patch if action == "" else client.post
            assert (
                method(f"/api/v1/admin/users/{user_b.pk}{action}", body, format="json").status_code
                == 403
            )
        user_b.refresh_from_db()
        assert (user_b.first_name, user_b.is_active, user_b.version) == ("Priya", True, 1)

    def test_signed_in_endpoints_ignore_attempts_to_act_for_someone_else(self, user_a, user_b):
        """/auth/me and /auth/password/change act on the session user only; there is no id."""
        client = signed_in(user_a)
        response = client.post(
            "/api/v1/auth/password/change",
            {
                "current_password": "correct-horse-battery-staple",
                "new_password": "another-good-passphrase-1",
                "user_id": str(user_b.pk),
            },
            format="json",
        )
        assert response.status_code == 400  # unknown field, nothing changed
        user_b.refresh_from_db()
        assert user_b.check_password("correct-horse-battery-staple")


UNSAFE = [(r, m) for r, m in CASES if m not in ("GET", "HEAD", "OPTIONS")]


@pytest.mark.parametrize(("route", "method"), UNSAFE)
def test_every_unsafe_route_requires_a_csrf_token_signed_in(route, method, admin, user_b):
    client = signed_in(admin, APIClient(enforce_csrf_checks=True))
    response = call(client, method, concrete(route, user_b))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


@pytest.mark.parametrize(
    ("route", "method"), [(r, m) for r, m in UNSAFE if AUTHZ_MATRIX[r].access == "public"]
)
def test_every_unsafe_public_route_requires_a_csrf_token_anonymously(route, method, user_b):
    response = call(APIClient(enforce_csrf_checks=True), method, concrete(route, user_b))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_csrf_protection_is_not_satisfied_by_the_cookie_alone(admin, user_b):
    """Double submit: the header must match; a cross-site form can send the cookie only."""
    client = signed_in(admin, APIClient(enforce_csrf_checks=True))
    client.get("/api/v1/auth/csrf")
    assert settings.CSRF_COOKIE_NAME in client.cookies
    response = client.post(f"/api/v1/admin/users/{user_b.pk}/deactivate")
    assert response.status_code == 403


# --- Phase 9: a view-only role and a deactivated user, on every route --------------------------
VIEW_ONLY = frozenset(
    {
        Capability.CRM_ACCESS_OWN,
        Capability.CRM_VIEW_ALL,
        Capability.WORKSPACE_VIEW_ANY,
        Capability.AUDIT_VIEW,
    }
)
SAFE = frozenset({"GET", "HEAD", "OPTIONS"})


def in_workspace(route: str, target: User, workspace: str) -> str:
    return concrete(route, target).replace("/workspaces/me", f"/workspaces/{workspace}", 1)


@pytest.mark.parametrize(("route", "method"), CASES)
def test_a_view_only_role_reads_everywhere_and_writes_nowhere_but_home(
    route, method, monkeypatch, admin, user_b
):
    """A role that may view every workspace but manage none (simulated: the admin role
    reduced to viewing). Reads pass; every write in someone else's workspace or the
    organisation is refused: never 2xx, whatever the record."""
    monkeypatch.setitem(ROLE_CAPABILITIES, Role.ADMIN, VIEW_ONLY)
    client = signed_in(admin)
    rule = AUTHZ_MATRIX[route]
    kind = rule.access.partition(":")[0]
    workspaces = [str(user_b.pk), "all"] if "<str:workspace>" in route else [""]
    for workspace in workspaces:
        path = in_workspace(route, user_b, workspace) if workspace else concrete(route, user_b)
        response = call(client, method, path)
        status = response.status_code
        if kind == "capability":
            if holds_any(VIEW_ONLY, rule.access):
                assert status not in (401, 403), (path, status)
            else:
                assert status == 403, (path, status)
        elif kind == "workspace":
            if method in SAFE:
                assert status not in (401, 403), (path, status)
            else:
                assert status in (403, 404), (path, status)
                assert status == 403 or "<uuid:" in route, (path, status)
        elif kind == "authenticated":
            assert status not in (401, 403), (path, status)


@pytest.mark.parametrize(
    ("route", "method"), [(r, m) for r, m in CASES if AUTHZ_MATRIX[r].access != "public"]
)
def test_a_deactivated_users_open_session_reaches_nothing(route, method, admin, user_a, user_b):
    """A session opened before deactivation is dead on its very next request, on every
    route (the session hash covers `is_active` and `session_epoch`)."""
    from arkray.identity import services as identity_services

    client = signed_in(user_a)
    identity_services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
    response = call(client, method, concrete(route, user_b))
    assert response.status_code == 401, response.content
    assert response.json()["error"]["code"] == "not_authenticated"


# --- ADR-0035: a team administrator on every route --------------------------------------------
# Pipeline configuration is refused in the team scope (workspace `all` of a team administrator):
# a member's personal pipeline is configured from their own workspace, shared ones by super
# administrators. Here, with an empty body and a record id that exists nowhere, they may answer
# 400 or 404 first; never a success (tests/security/test_teams_crm.py proves the 403 with valid
# requests).
TEAM_SCOPE_CONFIGURATION = {
    ("api/v1/workspaces/<str:workspace>/pipelines", "POST"),
    (PIPELINE, "PATCH"),
    (f"{PIPELINE}/stages", "PUT"),
    (f"{PIPELINE}/fields", "PUT"),
    (f"{PIPELINE}/fields/<uuid:field_id>/delete-values", "POST"),
    (f"{PIPELINE}/archive", "POST"),
    (f"{PIPELINE}/restore", "POST"),
}


@pytest.mark.parametrize(("route", "method"), CASES)
def test_team_admins_reach_their_team_and_nothing_organisation_wide(
    route, method, team_admin, team_member, user_b
):
    """Tara (team_admin) with Rahul (her team): the routes her capabilities name answer her,
    on her member and in her team's workspaces (`all`, Rahul's id); the organisation-wide ones
    (security events, privacy requests) are 403 whatever the target."""
    client = signed_in(team_admin)
    rule = AUTHZ_MATRIX[route]
    kind = rule.access.partition(":")[0]
    workspaces = ["me", "all", str(team_member.pk)] if "<str:workspace>" in route else [""]
    for workspace in workspaces:
        path = (
            in_workspace(route, team_member, workspace)
            if workspace
            else concrete(route, team_member)
        )
        response = call(client, method, path)
        status = response.status_code
        if kind == "capability" and not holds_any(ROLE_CAPABILITIES[Role.TEAM_ADMIN], rule.access):
            assert status == 403, (path, status, response.content)
            assert response.json()["error"]["code"] == "permission_denied"
        elif workspace == "all" and (route, method) in TEAM_SCOPE_CONFIGURATION:
            assert status in (400, 403, 404), (path, status, response.content)
        else:
            assert status not in (401, 403), (path, status, response.content)


@pytest.mark.parametrize(
    ("route", "method"),
    [(r, m) for r, m in CASES if AUTHZ_MATRIX[r].access != "public" and "<uuid:user_id>" in r],
)
def test_a_team_admin_learns_nothing_about_accounts_outside_her_team(
    route, method, team_admin, team_member, user_b, other_team_member
):
    """Priya (no team) and Kavya (another team) answer exactly like an account that doesn't
    exist: 404 where Tara may ask at all, 403 where she may not (or the same 400 for a body
    the route never accepts, whoever the target)."""
    client = signed_in(team_admin)
    ghost = UserFactory.build()  # never saved
    answers = [call(client, method, concrete(route, u)) for u in (user_b, other_team_member)]
    missing = call(client, method, concrete(route, ghost))
    for answer in answers:
        assert answer.status_code == missing.status_code, (route, answer.content)
        assert answer.status_code in (400, 403, 404), (route, answer.content)
        assert without_request_id(answer) == without_request_id(missing)


@pytest.mark.parametrize(
    ("route", "method"), [(r, m) for r, m in CASES if AUTHZ_MATRIX[r].access != "public"]
)
def test_a_deactivated_team_admins_open_session_reaches_nothing(
    route, method, admin, team_admin, team_member
):
    from arkray.identity import services as identity_services

    client = signed_in(team_admin)
    identity_services.deactivate_user(actor_id=admin.pk, user_id=team_admin.pk)
    response = call(client, method, concrete(route, team_member))
    assert response.status_code == 401, response.content
