"""Attachment events that leave no durable state, counted for the metrics endpoint
(docs/observability.md#attachment-storage).

Most attachment gauges are computed at scrape time from PostgreSQL (activities.metrics), like
every other gauge. A few things happen without a row to show for them: an upload refused
before a row exists, a download that found storage down or the object gone (the download
path never writes to the database), how long each storage call took. Gunicorn runs several
processes, so in-process counters would each hold a fraction; these live in the shared cache
(Redis in production) instead, in 10-minute windows that expire on their own, and a scrape
sums the last six: "the last hour", the same figure from every process and pod.

Bounded: every label value comes from a fixed list (anything else is counted as "other"),
so the key space is a few hundred keys at most; never a name, key, user or record. Best
effort: with the cache down the counters are lost (the cache fails fast, so a storage call
never waits for it) and the structured log lines remain the record.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable
from typing import Any

from django.core.cache import cache

logger = logging.getLogger(__name__)

WINDOW_S = 600
WINDOWS = 6  # summed by a scrape: the last 50-60 minutes
PREFIX = "attachments:metrics"
RECONCILE_KEY = "attachments:reconcile:last"
# Long enough for "the daily check hasn't run for 48 hours" to be seen as an age, not as a
# missing value.
RECONCILE_TTL_S = 8 * 24 * 3600

UPLOAD_REASONS = ("too_large", "rejected", "storage_error", "timeout", "quota", "other")
DOWNLOAD_REASONS = ("storage_unavailable", "missing")
OPERATIONS = ("save", "open", "delete", "size", "list", "probe")
ERROR_CLASSES = (
    "missing",
    "access_denied",
    "timeout",
    "connection",
    "server_error",
    "no_space",
    "integrity",
    "busy",
    "circuit_open",
    "other",
)
# Seconds; the last bucket is +Inf. Fixed, so every process and pod adds to the same series.
LATENCY_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, math.inf)


def _allowed(value: str, allowed: Iterable[str]) -> str:
    return value if value in allowed else "other"


def _key(window: int, *parts: str) -> str:
    return ":".join((PREFIX, str(window), *parts))


def _bump(*parts: str) -> None:
    key = _key(int(time.time() // WINDOW_S), *parts)
    try:
        try:
            cache.incr(key)
        except ValueError:  # the window's first event
            if not cache.add(key, 1, timeout=WINDOW_S * (WINDOWS + 1)):
                cache.incr(key)
    except Exception:  # noqa: BLE001 — a lost count must never fail the operation
        logger.debug("attachment_metric_lost")


def upload_stored() -> None:
    _bump("upload", "stored", "")


def upload_failed(reason: str) -> None:
    _bump("upload", "failed", _allowed(reason, UPLOAD_REASONS))


def download_failed(reason: str) -> None:
    _bump("download_failed", _allowed(reason, DOWNLOAD_REASONS))


def object_missing() -> None:
    _bump("object_missing")


def storage_call(operation: str, seconds: float, error_class: str | None) -> None:
    """One storage operation: its latency bucket and, if it failed, why."""
    operation = _allowed(operation, OPERATIONS)
    bucket = next(i for i, bound in enumerate(LATENCY_BUCKETS) if seconds <= bound)
    _bump("latency", operation, str(bucket))
    if error_class is not None:
        _bump("error", operation, _allowed(error_class, ERROR_CLASSES))


def series() -> list[tuple[str, ...]]:
    """Every counter that can exist (the scrape reads them all in one round trip)."""
    found: list[tuple[str, ...]] = [("upload", "stored", "")]
    found += [("upload", "failed", reason) for reason in UPLOAD_REASONS]
    found += [("download_failed", reason) for reason in DOWNLOAD_REASONS]
    found.append(("object_missing",))
    found += [("error", op, error) for op in OPERATIONS for error in ERROR_CLASSES]
    found += [("latency", op, str(i)) for op in OPERATIONS for i in range(len(LATENCY_BUCKETS))]
    return found


def last_hour() -> dict[tuple[str, ...], int]:
    """Each counter summed over the last WINDOWS windows. Raises if the cache can't answer
    (the scrape then leaves these gauges out rather than reporting zeros)."""
    current = int(time.time() // WINDOW_S)
    wanted = {
        _key(window, *parts): parts
        for parts in series()
        for window in range(current - WINDOWS + 1, current + 1)
    }
    found = cache.get_many(list(wanted))
    totals: dict[tuple[str, ...], int] = dict.fromkeys(series(), 0)
    for key, value in found.items():
        totals[wanted[key]] += int(value)
    return totals


def publish_reconcile(summary: dict[str, Any]) -> bool:
    """The last full reconciliation's outcome, for the scrape (its age, outcome and
    mismatches). In the cache, not the database: a check run is strictly read-only."""
    try:
        cache.set(RECONCILE_KEY, {**summary, "finished_at": time.time()}, RECONCILE_TTL_S)
        return bool(cache.get(RECONCILE_KEY))
    except Exception:  # noqa: BLE001
        logger.warning("attachment_reconcile_unpublished")
        return False


def last_reconcile() -> dict[str, Any] | None:
    value = cache.get(RECONCILE_KEY)
    return value if isinstance(value, dict) else None
