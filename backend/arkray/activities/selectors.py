"""Read queries for activities and timelines. Every query over activities starts from
`scope.apply()`, and so does every count; a timeline is read only if its lead (or
opportunity) is visible, and each entry only if the record it is about is visible now
(docs/activities.md#timeline). The only unscoped reader is `activity_by_id`, which services
use to return a record the actor has just been authorised to change.

Filters and sorts are allowlisted and bounded; every ordering ends in the primary key, so
keyset cursors are exact (core.keyset). List rows carry a bounded preview of the text,
never a whole note.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from django.db.models import Count, Q, QuerySet
from django.db.models.functions import Substr

from arkray.core.access import AccessScope
from arkray.core.business_time import business_midnight, today_bounds
from arkray.core.errors import NotFoundError
from arkray.core.keyset import KeysetOrdering, SortKey
from arkray.identity.models import User
from arkray.leads import selectors as lead_selectors
from arkray.pipeline import selectors as pipeline_selectors

from .models import CURRENT_STATUSES, Activity, ActivityStatus, ActivityType, TimelineEntry
from .timeline import USER_KEYS

PREVIEW_LENGTH = 240  # characters of a note (or description) shown in lists and timelines
UPCOMING_MEETINGS_DEFAULT = 5

ORDERINGS: dict[str, KeysetOrdering] = {
    ordering.name: ordering
    for ordering in (
        KeysetOrdering(
            "-created_at", (SortKey("created_at", descending=True), SortKey("id", descending=True))
        ),
        KeysetOrdering("created_at", (SortKey("created_at"), SortKey("id"))),
        # Due / start time: soonest first (tasks without a due date last), or latest first.
        KeysetOrdering("scheduled", (SortKey("schedule_sort"), SortKey("id"))),
        KeysetOrdering(
            "-scheduled",
            (SortKey("schedule_sort", descending=True), SortKey("id", descending=True)),
        ),
    )
}
DEFAULT_ORDERING = "-created_at"
# The organisation-wide newest/oldest sorts read their own column and index (see
# Activity.created_sort): one owner's list can then never be served by walking every
# activity of the organisation (Phase 4 review, P1).
_ORGANISATION_ORDERINGS: dict[str, KeysetOrdering] = {
    ordering.name: ordering
    for ordering in (
        KeysetOrdering(
            "-created_at",
            (SortKey("created_sort", descending=True), SortKey("id", descending=True)),
        ),
        KeysetOrdering("created_at", (SortKey("created_sort"), SortKey("id"))),
    )
}
TIMELINE_ORDERING = KeysetOrdering(
    "-occurred_at", (SortKey("occurred_at", descending=True), SortKey("id", descending=True))
)

_PERSON = ("id", "first_name", "last_name", "is_active")
_LIST_FIELDS = (
    "id",
    "type",
    "title",
    "status",
    "priority",
    "due_at",
    "starts_at",
    "ends_at",
    "completed_at",
    "cancelled_at",
    "archived_at",
    "version",
    "created_at",
    "updated_at",
    "schedule_sort",
    "created_sort",  # the organisation-wide orderings' cursor value
    "lead__id",
    "lead__first_name",
    "lead__last_name",
    "lead__organization_name",
    "lead__display_name",
    "lead__owner_id",
    "opportunity__id",
    "opportunity__title",
    "opportunity__owner_id",
    "opportunity__status",
    *(f"owner__{f}" for f in _PERSON),
    *(f"created_by__{f}" for f in _PERSON),
)


@dataclass(frozen=True, slots=True)
class ActivityFilters:
    type: str | None = None
    status: str | None = None
    lead_id: UUID | None = None
    opportunity_id: UUID | None = None
    owner_id: UUID | None = None  # organisation-wide workspace only; narrows, never widens
    # Inclusive business dates on the schedule (a task's due date, a meeting's day, a
    # note's creation day).
    date_from: date | None = None
    date_to: date | None = None
    overdue: bool = False  # open tasks past their due time
    current: bool = False  # open tasks and scheduled meetings
    upcoming: bool = False  # open tasks and scheduled meetings due / starting from now on
    cancelled: bool | None = None  # False: leave cancelled tasks and meetings out
    archived: bool = False


@dataclass(frozen=True, slots=True)
class ActivitySummary:
    """The authoritative activity figures for a scope at one moment (Phase 5's dashboard
    shows exactly these; it must not re-derive them)."""

    open_tasks: int  # open, not archived
    tasks_due_today: int  # open, due during today's business day (earlier today included)
    overdue_tasks: int  # open, due before now
    meetings_today: int  # scheduled or completed, starting during today's business day
    upcoming_meetings: int  # scheduled, starting now or later


# --- activities --------------------------------------------------------------------------------
def _implied_type(filters: ActivityFilters) -> str | None:
    """A status belongs to one type (open: tasks; scheduled: meetings), so does "overdue":
    stating the type lets PostgreSQL use the per-type schedule indexes."""
    if filters.overdue or filters.status == ActivityStatus.OPEN:
        return ActivityType.TASK
    if filters.status == ActivityStatus.SCHEDULED:
        return ActivityType.MEETING
    return filters.type


def ordering(name: str, scope: AccessScope, filters: ActivityFilters) -> KeysetOrdering:
    """The keyset ordering a list request uses. Organisation-wide lists without an owner,
    lead or opportunity filter sort newest/oldest by the organisation's own column; every
    narrower list by the column the owner, lead and opportunity indexes are built on."""
    narrowed = filters.owner_id or filters.lead_id or filters.opportunity_id
    if scope.is_organization_wide and not narrowed and name in _ORGANISATION_ORDERINGS:
        return _ORGANISATION_ORDERINGS[name]
    return ORDERINGS[name]


def _filtered(scope: AccessScope, filters: ActivityFilters, *, now: datetime) -> QuerySet[Activity]:
    queryset = scope.apply(Activity.objects.all())
    queryset = queryset.filter(archived_at__isnull=not filters.archived)
    if filters.owner_id is not None:
        queryset = queryset.filter(owner_id=filters.owner_id)  # narrows the scope only
    activity_type = _implied_type(filters)
    if activity_type is not None:
        queryset = queryset.filter(type=activity_type)
    if filters.status is not None:
        queryset = queryset.filter(status=filters.status)
    if filters.overdue:
        # Undated tasks sort as 9999-12-31, so they are never overdue.
        queryset = queryset.filter(status=ActivityStatus.OPEN, schedule_sort__lt=now)
    if filters.current or filters.upcoming:
        # Open tasks and scheduled meetings: "open" is only a task's status and "scheduled"
        # only a meeting's (activities_activity_status_valid), so the status alone says it,
        # in the words of the current-work indexes' condition.
        queryset = queryset.filter(status__in=CURRENT_STATUSES)
    if filters.upcoming:
        queryset = queryset.filter(schedule_sort__gte=now)
    if filters.cancelled is False:
        queryset = queryset.exclude(status=ActivityStatus.CANCELLED)  # notes (NULL) stay
    elif filters.cancelled is True:
        queryset = queryset.filter(status=ActivityStatus.CANCELLED)
    if filters.lead_id is not None:
        queryset = queryset.filter(lead_id=filters.lead_id)
    if filters.opportunity_id is not None:
        queryset = queryset.filter(opportunity_id=filters.opportunity_id)
    if filters.date_from is not None:
        queryset = queryset.filter(schedule_sort__gte=business_midnight(filters.date_from))
    if filters.date_to is not None:
        end = business_midnight(filters.date_to + timedelta(days=1))
        queryset = queryset.filter(schedule_sort__lt=end)
    return queryset


def activity_list(
    scope: AccessScope, filters: ActivityFilters, *, now: datetime
) -> QuerySet[Activity]:
    """The scoped, filtered activities of a list page (ordered and paginated by the caller):
    lead, opportunity, owner and author joined, the text cut to a preview in PostgreSQL."""
    return (
        _filtered(scope, filters, now=now)
        .select_related("lead", "opportunity", "owner", "created_by")
        .only(*_LIST_FIELDS)
        .annotate(text_preview=Substr("description", 1, PREVIEW_LENGTH + 1))
    )


def _with_relations(queryset: QuerySet[Activity]) -> QuerySet[Activity]:
    return queryset.select_related(
        "lead", "opportunity", "owner", "created_by", "completed_by", "cancelled_by"
    ).only(
        *_LIST_FIELDS,
        "description",
        "location",
        "meeting_url",
        "lead__archived_at",
        "opportunity__archived_at",
        *(f"completed_by__{f}" for f in _PERSON),
        *(f"cancelled_by__{f}" for f in _PERSON),
    )


def activity_detail(scope: AccessScope, activity_id: UUID) -> Activity:
    """One activity if `scope` may see it; otherwise NotFoundError, exactly as if it didn't
    exist, so a guessed id reveals nothing."""
    found = _with_relations(scope.apply(Activity.objects.filter(pk=activity_id))).first()
    if found is None:
        raise NotFoundError()
    return found


def activity_by_id(activity_id: UUID) -> Activity:
    """Unscoped: only for services returning a record the actor has just changed."""
    return _with_relations(Activity.objects.filter(pk=activity_id)).get()


def activity_summary(scope: AccessScope, *, now: datetime) -> ActivitySummary:
    """Open tasks, tasks due today, overdue tasks, today's meetings and upcoming meetings in
    `scope`, in one aggregate query over the authoritative table. "Today" is the business
    day containing `now` (core.business_time); archived activities never count."""
    start, end = today_bounds(now)
    today = Q(schedule_sort__gte=start, schedule_sort__lt=end)
    task = Q(type=ActivityType.TASK, status=ActivityStatus.OPEN)
    meeting = Q(
        type=ActivityType.MEETING,
        status__in=[ActivityStatus.SCHEDULED, ActivityStatus.COMPLETED],
    )
    # Only meetings from today's start on can count (today's or upcoming): stating it keeps
    # past meetings out of the organisation-wide scan, which then follows open work rather
    # than all history (Phase 4 review: a sequential scan before; 37 ms at 2M activities).
    row = (
        scope.apply(Activity.objects.filter(archived_at__isnull=True))
        .filter(task | (meeting & Q(schedule_sort__gte=start)))
        .aggregate(
            open_tasks=Count("id", filter=task),
            tasks_due_today=Count("id", filter=task & today),
            overdue_tasks=Count("id", filter=task & Q(schedule_sort__lt=now)),
            meetings_today=Count("id", filter=meeting & today),
            upcoming_meetings=Count(
                "id",
                filter=Q(type=ActivityType.MEETING, status=ActivityStatus.SCHEDULED)
                & Q(schedule_sort__gte=now),
            ),
        )
    )
    return ActivitySummary(**row)


def upcoming_meetings(
    scope: AccessScope, *, now: datetime, limit: int = UPCOMING_MEETINGS_DEFAULT
) -> list[Activity]:
    """The next scheduled meetings in `scope`, soonest first (bounded; for Phase 5)."""
    if not 1 <= limit <= 50:
        raise ValueError("limit out of range")
    return list(
        activity_list(
            scope,
            ActivityFilters(type=ActivityType.MEETING, status=ActivityStatus.SCHEDULED),
            now=now,
        )
        .filter(schedule_sort__gte=now)
        .order_by("schedule_sort", "id")[:limit]
    )


# --- timelines ---------------------------------------------------------------------------------
_TIMELINE_FIELDS = (
    "id",
    "kind",
    "occurred_at",
    "data",
    "lead_id",
    "opportunity_id",
    "activity_id",
    *(f"actor__{f}" for f in _PERSON),
    "activity__id",
    "activity__type",
    "activity__title",
    "activity__status",
    "activity__due_at",
    "activity__starts_at",
    "activity__ends_at",
    "activity__owner_id",
    "activity__archived_at",
    "opportunity__id",
    "opportunity__title",
    "opportunity__status",
    "opportunity__owner_id",
    "opportunity__archived_at",
)


def _activity_visible(scope: AccessScope) -> Q:
    return Q(activity__isnull=False, activity__archived_at__isnull=True) & scope.condition(
        "activity__owner_id"
    )


def _entries(queryset: QuerySet[TimelineEntry]) -> QuerySet[TimelineEntry]:
    return (
        queryset.select_related("actor", "activity", "opportunity")
        .only(*_TIMELINE_FIELDS)
        .annotate(text_preview=Substr("activity__description", 1, PREVIEW_LENGTH + 1))
    )


def lead_timeline(scope: AccessScope, lead_id: UUID) -> QuerySet[TimelineEntry]:
    """A lead's history, if `scope` may see the lead (NotFoundError otherwise). Lead events
    are visible to whoever sees the lead; an event about an activity or an opportunity only
    while that record is visible in the same scope and not archived: the previous owner's
    completed meetings and closed opportunities stay theirs after a reassignment (owner-
    based visibility, docs/authorization.md#related-records-and-timelines). Newest first;
    paginated by the caller."""
    lead_selectors.lead_ref(scope, lead_id)
    lead_event = Q(activity__isnull=True, opportunity__isnull=True)
    opportunity_event = Q(
        activity__isnull=True, opportunity__isnull=False, opportunity__archived_at__isnull=True
    ) & scope.condition("opportunity__owner_id")
    return _entries(
        TimelineEntry.objects.filter(lead_id=lead_id).filter(
            lead_event | opportunity_event | _activity_visible(scope)
        )
    )


def opportunity_timeline(scope: AccessScope, opportunity_id: UUID) -> QuerySet[TimelineEntry]:
    """An opportunity's history (creation, stage changes, won, lost, reopened) and its
    visible activities, if `scope` may see the opportunity (NotFoundError otherwise). Lead
    events are not repeated here."""
    pipeline_selectors.opportunity_ref(scope, opportunity_id)
    return _entries(
        TimelineEntry.objects.filter(opportunity_id=opportunity_id).filter(
            Q(activity__isnull=True) | _activity_visible(scope)
        )
    )


def people_in(entries: Iterable[TimelineEntry]) -> dict[str, User]:
    """The users named in a page of entries' snapshots (a reassignment's previous and new
    owner, a lead's owner when created), in one query (none if the page names nobody)."""
    ids: set[UUID] = set()
    for entry in entries:
        data: dict[str, Any] = entry.data or {}
        for key in USER_KEYS:
            try:
                ids.add(UUID(str(data[key])))
            except (KeyError, ValueError):
                continue  # not named, or not an id (never trusted blindly)
    if not ids:
        return {}
    return {str(user.pk): user for user in User.objects.filter(pk__in=ids).only(*_PERSON)}
