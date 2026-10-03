"""The activities module's reactions to lead and pipeline changes (ADR-0017). They run
inside the publishing service's transaction: whatever they do commits or rolls back with
that change, and raising refuses it altogether.

- LeadReassigned: the lead's current work (open tasks, scheduled meetings, notes) follows
  it to the new owner, after the pipeline has moved the open opportunities (lock order lead
  -> opportunities -> activities: these subscribers are registered after the pipeline's,
  INSTALLED_APPS order, pinned by a test); completed and cancelled work keeps the owner who
  had it. The reassignment is recorded on the timeline.
- The other lead events, and opportunity creation and stage changes, are recorded on the
  timeline (timeline.py), with the names of statuses and stages as they are at that moment.
"""

from __future__ import annotations

from arkray.core.domain_events import subscribe
from arkray.leads.events import (
    LeadArchived,
    LeadCreated,
    LeadReassigned,
    LeadRestored,
    LeadStatusChanged,
)
from arkray.pipeline.events import OpportunityCreated, OpportunityStageChanged

from . import services, timeline


@subscribe(LeadReassigned)
def current_work_follows_the_lead(event: LeadReassigned) -> None:
    services.follow_lead_owner(
        lead_id=event.lead_id,
        to_owner_id=event.owner_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
    )
    timeline.lead_reassigned(event)


subscribe(LeadCreated)(timeline.lead_created)
subscribe(LeadStatusChanged)(timeline.lead_status_changed)
subscribe(LeadArchived)(timeline.lead_archived)
subscribe(LeadRestored)(timeline.lead_restored)
subscribe(OpportunityCreated)(timeline.opportunity_created)
subscribe(OpportunityStageChanged)(timeline.opportunity_stage_changed)
