"""Activity use cases. Each runs in one transaction that also writes its timeline entry,
audit event and domain event, so the record, its history and every subscriber agree.

Authorization, applied here for every caller (API, future imports, Ask Arkray tools):
- the activity, its lead and its opportunity are looked up through the caller's
  AccessScope: outside it, NotFoundError (404), indistinguishable from a record that
  doesn't exist. A link to someone else's lead or opportunity is a 404 too, so creating an
  activity can't be used to learn that a record exists;
- writing requires identity.workspaces.authorize_write (own workspace: crm.access_own;
  another user's or the organisation's: crm.manage_any), else 403;
- nobody chooses an activity's owner: current work belongs to the lead's (active) owner,
  so an admin's task in Rahul's workspace is Rahul's, with the admin as `created_by`;
- a note's text is its author's words: only the author may edit it (anyone who may write
  in the workspace may archive it).

Lock order (docs/activities.md#lock-order), the same in every operation:

    1. the lead          leads.selectors.lock_lead / lock_lead_by_id: FOR NO KEY UPDATE.
                         Its owner can't change until we commit; meeting completion's
                         last-contact update is a non-key update under this same lock
    2. opportunities     never locked here: their owner and archive state only change under
                         the lead's lock, which we hold; read only
    3. activities        FOR NO KEY UPDATE OF the activity row only; several at once only in
                         ascending id order (the reassignment subscriber)
    4. user rows         FOR SHARE (identity.selectors.lock_assignable_user); leaf locks
    5. inserts           timeline, audit, idempotency records

The activity's lead id is read first (unlocked: it never changes), then the lead is
locked, then the activity is locked through the scope and re-checked. The database backs
the ownership rule up: current work whose owner isn't its lead's owner can't be committed.

Concurrency: every change requires the `version` the client last saw (409 otherwise) and
bumps it. Requests that would change nothing (completing a completed task, cancelling a
cancelled meeting, reopening open work, archiving an archived activity) succeed without a
version check or a new version, so double submits and retries are harmless.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import idempotency
from arkray.core.access import AccessScope
from arkray.core.domain_events import publish
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from arkray.identity.models import User
from arkray.identity.selectors import lock_assignable_user
from arkray.identity.workspaces import authorize_write
from arkray.leads import selectors as lead_selectors
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import selectors as pipeline_selectors
from arkray.pipeline.models import Opportunity

from . import events, selectors, timeline, validation
from .models import (
    CURRENT_STATUSES,
    Activity,
    ActivityStatus,
    ActivityType,
    Priority,
    TimelineKind,
)
from .spec import SPECS, TypeSpec

IDEMPOTENT_CREATE = "activities.create"

LINK_REQUIRED = "Choose the lead or opportunity this is about."
INVALID_TYPE = "Choose task, meeting or note."
OPPORTUNITY_OF_ANOTHER_LEAD = "This opportunity belongs to a different lead."
LEAD_ARCHIVED = "This lead is archived. Restore it before adding activities."
OPPORTUNITY_ARCHIVED = "This opportunity is archived. Restore it before adding activities."
LEAD_ELSEWHERE = (
    "This opportunity's lead now belongs to someone else, so activities can't be added "
    "here. Ask an administrator."
)
OWNER_NOT_ASSIGNABLE = (
    "This lead's owner is deactivated. Reassign the lead to an active user first."
)
AUTHOR_ONLY = "Only the note's author can edit it."
NO_LIFECYCLE = "Notes can't be completed, cancelled or reopened."
NOT_STARTED = (
    "This meeting hasn't started yet. Change its time to when it took place, then complete it."
)
LEAD_ARCHIVED_REOPEN = "This lead is archived. Restore the lead before reopening its activities."
LEAD_ARCHIVED_RESTORE = "This lead is archived. Restore the lead first."
ALREADY_CREATED_ELSEWHERE = (
    "This was already created by an earlier request and has since left this workspace."
)


@dataclass(frozen=True, slots=True)
class CreateResult:
    activity: Activity
    replayed: bool  # an earlier request with the same Idempotency-Key created it


def _noun(activity: Activity) -> str:
    return SPECS[activity.type].label.lower()


# --- helpers -----------------------------------------------------------------------------------
def _audit(
    verb: str,
    actor_id: UUID,
    activity: Activity,
    *,
    workspace: str | None,
    subject: UUID,
    **metadata: Any,
) -> None:
    audit.record(
        f"{activity.type}.{verb}",
        actor_id=actor_id,
        target_type="activity",
        target_id=activity.pk,
        subject_user_id=subject if subject != actor_id else None,
        metadata={**({"workspace": workspace} if workspace else {}), **metadata},
    )


def _publish(
    event_type: type[events.ActivityEvent],
    activity: Activity,
    actor_id: UUID,
    at: datetime,
    **extra: Any,
) -> None:
    publish(
        event_type(
            activity_id=activity.pk,
            activity_type=activity.type,
            lead_id=activity.lead_id,
            opportunity_id=activity.opportunity_id,
            owner_id=activity.owner_id,
            actor_id=actor_id,
            occurred_at=at,
            **extra,
        )
    )


def _lock(scope: AccessScope, activity_id: UUID) -> tuple[Activity, Lead]:
    """The activity, locked, and its lead, locked first (lock order). NotFoundError if the
    activity is outside the scope. The lead itself may be outside it: a completed task or
    meeting keeps its owner after its lead is reassigned."""
    lead_id = (
        scope.apply(Activity.objects.filter(pk=activity_id))
        .values_list("lead_id", flat=True)
        .first()
    )
    if lead_id is None:
        raise NotFoundError()
    lead = lead_selectors.lock_lead_by_id(lead_id)
    activity = scope.apply(
        Activity.objects.select_for_update(no_key=True, of=("self",)).filter(pk=activity_id)
    ).first()
    if activity is None:  # moved out of the scope (its lead was reassigned) meanwhile
        raise NotFoundError()
    return activity, lead


def _lock_link(
    scope: AccessScope, lead_id: UUID | None, opportunity_id: UUID | None
) -> tuple[Lead, Opportunity | None]:
    """The lead a new activity is about, locked, and its opportunity if one is given. Both
    must be visible in `scope` (404 otherwise: the same answer as for an id that doesn't
    exist), and the opportunity must be the lead's."""
    if lead_id is not None:
        lead = lead_selectors.lock_lead(scope, lead_id)
        if opportunity_id is None:
            return lead, None
        opportunity = pipeline_selectors.opportunity_ref(scope, opportunity_id)
        if opportunity.lead_id != lead.pk:
            raise InvalidInputError(details={"opportunity": [OPPORTUNITY_OF_ANOTHER_LEAD]})
        return lead, opportunity
    if opportunity_id is None:
        raise InvalidInputError(details={"lead": [LINK_REQUIRED]})
    found = pipeline_selectors.opportunity_ref(scope, opportunity_id)
    lead = lead_selectors.lock_lead_by_id(found.lead_id)
    # Re-read under the lead's lock first: a reassignment that moved an open opportunity
    # out of this workspace meanwhile makes it a plain 404, never a revealing 422 (review).
    opportunity = pipeline_selectors.opportunity_ref(scope, opportunity_id)
    if not scope.permits_owner(lead.owner_id):
        # A closed opportunity kept by its closer after the lead moved on: new work on it
        # would belong to the lead's current owner, outside this workspace.
        raise BusinessRuleViolation(LEAD_ELSEWHERE)
    return lead, opportunity


