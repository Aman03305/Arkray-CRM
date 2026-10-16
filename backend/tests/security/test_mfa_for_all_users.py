"""Regression coverage for the retired all-users MFA policy.

Some existing deployments can still carry ``MFA_REQUIRED_FOR_ALL_USERS`` in their
environment during a rolling upgrade.  It must not broaden MFA beyond Super Admins:
Admins and Users sign in directly, cannot enrol, and are never caught by MFA gates.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from arkray.identity import mfa_services
from tests.authz_matrix import AUTHZ_MATRIX
from tests.factories import AdminFactory, TeamAdminFactory, UserFactory
from tests.helpers import signed_in
from tests.mfa_helpers import Clock, enrol, password_step
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


class TestLegacyAllUsersFlag:
    def test_a_sales_user_is_not_forced_to_enrol(self, required_for_all, user_a):
        client = signed_in(user_a)
        viewer = client.get(ME).json()
        assert viewer["mfa_enrollment_required"] is False
        for path in (
            "/api/v1/workspaces/me",
            "/api/v1/workspaces/me/leads",
            "/api/v1/workspaces/me/dashboard",
        ):
            cache.clear()
            response = client.get(path)
            assert code(response) != "mfa_enrollment_required", path
        assert client.get("/api/v1/auth/mfa").json()["required"] is False

    def test_a_sales_user_cannot_enrol(self, required_for_all, user_a):
        client = signed_in(user_a)
        response = client.post(
            "/api/v1/auth/mfa/setup",
            {"current_password": "correct-horse-battery-staple"},
            format="json",
        )
        assert (response.status_code, code(response)) == (403, "permission_denied")

    def test_a_legacy_device_does_not_change_sales_user_login(self, required_for_all, user_a):
        enrol(user_a)
        client = APIClient()
        response = password_step(client, user_a)
        assert response.status_code == 200
        assert client.get(ME).json()["mfa_enrollment_required"] is False
        assert client.get("/api/v1/workspaces/me").status_code == 200

    def test_a_sales_user_cannot_manage_a_legacy_device(self, required_for_all, user_a):
        enrolment = enrol(user_a)
        response = signed_in(user_a).post(
            "/api/v1/auth/mfa/disable",
            {
                "current_password": "correct-horse-battery-staple",
                "code": enrolment.recovery_codes[0],
            },
            format="json",
        )
        assert (response.status_code, code(response)) == (403, "permission_denied")

    def test_the_legacy_flag_does_not_replace_the_super_admin_policy(
        self, required_for_all, admin, settings
    ):
        settings.MFA_REQUIRED_FOR_ADMINS = False
        assert not mfa_services.required_for(admin)
        assert signed_in(admin).get(ME).json()["mfa_enrollment_required"] is False


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
    # The legacy "everyone" flag is deliberately inert. The administrators setting applies
    # to the Super Admin role only.
    ("sales_user", "administrators"),
    ("sales_user", "everyone"),
    ("admin", "administrators"),
    ("admin", "everyone"),
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
    gated = mode == "administrators" and role == "admin"
    if route in ALLOWED_BEFORE_ENROLMENT or not gated:
        assert error_code(response) != "mfa_enrollment_required", (method, route)
        return
    assert response.status_code == 403, (method, route, response.content)
    if not may_ask(role, rule.access):
        # Refused for the capability before any gate (and nothing was let through).
        assert error_code(response) == "permission_denied", (method, route)
    else:
        assert error_code(response) == "mfa_enrollment_required", (method, route)
