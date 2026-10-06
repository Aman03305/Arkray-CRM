"""Abuse limits, two users at a time: one user (or one address) sending too much is refused
with 429 and Retry-After, while another user's requests in the same minute still succeed.

What limits each path (docs/security.md "Excessive requests", docs/search.md#resource-protection):

- Writes (opportunity creation, notes, stage moves) and administration: only the global
  per-user rate (API_THROTTLE_USER, 600/min, keyed by the user), shared with every other
  request the user makes; there is no separate write rate.
- Ask Arkray: its own per-user scope (API_THROTTLE_ASK, 20/min) on top, plus the
  PostgreSQL-counted pending-question bulkheads (ai.service).
- Search: its own per-user scope (API_THROTTLE_SEARCH): arkray/search/tests.
- Sign-in: PostgreSQL-backed budgets per account and browser, and per address (identity.
  throttling); password-reset requests per address per hour (and emails per account, in
  the job). These hold when Redis is down.
- Every DRF rate lives in the cache: with Redis down they don't apply (fail open).

Rates are lowered here so a test needs a handful of requests; the keys and the refusals are
the production ones.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest import mock

import pytest
from django.conf import settings
from django.test import override_settings
from rest_framework.settings import api_settings
from rest_framework.test import APIClient
from rest_framework.throttling import (
    AnonRateThrottle,
    ScopedRateThrottle,
    SimpleRateThrottle,
    UserRateThrottle,
)

from arkray.core.redis import cache_breaker, fail_fast_pool_kwargs
from tests.factories import (
    DEFAULT_PASSWORD,
    AdminFactory,
    LeadFactory,
    OpportunityFactory,
    default_stage,
)
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

DEAL = {
    "account_name": "ABC Diagnostics",
    "customer_name": "ABC Diagnostics Mumbai",
    "contact_phone": "9876543210",
    "address": "Mumbai",
    "instrument_name": "Adams 8380 V-lite",
    "work_load": "300 tests/day",
    "value": "850000",
    "expected_cpt": "Rs 18 per test",
    "opportunity_date": "2026-10-05",
    "expected_close_date": "2026-12-15",
}
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"  # nothing listens on port 1
DEAD_REDIS_CACHE = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": DEAD_REDIS_URL,
        "OPTIONS": {
            "SOCKET_CONNECT_TIMEOUT": 1,
            "SOCKET_TIMEOUT": 1,
            "IGNORE_EXCEPTIONS": True,
            "CONNECTION_POOL_KWARGS": fail_fast_pool_kwargs(DEAD_REDIS_URL),
        },
    }
}


@pytest.fixture
def rates(monkeypatch) -> Callable[..., None]:
    """Lower some DRF rates for the test (each throttle class reads its own attribute)."""

    def lower(**overrides: str) -> None:
        current = {**api_settings.DEFAULT_THROTTLE_RATES, **overrides}
        for throttle in (
            SimpleRateThrottle,
            AnonRateThrottle,
            UserRateThrottle,
            ScopedRateThrottle,
        ):
            monkeypatch.setattr(throttle, "THROTTLE_RATES", current)

    return lower


def refused(response: Any) -> bool:
    return (
        response.status_code == 429
        and response.json()["error"]["code"] == "rate_limited"
        and int(response["Retry-After"]) >= 1
    )


def login(client: APIClient, email: str, password: str = DEFAULT_PASSWORD) -> Any:
    return client.post("/api/v1/auth/login", {"email": email, "password": password}, format="json")


# --- writes and administration: the per-user rate ------------------------------------------------
class TestOneUsersSpamLeavesOthersAlone:
    def test_creating_opportunities(self, rates, user_a, user_b):
        rates(user="3/min")
        spammer, colleague = signed_in(user_a), signed_in(user_b)

        def create(client: APIClient) -> Any:
            return client.post(
                "/api/v1/workspaces/me/opportunities",
                DEAL,
                format="json",
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )

        assert [create(spammer).status_code for _ in range(3)] == [201] * 3
        assert refused(create(spammer))
        assert create(colleague).status_code == 201

    def test_adding_notes(self, rates, user_a, user_b):
        rates(user="3/min")
        url = "/api/v1/workspaces/me/activities"

        def note(client: APIClient, owner: Any) -> Any:
            lead: Any = LeadFactory(owner=owner)
            body = {"type": "note", "lead": str(lead.pk), "description": "x"}
            return client.post(url, body, format="json")

        spammer, colleague = signed_in(user_a), signed_in(user_b)
        assert [note(spammer, user_a).status_code for _ in range(3)] == [201] * 3
        assert refused(note(spammer, user_a))
        assert note(colleague, user_b).status_code == 201

    def test_moving_deals_between_stages(self, rates, user_a, user_b):
        rates(user="3/min")
        mine = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=default_stage("new"))
        theirs = OpportunityFactory(lead=LeadFactory(owner=user_b), stage=default_stage("new"))

        def move(client: APIClient, opportunity: Any, stage: str, version: int) -> Any:
            url = f"/api/v1/workspaces/me/opportunities/{opportunity.pk}/move"
            body = {"stage": str(default_stage(stage).pk), "version": version}
            return client.post(url, body, format="json")

        spammer, colleague = signed_in(user_a), signed_in(user_b)
        stages = ["qualified", "proposal", "qualified"]
        codes = [move(spammer, mine, stage, n).status_code for n, stage in enumerate(stages, 1)]
        assert codes == [200] * 3
        assert refused(move(spammer, mine, "new", 4))
        assert move(colleague, theirs, "qualified", 1).status_code == 200

    def test_administration(self, rates, admin):
        rates(user="3/min")
        noisy, other = signed_in(admin), signed_in(AdminFactory())
        url = "/api/v1/admin/users"
        assert [noisy.get(url).status_code for _ in range(3)] == [200] * 3
        assert refused(noisy.get(url))
        assert other.get(url).status_code == 200


@pytest.mark.usefixtures("ai_on")
def test_asking_arkray(rates, user_a, user_b):
    """Its own scope: asking is limited, reading Ask's status isn't."""
    rates(ask="2/min")
    url = "/api/v1/workspaces/me/ask"
    with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
        spammer, colleague = signed_in(user_a), signed_in(user_b)

        def ask(client: APIClient) -> Any:
            return client.post(url, {"question": "pipeline value"}, format="json")

        assert [ask(spammer).status_code for _ in range(2)] == [201, 201]
        assert refused(ask(spammer))
        assert spammer.get(url).status_code == 200
        assert ask(colleague).status_code == 201


# --- sign-in and password resets: durable, PostgreSQL-backed ----------------------------------
def test_guessing_a_password_locks_only_unrecognised_browsers_out_of_that_account(user_a, user_b):
    """An attacker (any address) can lock the account for unrecognised browsers, the victim
    on a new browser included, for a bounded time (60 s, doubling to at most 15 minutes);
    never the browser the victim signs in from, and never another account."""
    victim = APIClient()
    assert login(victim, user_a.email).status_code == 200  # this browser is now trusted
    attacker = APIClient()
    for _ in range(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD):
        assert login(attacker, user_a.email, "wrong-password-1").status_code == 400
    assert refused(login(attacker, user_a.email))  # even with the right password
    assert refused(login(APIClient(), user_a.email))  # the victim on a new browser too
    assert login(victim, user_a.email).status_code == 200
    assert login(APIClient(), user_b.email).status_code == 200  # same address, other account


def test_password_reset_requests_from_one_address(settings, user_a, user_b):
    settings.PASSWORD_RESET_SOURCE_LIMIT_PER_HOUR = 3

    def reset(email: str, address: str) -> Any:
        return APIClient().post(
            "/api/v1/auth/password-reset", {"email": email}, format="json", REMOTE_ADDR=address
        )

    assert [reset(user_a.email, "203.0.113.5").status_code for _ in range(3)] == [202] * 3
    assert refused(reset(user_b.email, "203.0.113.5"))  # the address, whatever the account
    assert reset(user_b.email, "198.51.100.7").status_code == 202
    assert reset(user_a.email, "198.51.100.7").status_code == 202


# --- Redis down -------------------------------------------------------------------------------
@pytest.fixture
def redis_down() -> Iterator[None]:
    cache_breaker.reset()
    with override_settings(CACHES=DEAD_REDIS_CACHE):
        yield
    cache_breaker.reset()


@pytest.mark.usefixtures("redis_down")
def test_with_redis_down_request_rates_fail_open(rates, user_a):
    """The documented policy (docs/reliability.md): the CRM keeps answering without its
    cache, so no DRF rate applies; sign-in budgets don't depend on Redis
    (test_outages.py::test_brute_force_protection_does_not_switch_off)."""
    rates(user="1/min", search="1/min")
    client = signed_in(user_a)
    lead = str(LeadFactory(owner=user_a).pk)
    note = {"type": "note", "lead": lead, "description": "x"}
    assert [client.get("/api/v1/workspaces/me/search?q=abc").status_code for _ in range(3)] == [
        200
    ] * 3
    assert [
        client.post("/api/v1/workspaces/me/activities", note, format="json").status_code
        for _ in range(3)
    ] == [201] * 3