def _require_version(activity: Activity, version: int) -> None:
    if activity.version != version:
        raise ConflictError()


def _require_not_archived(activity: Activity) -> None:
    if activity.archived_at is not None:
        raise BusinessRuleViolation(
            f"This {_noun(activity)} is archived. Restore it to make changes."
        )


def _require_assignable(owner_id: UUID) -> None:
    if not lock_assignable_user(owner_id):
        raise BusinessRuleViolation(OWNER_NOT_ASSIGNABLE)


def _lifecycle(activity: Activity) -> TypeSpec:
    spec = SPECS[activity.type]
    if not spec.has_lifecycle:
        raise BusinessRuleViolation(NO_LIFECYCLE)
    return spec


def _replay(scope: AccessScope, activity_id: UUID) -> Activity:
    try:
        return selectors.activity_detail(scope, activity_id)
    except NotFoundError:
        raise ConflictError(ALREADY_CREATED_ELSEWHERE) from None


# --- create ------------------------------------------------------------------------------------
def create_activity(
    *,
    actor: User,
    scope: AccessScope,
    activity_type: str,
    fields: Mapping[str, Any],
    lead_id: UUID | None = None,
    opportunity_id: UUID | None = None,
    idempotency_key: UUID | None = None,
) -> CreateResult:
    """Create a task, meeting or note about a lead (or one of its opportunities) in `scope`.
    It belongs to the lead's owner; `created_by` is the actor. Tasks start open, meetings
    scheduled; a note is simply added. A timeline entry records it."""
    authorize_write(actor, scope)
    spec = SPECS.get(activity_type)
    if spec is None:
        raise InvalidInputError(details={"type": [INVALID_TYPE]})
    if lead_id is None and opportunity_id is None:
        raise InvalidInputError(details={"lead": [LINK_REQUIRED]})
    cleaned = validation.clean_fields(fields)
    validation.require_fields_of(spec, cleaned, creating=True)
    if spec.type == ActivityType.MEETING:
        validation.check_meeting_times(cleaned["starts_at"], cleaned["ends_at"])
    if spec.type == ActivityType.TASK:
        cleaned.setdefault("priority", Priority.NORMAL)
    digest = idempotency.request_digest(
        scope.kind.value,
        str(scope.subject_user_id),
        spec.type,
        str(lead_id),
        str(opportunity_id),
        cleaned,
    )
    if idempotency_key is not None:
        earlier = idempotency.replayed_resource(
            actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest
        )
        if earlier is not None:
            return CreateResult(_replay(scope, earlier), replayed=True)
    try:
        with transaction.atomic():
            lead, opportunity = _lock_link(scope, lead_id, opportunity_id)
            if lead.archived_at is not None:
                raise BusinessRuleViolation(LEAD_ARCHIVED)
            if opportunity is not None and opportunity.archived_at is not None:
                raise BusinessRuleViolation(OPPORTUNITY_ARCHIVED)
            _require_assignable(lead.owner_id)
            now = timezone.now()
            activity = Activity.objects.create(
                type=spec.type,
                lead_id=lead.pk,
                opportunity_id=opportunity.pk if opportunity else None,
                owner_id=lead.owner_id,
                created_by_id=actor.pk,
                status=spec.initial_status,
                created_at=now,
                **cleaned,
            )
            timeline.for_activity(
                spec.created_kind,
                activity,
                actor_id=actor.pk,
                at=now,
                data=timeline.meeting_times(activity.starts_at, activity.ends_at)
                if spec.type == ActivityType.MEETING
                else None,
            )
            _audit(
                "created",
                actor.pk,
                activity,
                workspace=scope.kind.value,
                subject=lead.owner_id,
                owner_id=str(lead.owner_id),
                lead_id=str(lead.pk),
                opportunity_id=str(opportunity.pk) if opportunity else None,
            )
            _publish(events.ActivityCreated, activity, actor.pk, now, status=activity.status)
            if idempotency_key is not None:
                idempotency.remember(
                    actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest, activity.pk
                )
    except IntegrityError as error:
        # A concurrent request with the same key won the race: return its result.
        if idempotency_key is not None and idempotency.is_duplicate_key(error):
            earlier = idempotency.replayed_resource(
                actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest
            )
            if earlier is not None:
                return CreateResult(_replay(scope, earlier), replayed=True)
        raise
    return CreateResult(selectors.activity_by_id(activity.pk), replayed=False)


