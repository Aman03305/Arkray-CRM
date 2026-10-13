"""Administrators can't get past setting up two-step verification (ADR-0033, R125).

While MFA_REQUIRED_FOR_ADMINS is on (production refuses it off, except with the local opt-in
on a local host), an administrator without a second factor reaches only the views marked
`allowed_before_mfa_enrollment`: every other API route answers 403 `mfa_enrollment_required`.
Enumerated here from the authorization matrix (which tests/architecture checks against the
URLconf, with each view's permission class), so a new route is gated or listed below on
purpose. Nothing outside the API serves account or CRM data (the probes and the token-only
metrics), and neither a support session nor a step-up mark gets past the gate."""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.urls import URLPattern, URLResolver, get_resolver

from arkray.identity import mfa_services
from arkray.identity.models import SupportSession
from arkray.identity.policy import ROLE_CAPABILITIES, Capability
from tests.architecture.test_authorization_baseline import api_views
from tests.architecture.test_production_settings import load_production_settings
from tests.authz_matrix import AUTHZ_MATRIX
from tests.factories import TeamAdminFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

# Each one, and why an administrator needs it before having a second factor.
ALLOWED_BEFORE_ENROLMENT = {
    "api/v1/auth/me": "who is signed in, and that setting up is required",
    "api/v1/auth/password/change": "an administrator-chosen password is replaced first",
    "api/v1/auth/mfa": "the two-step verification status",
    "api/v1/auth/mfa/setup": "starting the setup",
    "api/v1/auth/mfa/confirm": "finishing it",
}
# No session needed, none used: signing in, its second step, and the emailed account links.
PUBLIC = {
    "api/v1/auth/csrf",
    "api/v1/auth/login",
    "api/v1/auth/login/mfa",
    "api/v1/auth/logout",
    "api/v1/auth/password-reset",
    "api/v1/auth/password-reset/confirm",
    "api/v1/auth/invitations/verify",
    "api/v1/auth/invitations/accept",
    "api/v1/auth/sign-in-unlock",  # review of 73ff1e3, L1: the emailed unlock link
    "api/v1/auth/sign-in-unlock/verify",  # whose account it is, masked (review round 1)
}
# Everything outside /api/: the probes and the metrics (a bearer token, never a session).
NOT_API = {"health/metrics", "health/live", "health/ready"}
# What a role may hold without being asked for a second factor.
UNPRIVILEGED = {Capability.CRM_ACCESS_OWN, Capability.AI_QUERY}
PARAMETER = re.compile(r"<(str|uuid):[a-z_]+>")


@pytest.fixture(autouse=True)
def _required(settings):
    settings.MFA_REQUIRED_FOR_ADMINS = True


def error_code(response) -> str | None:
    try:
        return str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None


def path_of(route: str) -> str:
    """A concrete path: the caller's own workspace, and ids nobody has (the gate answers
    before anything is looked up)."""
    return "/" + PARAMETER.sub(
        lambda match: "me" if match.group(1) == "str" else str(uuid4()), route
    )


def may_ask(role: str, access: str) -> bool:
    """Whether `role` passes a route's permission class: a capability rule names one
    capability, or several of which any one will do (`a|b`, ADR-0035)."""
    kind, _, capabilities = access.partition(":")
    if kind != "capability":
        return True
    held = {capability.value for capability in ROLE_CAPABILITIES[role]}
    return any(capability in held for capability in capabilities.split("|"))


def send(client, method: str, route: str):
    cache.clear()  # the request-rate throttle's window, between many requests
    return getattr(client, method.lower())(path_of(route), {}, format="json")


GATED = sorted(
    (route, method)
    for route, rule in AUTHZ_MATRIX.items()
    if rule.access != "public"
    for method in sorted(rule.methods)
)


def all_routes(patterns=None, prefix: str = "") -> set[str]:
    found: set[str] = set()
    for entry in get_resolver().url_patterns if patterns is None else patterns:
        full = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            found |= all_routes(entry.url_patterns, full)
        elif isinstance(entry, URLPattern):
            found.add(full)
    return found


# --- what is reachable before enrolment ---------------------------------------------------------
def test_the_routes_reachable_before_enrolment_are_exactly_these():
    marked = {
        route
        for route, view in api_views().items()
        if getattr(view.view_class, "allowed_before_mfa_enrollment", False)
    }
    assert marked == set(ALLOWED_BEFORE_ENROLMENT)


def test_the_public_routes_are_the_sign_in_bootstrap_only():
    public = {route for route, rule in AUTHZ_MATRIX.items() if rule.access == "public"}
    assert public == PUBLIC


