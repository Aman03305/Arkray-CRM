"""Refused requests count against the API's rate limits too (adversarial review of 73ff1e3,
L2 / AUTH-1).

DRF checks permissions before throttles, so a request refused 401 or 403 never reached a
throttle: 200 anonymous `GET /auth/me` were 200 x 401, and a sales user's 700 x 403 on an
administrator route counted nothing against their budget. Each still costs a web worker (and
a bogus session cookie a session lookup). `core.api.ApiView` now authenticates, then
throttles, then checks permissions: anonymous callers by address, signed-in ones by user.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from arkray.core.throttling import UserRateThrottle
from tests.factories import UserFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

ME = "/api/v1/auth/me"


def anon_limit() -> int:
    return int(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["anon"].split("/")[0])


def test_anonymous_requests_to_a_protected_route_are_rate_limited():
    client = APIClient(REMOTE_ADDR="203.0.113.9")
    limit = anon_limit()
    statuses = [client.get(ME).status_code for _ in range(limit + 10)]
    assert statuses[:limit] == [401] * limit
    assert set(statuses[limit:]) == {429}
    refused = client.get(ME)
    assert refused.json()["error"]["code"] == "rate_limited"
    assert int(refused["Retry-After"]) >= 1
    # Another address is unaffected.
    assert APIClient(REMOTE_ADDR="198.51.100.9").get(ME).status_code == 401


def test_a_bogus_session_cookie_is_rate_limited_alike():
    client = APIClient(REMOTE_ADDR="203.0.113.10")
    client.cookies[settings.SESSION_COOKIE_NAME] = "not-a-real-session-key-0000000000"
    limit = anon_limit()
    statuses = [client.get("/api/v1/workspaces/me").status_code for _ in range(limit + 5)]
    assert statuses[:limit] == [401] * limit
    assert set(statuses[limit:]) == {429}


def test_forbidden_requests_count_against_the_users_budget(user_a, monkeypatch):
    """The review's second test, at a lower rate so it runs quickly: a sales user refused
    403 on an administrator route spends their own budget; their next request is a 429."""
    monkeypatch.setitem(UserRateThrottle.THROTTLE_RATES, "user", "20/min")
    client = signed_in(user_a)
    statuses = [client.get("/api/v1/admin/users").status_code for _ in range(20)]
    assert statuses == [403] * 20
    assert client.get(ME).status_code == 429
    # Keyed by the user, not the address: a colleague at the same address is unaffected.
    assert signed_in(UserFactory()).get(ME).status_code == 200


def test_public_routes_keep_their_order(api_client):
    """AllowAny routes were throttled already; they still are (CSRF cookie: anon budget)."""
    limit = anon_limit()
    statuses = [api_client.get("/api/v1/auth/csrf").status_code for _ in range(limit + 3)]
    assert statuses[:limit] == [204] * limit
    assert set(statuses[limit:]) == {429}