# --- edit --------------------------------------------------------------------------------------
def update_activity(
    *,
    actor: User,
    scope: AccessScope,
    activity_id: UUID,
    version: int,
    changes: Mapping[str, Any],
) -> Activity:
    """Change an activity's own fields (its type's: a task's subject, description, priority
    and due time; a meeting's subject, agenda, times, location and link; a note's text).
    Completed and cancelled work is history: reopen it first. Only fields whose value
    actually changes are written and audited (by name: no text)."""
    authorize_write(actor, scope)
    cleaned = validation.clean_fields(changes)
    with transaction.atomic():
        activity, _ = _lock(scope, activity_id)
        spec = SPECS[activity.type]
        if activity.type == ActivityType.NOTE and activity.created_by_id != actor.pk:
            # Rewriting someone else's words while their name stays on them would falsify
            # the record: the author edits, others may archive.
            raise PermissionDeniedError(AUTHOR_ONLY)
        _require_version(activity, version)
        _require_not_archived(activity)
        validation.require_fields_of(spec, cleaned, creating=False)
        if spec.has_lifecycle and activity.status not in CURRENT_STATUSES:
            raise BusinessRuleViolation(
                f"This {_noun(activity)} is {activity.status}. Reopen it to make changes."
            )
        changed = [f for f, v in cleaned.items() if getattr(activity, f) != v]
        if not changed:
            return selectors.activity_by_id(activity.pk)
        previous_start = activity.starts_at
        for field in changed:
            setattr(activity, field, cleaned[field])
        if activity.type == ActivityType.MEETING:
            assert activity.starts_at is not None  # noqa: S101 — a CHECK guarantees both
            assert activity.ends_at is not None  # noqa: S101
            validation.check_meeting_times(activity.starts_at, activity.ends_at)
        now = timezone.now()
        activity.version += 1
        activity.updated_at = now
        activity.save(update_fields=[*changed, "version", "updated_at"])
        if {"starts_at", "ends_at"} & set(changed):
            timeline.for_activity(
                TimelineKind.MEETING_RESCHEDULED,
                activity,
                actor_id=actor.pk,
                at=now,
                data={
                    "from_starts_at": previous_start.isoformat() if previous_start else None,
                    "to_starts_at": activity.starts_at.isoformat() if activity.starts_at else None,
                    "to_ends_at": activity.ends_at.isoformat() if activity.ends_at else None,
                },
            )
        _audit(
            "updated",
            actor.pk,
            activity,
            workspace=scope.kind.value,
            subject=activity.owner_id,
            fields=sorted(changed),
        )
        _publish(events.ActivityUpdated, activity, actor.pk, now, fields=tuple(sorted(changed)))
    return selectors.activity_by_id(activity.pk)


