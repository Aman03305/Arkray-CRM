"""Guards that keep authorization deny-by-default as the codebase grows."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.urls import URLPattern, URLResolver, get_resolver, include, path
from rest_framework.permissions import AllowAny

from arkray.core.api import ApiView
from arkray.identity.permissions import IsActiveUser, requires
from arkray.identity.policy import Capability
from tests.authz_matrix import AUTHZ_MATRIX


def api_views(patterns=None, prefix: str = "") -> dict[str, object]:
    """Every concrete route under api/ (full resolver pattern string) -> its view callback."""
    patterns = get_resolver().url_patterns if patterns is None else patterns
    views: dict[str, object] = {}
    for entry in patterns:
        full = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            views.update(api_views(entry.url_patterns, full))
        elif isinstance(entry, URLPattern) and full.startswith("api/"):
            views[full] = entry.callback
    return views


def api_routes(patterns=None) -> list[str]:
    return list(api_views(patterns))


def expected_permissions(access: str) -> list[type]:
    if access == "public":
        return [AllowAny]
    if access in {"authenticated", "workspace"}:
        return [IsActiveUser]
    kind, _, capability = access.partition(":")
    assert kind == "capability", f"unknown access rule {access!r}"
    return [requires(Capability(capability))]


def unregistered(routes: list[str]) -> list[str]:
    return sorted(r for r in routes if r not in AUTHZ_MATRIX)


def test_drf_default_permission_is_deny_all():
    assert settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] == [
        "arkray.core.permissions.DenyAll"
    ]


def test_every_api_route_is_in_the_authorization_matrix():
    missing = unregistered(api_routes())
    assert not missing, f"Add these routes to tests/authz_matrix.py with tests: {missing}"


def test_matrix_has_no_stale_entries():
    stale = sorted(set(AUTHZ_MATRIX) - set(api_routes()))
    assert not stale, f"Remove routes that no longer exist from the matrix: {stale}"


@pytest.mark.parametrize("route", sorted(AUTHZ_MATRIX))
def test_view_declares_exactly_the_permission_its_rule_requires(route):
    view_class = api_views()[route].view_class
    assert list(view_class.permission_classes) == expected_permissions(AUTHZ_MATRIX[route].access)


@pytest.mark.parametrize("route", sorted(AUTHZ_MATRIX))
def test_view_exposes_exactly_the_methods_in_the_matrix(route):
    view_class = api_views()[route].view_class
    implemented = {m.upper() for m in view_class.http_method_names if hasattr(view_class, m)}
    assert implemented - {"OPTIONS", "HEAD"} == AUTHZ_MATRIX[route].methods


@pytest.mark.parametrize("route", sorted(AUTHZ_MATRIX))
def test_view_uses_the_csrf_enforcing_api_base(route):
    """ApiView enforces CSRF on every unsafe request, signed in or not (DRF alone does not
    for anonymous requests), and marks responses uncacheable."""
    assert issubclass(api_views()[route].view_class, ApiView)


def test_workspace_routes_take_the_workspace_segment():
    for route, rule in AUTHZ_MATRIX.items():
        if rule.access == "workspace":
            assert route.startswith("api/v1/workspaces/<str:workspace>")


def test_every_crm_route_is_nested_under_a_workspace():
    """CRM data is only reachable through a resolved AccessScope."""
    for route in AUTHZ_MATRIX:
        for resource in (
            "/leads",
            "/opportunities",
            "/pipeline-board",
            "/pipeline-summary",
            "/activities",
            "/activity-summary",
        ):
            if resource in route:
                assert route.startswith(f"api/v1/workspaces/<str:workspace>{resource}"), route


def test_guard_detects_an_unregistered_route():
    """The guard itself works (the matrix is empty in Phase 0, so prove it on a fake route)."""
    fake = [path("api/v1/", include(([path("secret-data", lambda r: HttpResponse())], "fake")))]
    assert unregistered(api_routes(fake)) == ["api/v1/secret-data"]


def test_browsable_api_and_form_parsers_are_disabled():
    rf = settings.REST_FRAMEWORK
    assert rf["DEFAULT_RENDERER_CLASSES"] == ["rest_framework.renderers.JSONRenderer"]
    # JSON only, always UTF-8 (a stricter subclass of DRF's JSONParser, see core.parsers).
    assert rf["DEFAULT_PARSER_CLASSES"] == ["arkray.core.parsers.Utf8JSONParser"]
    assert rf["DEFAULT_METADATA_CLASS"] is None  # OPTIONS must not describe serializers


def test_session_and_csrf_cookie_hardening():
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    # Database sessions (a subclass whose cookie ends with the browser: privacy P2-12).
    assert settings.SESSION_ENGINE == "arkray.identity.session_store"
    from django.contrib.sessions.backends.db import SessionStore as DatabaseStore

    from arkray.identity.session_store import SessionStore

    assert issubclass(SessionStore, DatabaseStore)
    assert settings.CSRF_COOKIE_SAMESITE == "Lax"
    assert "django.middleware.csrf.CsrfViewMiddleware" in settings.MIDDLEWARE


def test_django_admin_site_is_not_installed():
    """No second, unaudited administration surface."""
    assert "django.contrib.admin" not in settings.INSTALLED_APPS
