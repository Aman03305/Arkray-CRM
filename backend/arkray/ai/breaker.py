"""The circuit breaker around the language model (docs/reliability.md).

AI_BREAKER_FAILURES provider failures in a row (timeouts, 5xx, 429, connection errors)
open the breaker for AI_BREAKER_COOLDOWN_S: questions are then answered without the model
(router or retrieval) instead of each waiting for a timeout. State is shared through the
cache (Redis) so every ai worker sees it, with an in-process copy that keeps working when
Redis is down (then each process trips on its own failures).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_OPEN_KEY = "ai:breaker:open"
_FAILURES_KEY = "ai:breaker:failures"
_lock = threading.Lock()
_local = {"failures": 0, "open_until": 0.0}


def is_open() -> bool:
    if time.monotonic() < _local["open_until"]:
        return True
    try:
        return bool(cache.get(_OPEN_KEY))
    except Exception:  # noqa: BLE001 — the cache is optional
        return False


def record_success() -> None:
    with _lock:
        _local["failures"] = 0
    with contextlib.suppress(Exception):  # the cache is optional
        cache.delete(_FAILURES_KEY)


def record_failure(reason: str) -> None:
    """Count a provider failure: in the shared counter (which any worker's success resets),
    or, only while the cache is unavailable, in this process's own."""
    cooldown = settings.AI_BREAKER_COOLDOWN_S
    shared: int | None = None
    with contextlib.suppress(Exception):
        cache.add(_FAILURES_KEY, 0, timeout=cooldown)
        shared = int(cache.incr(_FAILURES_KEY))
    with _lock:
        if shared is None:
            _local["failures"] += 1
            failures = _local["failures"]
        else:
            _local["failures"] = 0
            failures = shared
    if failures >= settings.AI_BREAKER_FAILURES:
        with _lock:
            was_open = time.monotonic() < _local["open_until"]
            _local["open_until"] = time.monotonic() + cooldown
            _local["failures"] = 0
        with contextlib.suppress(Exception):
            cache.set(_OPEN_KEY, 1, timeout=cooldown)
            cache.delete(_FAILURES_KEY)
        if not was_open:
            logger.error("ai_breaker_opened", extra={"reason": reason, "cooldown_s": cooldown})


def reset() -> None:
    """Close the breaker (tests, operators)."""
    with _lock:
        _local["failures"] = 0
        _local["open_until"] = 0.0
    with contextlib.suppress(Exception):
        cache.delete_many([_OPEN_KEY, _FAILURES_KEY])
