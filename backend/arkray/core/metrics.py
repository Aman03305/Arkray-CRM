"""Operational gauges for the metrics endpoint (Phase 10; docs/observability.md#metrics).

Computed when scraped, from PostgreSQL and the broker: the outbox backlog and its age per
queue, dead events, database connections, the broker's queue lengths, the cache. Labels are
queue names, statuses and connection states only: never a user, a record or any text.
Request counts, latency and 5xx rates come from the structured access log (one line per
request with route, status and duration), not from in-process counters, which multiple
gunicorn processes would each keep separately.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import connection

logger = logging.getLogger(__name__)

# Celery queues (config/settings: CELERY_TASK_ROUTES and the workers' --queues).
QUEUES = ("outbox", "default", "email", "ai_index", "ai")
# A scrape has to answer while things fail, which is when it matters. Socket timeouts don't
# bound a connection attempt's DNS lookup: with Redis's container stopped each probe stalled
# about 4 s, and the Phase 10 drill's 5 s scrapes timed out. So the Redis probes run with a
# deadline; one that overruns reports its dependency down and finishes in the background.
PROBE_TIMEOUT_S = 2.0
_probes = ThreadPoolExecutor(max_workers=2, thread_name_prefix="metrics-probe")


def bounded[T](probe: Callable[[], T]) -> T:
    """`probe()` within PROBE_TIMEOUT_S, or TimeoutError (it then finishes in the background)."""
    return _probes.submit(probe).result(timeout=PROBE_TIMEOUT_S)


# Kombu's Redis transport keeps one list per priority step besides the plain one.
_PRIORITY_SUFFIXES = ("", "\x06\x163", "\x06\x166", "\x06\x169")


@dataclass
class Gauge:
    name: str
    help: str
    samples: list[tuple[dict[str, str], float]] = field(default_factory=list)

    def add(self, value: float, **labels: str) -> None:
        self.samples.append((labels, float(value)))


def outbox() -> list[Gauge]:
    events = Gauge(
        "arkray_outbox_events", "Outbox events waiting, being processed or dead, per queue."
    )
    age = Gauge(
        "arkray_outbox_oldest_due_seconds",
        "Age of the oldest pending outbox event that is due (0 when none), per queue.",
    )
    # Handed to the broker but taken by no worker: a worker that starts an event moves it to
    # the short running lease, so an in-flight event whose lease ends later than that is
    # still waiting since its dispatch. With a queue's workers down, events sat here (not
    # pending) for the 30-minute dispatch lease, invisible to every other figure
    # (whole-software audit).
    undelivered = Gauge(
        "arkray_outbox_oldest_undelivered_seconds",
        "Seconds since the oldest in-flight event was handed to the broker without a worker"
        " starting it (0 when none), per queue.",
    )
    with connection.cursor() as cursor:
        # One branch per status, so each reads its own partial index; a single
        # `status IN (...)` scanned the whole table, done rows included (Phase 10 review).
        cursor.execute(
            "(SELECT queue, 'pending', count(*) FROM core_outbox_event"
            " WHERE status = 'pending' GROUP BY 1)"
            " UNION ALL (SELECT queue, 'in_flight', count(*) FROM core_outbox_event"
            " WHERE status = 'in_flight' GROUP BY 1)"
            " UNION ALL (SELECT queue, 'dead', count(*) FROM core_outbox_event"
            " WHERE status = 'dead' GROUP BY 1)"
        )
        found = {(queue, status): n for queue, status, n in cursor.fetchall()}
        cursor.execute(
            "SELECT queue, EXTRACT(EPOCH FROM now() - min(available_at))"
            " FROM core_outbox_event WHERE status = 'pending' AND available_at <= now()"
            " GROUP BY 1"
        )
        oldest = {queue: float(seconds) for queue, seconds in cursor.fetchall()}
        cursor.execute(
            "SELECT queue, max(%s - EXTRACT(EPOCH FROM locked_until - now()))"
            " FROM core_outbox_event WHERE status = 'in_flight'"
            " AND locked_until > now() + make_interval(secs => %s) GROUP BY 1",
            [settings.OUTBOX_DISPATCH_LEASE_SECONDS, settings.OUTBOX_LEASE_SECONDS],
        )
        waiting = {queue: max(float(seconds), 0.0) for queue, seconds in cursor.fetchall()}
    for queue in sorted({*QUEUES, *(q for q, _ in found)}):
        for status in ("pending", "in_flight", "dead"):
            events.add(found.get((queue, status), 0), queue=queue, status=status)
        age.add(round(oldest.get(queue, 0.0), 1), queue=queue)
        undelivered.add(round(waiting.get(queue, 0.0), 1), queue=queue)
    return [events, age, undelivered]


def database() -> list[Gauge]:
    connections = Gauge(
        "arkray_db_connections", "Connections to this database by state (pg_stat_activity)."
    )
    server = Gauge(
        "arkray_db_server_connections",
        "Client connections to the whole server, every database and state: what"
        " max_connections limits.",
    )
    limit = Gauge("arkray_db_max_connections", "PostgreSQL max_connections.")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(state, 'unknown'), count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() GROUP BY 1"
        )
        rows = dict(cursor.fetchall())
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'"
        )
        server.add(float(cursor.fetchone()[0]))
        cursor.execute("SHOW max_connections")
        limit.add(float(cursor.fetchone()[0]))
    for state in ("active", "idle", "idle in transaction", "idle in transaction (aborted)"):
        connections.add(rows.pop(state, 0), state=state)
    connections.add(sum(rows.values()), state="other")
    return [connections, server, limit]


def broker() -> list[Gauge]:
    up = Gauge("arkray_broker_up", "1 when the broker answered this scrape.")
    lengths = Gauge("arkray_broker_queue_length", "Messages waiting in the broker, per queue.")

    def lengths_now() -> list[int]:
        import redis

        client: Any = redis.Redis.from_url(
            settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1
        )
        pipe = client.pipeline()
        for queue in QUEUES:
            for suffix in _PRIORITY_SUFFIXES:
                pipe.llen(queue + suffix)
        counts: list[int] = pipe.execute()
        return counts

    try:
        counts = bounded(lengths_now)
    except Exception:  # noqa: BLE001 — an unreachable broker is a value, not an error
        logger.warning("metrics_broker_unavailable")
        up.add(0)
        return [up]
    up.add(1)
    per_queue = len(_PRIORITY_SUFFIXES)
    for index, queue in enumerate(QUEUES):
        lengths.add(sum(counts[index * per_queue : (index + 1) * per_queue]), queue=queue)
    return [up, lengths]


def cache_up() -> list[Gauge]:
    gauge = Gauge("arkray_cache_up", "1 when the cache answered this scrape.")

    def answered() -> bool:
        cache.set("metrics:probe", "1", timeout=5)
        return bool(cache.get("metrics:probe") == "1")

    try:
        gauge.add(1 if bounded(answered) else 0)
    except Exception:  # noqa: BLE001
        gauge.add(0)
    return [gauge]


def render(gauges: list[Gauge]) -> str:
    """Prometheus text exposition format (version 0.0.4)."""
    lines: list[str] = []
    for gauge in gauges:
        lines.append(f"# HELP {gauge.name} {gauge.help}")
        lines.append(f"# TYPE {gauge.name} gauge")
        for labels, value in gauge.samples:
            rendered = ",".join(f'{key}="{_escape(val)}"' for key, val in labels.items())
            number = int(value) if value.is_integer() else value
            lines.append(
                f"{gauge.name}{{{rendered}}} {number}" if rendered else f"{gauge.name} {number}"
            )
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
