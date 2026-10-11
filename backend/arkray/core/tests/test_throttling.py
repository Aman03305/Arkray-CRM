"""The API's rate limits (core.throttling, R127): who a client is (the trusted client
address, an IPv6 client by its /64, never a forwarded header the client wrote) and what
happens while the cache (Redis) is unavailable (a bounded per-process count, never "no
limit" and never "refuse everyone")."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings
from django.test import override_settings
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from arkray.core import throttling
from arkray.core.middleware import client_ip
from arkray.core.redis import cache_breaker, fail_fast_pool_kwargs

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


class ThreeAMinute(throttling.AnonRateThrottle):
    rate = "3/min"


class Probe(APIView):
    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]
    throttle_classes = [ThreeAMinute]

    def get(self, request: Any) -> Response:
        return Response({"ok": True})


def call(remote: str = "203.0.113.5", forwarded: str | None = None) -> int:
    extra: dict[str, Any] = {"REMOTE_ADDR": remote}
    if forwarded is not None:
        extra["HTTP_X_FORWARDED_FOR"] = forwarded
    request = APIRequestFactory().get("/probe", **extra)
    return int(Probe.as_view()(request).status_code)


@pytest.fixture(autouse=True)
def _fresh_counts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from django.core.cache import cache

    cache.clear()
    throttling.local_buckets.clear()
    cache_breaker.reset()
    monkeypatch.setattr(throttling, "_last_fallback_log", 0.0)
    yield
    throttling.local_buckets.clear()
    cache_breaker.reset()


@pytest.fixture
def redis_down() -> Iterator[None]:
    with override_settings(CACHES=DEAD_REDIS_CACHE):
        yield


# --- keys ------------------------------------------------------------------------------------
class TestAddressKey:
    @pytest.mark.parametrize(
        ("address", "key"),
        [
            ("203.0.113.5", "203.0.113.5"),
            ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::"),
            ("2001:db8:1:2::1", "2001:db8:1:2::"),
            ("2001:0DB8:0001:0002:ffff:ffff:ffff:ffff", "2001:db8:1:2::"),
            ("2001:db8::1", "2001:db8::"),
            ("2001:db8::5:1:2:3:4", "2001:db8:0:5::"),
            ("::ffff:198.51.100.7", "198.51.100.7"),
            ("::1", "::"),
            ("fe80::1", "fe80::"),
        ],
    )
    def test_ipv4_as_is_and_ipv6_by_its_64(self, address, key):
        assert throttling.address_key(address) == key

    @pytest.mark.parametrize("value", [None, "", "not-an-ip", "2001:db8::1/64", "1.2.3"])
    def test_anything_else_is_no_key(self, value):
        assert throttling.address_key(value) is None


class TestClientIpCantBeSpoofed:
    """X-Forwarded-For is read from the right: each trusted proxy appends the address it
    saw, so only the last TRUSTED_PROXY_COUNT entries were written by someone trusted."""

    @override_settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_ADDRESSES=["10.0.0.2"])
    @pytest.mark.parametrize(
        "forwarded",
        [
            "198.51.100.7",
            "6.6.6.6, 198.51.100.7",
            "6.6.6.6,7.7.7.7,  198.51.100.7",
            "unknown, 198.51.100.7",
            ", 198.51.100.7",
        ],
    )
    def test_entries_the_client_prepends_are_ignored(self, forwarded):
        request = APIRequestFactory().get(
            "/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR=forwarded
        )
        assert client_ip(request) == "198.51.100.7"

    @override_settings(TRUSTED_PROXY_COUNT=2, TRUSTED_PROXY_ADDRESSES=["10.0.0.2"])
    def test_two_proxies_read_the_second_entry_from_the_right(self):
        request = APIRequestFactory().get(
            "/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.7, 10.0.0.9"
        )
        assert client_ip(request) == "198.51.100.7"

    @override_settings(TRUSTED_PROXY_COUNT=2, TRUSTED_PROXY_ADDRESSES=["10.0.0.2"])
    def test_fewer_entries_than_proxies_never_trusts_the_clients_entry(self):
        """DRF's own get_ident takes the leftmost entry then (the client's); here the peer
        address is used, which a client can't choose."""
        request = APIRequestFactory().get(
            "/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="6.6.6.6"
        )
        assert client_ip(request) == "10.0.0.2"

    @override_settings(TRUSTED_PROXY_COUNT=0)
    def test_without_trusted_proxies_the_header_is_ignored(self):
        request = APIRequestFactory().get(
            "/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="6.6.6.6"
        )
        assert client_ip(request) == "10.0.0.2"

    @override_settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_ADDRESSES=["10.0.0.2"])
    def test_ports_and_brackets_from_a_proxy_are_tolerated(self):
        for forwarded, expected in [
            ("198.51.100.7:51234", "198.51.100.7"),
            ("[2001:db8::7]:443", "2001:db8::7"),
            ("2001:DB8::7", "2001:db8::7"),
        ]:
            request = APIRequestFactory().get(
                "/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR=forwarded
            )
            assert client_ip(request) == expected


