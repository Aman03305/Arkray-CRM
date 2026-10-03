"""Fail-fast Redis connections for the cache.

The cache is an optimisation, so it must never make the CRM slow. During a Redis outage a
single connection attempt can take seconds (DNS resolution is not bounded by socket
timeouts), and DRF throttling touches the cache on every API request. So cache connections:

- never retry (redis-py's retry/backoff is disabled), and
- share a per-process circuit breaker: after a connection failure, every cache connection
  attempt fails instantly for COOLDOWN_SECONDS. django-redis (IGNORE_EXCEPTIONS) turns those
  failures into cache misses, and the CRM carries on at full speed.

Only the cache uses this. The Celery broker has its own connection handling, and outbox work
waits durably in PostgreSQL while the broker is down.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from redis.backoff import NoBackoff
from redis.connection import Connection, SSLConnection, UnixDomainSocketConnection
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

COOLDOWN_SECONDS = 15.0

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown = cooldown_seconds
        self._open_until = 0.0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def trip(self) -> None:
        with self._lock:
            was_open = time.monotonic() < self._open_until
            self._open_until = time.monotonic() + self._cooldown
        if not was_open:
            # One line per outage window per process, not one per cache operation.
            logger.warning("cache_circuit_opened", extra={"cooldown_s": self._cooldown})

    def reset(self) -> None:
        with self._lock:
            self._open_until = 0.0


cache_breaker = CircuitBreaker(COOLDOWN_SECONDS)


class _FailFastMixin:
    def connect(self) -> None:
        if cache_breaker.is_open():
            raise RedisConnectionError("Redis cache unavailable (circuit open).")
        try:
            super().connect()  # type: ignore[misc]
        except (RedisConnectionError, RedisTimeoutError, OSError):
            cache_breaker.trip()
            raise


class FailFastConnection(_FailFastMixin, Connection):
    pass


class FailFastSSLConnection(_FailFastMixin, SSLConnection):
    pass


class FailFastUnixConnection(_FailFastMixin, UnixDomainSocketConnection):
    pass


def fail_fast_pool_kwargs(url: str) -> dict[str, Any]:
    """CONNECTION_POOL_KWARGS for django-redis: no retries, circuit-broken connections."""
    connection_class: type[Connection] | type[UnixDomainSocketConnection]
    if url.startswith("rediss://"):
        connection_class = FailFastSSLConnection
    elif url.startswith("unix://"):
        connection_class = FailFastUnixConnection
    else:
        connection_class = FailFastConnection
    return {"connection_class": connection_class, "retry": Retry(NoBackoff(), 0)}
