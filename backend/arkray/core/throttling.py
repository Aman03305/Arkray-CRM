"""The API's request-rate limits (DRF throttles), as Arkray needs them (R127).

DRF's own throttles have two gaps this module closes:

- **Who a client is.** DRF keys an anonymous client by its own reading of X-Forwarded-For
  (with fewer entries than NUM_PROXIES it takes the leftmost one, which the client wrote)
  and by the full address. Here the key is `core.middleware.client_ip` (the trusted-proxy
  aware address the audit trail and the durable sign-in limits use), and an IPv6 client
  counts as its /64: one subscriber line or host usually holds a whole /64, so keying by
  the full address let one machine take 2**64 fresh budgets.
- **Redis down.** The counts live in the cache (Redis), whose errors are swallowed
  (IGNORE_EXCEPTIONS): DRF then reads every count as zero and allows everything. Here a
  cache failure switches the throttle to a per-process, in-memory token bucket with the
  same rate for as long as the cache is unavailable. It is bounded (BUCKETS_PER_PROCESS
  keys, least recently used dropped first) and never fails closed for everyone: the CRM
  keeps answering during a Redis outage, as it must, and nobody gets unlimited work out of
  it. Each process counts on its own, so the effective limit during an outage is the
  configured rate times the number of processes a client's requests reach (gunicorn
  workers x pods); see docs/operations.md#rate-limits-while-redis-is-down.

The durable, PostgreSQL-backed limits (identity.throttling: sign-in failures, password-reset
requests) and the database caps (Ask's pending questions, data exports, upload slots) don't
depend on the cache at all and stay the authority where a guess or a job is expensive.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from django.core.cache import caches
from django.http import HttpRequest
from rest_framework import throttling as drf

from .middleware import client_ip

logger = logging.getLogger(__name__)

IPV6_PREFIX = 64
# In-memory buckets per process while the cache is unavailable: about 100 bytes each.
BUCKETS_PER_PROCESS = 10_000
# One warning per process per this many seconds while the fallback is in use.
FALLBACK_LOG_INTERVAL_S = 60.0
UNKNOWN_CLIENT = "unknown"


def address_key(ip: str | None) -> str | None:
    """The rate-limit key for a client address: IPv4 as is, IPv6 reduced to its /64 (an
    IPv4-mapped IPv6 address counts as the IPv4 address). None if it isn't an address.

    The same rule as identity.throttling.source_key (the durable limits) and the edge
    proxy's `$arkray_client_key` (infrastructure/nginx/arkray.conf)."""
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        network = ipaddress.IPv6Network((address, IPV6_PREFIX), strict=False)
        return str(network.network_address)
    return str(address)


class CacheUnavailable(Exception):
    """The throttle's counts can't be read or written."""


class _StrictCache:
    """The default cache, reporting failures instead of hiding them.

    django-redis's IGNORE_EXCEPTIONS turns every Redis error into a miss (right for the
    rest of the CRM: the cache is an optimisation). A throttle must know the difference
    between "no requests yet" and "can't count", so it talks to django-redis's client,
    below the layer that swallows errors (still through the circuit breaker's fail-fast
    connections, arkray.core.redis). Any other cache backend is called as is."""

    @staticmethod
    def _target() -> Any:
        cache = caches["default"]
        if getattr(cache, "_ignore_exceptions", False) and hasattr(cache, "client"):
            return cache.client
        return cache

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self._target().get(key, default=default)
        except Exception as error:
            raise CacheUnavailable(type(error).__name__) from error

    def set(self, key: str, value: Any, timeout: int | None = None) -> None:
        try:
            self._target().set(key, value, timeout=timeout)
        except Exception as error:
            raise CacheUnavailable(type(error).__name__) from error


class LocalBuckets:
    """Token buckets in this process's memory: `capacity` requests, refilled continuously
    at capacity/duration a second. At most `max_keys` buckets; the least recently used is
    dropped first (a dropped key starts again with a full bucket, which is no more than a
    fresh address gets)."""

    def __init__(self, max_keys: int = BUCKETS_PER_PROCESS) -> None:
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: str, capacity: int, duration: float, now: float) -> float | None:
        """None if a request may go ahead (a token taken), else the seconds until one may."""
        if capacity <= 0:
            return float(duration)
        refill = capacity / duration
        with self._lock:
            tokens, updated = self._buckets.pop(key, (float(capacity), now))
            tokens = min(float(capacity), tokens + max(0.0, now - updated) * refill)
            if tokens >= 1.0:
                tokens -= 1.0
                wait = None
            else:
                wait = (1.0 - tokens) / refill
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
        return wait

    def __len__(self) -> int:
        return len(self._buckets)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


local_buckets = LocalBuckets()
_last_fallback_log = 0.0
_log_lock = threading.Lock()


def _note_fallback(error: CacheUnavailable) -> None:
    global _last_fallback_log
    now = time.monotonic()
    with _log_lock:
        if now - _last_fallback_log < FALLBACK_LOG_INTERVAL_S and _last_fallback_log:
            return
        _last_fallback_log = now
    logger.warning("throttle_local_fallback", extra={"reason": str(error)})


class ResilientRateThrottleMixin(drf.SimpleRateThrottle):
    """A DRF rate throttle keyed by `address_key(client_ip)`, falling back to this
    process's LocalBuckets while the cache is unavailable."""

    cache = _StrictCache()  # type: ignore[assignment]

    def get_ident(self, request: HttpRequest) -> str:
        return address_key(client_ip(request)) or UNKNOWN_CLIENT

    def allow_request(self, request: Any, view: Any) -> bool:
        self.key = None
        self._local_wait: float | None = None
        try:
            return bool(super().allow_request(request, view))
        except CacheUnavailable as error:
            _note_fallback(error)
        key: str | None = getattr(self, "key", None)
        if key is None:  # get_cache_key itself needed the cache: nothing to count on
            return True
        # A rate is set (DRF returned before counting otherwise).
        capacity = int(getattr(self, "num_requests", 0) or 0)
        duration = float(getattr(self, "duration", 1) or 1)
        self._local_wait = local_buckets.take(key, capacity, duration, time.monotonic())
        return self._local_wait is None

    def wait(self) -> float | None:
        if self._local_wait is not None:
            return self._local_wait
        return super().wait()


class AnonRateThrottle(ResilientRateThrottleMixin, drf.AnonRateThrottle):
    """Anonymous requests per client address (IPv6 by /64): API_THROTTLE_ANON."""


class UserRateThrottle(ResilientRateThrottleMixin, drf.UserRateThrottle):
    """Requests per signed-in user (anonymous ones per address): API_THROTTLE_USER."""


class ScopedRateThrottle(ResilientRateThrottleMixin, drf.ScopedRateThrottle):
    """A view's own budget (`throttle_scope`), per user or per address."""