# --- lifecycle ---------------------------------------------------------------------------------
def complete_activity(
    *, actor: User, scope: AccessScope, activity_id: UUID, version: int
) -> Activity:
    """Mark a task done, or record that a meeting took place. Completing a meeting is a
    customer interaction: the lead's last contact advances to the meeting's start time
    (never backwards; leads.services.record_contact). A meeting can only be completed once it
    has started. Completing completed work is a no-op success."""
    authorize_write(actor, scope)
    with transaction.atomic():
        activity, lead = _lock(scope, activity_id)
        spec = _lifecycle(activity)
        if activity.status == ActivityStatus.COMPLETED:
            return selectors.activity_by_id(activity.pk)
        _require_version(activity, version)
        _require_not_archived(activity)
        if activity.status == ActivityStatus.CANCELLED:
            raise BusinessRuleViolation(f"This {_noun(activity)} was cancelled. Reopen it first.")
        now = timezone.now()
        is_meeting = activity.type == ActivityType.MEETING
        if is_meeting and activity.starts_at is not None and activity.starts_at > now:
            raise BusinessRuleViolation(NOT_STARTED)
        previous = activity.status
        activity.status = ActivityStatus.COMPLETED
        activity.completed_at = now
        activity.completed_by_id = actor.pk
        activity.version += 1
        activity.updated_at = now
        activity.save(
            update_fields=["status", "completed_at", "completed_by", "version", "updated_at"]
        )
        if is_meeting and activity.starts_at is not None:
            lead_services.record_contact(
                actor_id=actor.pk,
                lead_id=lead.pk,
                contacted_at=activity.starts_at,
                via="meeting_completed",
                via_id=activity.pk,
                workspace=scope.kind.value,
            )
        _closed(spec.completed_kind, "completed", activity, actor, scope, previous, now)
    return selectors.activity_by_id(activity.pk)


