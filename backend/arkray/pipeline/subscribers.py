"""The pipeline's reactions to lead changes (ADR-0017). They run inside the lead service's
transaction: whatever they do commits or rolls back with the lead change, and raising
refuses the lead change altogether.

- LeadReassigned: the lead's open opportunities follow it to the new owner (ownership
  coherence, docs/authorization.md#ownership-coherence-v1); won and lost ones keep the
  owner who closed them. Historical attribution (created_by, audit actors, stage history
  actors) never changes.
- LeadStatusChanged / LeadCreated into a *converted* status: refused unless the lead has an
  opportunity of its owner's (opportunities another user closed before a reassignment are
  invisible to the owner and don't count: the answer must not reveal them). "Converted"
  means the lead has entered the opportunity process; the Convert operation
  (services.convert_lead) creates the opportunity and sets the status at once.
"""

from __future__ import annotations

from arkray.core.domain_events import subscribe
from arkray.leads import selectors as lead_selectors
from arkray.leads.events import LeadCreated, LeadReassigned, LeadStatusChanged
from arkray.leads.models import StatusCategory

from . import services


@subscribe(LeadReassigned)
def open_opportunities_follow_the_lead(event: LeadReassigned) -> None:
    services.follow_lead_owner(
        lead_id=event.lead_id,
        to_owner_id=event.owner_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
    )


@subscribe(LeadStatusChanged)
def converted_leads_have_an_opportunity(event: LeadStatusChanged) -> None:
    if event.to_category == StatusCategory.CONVERTED:
        services.require_opportunity_for_conversion(event.lead_id, event.owner_id)


@subscribe(LeadCreated)
def new_leads_are_not_converted(event: LeadCreated) -> None:
    status = lead_selectors.status_by_key(event.status)
    if status is not None and status.category == StatusCategory.CONVERTED:
        services.require_opportunity_for_conversion(event.lead_id, event.owner_id)
