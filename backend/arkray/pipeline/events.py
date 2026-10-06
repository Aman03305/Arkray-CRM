"""Domain events published by pipeline services, inside the transaction of the change
(arkray.core.domain_events). Identifiers, keys and statuses only: never titles, amounts,
descriptions or lost reasons.

Subscribers: the lead/opportunity timeline (arkray.activities) subscribes to
OpportunityCreated and OpportunityStageChanged; Ask Arkray (arkray.ai) subscribes to every
event to re-index through the outbox. Notifications and automation would subscribe to
OpportunityWon / OpportunityLost. Dashboards do NOT consume events: they query the
authoritative tables (arkray.pipeline.metrics).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from arkray.core.domain_events import DomainEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityEvent(DomainEvent):
    opportunity_id: UUID
    lead_id: UUID
    owner_id: UUID  # the owner after the change
    actor_id: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityCreated(OpportunityEvent):
    pipeline_id: UUID
    stage_id: UUID
    status: str
    via_conversion: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityUpdated(OpportunityEvent):
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityStageChanged(OpportunityEvent):
    """Every stage transition, reopening included (from_status won/lost -> open)."""

    from_stage_id: UUID
    to_stage_id: UUID
    from_status: str
    to_status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityWon(OpportunityEvent):
    """Published in addition to OpportunityStageChanged (or OpportunityCreated) when an
    opportunity closes as won, for consumers that only care about outcomes."""

    stage_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityLost(OpportunityEvent):
    stage_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityOwnerChanged(OpportunityEvent):
    """The owner followed the lead: the lead was reassigned (reason "lead_reassigned"), or a
    closed opportunity was reopened after its lead had changed hands ("reopened")."""

    from_owner_id: UUID
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityArchived(OpportunityEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunityRestored(OpportunityEvent):
    pass
