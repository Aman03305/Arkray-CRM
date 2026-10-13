"""Two-step verification for every user, when the organisation requires it (adversarial review
of 73ff1e3, L1 (f); the owner's remediation brief lists "MFA for all users").

`MFA_REQUIRED_FOR_ALL_USERS` extends ADR-0033's enrolment gate from administrators to every
role: on, a user without a confirmed second factor reaches only who-am-I, sign-out, the
password change and the two-step setup (403 `mfa_enrollment_required` elsewhere), can't turn
it off, and the viewer says so (`mfa_enrollment_required`, which the web app follows rather
than the role). On by default in production, off in development and tests; an organisational
policy, so it stays a setting (docs/deployment.md;
tests/architecture/test_account_policy_settings.py). With it off, nothing changes:
administrators still need one when MFA_REQUIRED_FOR_ADMINS is on.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache

from arkray.identity import mfa_services
from tests.authz_matrix import AUTHZ_MATRIX
from tests.factories import AdminFactory, TeamAdminFactory, UserFactory
from tests.helpers import signed_in
from tests.mfa_helpers import Clock, code_for, enrol, sign_in_with_code
from tests.security.test_admin_mfa_enforcement import (
    ALLOWED_BEFORE_ENROLMENT,
    GATED,
    error_code,
    may_ask,
    send,
)

pytestmark = pytest.mark.django_db

ME = "/api/v1/auth/me"


@pytest.fixture
def required_for_all(settings):
    settings.MFA_REQUIRED_FOR_ALL_USERS = True


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """The time two-step verification sees (as tests.mfa_helpers.clock)."""
    current = Clock()
    monkeypatch.setattr(mfa_services, "_now", lambda: current.now)
    return current


def code(response) -> str | None:
    try:
        return str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None


class TestRequiredForEveryone:
    def test_a_sales_user_without_one_must_enrol_first(self, required_for_all, user_a):
        client = signed_in(user_a)
        viewer = client.get(ME).json()
        assert viewer["mfa_enrollment_required"] is True
        for path in (
            "/api/v1/workspaces/me",
            "/api/v1/workspaces/me/leads",
            "/api/v1/workspaces/me/dashboard",
        ):
            cache.clear()
            response = client.get(path)
            assert response.status_code == 403, path
            assert code(response) == "mfa_enrollment_required", path
        assert client.get("/api/v1/auth/mfa").json()["required"] is True

    def test_the_setup_is_reachable(self, required_for_all, user_a):
        client = signed_in(user_a)
        response = client.post(
            "/api/v1/auth/mfa/setup",
            {"current_password": "correct-horse-battery-staple"},
            format="json",
        )
        assert response.status_code == 200, response.content

    def test_once_enrolled_the_app_opens(self, required_for_all, user_a):
        enrol(user_a)
        client = signed_in(user_a)
        assert client.get(ME).json()["mfa_enrollment_required"] is False
        assert client.get("/api/v1/workspaces/me").status_code == 200

    def test_it_cant_be_turned_off(self, required_for_all, user_a, clock):
        enrolment = enrol(user_a)
        client = signed_in(user_a)
        assert sign_in_with_code(client, user_a, enrolment.secret, clock).status_code == 200
        clock.next_step()
        response = client.post(
            "/api/v1/auth/mfa/disable",
            {
                "current_password": "correct-horse-battery-staple",
                "code": code_for(enrolment.secret, clock),
            },
            format="json",
        )
        assert response.status_code == 422
        assert "can't be turned off" in response.json()["error"]["message"]
        status = client.get("/api/v1/auth/mfa").json()
        assert (status["required"], status["can_disable"]) == (True, False)

    def test_administrators_are_included(self, required_for_all, admin, settings):
        settings.MFA_REQUIRED_FOR_ADMINS = False
        assert mfa_services.required_for(admin)
        assert signed_in(admin).get(ME).json()["mfa_enrollment_required"] is True


class TestOffUnchanged:
    def test_a_sales_user_is_not_asked(self, user_a, settings):
        settings.MFA_REQUIRED_FOR_ADMINS = True
        assert settings.MFA_REQUIRED_FOR_ALL_USERS is False
        client = signed_in(user_a)
        assert client.get(ME).json()["mfa_enrollment_required"] is False
        assert client.get("/api/v1/workspaces/me").status_code == 200
        assert not mfa_services.required_for(user_a)

    def test_administrators_still_are(self, admin, settings):
        settings.MFA_REQUIRED_FOR_ADMINS = True
        assert mfa_services.required_for(admin)
        cache.clear()
        response = signed_in(admin).get("/api/v1/admin/users")
        assert code(response) == "mfa_enrollment_required"


# --- review round 1 (TESTS-2): the whole route sweep, per role and setting ----------------------
COMBINATIONS = [
    # (role, which setting is on): administrators-only must leave a sales user alone; for
    # everyone it asks both (the administrators-only case for an administrator is
    # test_admin_mfa_enforcement.py's own sweep).
    ("sales_user", "administrators"),
    ("sales_user", "everyone"),
    ("admin", "everyone"),
    # A team administrator is an administrator here (ADR-0035): asked in both modes.
    ("team_admin", "administrators"),
    ("team_admin", "everyone"),
]
FACTORIES = {"admin": AdminFactory, "team_admin": TeamAdminFactory, "sales_user": UserFactory}


@pytest.mark.parametrize(("role", "mode"), COMBINATIONS)
@pytest.mark.parametrize(("route", "method"), GATED)
def test_every_route_follows_the_setting_for_every_role(settings, role, mode, route, method):
    settings.MFA_REQUIRED_FOR_ADMINS = mode == "administrators"
    settings.MFA_REQUIRED_FOR_ALL_USERS = mode == "everyone"
    user = FACTORIES[role]()
    response = send(signed_in(user), method, route)
    rule = AUTHZ_MATRIX[route]
    gated = mode == "everyone" or role in ("admin", "team_admin")
    if route in ALLOWED_BEFORE_ENROLMENT or not gated:
        assert error_code(response) != "mfa_enrollment_required", (method, route)
        return
    assert response.status_code == 403, (method, route, response.content)
    if not may_ask(role, rule.access):
        # Refused for the capability before any gate (and nothing was let through).
        assert error_code(response) == "permission_denied", (method, route)
    else:
        assert error_code(response) == "mfa_enrollment_required", (method, route)
