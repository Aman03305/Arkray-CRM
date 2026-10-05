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

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from django.db.models import Count, Q, QuerySet, Value
from django.db.models.functions import Greatest, StrIndex, Substr, Upper

from arkray.core.access import AccessScope
from arkray.core.business_time import business_midnight, today_bounds
from arkray.core.errors import NotFoundError
from arkray.core.keyset import KeysetOrdering, SortKey
from arkray.core.knowledge import KnowledgeDocument, SourceType, compose
from arkray.core.ranking import Matches, SearchQuery, top_matches
from arkray.identity.models import User
from arkray.leads import selectors as lead_selectors
from arkray.pipeline import selectors as pipeline_selectors

from .models import (
    CURRENT_STATUSES,
    SEARCH_TEXT,
    Activity,
    ActivityStatus,
    ActivityType,
    TimelineEntry,
)
from .timeline import USER_KEYS

PREVIEW_LENGTH = 240  # characters of a note (or description) shown in lists and timelines
# A note found by search shows the same bounded preview, starting this many characters
# before the first search word, so the match is in view (docs/search.md#notes).
PREVIEW_CONTEXT = 60
UPCOMING_MEETINGS_DEFAULT = 5
NEXT_TASKS_DEFAULT = 5

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
def listable(scope: AccessScope) -> QuerySet[Activity]:
    """Every activity the scope may see, archived or not, whatever the filters (for checks
    such as "is this record still visible to the caller")."""
    return scope.apply(Activity.objects.all())


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
        "lead", "opportunity", "owner", "created_by", "completed_by", "cancelled_by", "edited_by"
    ).only(
        *_LIST_FIELDS,
        "description",
        "location",
        "meeting_url",
        "edited_at",
        "lead__archived_at",
        "opportunity__archived_at",
        *(f"completed_by__{f}" for f in _PERSON),
        *(f"cancelled_by__{f}" for f in _PERSON),
        *(f"edited_by__{f}" for f in _PERSON),
    )


NOTE_ORDERING = KeysetOrdering(
    "-created_at", (SortKey("created_at", descending=True), SortKey("id", descending=True))
)