class TestOnlyTheProxyIsBelieved:
    """Adversarial review P2-2: X-Forwarded-For is read only when the peer that connected is
    one of TRUSTED_PROXY_ADDRESSES; anything else that reaches the app server directly
    (another container, the web tier) is keyed by its own address."""

    @override_settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_ADDRESSES=["10.252.17.10"])
    def test_a_header_from_another_peer_is_ignored(self):
        request = APIRequestFactory().get(
            "/", REMOTE_ADDR="10.9.9.9", HTTP_X_FORWARDED_FOR="198.51.100.7"
        )
        assert client_ip(request) == "10.9.9.9"

    @override_settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_ADDRESSES=["10.252.17.10"])
    def test_the_proxys_header_is_honoured(self):
        request = APIRequestFactory().get(
            "/", REMOTE_ADDR="10.252.17.10", HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.7"
        )
        assert client_ip(request) == "198.51.100.7"

    @override_settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_ADDRESSES=["10.252.17.10"])
    def test_rotating_the_header_from_another_peer_buys_nothing(self):
        """The review's probe: 20 requests at 3/min from 10.9.9.9, each naming a new client,
        were all answered 200."""
        codes = [call("10.9.9.9", f"203.0.113.{n}") for n in range(20)]
        assert codes == [200] * 3 + [429] * 17

    @override_settings(
        TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_ADDRESSES=["10.0.0.0/24", "2001:db8:aa::/64"]
    )
    @pytest.mark.parametrize(
        ("peer", "trusted"),
        [
            ("10.0.0.7", True),
            ("10.0.1.7", False),
            ("2001:db8:aa::5", True),
            ("2001:db8:aa:1::5", False),
            ("::ffff:10.0.0.7", True),
            ("", False),
            ("garbage", False),
        ],
    )
    def test_ranges_of_both_families(self, peer, trusted):
        request = APIRequestFactory().get(
            "/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_FOR="198.51.100.7"
        )
        assert (client_ip(request) == "198.51.100.7") is trusted

    @override_settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_ADDRESSES=[])
    def test_no_proxy_addresses_trusts_no_header(self):
        request = APIRequestFactory().get(
            "/", REMOTE_ADDR="10.252.17.10", HTTP_X_FORWARDED_FOR="198.51.100.7"
        )
        assert client_ip(request) == "10.252.17.10"


class TestThrottleKeys:
    def test_the_default_throttles_are_these(self):
        assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] == [
            "arkray.core.throttling.AnonRateThrottle",
            "arkray.core.throttling.UserRateThrottle",
        ]

    def test_one_ipv6_64_shares_one_budget(self):
        codes = [call(f"2001:db8:aa:bb:{n:x}::1") for n in range(4)]
        assert codes == [200, 200, 200, 429]
        assert call("2001:db8:aa:bc::1") == 200  # the neighbouring /64

    @override_settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_ADDRESSES=["10.0.0.2"])
    def test_a_rotating_forwarded_header_buys_no_fresh_budget(self):
        codes = [call("10.0.0.2", f"6.6.6.{n}, 198.51.100.7") for n in range(4)]
        assert codes == [200, 200, 200, 429]
        assert call("10.0.0.2", "198.51.100.8") == 200


# --- Redis down --------------------------------------------------------------------------------
@pytest.mark.usefixtures("redis_down")
class TestRedisDown:
    def test_each_process_counts_in_memory(self, caplog):
        assert [call() for _ in range(4)] == [200, 200, 200, 429]
        assert call("198.51.100.8") == 200  # another client keeps its own budget
        assert "throttle_local_fallback" in caplog.text

    def test_the_refusal_says_when_to_retry(self):
        for _ in range(3):
            call()
        request = APIRequestFactory().get("/probe", REMOTE_ADDR="203.0.113.5")
        response = Probe.as_view()(request)
        assert response.status_code == 429
        assert 1 <= int(response["Retry-After"]) <= 21  # one token every 20 s


def test_the_count_moves_back_to_the_cache_when_it_returns():
    with override_settings(CACHES=DEAD_REDIS_CACHE):
        assert [call() for _ in range(4)] == [200, 200, 200, 429]
    cache_breaker.reset()
    # The cache is back: its count applies again (it never saw the outage's requests, so it
    # starts afresh), bounded by the configured rate like before.
    assert [call() for _ in range(4)] == [200, 200, 200, 429]


def test_a_working_cache_never_uses_the_local_buckets():
    assert [call() for _ in range(4)] == [200, 200, 200, 429]
    assert len(throttling.local_buckets) == 0


class TestLocalBuckets:
    def test_capacity_then_refill(self):
        buckets = throttling.LocalBuckets()
        assert [buckets.take("k", 3, 60, 0.0) for _ in range(3)] == [None] * 3
        assert buckets.take("k", 3, 60, 0.0) == pytest.approx(20.0)
        assert buckets.take("k", 3, 60, 20.0) is None  # one token back after 20 s
        assert buckets.take("k", 3, 60, 20.0) is not None

    def test_bounded_memory(self):
        buckets = throttling.LocalBuckets(max_keys=100)
        for n in range(1_000):
            buckets.take(f"k{n}", 3, 60, 0.0)
        assert len(buckets) == 100

    def test_the_least_recently_used_key_goes_first(self):
        buckets = throttling.LocalBuckets(max_keys=2)
        for _ in range(3):
            buckets.take("busy", 3, 60, 0.0)
        buckets.take("other", 3, 60, 0.0)
        buckets.take("busy", 3, 60, 0.0)  # refused, and now the most recent
        buckets.take("third", 3, 60, 0.0)  # drops "other"
        assert buckets.take("busy", 3, 60, 0.0) is not None
