"""Writing the lead and opportunity timeline
([ADR-0021](../../../docs/adr/0021-materialized-timeline.md)).

Entries are written in the transaction of the change they record: by the activity
services for activity events, and by the subscribers to the lead and pipeline domain
events for lead and opportunity events (subscribers.py). Reading, with visibility applied,
is selectors.lead_timeline / opportunity_timeline.

`data` is a small snapshot of what explains the event later even if the records change:
status and stage names as they were (statuses and stages can be renamed), owner ids (people
are rendered by their current name; their identity never changes) and a meeting's times
when scheduled or rescheduled. Each kind has an allowlist of keys: titles, note bodies,
descriptions and contact details are never copied here, they are read from the live
record, and only for a reader who may see it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from arkray.leads import selectors as lead_selectors
from arkray.leads.events import (
    LeadArchived,
    LeadCreated,
    LeadReassigned,
    LeadRestored,
    LeadStatusChanged,
)
from arkray.pipeline import selectors as pipeline_selectors
from arkray.pipeline.events import OpportunityCreated, OpportunityStageChanged
from arkray.pipeline.models import StageCategory

from .models import Activity, TimelineEntry, TimelineKind

_STAGE_CHANGE = frozenset({"from_stage", "to_stage"})
DATA_KEYS: Mapping[TimelineKind, frozenset[str]] = {
    TimelineKind.LEAD_CREATED: frozenset({"status", "status_name", "owner_id"}),
    TimelineKind.LEAD_STATUS_CHANGED: frozenset({"from", "from_name", "to", "to_name"}),
    TimelineKind.LEAD_REASSIGNED: frozenset({"from_owner_id", "to_owner_id"}),
    TimelineKind.OPPORTUNITY_CREATED: frozenset({"stage", "status", "via_conversion"}),
    TimelineKind.OPPORTUNITY_STAGE_CHANGED: _STAGE_CHANGE,
    TimelineKind.OPPORTUNITY_WON: _STAGE_CHANGE,
    TimelineKind.OPPORTUNITY_LOST: _STAGE_CHANGE,
    TimelineKind.OPPORTUNITY_REOPENED: _STAGE_CHANGE,
    TimelineKind.MEETING_SCHEDULED: frozenset({"starts_at", "ends_at"}),
    TimelineKind.MEETING_RESCHEDULED: frozenset({"from_starts_at", "to_starts_at", "to_ends_at"}),
}
# Data keys holding user ids, rendered as people when read.
USER_KEYS = frozenset({"owner_id", "from_owner_id", "to_owner_id"})


def record(
    kind: TimelineKind,
    *,
    lead_id: UUID,
    actor_id: UUID | None,
    at: datetime,
    opportunity_id: UUID | None = None,
    activity_id: UUID | None = None,
    data: Mapping[str, Any] | None = None,
) -> TimelineEntry:
    snapshot = dict(data or {})
    unexpected = set(snapshot) - DATA_KEYS.get(kind, frozenset())
    if unexpected:  # a programming error: only allowlisted, safe values are ever recorded
        raise ValueError(f"Timeline data not allowed for {kind}: {sorted(unexpected)}")
    return TimelineEntry.objects.create(
        kind=kind,
        lead_id=lead_id,
        opportunity_id=opportunity_id,
        activity_id=activity_id,
        actor_id=actor_id,
        occurred_at=at,
        data=snapshot,
    )


def for_activity(
    kind: TimelineKind,
    activity: Activity,
    *,
    actor_id: UUID,
    at: datetime,
    data: Mapping[str, Any] | None = None,
) -> TimelineEntry:
    """An activity event, filed under the activity's lead (and opportunity, if linked)."""
    return record(
        kind,
        lead_id=activity.lead_id,
        opportunity_id=activity.opportunity_id,
        activity_id=activity.pk,
        actor_id=actor_id,
        at=at,
        data=data,
    )


def meeting_times(starts_at: datetime | None, ends_at: datetime | None) -> dict[str, str | None]:
    return {
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
    }


# --- lead events -------------------------------------------------------------------------------
def lead_created(event: LeadCreated) -> None:
    status = lead_selectors.status_by_key(event.status)
    record(
        TimelineKind.LEAD_CREATED,
        lead_id=event.lead_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
        data={
            "status": event.status,
            "status_name": status.name if status else event.status,
            "owner_id": str(event.owner_id),
        },
    )


def lead_status_changed(event: LeadStatusChanged) -> None:
    names = {status.key: status.name for status in lead_selectors.statuses()}
    record(
        TimelineKind.LEAD_STATUS_CHANGED,
        lead_id=event.lead_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
        data={
            "from": event.from_status,
            "from_name": names.get(event.from_status, event.from_status),
            "to": event.to_status,
            "to_name": names.get(event.to_status, event.to_status),
        },
    )


def lead_reassigned(event: LeadReassigned) -> None:
    record(
        TimelineKind.LEAD_REASSIGNED,
        lead_id=event.lead_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
        data={"from_owner_id": str(event.from_owner_id), "to_owner_id": str(event.owner_id)},
    )


def lead_archived(event: LeadArchived) -> None:
    record(
        TimelineKind.LEAD_ARCHIVED,
        lead_id=event.lead_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
    )


def lead_restored(event: LeadRestored) -> None:
    record(
        TimelineKind.LEAD_RESTORED,
        lead_id=event.lead_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
    )


# --- opportunity events ------------------------------------------------------------------------
def opportunity_created(event: OpportunityCreated) -> None:
    stage = pipeline_selectors.stages_by_id([event.stage_id]).get(event.stage_id)
    record(
        TimelineKind.OPPORTUNITY_CREATED,
        lead_id=event.lead_id,
        opportunity_id=event.opportunity_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
        data={
            "stage": stage.name if stage else "",
            "status": event.status,
            "via_conversion": event.via_conversion,
        },
    )


def stage_change_kind(from_status: str, to_status: str) -> TimelineKind:
    if from_status != StageCategory.OPEN and to_status == StageCategory.OPEN:
        return TimelineKind.OPPORTUNITY_REOPENED
    if to_status == StageCategory.WON:
        return TimelineKind.OPPORTUNITY_WON
    if to_status == StageCategory.LOST:
        return TimelineKind.OPPORTUNITY_LOST
    return TimelineKind.OPPORTUNITY_STAGE_CHANGED


def opportunity_stage_changed(event: OpportunityStageChanged) -> None:
    stages = pipeline_selectors.stages_by_id([event.from_stage_id, event.to_stage_id])
    source, target = stages.get(event.from_stage_id), stages.get(event.to_stage_id)
    record(
        stage_change_kind(event.from_status, event.to_status),
        lead_id=event.lead_id,
        opportunity_id=event.opportunity_id,
        actor_id=event.actor_id,
        at=event.occurred_at,
        data={
            "from_stage": source.name if source else "",
            "to_stage": target.name if target else "",
        },
    )