def opportunity_notes(scope: AccessScope, opportunity_id: UUID) -> QuerySet[Activity]:
    """An opportunity's notes (not archived), newest first, whole text: the deal page's Notes.
    NotFoundError unless `scope` may see the opportunity; each note must itself be visible in
    the scope (notes follow their lead, like the timeline's). Paginated by the caller; served
    by activities_opportunity_idx."""
    pipeline_selectors.opportunity_ref(scope, opportunity_id)
    return _with_relations(
        scope.apply(
            Activity.objects.filter(
                opportunity_id=opportunity_id, type=ActivityType.NOTE, archived_at__isnull=True
            )
        )
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


# What a global search result shows of a task, meeting or note: type, title, status, when,
# the lead (only if visible in the scope; the serializer decides) and the owner. A note only
# as a bounded preview, cut in PostgreSQL: the whole body never leaves the database.
_SEARCH_RESULT_FIELDS = (
    "id",
    "type",
    "title",
    "status",
    "priority",
    "due_at",
    "starts_at",
    "ends_at",
    "location",
    "archived_at",
    "created_at",
    "lead__id",
    "lead__first_name",
    "lead__last_name",
    "lead__organization_name",
    "lead__display_name",
    "lead__owner_id",
    *(f"owner__{f}" for f in _PERSON),
)


def search(
    scope: AccessScope, query: SearchQuery, activity_type: ActivityType, *, limit: int
) -> Matches[Activity]:
    """Global search's tasks, meetings or notes: every search word occurs in the type's search
    text (models.SEARCH_TEXT, case-insensitive substring), over the activities `scope` may
    list, archived ones left out; completed and cancelled work alike (it is history). Ranked
    by core.ranking against the title (a note's body), then by the activity's own date, the
    latest first: a task's due date (undated tasks first: 9999-12-31), a meeting's start, a
    note's creation (`schedule_sort`). That order is served for one type by the type's own
    schedule indexes (per owner and organisation-wide), so the recent pass reads exactly
    that type's newest records however rare the type is (a created-order walk of all
    activities would read every other type's rows to find them; Phase 7 review).

    A note's body is returned only as `text_preview`: PREVIEW_LENGTH (+1, to tell whether it
    was cut) characters starting PREVIEW_CONTEXT before the first search word, and
    `preview_start`, where it starts (1 = the beginning)."""
    scoped = scope.apply(
        Activity.objects.filter(type=activity_type, archived_at__isnull=True)
    ).annotate(search_text=SEARCH_TEXT[activity_type])
    newest = ("-schedule_sort", "-id")
    is_note = activity_type == ActivityType.NOTE

    def shape(found: QuerySet[Activity]) -> QuerySet[Activity]:
        found = found.select_related("lead", "owner").only(*_SEARCH_RESULT_FIELDS)
        if not is_note:
            return found
        start = Greatest(
            StrIndex(Upper("description"), Upper(Value(query.terms[0]))) - PREVIEW_CONTEXT, 1
        )
        return found.annotate(
            preview_start=start,
            text_preview=Substr("description", start, PREVIEW_LENGTH + 1),
        )

    return top_matches(
        scoped,
        text="search_text",
        query=query,
        label="description" if is_note else "title",
        newest=newest,
        limit=limit,
        shape=shape,
    )


def activity_summary(scope: AccessScope, *, now: datetime) -> ActivitySummary:
    """Open tasks, tasks due today, overdue tasks, today's meetings and upcoming meetings in
    `scope`, from the authoritative table. "Today" is the business day containing `now`
    (core.business_time); archived activities never count.

    Two aggregate queries, each one bounded index range: the open tasks, and the meetings
    from today's start on (a past meeting can't be today's or upcoming). One query over
    "open task OR meeting from today" (Phase 4) read the owner's whole history when the
    planner served the OR from the owner's index (13-25 ms for the heaviest owner at 2M
    activities), and organisation-wide flipped to a sequential scan of every activity as open
    work grew (154-243 ms; Phase 5 performance review, P2). Split, each part reads only what
    it counts (index-only for one owner or the organisation)."""
    start, end = today_bounds(now)
    live = scope.apply(Activity.objects.filter(archived_at__isnull=True))
    tasks = live.filter(type=ActivityType.TASK, status=ActivityStatus.OPEN).aggregate(
        open_tasks=Count("id"),
        tasks_due_today=Count("id", filter=Q(schedule_sort__gte=start, schedule_sort__lt=end)),
        overdue_tasks=Count("id", filter=Q(schedule_sort__lt=now)),
    )
    meetings = live.filter(
        type=ActivityType.MEETING,
        status__in=[ActivityStatus.SCHEDULED, ActivityStatus.COMPLETED],
        schedule_sort__gte=start,
    ).aggregate(
        meetings_today=Count("id", filter=Q(schedule_sort__lt=end)),
        upcoming_meetings=Count(
            "id", filter=Q(status=ActivityStatus.SCHEDULED, schedule_sort__gte=now)
        ),
    )
    return ActivitySummary(**tasks, **meetings)


@dataclass(frozen=True, slots=True)
class OwnerTaskCount:
    owner_id: UUID
    open_tasks: int
    overdue_tasks: int


def task_counts_by_owner(scope: AccessScope, *, now: datetime, limit: int) -> list[OwnerTaskCount]:
    """Open and overdue tasks per owner in `scope` (organisation-wide questions such as "who
    has the most overdue tasks"), most overdue first, at most `limit` owners. The same
    definitions as `activity_summary`. One grouped query over the open tasks."""
    if not 1 <= limit <= 50:
        raise ValueError("limit out of range")
    rows = (
        scope.apply(
            Activity.objects.filter(
                archived_at__isnull=True, type=ActivityType.TASK, status=ActivityStatus.OPEN
            )
        )
        .values("owner_id")
        .order_by()
        .annotate(
            open_tasks=Count("id"),
            overdue_tasks=Count("id", filter=Q(schedule_sort__lt=now)),
        )
        .order_by("-overdue_tasks", "-open_tasks", "owner_id")[:limit]
    )
    return [OwnerTaskCount(**row) for row in rows]


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


def next_open_tasks(
    scope: AccessScope, *, now: datetime, limit: int = NEXT_TASKS_DEFAULT
) -> list[Activity]:
    """The first open tasks in `scope` by due time (bounded; for Phase 5): the start of the
    Activities page's Tasks tab (open, soonest due first), so overdue tasks come first,
    then today's, then later ones, and undated tasks last."""
    if not 1 <= limit <= 50:
        raise ValueError("limit out of range")
    return list(
        activity_list(
            scope, ActivityFilters(type=ActivityType.TASK, status=ActivityStatus.OPEN), now=now
        ).order_by("schedule_sort", "id")[:limit]
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


# --- Ask Arkray's semantic index (Phase 8, docs/rag-architecture.md) -------------------------
# Notes are embedded; tasks and meetings only when they have a description (a bare title is
# found by global search). The text is the title and the description, nothing else: never
# a meeting's link (links often carry passcodes) or location.
_KNOWLEDGE_FIELDS = (
    "id",
    "type",
    "owner_id",
    "lead_id",
    "opportunity_id",
    "title",
    "description",
    "due_at",
    "starts_at",
    "created_at",
    "updated_at",
)
_HAS_KNOWLEDGE = Q(archived_at__isnull=True) & ~Q(description="")
_KNOWLEDGE_LABELS = {
    ActivityType.TASK: ("Task", SourceType.TASK),
    ActivityType.MEETING: ("Meeting", SourceType.MEETING),
    ActivityType.NOTE: ("Note", SourceType.NOTE),
}


def _knowledge(queryset: QuerySet[Activity]) -> list[KnowledgeDocument]:
    documents = []
    for activity in queryset.filter(_HAS_KNOWLEDGE).only(*_KNOWLEDGE_FIELDS):
        kind, source_type = _KNOWLEDGE_LABELS[ActivityType(activity.type)]
        when = activity.starts_at or activity.due_at or activity.created_at
        documents.append(
            KnowledgeDocument(
                source_type=source_type,
                source_id=activity.pk,
                owner_id=activity.owner_id,
                lead_id=activity.lead_id,
                opportunity_id=activity.opportunity_id,
                label=activity.title or kind,
                text=compose(
                    (kind, activity.title),
                    (
                        "Text" if activity.type == ActivityType.NOTE else "Description",
                        activity.description,
                    ),
                ),
                occurred_at=when,
                updated_at=activity.updated_at,
            )
        )
    return documents


def knowledge_documents(
    scope: AccessScope, activity_ids: Collection[UUID]
) -> list[KnowledgeDocument]:
    """The embeddable text of the given activities that `scope` may see now (archived ones
    have none). Retrieval re-reads every hit through this, so a note that followed its lead
    to a new owner, or was archived, can never surface from a stale vector."""
    if not activity_ids:
        return []
    return _knowledge(scope.apply(Activity.objects.filter(pk__in=list(activity_ids))))


def knowledge_documents_for_indexing(
    activity_ids: Collection[UUID],
) -> list[KnowledgeDocument]:
    """Unscoped: only for the indexer (runs as the system)."""
    if not activity_ids:
        return []
    return _knowledge(Activity.objects.filter(pk__in=list(activity_ids)))


def knowledge_source_ids(*, after: UUID | None, limit: int) -> list[UUID]:
    """Ids of activities with a knowledge document, in id order (indexer only)."""
    queryset = Activity.objects.filter(_HAS_KNOWLEDGE)
    if after is not None:
        queryset = queryset.filter(pk__gt=after)
    return list(queryset.order_by("pk").values_list("pk", flat=True)[:limit])
