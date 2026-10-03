"""identity.throttling internals: lockout arithmetic, buckets, device cookies, cost."""

from datetime import timedelta

import pytest
from django.conf import settings
from django.core import signing
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.identity import throttling
from arkray.identity.models import AuthThrottleEvent, ThrottleKind
from arkray.identity.throttling import LoginThrottle, lockout_seconds, login_identifier

pytestmark = pytest.mark.django_db

IDENTIFIER = login_identifier("rahul@example.test")


def failures(count, *, identifier=IDENTIFIER, device="", ip="203.0.113.5", age=timedelta(0)):
    AuthThrottleEvent.objects.bulk_create(
        AuthThrottleEvent(
            kind=ThrottleKind.LOGIN_FAILURE,
            identifier_hash=identifier,
            device_id=device,
            ip_address=ip,
            occurred_at=timezone.now() - age,
        )
        for _ in range(count)
    )


def test_lockouts_double_and_are_capped():
    assert [lockout_seconds(n) for n in range(4, 12)] == [0, 60, 120, 240, 480, 900, 900, 900]


def test_the_identifier_is_keyed_and_canonical():
    assert login_identifier(" Rahul@Example.TEST ") == IDENTIFIER
    assert "rahul" not in IDENTIFIER
    with override_settings(SECRET_KEY="another-secret-key-" + "x" * 40):
        assert login_identifier("rahul@example.test") != IDENTIFIER


class TestCheck:
    def test_allows_below_the_threshold(self):
        failures(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD - 1)
        assert LoginThrottle([IDENTIFIER], "", "203.0.113.5").check() is None

    def test_locks_at_the_threshold(self):
        failures(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD)
        denied = LoginThrottle([IDENTIFIER], "", "203.0.113.5").check()
        assert denied.reason == "account"
        assert 55 <= denied.retry_after <= 60

    def test_failures_outside_the_window_are_forgotten(self):
        failures(
            settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD,
            age=timedelta(seconds=settings.LOGIN_THROTTLE_WINDOW_S + 1),
        )
        assert LoginThrottle([IDENTIFIER], "", None).check() is None

    def test_each_trusted_browser_has_its_own_bucket(self):
        failures(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD)  # untrusted browsers
        assert LoginThrottle([IDENTIFIER], "a" * 32, None).check() is None
        failures(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD, device="b" * 32)
        assert LoginThrottle([IDENTIFIER], "b" * 32, None).check().reason == "account"

    def test_the_source_limit_spans_accounts(self):
        for i in range(settings.LOGIN_SOURCE_FAILURE_THRESHOLD):
            failures(1, identifier=login_identifier(f"u{i}@example.test"))
        assert LoginThrottle([IDENTIFIER], "", "203.0.113.5").check().reason == "source"
        assert LoginThrottle([IDENTIFIER], "", "198.51.100.1").check() is None

    def test_cost_is_bounded_under_attack(self):
        """At most two index-served, LIMITed queries, however much evidence piles up."""
        failures(2000, identifier=login_identifier("sprayed@example.test"))
        failures(2000)
        for identifier in (login_identifier("fresh@example.test"), IDENTIFIER):
            with CaptureQueriesContext(connection) as queries:
                assert LoginThrottle([identifier], "", "203.0.113.5").check() is not None
            assert 1 <= len(queries) <= 2
            assert all("LIMIT" in q["sql"] for q in queries)

    def test_the_account_query_uses_the_partial_index(self):
        failures(50)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            sql, params = (
                AuthThrottleEvent.objects.filter(
                    kind=ThrottleKind.LOGIN_FAILURE, identifier_hash=IDENTIFIER, device_id=""
                )
                .order_by("-occurred_at")
                .values_list("occurred_at")[:10]
                .query.sql_with_params()
            )
            cursor.execute(f"EXPLAIN {sql}", params)
            plan = "\n".join(row[0] for row in cursor.fetchall())
        assert "auth_throttle_account_idx" in plan


class TestDeviceCookie:
    def request_with(self, value):
        request = RequestFactory().post("/api/v1/auth/login")
        request.COOKIES[settings.LOGIN_DEVICE_COOKIE_NAME] = value
        return request

    def issued_cookie(self, identifier=IDENTIFIER, device="c" * 32):
        response = HttpResponse()
        throttling.remember_device(response, identifier, device)
        return response.cookies[settings.LOGIN_DEVICE_COOKIE_NAME].value

    def test_round_trip(self):
        assert (
            throttling.read_device(self.request_with(self.issued_cookie()), [IDENTIFIER])
            == "c" * 32
        )

    def test_is_bound_to_one_account(self):
        cookie = self.issued_cookie(identifier=login_identifier("other@example.test"))
        assert throttling.read_device(self.request_with(cookie), [IDENTIFIER]) == ""

    @pytest.mark.parametrize(
        "value",
        [
            "garbage",
            signing.dumps({"i": IDENTIFIER, "d": "c" * 32}, salt="some-other-salt"),
            signing.dumps({"i": IDENTIFIER, "d": "../../etc"}, salt="arkray.identity.login-device"),
            signing.dumps(["not", "a", "dict"], salt="arkray.identity.login-device"),
        ],
    )
    def test_forged_or_malformed_cookies_are_ignored(self, value):
        assert throttling.read_device(self.request_with(value), [IDENTIFIER]) == ""

    def test_expired_cookies_are_ignored(self):
        cookie = self.issued_cookie()
        with override_settings(LOGIN_DEVICE_COOKIE_AGE_S=-1):
            assert throttling.read_device(self.request_with(cookie), [IDENTIFIER]) == ""


class TestResetRequests:
    def test_limit_per_source(self):
        for _ in range(settings.PASSWORD_RESET_SOURCE_LIMIT_PER_HOUR):
            assert throttling.reserve_reset_request("203.0.113.5") is None
        assert throttling.reserve_reset_request("203.0.113.5").reason == "source"
        assert throttling.reserve_reset_request("198.51.100.1") is None

    def test_unknown_sources_share_one_budget(self):
        """A missing or unparseable address must not mean "no limit" (fail closed)."""
        for _ in range(settings.PASSWORD_RESET_SOURCE_LIMIT_PER_HOUR):
            assert throttling.reserve_reset_request(None) is None
        assert throttling.reserve_reset_request(None).reason == "source"


def test_purge_removes_only_expired_evidence():
    failures(3, age=timedelta(seconds=settings.AUTH_THROTTLE_RETENTION_S + 60))
    failures(2)
    assert throttling.purge_expired() == 3
    assert AuthThrottleEvent.objects.count() == 2
