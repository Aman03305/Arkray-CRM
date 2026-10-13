"""Transactional outbox: durable, bounded, at-least-once background work.

Why: a CRM write (create lead, invite user, move a deal) must succeed even when Redis, the
email server or the AI provider is down. So side effects are never performed inline.
Instead the service writes an `OutboxEvent` *in the same database transaction* as the
business change; if the transaction rolls back, the event never existed.

Flow:
    service  --enqueue()-->  core_outbox_event (PostgreSQL, durable)
    relay    (beat tick on its own "outbox" queue, so it is never stuck behind work)
             claims due events (SKIP LOCKED) with a fresh claim token, dispatches them to
             Celery queues, never more than OUTBOX_MAX_IN_FLIGHT per queue (bounded broker)
    worker   --process_event(id, token)--> runs the registered handler

Rules:
- Exactly one retry layer: the outbox. Handlers must not use Celery retries.
- Delivery is at-least-once; handlers MUST be idempotent.
- Every state transition presents the claim token, so stale or duplicated broker messages
  (requeues after a crash, redeliveries, a publish that "failed" but went through) are no-ops.
- Two leases: a long *dispatch* lease while the message waits in the broker (the message
  expires with it), then a short *running* lease once a worker starts. An expired lease
  returns the event to pending with a new claim on the next relay.
- Failures back off exponentially with jitter; after `max_attempts` the event is marked
  `dead` and logged at ERROR for an operator. Nothing retries forever.
- Raise `PermanentFailure` when retrying cannot help.
- Payloads carry identifiers, not content (the handler reloads current state).
- Fair between users (review of 73ff1e3, M6): each event carries a fairness key, the user whose
  request queued it ('' for system work: beat, commands, the previous release), inherited by
  the follow-up work its handler queues. The relay takes turns between keys instead of
  claiming first-come, first-served (`_claim`), so one account's backlog (50 queued
  re-indexings, a burst of long notes) can't hold everyone else's work behind it. Within one
  key the order stays first-in, first-out; no handler relies on an order across keys (each
  reloads the current state, and retries and several workers reorder events anyway).
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.db.models import Count, F
from django.utils import timezone

from .context import current_correlation_id, get_context, update_context
from .logging import safe_exception_text
from .models import OutboxEvent, OutboxStatus

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 16 * 1024
_ERROR_TEXT_LIMIT = 1000
FAIRNESS_KEY_LENGTH = 64  # OutboxEvent.fairness_key
# The fairness key of the event whose handler is running in this context: the work it queues
# is the same user's (a continuation, the next batch), never a turn of its own as system work.
_running_key: ContextVar[str | None] = ContextVar("arkray_outbox_fairness_key", default=None)

HandlerFunc = Callable[[dict[str, Any]], None]
Dispatcher = Callable[[OutboxEvent], None]


class PermanentFailure(Exception):
    """Raised by a handler when retrying cannot succeed (e.g. the source record is gone)."""


@dataclass(frozen=True, slots=True)
class OutboxHandler:
    topic: str
    func: HandlerFunc
    queue: str
    max_attempts: int
    # Seconds the handler may run (Celery's soft limit; the hard one TIME_LIMIT_GRACE later)
    # when the workers' default (CELERY_TASK_SOFT_TIME_LIMIT, 100 s) is too short for it: a
    # data-subject export (review round 1, QUEUES-1). None: the default.
    time_limit: int | None = None


_HANDLERS: dict[str, OutboxHandler] = {}


TIME_LIMIT_GRACE = 15  # seconds between a handler's soft and hard time limits


def handler(
    topic: str, *, queue: str = "default", max_attempts: int = 8, time_limit: int | None = None
) -> Callable[[HandlerFunc], HandlerFunc]:
    """Register the handler for `topic`. Modules register handlers from AppConfig.ready().
    `time_limit`: seconds it may run instead of the workers' default; its hard limit must end
    before the running lease (OUTBOX_LEASE_SECONDS), or a slow run would be claimed again
    while it still runs."""

    def register(func: HandlerFunc) -> HandlerFunc:
        if topic in _HANDLERS:
            raise ImproperlyConfigured(f"Duplicate outbox handler for topic {topic!r}.")
        if queue not in settings.OUTBOX_MAX_IN_FLIGHT:
            raise ImproperlyConfigured(f"Outbox queue {queue!r} is not configured.")
        if (
            time_limit is not None
            and time_limit + TIME_LIMIT_GRACE >= settings.OUTBOX_LEASE_SECONDS
        ):
            raise ImproperlyConfigured(
                f"Outbox handler {topic!r}: its time limit must end before the running lease."
            )
        _HANDLERS[topic] = OutboxHandler(topic, func, queue, max_attempts, time_limit)
        return func

    return register


# --- producing -----------------------------------------------------------------------------
def current_fairness_key() -> str:
    """Whose work is being queued here: the running event's key inside a handler (its
    follow-up work stays that user's), else the signed-in user of the request, else '' (system
    work: beat, management commands)."""
    running = _running_key.get()
    if running is not None:
        return running
    context = get_context()
    return (context.user_id or "") if context is not None else ""


def enqueue(
    topic: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str = "",
    delay_seconds: float = 0,
    fairness_key: str | None = None,
) -> OutboxEvent | None:
    """Record background work. Call inside the transaction that makes the business change.

    With a `dedupe_key`, the work may be coalesced into an identical event that is pending
    and has never been attempted; then None is returned. That event is row-locked until the
    caller's transaction ends, so the relay (SKIP LOCKED) cannot run it on pre-commit state.
    Coalescing is best-effort: if the candidate is locked by someone else, a new event is
    written — duplicates are harmless because handlers are idempotent. It ignores the
    fairness key: two people's quick edits of one record are still one re-index.

    `fairness_key`: whose turn the work takes (review of 73ff1e3, M6); by default
    `current_fairness_key()`.
    """
    spec = _HANDLERS.get(topic)
    if spec is None:
        raise ImproperlyConfigured(f"No outbox handler registered for topic {topic!r}.")
    payload = payload or {}
    if len(json.dumps(payload).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("Outbox payloads carry identifiers, not content; payload too large.")
    key = current_fairness_key() if fairness_key is None else fairness_key

    with transaction.atomic():  # a savepoint inside the caller's transaction
        if dedupe_key:
            coalesce_into = (
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(topic=topic, dedupe_key=dedupe_key, status=OutboxStatus.PENDING, attempts=0)
                .only("id")
                .first()
            )
            if coalesce_into is not None:
                return None
        return OutboxEvent.objects.create(
            topic=topic,
            queue=spec.queue,
            payload=payload,
            dedupe_key=dedupe_key,
            max_attempts=spec.max_attempts,
            available_at=timezone.now() + timedelta(seconds=delay_seconds),
            correlation_id=current_correlation_id()[:64],
            fairness_key=key[:FAIRNESS_KEY_LENGTH],
        )


def waiting(topic: str, dedupe_key: str) -> bool:
    """Whether work of `topic` with `dedupe_key` is pending (due, or backing off after a
    failure): it will run from its own start. A producer of follow-up work uses it to leave
    its next step to fresh work that redoes it anyway (backpressure; ai.indexing). Work
    already in flight doesn't count: it may be the caller's own event."""
    return OutboxEvent.objects.filter(
        topic=topic, dedupe_key=dedupe_key, status=OutboxStatus.PENDING
    ).exists()


# --- relaying ------------------------------------------------------------------------------
def _dispatch_to_celery(event: OutboxEvent) -> None:
    from .tasks import process_outbox_event

    options: dict[str, Any] = {
        "queue": event.queue,
        # A message still queued when its dispatch lease ends is discarded by the worker;
        # by then the event has been (or will be) re-claimed under a new token.
        "expires": settings.OUTBOX_DISPATCH_LEASE_SECONDS,
    }
    spec = _HANDLERS.get(event.topic)
    if spec is not None and spec.time_limit is not None:  # else the workers' default
        options["soft_time_limit"] = spec.time_limit
        options["time_limit"] = spec.time_limit + TIME_LIMIT_GRACE
    process_outbox_event.apply_async(args=(event.pk, str(event.claim_token)), **options)


def _release(event_ids: list[int]) -> int:
    """Return in-flight events to pending (their claim tokens become invalid)."""
    return OutboxEvent.objects.filter(pk__in=event_ids, status=OutboxStatus.IN_FLIGHT).update(
        status=OutboxStatus.PENDING, locked_until=None, claim_token=None
    )


def relay(*, dispatch: Dispatcher | None = None, now: datetime | None = None) -> int:
    """Claim due events and hand them to workers. Returns the number dispatched."""
    now = now or timezone.now()
    dispatch = dispatch or _dispatch_to_celery

    recovered = OutboxEvent.objects.filter(
        status=OutboxStatus.IN_FLIGHT, locked_until__lt=now
    ).update(status=OutboxStatus.PENDING, locked_until=None, claim_token=None)
    if recovered:
        logger.warning("outbox_leases_recovered", extra={"count": recovered})

    claimed: list[OutboxEvent] = []
    for queue, max_in_flight in settings.OUTBOX_MAX_IN_FLIGHT.items():
        claimed.extend(_claim(queue, max_in_flight, now))

    for index, event in enumerate(claimed):
        try:
            dispatch(event)
        except Exception:  # broker trouble; release the rest and stop
            logger.warning(
                "outbox_dispatch_failed",
                extra={"outbox_event_id": event.pk, "topic": event.topic},
                exc_info=True,
            )
            # Includes the failed one: if its publish actually went through, the message
            # carries a now-invalid token and is ignored.
            _release([e.pk for e in claimed[index:]])
            return index
    return len(claimed)


# The keys with pending work in a queue (a loose index scan: one probe per key, however long a
# key's backlog), then each key's oldest due events, at most `per_key` of them, in index order
# (outbox_pending_fair_idx, core.0008). 'pending' is a literal so the partial index applies.
_FAIR_CANDIDATES = """
WITH RECURSIVE pending_keys(fairness_key) AS (
    (SELECT fairness_key FROM core_outbox_event
      WHERE queue = %(queue)s AND status = 'pending'
      ORDER BY fairness_key LIMIT 1)
    UNION ALL
    SELECT (SELECT e.fairness_key FROM core_outbox_event e
             WHERE e.queue = %(queue)s AND e.status = 'pending'
               AND e.fairness_key > k.fairness_key
             ORDER BY e.fairness_key LIMIT 1)
      FROM pending_keys k
     WHERE k.fairness_key IS NOT NULL
)
SELECT due.id, k.fairness_key, due.available_at
  FROM pending_keys k
 CROSS JOIN LATERAL (
       SELECT e.id, e.available_at FROM core_outbox_event e
        WHERE e.queue = %(queue)s AND e.status = 'pending'
          AND e.fairness_key = k.fairness_key AND e.available_at <= %(now)s
        ORDER BY e.available_at, e.id
        LIMIT %(per_key)s
       ) AS due
 WHERE k.fairness_key IS NOT NULL
