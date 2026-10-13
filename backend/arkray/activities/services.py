"""Activity use cases. Each runs in one transaction that also writes its timeline entry,
audit event and domain event, so the record, its history and every subscriber agree.

Authorization, applied here for every caller (API, future imports, Ask Arkray tools):
- the activity, its lead and its opportunity are looked up through the caller's
  AccessScope: outside it, NotFoundError (404), indistinguishable from a record that
  doesn't exist. A link to someone else's lead or opportunity is a 404 too, so creating an
  activity can't be used to learn that a record exists;
- writing requires identity.workspaces.authorize_write (own workspace: crm.access_own;
  another user's or the organisation's: crm.manage_any; a team member's or the team's:
  crm.manage_team, ADR-0035), else 403;
- nobody chooses an activity's owner: current work belongs to the lead's (active) owner,
  so an admin's task in Rahul's workspace is Rahul's, with the admin as `created_by`;
- a note's text is its author's words: the author may edit it, and so may an administrator
  working in someone's workspace (crm.manage_any; a team administrator in a member's or the
  team's, crm.manage_team); the note then records who edited it and when (`edited_by`,
  `edited_at`), so nobody's words are presented as someone else's.
  Anyone who may write in the workspace may archive it. Files on a note follow the same
  rule (attachments.py).

Lock order (docs/activities.md#lock-order), the same in every operation:

    1. the lead          leads.selectors.lock_lead / lock_lead_by_id: FOR NO KEY UPDATE.
                         Its owner can't change until we commit; meeting completion's
                         last-contact update is a non-key update under this same lock
    2. opportunities     never locked here: their owner and archive state only change under
                         the lead's lock, which we hold; read only
    3. activities        FOR NO KEY UPDATE OF the activity row only; several at once only in
                         ascending id order (the reassignment subscriber)
    4. user rows         FOR SHARE (identity.selectors.lock_assignable_user); leaf locks
                         here (user administration locks them FOR NO KEY UPDATE, so a
                         write's foreign-key KEY SHARE never waits on it: ADR-0035)
    5. inserts           timeline, audit, idempotency records

The activity's lead id is read first (unlocked: it never changes), then the lead is
locked, then the activity is locked through the scope and re-checked. The database backs
the ownership rule up: current work whose owner isn't its lead's owner can't be committed.

Concurrency: every change requires the `version` the client last saw (409 otherwise) and
bumps it. Requests that would change nothing (completing a completed task, cancelling a
cancelled meeting, reopening open work, archiving an archived activity) succeed without a
version check or a new version, so double submits and retries are harmless.

Legal holds (core.holds, docs/privacy.md#legal-holds): while one covers the lead, its
activities' text (HELD_FIELDS) can't change and its notes' files can't be deleted (409
`legal_hold`, nothing written); checked under the lead's lock, serialised with placing a
hold. Adding work and moving it on stay allowed.

Erasure (docs/privacy.md#erasure): once the lead is erased (Lead.erased_at), the same text of
every one of its activities (tasks and meetings as well as notes) can't be written again, and
no activity or file can be added for it (422 `lead_erased`, nothing written): otherwise the
erasure could be undone (deep security audit M-1). Times, priority, completing, cancelling
and archiving go on.

Write budgets (review of 73ff1e3, M6; docs/reliability.md#write-budgets): one account could
post a long note every 100 ms all day, so the indexing fell hours behind for everyone and a
customer could collect enough activities to make their export or erasure fail. Creating is
budgeted twice, both far above real use and `0` turning either off:
- per person, ACTIVITY_MAX_CREATED_PER_USER_PER_DAY in any 24 hours, counted from the
  activities they created (never deleted: archiving gives nothing back) through their own
  index, one bounded statement whatever else they did (review round 1: counted from the audit
  trail it read all their other events too), under a per-person advisory lock taken first in
  the creating transaction (before the lead's: only creations take it, so no cycle): 429
  `rate_limited` with `Retry-After`;
- per customer, ACTIVITY_MAX_PER_CUSTOMER activities on one lead, archived ones included,
  counted under the lead's lock: 422 `activity_limit_reached`.
Editing, completing and archiving aren't budgeted (they create nothing; an edit's re-index is
coalesced while it waits, and the outbox takes turns between users).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import holds, idempotency
from arkray.core.access import AccessScope
from arkray.core.domain_events import publish
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)
from arkray.identity.models import User
from arkray.identity.policy import Capability, has_capability
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

LINK_REQUIRED = "Choose the opportunity this is about."
INVALID_TYPE = "Choose task, meeting or note."
OPPORTUNITY_OF_ANOTHER_LEAD = "This opportunity belongs to a different lead."
# Users see a lead as the opportunity's customer (ADR-0027): messages they can meet say so.
LEAD_ARCHIVED = "This customer's record is archived, so activities can't be added."
NOTE_LEAD_ARCHIVED = "This customer's record is archived, so its notes can't be changed."
OPPORTUNITY_ARCHIVED = "This opportunity is archived. Restore it before adding activities."
LEAD_ELSEWHERE = (
    "This opportunity's customer now belongs to someone else, so activities can't be added "
    "here. Ask an administrator."
)
# The same rules for an administrator in a user's workspace (Phase 6): the organisation-wide
# view, whose scope includes the lead's new owner, is where they can do it.
LEAD_ELSEWHERE_DELEGATED = (
    "This opportunity's customer now belongs to someone else, so activities can't be added "
    "in this user's workspace. Add them from the organisation-wide view."
)
# A team administrator (ADR-0035): their widest view is their team's, which can only if the
# customer's owner is in the team; a super administrator can always.
LEAD_ELSEWHERE_MEMBER = (
    "This opportunity's customer now belongs to someone else, so activities can't be added "
    "in this user's workspace. Add them from your team's view if they are in your team, or "
    "ask a super admin."
)
# ...and in their team's view, which already includes everyone in the team: the customer's
# owner is outside it (security review of ADR-0035).
LEAD_ELSEWHERE_TEAM = (
    "This opportunity's customer now belongs to someone outside your team, so activities "
    "can't be added here. Ask a super admin."
)
OWNER_NOT_ASSIGNABLE = (
    "This customer's owner is deactivated. Change the owner to an active user first."
)
AUTHOR_ONLY = "Only the note's author (or an administrator) can edit it."
NO_LIFECYCLE = "Notes can't be completed, cancelled or reopened."
NOT_STARTED = (
    "This meeting hasn't started yet. Change its time to when it took place, then complete it."
)
LEAD_ARCHIVED_REOPEN = "This customer's record is archived, so its activities can't be reopened."
LEAD_ARCHIVED_RESTORE = "This customer's record is archived, so its activities can't be restored."
OPPORTUNITY_ARCHIVED_REOPEN = (
    "This opportunity is archived. Restore the opportunity before reopening its activities."
)
OPPORTUNITY_ARCHIVED_RESTORE = "This opportunity is archived. Restore the opportunity first."
ALREADY_CREATED_ELSEWHERE = (
    "This was already created by an earlier request and has since left this workspace."
)
# What a legal hold on the lead freezes in its activities (core.holds, docs/privacy.md#legal-
# holds): the text an erasure redacts. Times, priority, completing, cancelling, reopening and
# archiving still change, and new activities can be added.
HELD_FIELDS = frozenset({"title", "description", "location", "meeting_url"})
# Write budgets (module docstring; review of 73ff1e3, M6).
DAILY_BUDGET_REACHED = (
    "You've added as many tasks, meetings and notes as one person can in a day. Try again later."
)
CUSTOMER_FULL = (
    "This customer already has {limit:,} activities, the most one customer can have, so no "
    "more can be added. Ask an administrator."
)
DAY = timedelta(days=1)
# Advisory-lock namespace ("ARKD"): one person's creations are counted against their daily
# budget one at a time (the two-key form of attachments' quota lock). Its own: "ARKB" is the
# legal-hold barrier's (review round 1; tests/architecture/test_advisory_locks.py).
_BUDGET_LOCK = 0x41524B44


class ActivityLimitReached(BusinessRuleViolation):
    """The customer has ACTIVITY_MAX_PER_CUSTOMER activities already (M6)."""

    code = "activity_limit_reached"


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


def lock_note(scope: AccessScope, note_id: UUID, *, adding: bool = False) -> Activity:
    """A note, locked (its lead first: the lock order), for operations on its files
    (attachments.py). NotFoundError outside the scope or if it isn't a note. `adding`: refused
    while the lead is archived (erasure archives it: nothing personal may be added after)."""
    note, lead = _lock(scope, note_id)
    if note.type != ActivityType.NOTE:
        raise NotFoundError()
    if adding:
        lead_services.require_not_erased(lead, lead_services.ERASED_NO_NEW)
    if adding and lead.archived_at is not None:
        raise BusinessRuleViolation(NOTE_LEAD_ARCHIVED)
    return note


def _elsewhere(
    actor: User, scope: AccessScope, own: str, delegated: str, member: str, team: str
) -> str:
    """Which explanation fits a refusal because the lead now lives outside `scope`: one's
    own workspace (ask an administrator), a super administrator's in a user's workspace (the
    organisation-wide view can), a team administrator's in a member's workspace (their
    team's view can, if the new owner is in it: ADR-0035), or a team administrator's team
    view (the new owner is outside the team: ask a super admin)."""
    if not scope.is_delegated:
        return own
    if scope.is_team:
        return team
    if not has_capability(actor, Capability.CRM_VIEW_ALL):
        return member
    return delegated


def _lock_link(
    actor: User, scope: AccessScope, lead_id: UUID | None, opportunity_id: UUID | None
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
        raise BusinessRuleViolation(
            _elsewhere(
                actor,
                scope,
                LEAD_ELSEWHERE,
                LEAD_ELSEWHERE_DELEGATED,
                LEAD_ELSEWHERE_MEMBER,
                LEAD_ELSEWHERE_TEAM,
            )
        )
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


def _opportunity_archived(activity: Activity) -> bool:
    """Read under the lead's lock, which every opportunity archive and restore takes."""
    return activity.opportunity_id is not None and pipeline_selectors.opportunity_is_archived(
        activity.opportunity_id
    )


def _lifecycle(activity: Activity) -> TypeSpec:
    spec = SPECS[activity.type]
    if not spec.has_lifecycle:
        raise BusinessRuleViolation(NO_LIFECYCLE)
    return spec


def _created_today(actor_id: UUID, now: datetime) -> QuerySet[Activity]:
    """What the person created in the 24 hours before `now`, newest first (activities are
    never deleted, and archiving or editing doesn't change who created one or when): a range
    of `activities_created_by_idx` (activities.0012), which no other write of theirs is in."""
    return Activity.objects.filter(created_by_id=actor_id, created_at__gt=now - DAY).order_by(
        "-created_at"
    )


def _charge_daily_budget(actor: User, now: datetime) -> None:
    """Refuse a creation past ACTIVITY_MAX_CREATED_PER_USER_PER_DAY in any 24 hours (0: no
    budget). The creator pays, also in someone else's workspace. Counted under a per-person
    advisory lock held until the creation commits (which is what the next count sees), so
    creations at once on different customers can't all pass. Taken first in the transaction,
    before the lead's lock. One statement, reading at most `cap` index entries: the cap-th
    newest creation in the window, if there is one (review round 1, QUEUES-3)."""
    cap = settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY
    if cap <= 0:
        return
    digest = int.from_bytes(hashlib.sha256(str(actor.pk).encode()).digest()[:4], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_BUDGET_LOCK, digest])
    cap_th = list(_created_today(actor.pk, now).values_list("created_at", flat=True)[cap - 1 : cap])
    if not cap_th:
        return
    # Creating is possible again once the cap-th newest is more than a day old.
    (counted,) = cap_th
    raise RateLimitedError(
        DAILY_BUDGET_REACHED, retry_after=max(60, int((counted + DAY - now).total_seconds()))
    )