def test_nothing_outside_the_api_takes_a_session():
    """No Django admin site, no other non-DRF view: the probes answer anyone with no data,
    and the metrics only a bearer token (aggregate counts)."""
    assert {route for route in all_routes() if not route.startswith("api/")} == NOT_API


@pytest.mark.parametrize(("route", "method"), GATED)
def test_every_other_route_refuses_an_administrator_without_a_second_factor(admin, route, method):
    response = send(signed_in(admin), method, route)
    if route in ALLOWED_BEFORE_ENROLMENT:
        assert error_code(response) != "mfa_enrollment_required", response.content
    else:
        assert response.status_code == 403, (method, route, response.content)
        assert error_code(response) == "mfa_enrollment_required", (method, route)


@pytest.mark.parametrize(("route", "method"), GATED)
def test_every_other_route_refuses_a_team_administrator_without_a_second_factor(route, method):
    """ADR-0035: an Admin is an administrator for two-step verification too. A route their
    role may not ask at all answers 403 `permission_denied` first (nothing let through)."""
    response = send(signed_in(TeamAdminFactory()), method, route)
    if route in ALLOWED_BEFORE_ENROLMENT:
        assert error_code(response) != "mfa_enrollment_required", response.content
        return
    assert response.status_code == 403, (method, route, response.content)
    expected = (
        "mfa_enrollment_required"
        if may_ask("team_admin", AUTHZ_MATRIX[route].access)
        else "permission_denied"
    )
    assert error_code(response) == expected, (method, route)


def test_the_probes_and_metrics_give_an_unenrolled_administrator_nothing(admin, settings):
    settings.METRICS_TOKEN = "m" * 40
    client = signed_in(admin)
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/metrics").status_code == 404  # a session is no token


def test_every_privileged_role_must_enrol():
    """Every role holding more than its own CRM and Ask Arkray (all users, all workspaces,
    user administration, the audit trail, support sessions, privacy) is one the gate asks."""
    for role, capabilities in ROLE_CAPABILITIES.items():
        if set(capabilities) - UNPRIVILEGED:
            assert mfa_services.required_for(SimpleNamespace(role=role)), role


# --- nothing gets past it -----------------------------------------------------------------------
def test_a_step_up_mark_in_the_session_does_not_get_past_it(admin):
    client = signed_in(admin)
    session = client.session
    session[mfa_services.SESSION_VERIFIED_AT] = time.time()
    session.save()
    for path in ("/api/v1/admin/users", "/api/v1/admin/privacy/exports"):
        cache.clear()
        response = client.get(path)
        assert (response.status_code, error_code(response)) == (403, "mfa_enrollment_required")
    verify = client.post("/api/v1/auth/mfa/verify", {"code": "123456"}, format="json")
    assert error_code(verify) == "mfa_enrollment_required"


def test_a_support_session_does_not_get_past_it(admin, user_a, settings):
    """Started while a second factor wasn't required (the only way an administrator without
    one could have one): once it is, the support session reaches nothing either."""
    settings.MFA_REQUIRED_FOR_ADMINS = False
    client = signed_in(admin)
    started = client.post(
        "/api/v1/admin/support-sessions",
        {"user": str(user_a.pk), "reason": "ticket 5"},
        format="json",
    )
    assert started.status_code == 201, started.content
    assert client.get(f"/api/v1/workspaces/{user_a.pk}/leads").status_code == 200

    settings.MFA_REQUIRED_FOR_ADMINS = True
    for path in (
        f"/api/v1/workspaces/{user_a.pk}/leads",
        f"/api/v1/workspaces/{user_a.pk}/dashboard",
        f"/api/v1/workspaces/{user_a.pk}",
    ):
        cache.clear()
        response = client.get(path)
        assert (response.status_code, error_code(response)) == (403, "mfa_enrollment_required")
    assert SupportSession.objects.filter(ended_at__isnull=True).count() == 1  # nothing else


# --- production ---------------------------------------------------------------------------------
def test_production_refuses_it_off_for_a_public_host_whatever_the_opt_in():
    refused = load_production_settings(
        MFA_REQUIRED_FOR_ADMINS="false",
        DJANGO_ALLOW_INSECURE_LOCAL_HTTP="true",
        DJANGO_ALLOWED_HOSTS="crm.example.com",
    )
    assert refused.returncode != 0
    assert "DJANGO_ALLOW_INSECURE_LOCAL_HTTP" in refused.stderr