"""


def _fair_order(queue: str, now: datetime, capacity: int, in_flight: Counter[str]) -> list[int]:
    """Up to `capacity` due events of the queue, in the order the relay hands them out: keys
    take turns. The k-th waiting event of a key that already has n events in flight is due in
    turn n + k, and among equal turns the older event goes first (review of 73ff1e3, M6). So a
    user whose work arrives behind another user's backlog is claimed within one claim per
    user ahead of it, whose claims already in the broker count against them; a key alone
    still gets every free slot (nothing is held back while there is room), and a key's own
    events keep their first-in, first-out order."""
    with connection.cursor() as cursor:
        cursor.execute(_FAIR_CANDIDATES, {"queue": queue, "now": now, "per_key": capacity})
        rows = cursor.fetchall()
    by_key: dict[str, list[tuple[datetime, int]]] = {}
    for event_id, key, available_at in rows:
        by_key.setdefault(key, []).append((available_at, event_id))
    ranked: list[tuple[int, datetime, int]] = []
    for key, events in by_key.items():
        for turn, (available_at, event_id) in enumerate(sorted(events), start=1):
            ranked.append((in_flight[key] + turn, available_at, event_id))
    ranked.sort()
    return [event_id for _, _, event_id in ranked[:capacity]]


def _claim(queue: str, max_in_flight: int, now: datetime) -> list[OutboxEvent]:
    """Move up to the queue's free capacity of due events to in_flight under a new claim, in
    the fair order (`_fair_order`), which is also the order they are dispatched in.

    The in-flight cap is what keeps broker queues bounded. It is a soft cap: two relays
    racing may briefly exceed it, which is harmless. The candidates are read without locks,
    then locked SKIP LOCKED and re-checked: one claimed by a concurrent relay, or held by a
    producer coalescing into it (enqueue), is left for a later tick.
    """
    with transaction.atomic():
        in_flight: Counter[str] = Counter(
            dict(
                OutboxEvent.objects.filter(queue=queue, status=OutboxStatus.IN_FLIGHT)
                .values_list("fairness_key")
                .annotate(count=Count("id"))
                .order_by()
            )
        )
        capacity = min(max_in_flight - in_flight.total(), settings.OUTBOX_RELAY_BATCH_SIZE)
        if capacity <= 0:
            return []
        order = _fair_order(queue, now, capacity, in_flight)
        if not order:
            return []
        locked = {
            event.pk: event
            for event in OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(pk__in=order, status=OutboxStatus.PENDING, available_at__lte=now)
            .only("id", "topic", "queue", "fairness_key")
        }
        events = [locked[event_id] for event_id in order if event_id in locked]
        if events:
            token = uuid.uuid4()
            lease = now + timedelta(seconds=settings.OUTBOX_DISPATCH_LEASE_SECONDS)
            OutboxEvent.objects.filter(pk__in=[e.pk for e in events]).update(
                status=OutboxStatus.IN_FLIGHT, locked_until=lease, claim_token=token
            )
            for event in events:
                event.claim_token = token
        return events


@dataclass(frozen=True, slots=True)
class Requeued:
    requeued: list[int]
    superseded: list[int]  # the same work is pending or running again under another event
    redacted: list[int]  # the payload was blanked after its retention: nothing left to run


def requeue_dead(event_ids: list[int]) -> Requeued:
    """Dead events back to pending, due now, with a fresh attempt budget: an operator's
    action once the cause is fixed (`manage.py outbox_requeue`; docs/runbooks.md). Safe
    because handlers are idempotent. The last error is kept until the event runs again."""
    requeued: list[int] = []
    superseded: list[int] = []
    redacted: list[int] = []
    chosen: set[tuple[str, str]] = set()  # (topic, dedupe key): the newest dead one runs
    with transaction.atomic():
        dead = (
            OutboxEvent.objects.select_for_update()
            .filter(pk__in=event_ids, status=OutboxStatus.DEAD)
            .order_by("-pk")
        )
        for event in dead:
            work = (event.topic, event.dedupe_key)
            if not event.payload:
                redacted.append(event.pk)
            elif event.dedupe_key and (
                work in chosen
                or OutboxEvent.objects.filter(
                    topic=event.topic,
                    dedupe_key=event.dedupe_key,
                    status__in=[OutboxStatus.PENDING, OutboxStatus.IN_FLIGHT],
                ).exists()
            ):
                superseded.append(event.pk)
            else:
                requeued.append(event.pk)
                chosen.add(work)
        requeued.sort()
        OutboxEvent.objects.filter(pk__in=requeued).update(
            status=OutboxStatus.PENDING,
            attempts=0,
            available_at=timezone.now(),
            locked_until=None,
            claim_token=None,
            finished_at=None,
        )
    logger.warning(
        "outbox_events_requeued",
        extra={
            "requeued": len(requeued),
            "superseded": len(superseded),
            "redacted": len(redacted),
            "event_ids": requeued[:100],
        },
    )
    return Requeued(requeued, superseded, redacted)


def purge_done(*, finished_before: datetime) -> int:
    """Delete done events finished before `finished_before`, in batches (short
    transactions, no long lock), at most OUTBOX_PURGE_MAX_BATCHES per call; the rest waits
    for the next run. Dead events are never purged here."""
    purged = 0
    for _ in range(settings.OUTBOX_PURGE_MAX_BATCHES):
        batch = list(
            OutboxEvent.objects.filter(status=OutboxStatus.DONE, finished_at__lt=finished_before)
            .order_by()
            .values_list("pk", flat=True)[: settings.OUTBOX_PURGE_BATCH]
        )
        if not batch:
            break
        deleted, _ = OutboxEvent.objects.filter(pk__in=batch, status=OutboxStatus.DONE).delete()
        purged += deleted
    return purged


@dataclass(frozen=True, slots=True)
class QueueShares:
    """One queue's pending work by fairness key, for an operator (`outbox_requeue --status`)."""

    queue: str
    pending: int
    keys: int  # users (and system work, '') with pending work
    busiest: list[tuple[str, int]]  # (fairness key, pending events), the largest first


