"""Domain events published by activity services, inside the transaction of the change
(arkray.core.domain_events). Identifiers, types and status keys only: never titles,
descriptions, note bodies, locations or links.

Phase 4 has no subscribers of its own events (the timeline is written by the services
directly, in the same module). The planned consumer is Ask Arkray (Phase 8): notes and
task/meeting descriptions are indexed for semantic search, and docs/rag-architecture.md
re-indexes a source when it is created, edited, reassigned, archived or restored, by
enqueuing outbox work from a subscriber. Dashboards (Phase 5) do NOT consume events: they
query the authoritative table (selectors.activity_summary).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from arkray.core.domain_events import DomainEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityEvent(DomainEvent):
    activity_id: UUID
    activity_type: str
    lead_id: UUID
    opportunity_id: UUID | None
    owner_id: UUID  # the owner after the change
    actor_id: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityCreated(ActivityEvent):
    status: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityUpdated(ActivityEvent):
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityStatusChanged(ActivityEvent):
    """Completed, cancelled or reopened."""

    from_status: str
    to_status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityOwnerChanged(ActivityEvent):
    """Current work followed its lead to a new owner ("lead_reassigned"), or a closed task or
    meeting was reopened after its lead had changed hands ("reopened")."""

    from_owner_id: UUID
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityArchived(ActivityEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityRestored(ActivityEvent):
    pass