def cancel_activity(
    *, actor: User, scope: AccessScope, activity_id: UUID, version: int
) -> Activity:
    """Call off a task or a meeting. It stays in the history (with who cancelled it and
    when) and can be reopened. Cancelling cancelled work is a no-op success."""
    authorize_write(actor, scope)
    with transaction.atomic():
        activity, _ = _lock(scope, activity_id)
        spec = _lifecycle(activity)
        if activity.status == ActivityStatus.CANCELLED:
            return selectors.activity_by_id(activity.pk)
        _require_version(activity, version)
        _require_not_archived(activity)
        if activity.status == ActivityStatus.COMPLETED:
            raise BusinessRuleViolation(f"This {_noun(activity)} is completed. Reopen it first.")
        now = timezone.now()
        previous = activity.status
        activity.status = ActivityStatus.CANCELLED
        activity.cancelled_at = now
        activity.cancelled_by_id = actor.pk
        activity.version += 1
        activity.updated_at = now
        activity.save(
            update_fields=["status", "cancelled_at", "cancelled_by", "version", "updated_at"]
        )
        _closed(spec.cancelled_kind, "cancelled", activity, actor, scope, previous, now)
    return selectors.activity_by_id(activity.pk)


def _closed(
    kind: TimelineKind | None,
    verb: str,
    activity: Activity,
    actor: User,
    scope: AccessScope,
    previous: str | None,
    at: datetime,
) -> None:
    assert kind is not None  # noqa: S101 — lifecycle types only
    assert previous is not None  # noqa: S101
    timeline.for_activity(kind, activity, actor_id=actor.pk, at=at)
    _audit(verb, actor.pk, activity, workspace=scope.kind.value, subject=activity.owner_id)
    _publish(
        events.ActivityStatusChanged,
        activity,
        actor.pk,
        at,
        from_status=previous,
        to_status=str(activity.status),
    )


def reopen_activity(
    *, actor: User, scope: AccessScope, activity_id: UUID, version: int
) -> Activity:
    """Make a completed or cancelled task open again, or a meeting scheduled again. The
    completion or cancellation is cleared on the activity but stays in the timeline and the
    audit trail. Open work belongs to the lead's current owner: if the lead changed hands
    meanwhile, reopening moves it to them, which only someone whose workspace includes that
    owner may do. A meeting's recorded contact is kept (it did happen, or the lead's last
    contact can be corrected on the lead). Reopening open work is a no-op success."""
    authorize_write(actor, scope)
    with transaction.atomic():
        activity, lead = _lock(scope, activity_id)
        spec = _lifecycle(activity)
        if activity.status == spec.initial_status:
            return selectors.activity_by_id(activity.pk)
        _require_version(activity, version)
        _require_not_archived(activity)
        previous_owner = activity.owner_id
        # Whether the lead lives elsewhere is checked first: its archive state is then the
        # other workspace's business and must not be revealed (review, an oracle).
        if lead.owner_id != previous_owner and not scope.permits_owner(lead.owner_id):
            raise BusinessRuleViolation(
                f"This {_noun(activity)}'s lead now belongs to someone else, so it can't be "
                "reopened here. Ask an administrator to reopen it."
            )
        if lead.archived_at is not None:
            raise BusinessRuleViolation(LEAD_ARCHIVED_REOPEN)
        _require_assignable(lead.owner_id)
        now = timezone.now()
        previous = str(activity.status)
        activity.status = spec.initial_status
        activity.completed_at = None
        activity.completed_by = None
        activity.cancelled_at = None
        activity.cancelled_by = None
        activity.owner_id = lead.owner_id
        activity.version += 1
        activity.updated_at = now
        activity.save(
            update_fields=[
                "status",
                "completed_at",
                "completed_by",
                "cancelled_at",
                "cancelled_by",
                "owner",
                "version",
                "updated_at",
            ]
        )
        assert spec.reopened_kind is not None  # noqa: S101 — lifecycle types only
        timeline.for_activity(spec.reopened_kind, activity, actor_id=actor.pk, at=now)
        _audit(
            "reopened",
            actor.pk,
            activity,
            workspace=scope.kind.value,
            subject=previous_owner,
            from_status=previous,
        )
        _publish(
            events.ActivityStatusChanged,
            activity,
            actor.pk,
            now,
            from_status=previous,
            to_status=str(activity.status),
        )
        if activity.owner_id != previous_owner:
            _audit(
                "owner_changed",
                actor.pk,
                activity,
                workspace=scope.kind.value,
                subject=previous_owner,
                from_owner_id=str(previous_owner),
                to_owner_id=str(activity.owner_id),
                lead_id=str(activity.lead_id),
                reason="reopened",
            )
            _publish(
                events.ActivityOwnerChanged,
                activity,
                actor.pk,
                now,
                from_owner_id=previous_owner,
                reason="reopened",
            )
    return selectors.activity_by_id(activity.pk)


