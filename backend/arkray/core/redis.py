"""Fail-fast Redis connections for the cache.

The cache is an optimisation, so it must never make the CRM slow. During a Redis outage a
single connection attempt can take seconds (DNS resolution is not bounded by socket
timeouts), and DRF throttling touches the cache on every API request. So cache connections:

- never retry (redis-py's retry/backoff is disabled),
- share a per-process circuit breaker: after a connection failure, every cache connection
  attempt fails instantly for COOLDOWN_SECONDS. django-redis (IGNORE_EXCEPTIONS) turns those
  failures into cache misses, and the CRM carries on at full speed, and
- find out whether Redis is back on a background thread, never in a request: when a
  cool-down runs out, one thread per process tries to connect while requests go on failing
  fast; its success closes the circuit, its failure starts a longer cool-down.

Only the cache uses this. The Celery broker has its own connection handling, and outbox work
waits durably in PostgreSQL while the broker is down.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from redis.backoff import NoBackoff
from redis.connection import Connection, SSLConnection, UnixDomainSocketConnection
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

COOLDOWN_SECONDS = 15.0
MAX_COOLDOWN_SECONDS = 120.0

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Per process: after a failure, fail instantly for a cool-down; each failure in a row
    doubles it, up to `max_cooldown_seconds`, and the first success resets it.

    Phase 10 drill: the probe that ends a cool-down stalls its request until it fails (with
    Redis's container stopped, about 4 s for the DNS lookup alone; about 10 s for a broker
    publish), and every gunicorn process probes on its own. At a fixed 15 s that took a
    quarter of each process's time for as long as an outage lasted.

    Callers that can check the dependency without doing real work (the cache) hand that
    check to `probe()`, which runs it on a background thread; the stall then costs no request
    at all (2026-10-06: with the dev Redis crash-looping, every process still held a request
    about 4 s every 2 minutes, which made pages feel randomly slow)."""

    def __init__(
        self,
        cooldown_seconds: float,
        max_cooldown_seconds: float | None = None,
        *,
        event: str = "cache_circuit_opened",
    ) -> None:
        self._base = cooldown_seconds
        self._max = max(max_cooldown_seconds or cooldown_seconds, cooldown_seconds)
        self._cooldown = cooldown_seconds
        self._failures = 0  # in a row
        self._event = event
        self._open_until = 0.0
        self._lock = threading.Lock()
        self._probe_thread: threading.Thread | None = None

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def recovering(self) -> bool:
        """A cool-down after a failure has run out: the next attempt finds out whether the
        dependency is back."""
        return self._failures > 0 and not self.is_open()

    def probe(self, check: Callable[[], object]) -> None:
        """Run `check` on a background thread, one at a time per process: if it returns, the
        circuit closes; if it raises, it opens again for a longer cool-down. Meanwhile
        `recovering()` stays true, so attempts keep failing fast instead of waiting."""
        with self._lock:
            if self._probe_thread is not None:
                return
            thread = threading.Thread(
                target=self._run_probe, args=(check,), name="circuit-probe", daemon=True
            )
            self._probe_thread = thread
        try:
            thread.start()
        except RuntimeError:  # no thread to be had: a later attempt tries again
            with self._lock:
                self._probe_thread = None

    def _run_probe(self, check: Callable[[], object]) -> None:
        try:
            check()
        except Exception:  # noqa: BLE001 — any failure means "still unavailable"
            self.trip()
        else:
            self.succeeded()
        finally:
            with self._lock:
                self._probe_thread = None

    def trip(self) -> None:
        with self._lock:
            now = time.monotonic()
            was_open = now < self._open_until
            if not was_open:  # the probe after a cool-down failed too: wait longer
                # Doubled from the current value, never from an exponent that keeps growing
                # (2**1024 can't become a float: after ~34 h of outage that raised on every
                # cache call; Phase 10 review).
                self._cooldown = self._base if not self._failures else self._cooldown * 2
                self._cooldown = min(self._cooldown, self._max)
                self._failures = min(self._failures + 1, 1_000_000)
            self._open_until = now + self._cooldown
        if not was_open:
            # One line per outage window per process, not one per operation.
            logger.warning(self._event, extra={"cooldown_s": self._cooldown})

    def succeeded(self) -> None:
        if self._failures:  # cheap when healthy: no lock unless recovering
            with self._lock:
                self._failures = 0
                self._cooldown = self._base

    def reset(self) -> None:
        with self._lock:
            self._open_until = 0.0
            self._failures = 0
            self._cooldown = self._base


cache_breaker = CircuitBreaker(COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS)


class _FailFastMixin:
    def connect(self) -> None:
        if cache_breaker.is_open():
            raise RedisConnectionError("Redis cache unavailable (circuit open).")
        if cache_breaker.recovering():
            # Whether Redis is back is found out off the request: while it is still down
            # that can take seconds (DNS), and this request must not wait for it.
            cache_breaker.probe(self._probe_connect)
            raise RedisConnectionError("Redis cache unavailable (checking).")
        try:
            super().connect()  # type: ignore[misc]
        except (RedisConnectionError, RedisTimeoutError, OSError):
            cache_breaker.trip()
            raise
        cache_breaker.succeeded()

    def _probe_connect(self) -> None:
        # redis-py's own socket set-up for this connection (address, TLS, timeouts). It only
        # reads the connection's settings, so it is safe beside the pool's use of the object.
        self._connect().close()  # type: ignore[attr-defined]


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
