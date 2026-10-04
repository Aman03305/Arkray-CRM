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
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .context import current_correlation_id, update_context
from .logging import safe_exception_text
from .models import OutboxEvent, OutboxStatus

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 16 * 1024
_ERROR_TEXT_LIMIT = 1000

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


_HANDLERS: dict[str, OutboxHandler] = {}


def handler(
    topic: str, *, queue: str = "default", max_attempts: int = 8
) -> Callable[[HandlerFunc], HandlerFunc]:
    """Register the handler for `topic`. Modules register handlers from AppConfig.ready()."""

    def register(func: HandlerFunc) -> HandlerFunc:
        if topic in _HANDLERS:
            raise ImproperlyConfigured(f"Duplicate outbox handler for topic {topic!r}.")
        if queue not in settings.OUTBOX_MAX_IN_FLIGHT:
            raise ImproperlyConfigured(f"Outbox queue {queue!r} is not configured.")
        _HANDLERS[topic] = OutboxHandler(topic, func, queue, max_attempts)
        return func

    return register


# --- producing -----------------------------------------------------------------------------
def enqueue(
    topic: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str = "",
    delay_seconds: float = 0,
) -> OutboxEvent | None:
    """Record background work. Call inside the transaction that makes the business change.

    With a `dedupe_key`, the work may be coalesced into an identical event that is pending
    and has never been attempted; then None is returned. That event is row-locked until the
    caller's transaction ends, so the relay (SKIP LOCKED) cannot run it on pre-commit state.
    Coalescing is best-effort: if the candidate is locked by someone else, a new event is
    written — duplicates are harmless because handlers are idempotent.
    """
    spec = _HANDLERS.get(topic)
    if spec is None:
        raise ImproperlyConfigured(f"No outbox handler registered for topic {topic!r}.")
    payload = payload or {}
    if len(json.dumps(payload).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("Outbox payloads carry identifiers, not content; payload too large.")

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
        )


# --- relaying ------------------------------------------------------------------------------
def _dispatch_to_celery(event: OutboxEvent) -> None:
    from .tasks import process_outbox_event

    process_outbox_event.apply_async(
        args=(event.pk, str(event.claim_token)),
        queue=event.queue,
        # A message still queued when its dispatch lease ends is discarded by the worker;
        # by then the event has been (or will be) re-claimed under a new token.
        expires=settings.OUTBOX_DISPATCH_LEASE_SECONDS,
    )


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


def _claim(queue: str, max_in_flight: int, now: datetime) -> list[OutboxEvent]:
    """Move up to the queue's free capacity of due events to in_flight under a new claim.

    The in-flight cap is what keeps broker queues bounded. It is a soft cap: two relays
    racing may briefly exceed it, which is harmless.
    """
    with transaction.atomic():
        in_flight = OutboxEvent.objects.filter(queue=queue, status=OutboxStatus.IN_FLIGHT).count()
        capacity = min(max_in_flight - in_flight, settings.OUTBOX_RELAY_BATCH_SIZE)
        if capacity <= 0:
            return []
        events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(queue=queue, status=OutboxStatus.PENDING, available_at__lte=now)
            .order_by("available_at", "id")
            .only("id", "topic", "queue")[:capacity]
        )
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

    try:
        spec.func(event.payload)
    except PermanentFailure as exc:
        _finish(event.pk, token, OutboxStatus.DEAD, _describe(exc))
        logger.error("outbox_event_dead", extra={**log, "reason": "permanent_failure"})
        return
    except Exception as exc:  # noqa: BLE001 — every failure is recorded and retried (bounded)
        _record_failure(event, token, exc, log)
        return
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
