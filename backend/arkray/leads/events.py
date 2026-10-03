"""Domain events published by lead services, inside the transaction of the change
(arkray.core.domain_events). Identifiers and keys only: never names or contact details.

Phase 2 has no subscribers, so publishing writes nothing and queues nothing. Extension
points for later phases:

- LeadReassigned: pipeline (Phase 3) and activities (Phase 4) subscribe to move the lead's
  open opportunities and current activities to the new owner in the same transaction;
  closed/completed records keep their historical owner
  (docs/authorization.md#ownership-coherence-v1).
- Every event: Ask Arkray (Phase 8) subscribes to enqueue re-indexing through the outbox;
  analytics and automation subscribe the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from arkray.core.domain_events import DomainEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadEvent(DomainEvent):
    lead_id: UUID
    owner_id: UUID  # the owner after the change
    actor_id: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadCreated(LeadEvent):
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadUpdated(LeadEvent):
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadStatusChanged(LeadEvent):
    from_status: str
    to_status: str
    from_category: str
    to_category: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadReassigned(LeadEvent):
    from_owner_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadArchived(LeadEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadRestored(LeadEvent):
    pass
