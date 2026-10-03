"""The cache must degrade to "miss" instantly during a Redis outage, never stall requests."""

import logging
import time

import pytest
from django_redis.cache import RedisCache
from redis import ConnectionPool
from redis.connection import Connection
from redis.exceptions import ConnectionError as RedisConnectionError

from arkray.core.redis import FailFastConnection, cache_breaker, fail_fast_pool_kwargs


@pytest.fixture(autouse=True)
def closed_breaker():
    cache_breaker.reset()
    yield
    cache_breaker.reset()


def test_breaker_skips_connection_attempts_after_a_failure(monkeypatch):
    attempts = []

    def failing_connect(self):
        attempts.append(1)
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(Connection, "connect", failing_connect)
    conn = FailFastConnection(host="redis.invalid", port=6379)
    with pytest.raises(RedisConnectionError, match="refused"):
        conn.connect()
    for _ in range(5):
        with pytest.raises(RedisConnectionError, match="circuit open"):
            conn.connect()
    assert attempts == [1]


def test_breaker_closes_after_cooldown(monkeypatch):
    cache_breaker.trip()
    assert cache_breaker.is_open()
    monkeypatch.setattr(cache_breaker, "_open_until", time.monotonic() - 1)
    assert not cache_breaker.is_open()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://cache:6379/1", "FailFastConnection"),
        ("rediss://cache:6380/0", "FailFastSSLConnection"),
        ("unix:///var/run/redis.sock?db=1", "FailFastUnixConnection"),
    ],
)
def test_connection_class_matches_the_url_scheme(url, expected):
    kwargs = fail_fast_pool_kwargs(url)
    pool = ConnectionPool.from_url(url, **kwargs)
    assert type(pool.make_connection()).__name__ == expected


def test_an_outage_logs_once_per_cooldown_not_per_operation(caplog):
    with caplog.at_level(logging.WARNING, logger="arkray.core.redis"):
        for _ in range(10):
            cache_breaker.trip()
    assert [r.getMessage() for r in caplog.records] == ["cache_circuit_opened"]


def test_unreachable_cache_is_a_fast_miss():
    """End to end through django-redis: 50 operations against a dead Redis stay fast."""
    url = "redis://127.0.0.1:1/0"  # nothing listens on port 1
    cache = RedisCache(
        url,
        {
            "OPTIONS": {
                "SOCKET_CONNECT_TIMEOUT": 1,
                "SOCKET_TIMEOUT": 1,
                "IGNORE_EXCEPTIONS": True,
                "CONNECTION_POOL_KWARGS": fail_fast_pool_kwargs(url),
            }
        },
    )
    started = time.monotonic()
    for i in range(25):
        cache.set(f"k{i}", "v")
        assert cache.get(f"k{i}") is None
    assert time.monotonic() - started < 2.0
    assert cache_breaker.is_open()
