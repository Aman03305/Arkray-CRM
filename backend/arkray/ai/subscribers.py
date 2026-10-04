"""Ask Arkray's reactions to CRM changes (ADR-0017): enqueue re-indexing through the outbox,
inside the changing transaction, and nothing else. No embedding, no network, no reads of
the changed record here: a CRM write never waits for AI, and if AI work later fails the
write has already committed.

Only changes that can alter what is indexed enqueue work: the embedded text (see each
module's `knowledge_documents`), the owner, the archive state. A lead's reassignment is
one event for the whole lead (its open opportunities and current work move with it in the
same transaction), not one per moved record.
"""

from __future__ import annotations

from arkray.activities.events import (
    ActivityArchived,
    ActivityCreated,
    ActivityEvent,
    ActivityOwnerChanged,
    ActivityRestored,
    ActivityUpdated,
)
from arkray.core.domain_events import subscribe
from arkray.leads.events import (
    LeadArchived,
    LeadCreated,
    LeadEvent,
    LeadReassigned,
    LeadRestored,
    LeadUpdated,
)
from arkray.pipeline.events import (
    OpportunityArchived,
    OpportunityCreated,
    OpportunityEvent,
    OpportunityOwnerChanged,
    OpportunityRestored,
    OpportunityStageChanged,
    OpportunityUpdated,
)

from .indexing import enqueue_lead, enqueue_source
from .sources import SourceModule

# Fields that appear in the knowledge documents (leads/pipeline/activities selectors).
LEAD_TEXT_FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "organization_name",
        "job_title",
        "city",
        "state",
        "country",
        "description",
    }
)
OPPORTUNITY_TEXT_FIELDS = frozenset({"title", "description", "lost_reason"})
ACTIVITY_TEXT_FIELDS = frozenset({"title", "description"})
FOLLOWS_LEAD = "lead_reassigned"  # covered by the lead-level re-sync


def _lead(event: LeadEvent) -> None:
    enqueue_source(SourceModule.LEAD, event.lead_id)


def _opportunity(event: OpportunityEvent) -> None:
    enqueue_source(SourceModule.OPPORTUNITY, event.opportunity_id)


def _activity(event: ActivityEvent) -> None:
    enqueue_source(SourceModule.ACTIVITY, event.activity_id)


subscribe(LeadCreated)(_lead)
subscribe(LeadArchived)(_lead)
subscribe(LeadRestored)(_lead)
subscribe(OpportunityCreated)(_opportunity)
subscribe(OpportunityArchived)(_opportunity)
subscribe(OpportunityRestored)(_opportunity)
subscribe(ActivityCreated)(_activity)
subscribe(ActivityArchived)(_activity)
subscribe(ActivityRestored)(_activity)


@subscribe(LeadUpdated)
def lead_updated(event: LeadUpdated) -> None:
    if LEAD_TEXT_FIELDS & set(event.fields):
        _lead(event)


@subscribe(LeadReassigned)
def lead_reassigned(event: LeadReassigned) -> None:
    enqueue_lead(event.lead_id)


@subscribe(OpportunityUpdated)
def opportunity_updated(event: OpportunityUpdated) -> None:
    if OPPORTUNITY_TEXT_FIELDS & set(event.fields):
        _opportunity(event)


@subscribe(OpportunityStageChanged)
def opportunity_stage_changed(event: OpportunityStageChanged) -> None:
    # Closing as lost records a lost reason; reopening clears it.
    if {event.from_status, event.to_status} & {"lost"}:
        _opportunity(event)


@subscribe(OpportunityOwnerChanged)
def opportunity_owner_changed(event: OpportunityOwnerChanged) -> None:
    if event.reason != FOLLOWS_LEAD:
        _opportunity(event)


@subscribe(ActivityUpdated)
def activity_updated(event: ActivityUpdated) -> None:
    if ACTIVITY_TEXT_FIELDS & set(event.fields):
        _activity(event)


@subscribe(ActivityOwnerChanged)
def activity_owner_changed(event: ActivityOwnerChanged) -> None:
    if event.reason != FOLLOWS_LEAD:
        _activity(event)