def _activities_of(lead_id: UUID) -> int:
    """How many activities the customer has, archived ones included: an export and an
    erasure handle every one (an index-only count of activities_lead_idx)."""
    return Activity.objects.filter(lead_id=lead_id).count()


def _require_room(lead: Lead) -> None:
    """Refuse a creation past ACTIVITY_MAX_PER_CUSTOMER (0: no budget). Called under the
    lead's lock, which every creation on the customer takes first, so two at once can't both
    take the last place."""
    limit = settings.ACTIVITY_MAX_PER_CUSTOMER
    if limit > 0 and _activities_of(lead.pk) >= limit:
        raise ActivityLimitReached(CUSTOMER_FULL.format(limit=limit))


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
    scheduled; a note is simply added. A timeline entry records it. Within the write budgets
    (module docstring): 429 past the actor's daily one, 422 `activity_limit_reached` past the
    customer's."""
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
        lead_services.digest_subject(scope),
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
            _charge_daily_budget(actor, timezone.now())  # its lock first: the lock order
            lead, opportunity = _lock_link(actor, scope, lead_id, opportunity_id)
            lead_services.require_not_erased(lead, lead_services.ERASED_NO_NEW)
            if lead.archived_at is not None:
                raise BusinessRuleViolation(LEAD_ARCHIVED)
            if opportunity is not None and opportunity.archived_at is not None:
                raise BusinessRuleViolation(OPPORTUNITY_ARCHIVED)
            _require_room(lead)
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
    actually changes are written and audited (by name: no text). Refused (409 `legal_hold`)
    while a legal hold covers the lead, and (422 `lead_erased`) once the lead is erased, if
    its text (HELD_FIELDS) would change."""
    authorize_write(actor, scope)
    cleaned = validation.clean_fields(changes)
    with transaction.atomic():
        activity, lead = _lock(scope, activity_id)
        spec = SPECS[activity.type]
        if activity.type == ActivityType.NOTE and lead.archived_at is not None:
            # An archived lead's notes stay as they were (erasure archives the lead: text
            # written after it could never be erased; enhancement review).
            raise BusinessRuleViolation(NOTE_LEAD_ARCHIVED)
        if (
            activity.type == ActivityType.NOTE
            and activity.created_by_id != actor.pk
            and not scope.is_delegated
        ):
            # Rewriting someone else's words would falsify the record: the author edits,
            # and an administrator managing the workspace (whose edit is then shown as
            # theirs: edited_by); others may archive.
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
        # Before anything is written (the lead's lock is held): a refusal changes nothing.
        lead_services.refuse_erased_changes(lead, changed, HELD_FIELDS)
        holds.refuse_frozen_changes(lead.pk, changed, HELD_FIELDS)
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
        activity.edited_at = now
        activity.edited_by_id = actor.pk
        activity.save(update_fields=[*changed, "edited_at", "edited_by", "version", "updated_at"])
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
            where = _elsewhere(
                actor,
                scope,
                "here. Ask an administrator to reopen it.",
                "in this user's workspace. Reopen it from the organisation-wide Activities.",
                "in this user's workspace. Reopen it from your team's Activities if they are in "
                "your team, or ask a super admin.",
                "here: they're outside your team. Ask a super admin to reopen it.",
            )
            raise BusinessRuleViolation(
                f"This {_noun(activity)}'s lead now belongs to someone else, so it can't be "
                f"reopened {where}"
            )
        if lead.archived_at is not None:
            raise BusinessRuleViolation(LEAD_ARCHIVED_REOPEN)
        if _opportunity_archived(activity):
            raise BusinessRuleViolation(OPPORTUNITY_ARCHIVED_REOPEN)
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
        if _opportunity_archived(activity) and scope.permits_owner(lead.owner_id):
            raise BusinessRuleViolation(OPPORTUNITY_ARCHIVED_RESTORE)
        if activity.status in CURRENT_STATUSES:
            # A restored open task or scheduled meeting is current work again: like creating
            # or reopening it, never for a deactivated owner (Phase 6 review).
            _require_assignable(activity.owner_id)
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