# --- archive -----------------------------------------------------------------------------------
def archive_activity(
    *, actor: User, scope: AccessScope, activity_id: UUID, version: int
) -> Activity:
    """Hide an activity from lists, timelines and counts (a note written by mistake, a
    duplicate task). Nothing is deleted and it can be restored."""
    authorize_write(actor, scope)
    with transaction.atomic():
        activity, _ = _lock(scope, activity_id)
        if activity.archived_at is not None:
            return selectors.activity_by_id(activity.pk)
        _require_version(activity, version)
        now = timezone.now()
        activity.archived_at = now
        activity.version += 1
        activity.updated_at = now
        activity.save(update_fields=["archived_at", "version", "updated_at"])
        _audit(
            "archived", actor.pk, activity, workspace=scope.kind.value, subject=activity.owner_id
        )
        _publish(events.ActivityArchived, activity, actor.pk, now)
    return selectors.activity_by_id(activity.pk)


def restore_activity(
    *, actor: User, scope: AccessScope, activity_id: UUID, version: int
) -> Activity:
    authorize_write(actor, scope)
    with transaction.atomic():
        activity, lead = _lock(scope, activity_id)
        if activity.archived_at is None:
            return selectors.activity_by_id(activity.pk)
        _require_version(activity, version)
        # An archived lead takes no restored work, in a workspace that holds the lead. A
        # completed meeting its holder kept after the lead moved on is theirs to restore:
        # the lead's state belongs to the other workspace and isn't revealed (review).
        if lead.archived_at is not None and scope.permits_owner(lead.owner_id):
            raise BusinessRuleViolation(LEAD_ARCHIVED_RESTORE)
        now = timezone.now()
        activity.archived_at = None
        activity.version += 1
        activity.updated_at = now
        activity.save(update_fields=["archived_at", "version", "updated_at"])
        _audit(
            "restored", actor.pk, activity, workspace=scope.kind.value, subject=activity.owner_id
        )
        _publish(events.ActivityRestored, activity, actor.pk, now)
    return selectors.activity_by_id(activity.pk)


# --- reactions to lead changes (registered in subscribers.py) ----------------------------------
def follow_lead_owner(*, lead_id: UUID, to_owner_id: UUID, actor_id: UUID, at: datetime) -> int:
    """The lead was just reassigned (its row is locked by the reassignment, and the pipeline
    has moved its open opportunities): move its CURRENT work to the new owner, archived
    items included: open tasks, scheduled meetings and notes (a note is the lead's context;
    its author, `created_by`, never changes). Completed and cancelled tasks and meetings keep
    the owner who had them: they record who did the work. Returns how many moved. Constant
    queries however many move: one lock, one update, one audit insert."""
    moving = list(
        Activity.objects.select_for_update(no_key=True)
        .filter(lead_id=lead_id)
        .filter(Q(type=ActivityType.NOTE) | Q(status__in=CURRENT_STATUSES))
        .exclude(owner_id=to_owner_id)
        .order_by("id")  # lock order for several activities
        .only("id", "type", "lead_id", "opportunity_id", "owner_id")
    )
    if not moving:
        return 0
    Activity.objects.filter(pk__in=[a.pk for a in moving]).update(
        owner_id=to_owner_id, version=F("version") + 1, updated_at=at
    )
    audit.record_many(
        [
            audit.Entry(
                f"{activity.type}.owner_changed",
                actor_id,
                "activity",
                activity.pk,
                activity.owner_id if activity.owner_id != actor_id else None,
                {
                    "from_owner_id": str(activity.owner_id),
                    "to_owner_id": str(to_owner_id),
                    "lead_id": str(lead_id),
                    "reason": "lead_reassigned",
                },
            )
            for activity in moving
        ]
    )
    for activity in moving:
        previous = activity.owner_id
        activity.owner_id = to_owner_id
        _publish(
            events.ActivityOwnerChanged,
            activity,
            actor_id,
            at,
            from_owner_id=previous,
            reason="lead_reassigned",
        )
    return len(moving)