def pending_shares(*, top: int = 5) -> list[QueueShares]:
    """Pending work per queue and key (one grouped read of the pending events' index)."""
    per_key: dict[str, list[tuple[str, int]]] = {}
    for queue, key, count in (
        OutboxEvent.objects.filter(status=OutboxStatus.PENDING)
        .values_list("queue", "fairness_key")
        .annotate(count=Count("id"))
        .order_by()
    ):
        per_key.setdefault(queue, []).append((key, count))
    return [
        QueueShares(
            queue=queue,
            pending=sum(count for _, count in keys),
            keys=len(keys),
            busiest=sorted(keys, key=lambda item: (-item[1], item[0]))[:top],
        )
        for queue, keys in sorted(per_key.items())
    ]


def redact_finished_payloads(topic: str, *, finished_before: datetime) -> int:
    """Blank the payloads of finished (done or dead) events of `topic`.

    For topics whose payload carries personal data (for example a submitted email address):
    the work is over, so the data is no longer needed. Returns the number of rows redacted.
    """
    return (
        OutboxEvent.objects.filter(
            topic=topic,
            status__in=[OutboxStatus.DONE, OutboxStatus.DEAD],
            finished_at__lt=finished_before,
        )
        .exclude(payload={})
        .update(payload={})
    )


