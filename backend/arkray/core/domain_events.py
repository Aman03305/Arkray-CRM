"""In-transaction domain events: how a lower module lets upper modules react to its changes.

A service publishes an event (for example "lead reassigned") *inside* the transaction that
made the change. Subscribers run synchronously, in that same transaction, so whatever they
do commits or rolls back together with the change. That is what the ownership-coherence
rule needs from Phase 3/4: reassigning a lead must move its open opportunities and current
activities atomically, or not at all (docs/authorization.md#ownership-coherence-v1).

Rules:
- Subscribers are registered by the *upper* module (from its AppConfig.ready), so the
  publishing module never imports them and the layering stays acyclic.
- Subscribers do database work only. Anything slow or external (re-indexing for Ask
  Arkray, emails, analytics exports) is written to the transactional outbox from the
  subscriber, never performed inline.
- An exception in a subscriber aborts the whole operation: better a refused reassignment
  than a lead whose opportunities stayed with the previous owner.
- With no subscribers, publishing costs nothing: no rows, no queued work.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from django.db import connection


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base class. Events carry identifiers and safe values (keys, ids), never contact data."""


Subscriber = Callable[[Any], None]
_SUBSCRIBERS: dict[type[DomainEvent], list[Subscriber]] = {}


def subscribe[E: DomainEvent](
    event_type: type[E],
) -> Callable[[Callable[[E], None]], Callable[[E], None]]:
    """Register a subscriber for exactly `event_type` (no inheritance matching)."""

    def register(func: Callable[[E], None]) -> Callable[[E], None]:
        _SUBSCRIBERS.setdefault(event_type, []).append(func)
        return func

    return register


def publish(event: DomainEvent) -> None:
    """Run every subscriber of `type(event)` now, inside the caller's transaction."""
    if not connection.in_atomic_block:
        raise RuntimeError("Domain events are published inside the transaction of the change.")
    for subscriber in tuple(_SUBSCRIBERS.get(type(event), ())):
        subscriber(event)


@contextmanager
def subscribed(event_type: type[DomainEvent], subscriber: Subscriber) -> Iterator[None]:
    """Temporarily subscribe (tests)."""
    _SUBSCRIBERS.setdefault(event_type, []).append(subscriber)
    try:
        yield
    finally:
        _SUBSCRIBERS[event_type].remove(subscriber)