# --- processing ----------------------------------------------------------------------------
def backoff_seconds(
    attempt: int, *, base: float = 10, cap: float = 3600, rng: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with "equal jitter": never zero, never synchronised."""
    ceiling = min(cap, base * 2.0 ** max(attempt - 1, 0))
    return ceiling / 2 + rng() * ceiling / 2


_EMAIL = re.compile(r"[^\s'\"<>(),;:@]+@[^\s'\"<>(),;:@]+")


def _describe(exc: BaseException) -> str:
    """An event's last error, without email addresses (an SMTP rejection quotes the
    recipient's, and dead events are kept until resolved: Phase 11 review)."""
    return _EMAIL.sub("[email]", safe_exception_text(exc))[:_ERROR_TEXT_LIMIT]


def _current(event_id: int, claim_token: UUID) -> Any:
    """Queryset matching the event only while this claim still owns it."""
    return OutboxEvent.objects.filter(
        pk=event_id, status=OutboxStatus.IN_FLIGHT, claim_token=claim_token
    )


def _finish(event_id: int, claim_token: UUID, status: OutboxStatus, error: str = "") -> None:
    _current(event_id, claim_token).update(
        status=status,
        finished_at=timezone.now(),
        locked_until=None,
        claim_token=None,
        last_error=error,
    )


def process_event(event_id: int, claim_token: UUID | str) -> None:
    """Run the handler for one claimed event and record the outcome."""
    token = UUID(str(claim_token))
    lease = timezone.now() + timedelta(seconds=settings.OUTBOX_LEASE_SECONDS)
    # Switch to the running lease and count the attempt *before* running, so a handler that
    # crashes the worker process still consumes attempts and eventually goes dead.
    started = _current(event_id, token).update(attempts=F("attempts") + 1, locked_until=lease)
    if not started:
        logger.info("outbox_stale_message_ignored", extra={"outbox_event_id": event_id})
        return

    event = OutboxEvent.objects.get(pk=event_id)
    if event.correlation_id:
        update_context(correlation_id=event.correlation_id)
    log = {"outbox_event_id": event.pk, "topic": event.topic, "attempt": event.attempts}

    spec = _HANDLERS.get(event.topic)
    if spec is None:
        _finish(event.pk, token, OutboxStatus.DEAD, "No handler registered for this topic.")
        logger.error("outbox_event_dead", extra={**log, "reason": "no_handler"})
        return
    if event.attempts > event.max_attempts:
        _finish(event.pk, token, OutboxStatus.DEAD, "Exceeded max attempts (worker crashed?).")
        logger.error("outbox_event_dead", extra={**log, "reason": "attempts_exhausted"})
        return

    # The work the handler queues is this event's user's (current_fairness_key).
    running = _running_key.set(event.fairness_key)
    try:
        spec.func(event.payload)
    except PermanentFailure as exc:
        _finish(event.pk, token, OutboxStatus.DEAD, _describe(exc))
        logger.error("outbox_event_dead", extra={**log, "reason": "permanent_failure"})
        return
    except Exception as exc:  # noqa: BLE001 — every failure is recorded and retried (bounded)
        _record_failure(event, token, exc, log)
        return
    finally:
        _running_key.reset(running)
    _finish(event.pk, token, OutboxStatus.DONE)


def _record_failure(event: OutboxEvent, token: UUID, exc: Exception, log: dict[str, Any]) -> None:
    error = _describe(exc)
    exc_type = type(exc).__name__
    if event.attempts >= event.max_attempts:
        _finish(event.pk, token, OutboxStatus.DEAD, error)
        logger.error(
            "outbox_event_dead",
            extra={**log, "reason": "retries_exhausted", "exc_type": exc_type},
        )
        return
    delay = backoff_seconds(event.attempts)
    _current(event.pk, token).update(
        status=OutboxStatus.PENDING,
        locked_until=None,
        claim_token=None,
        available_at=timezone.now() + timedelta(seconds=delay),
        last_error=error,
    )
    logger.warning(
        "outbox_event_retry_scheduled",
        extra={**log, "exc_type": exc_type, "retry_in_s": round(delay, 1)},
    )
