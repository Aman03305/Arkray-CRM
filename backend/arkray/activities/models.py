"""Activities: tasks, meetings and notes in one table, and the lead/opportunity timeline
(docs/activities.md, [ADR-0009](../../../docs/adr/0009-unified-activity-model.md),
[ADR-0020](../../../docs/adr/0020-activity-integrity.md),
[ADR-0021](../../../docs/adr/0021-materialized-timeline.md)).

Every activity belongs to a lead: directly, or through an opportunity of that lead. Which
columns a type uses, and which statuses it may have, are CHECK constraints (the matrix in
docs/database.md#activities_activity), so no code path can store a "note with a due date"
or a "completed task without a completion time".

Integrity the database enforces, whatever code path writes (migration 0002):
- an activity linked to an opportunity has that opportunity's lead: (opportunity_id,
  lead_id) references the opportunity's (id, lead_id);
- current work (an open task, a scheduled meeting, a note) is owned by its lead's owner:
  (lead_id, current_owner_id) references the lead's (id, owner_id), checked at commit, the
  same mechanism as open opportunities (ADR-0018). Completed and cancelled tasks and
  meetings keep the owner who had them (their `current_owner_id` is NULL);
- a timeline entry about an activity or an opportunity is filed under its lead;
- the timeline is append-only (trigger).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from django.db import models
from django.db.models import Case, ExpressionWrapper, F, Func, Q, Value, When
from django.db.models.functions import Coalesce, Length
from django.db.models.lookups import LessThanOrEqual
from django.utils import timezone

from arkray.core.models import AppendOnlyModel, TimeStampedModel, UUIDPrimaryKeyModel
from arkray.identity.models import User
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity

TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 10_000
LOCATION_MAX_LENGTH = 200
MEETING_URL_MAX_LENGTH = 500
# Due dates and meeting times: real timestamps in a sane range (naive values are refused
# at the API), and a meeting lasts at most a day.
EARLIEST_TIME = datetime(2000, 1, 1, tzinfo=UTC)
LATEST_TIME = datetime(2100, 1, 1, tzinfo=UTC)
MAX_MEETING_DURATION = timedelta(hours=24)
# "No due date" sorts after every real one, which keeps the schedule sort key NOT NULL so
# keyset cursors can bound index scans (the Phase 2 lesson from leads' last-contact sort).
UNDATED = datetime(9999, 12, 31, tzinfo=UTC)
# Stands for "no opportunity" in the keys that bind a timeline entry to its activity's
# opportunity (a foreign key can't compare NULLs).
NO_OPPORTUNITY = uuid.UUID(int=0)


class ActivityType(models.TextChoices):
    """Phase 4 types. Call, email and WhatsApp are planned: each adds an enum value, its
    statuses and columns with their CHECKs, and a type spec (spec.py), in one migration."""

    TASK = "task", "Task"
    MEETING = "meeting", "Meeting"
    NOTE = "note", "Note"


class ActivityStatus(models.TextChoices):
    """Stable keys (labels are the UI's business). Tasks: open, completed, cancelled.
    Meetings: scheduled, completed, cancelled. Notes have no status (NULL)."""

    OPEN = "open", "Open"
    SCHEDULED = "scheduled", "Scheduled"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class Priority(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"


TASK_STATUSES = (ActivityStatus.OPEN, ActivityStatus.COMPLETED, ActivityStatus.CANCELLED)
MEETING_STATUSES = (ActivityStatus.SCHEDULED, ActivityStatus.COMPLETED, ActivityStatus.CANCELLED)
# Actionable work: follows its lead when the lead is reassigned.
CURRENT_STATUSES = (ActivityStatus.OPEN, ActivityStatus.SCHEDULED)
# Historical outcomes: keep the owner who had the work.
CLOSED_STATUSES = (ActivityStatus.COMPLETED, ActivityStatus.CANCELLED)

# The owner while the activity is current work (an open task, a scheduled meeting, any
# note), else NULL: the key of the ownership foreign key (MATCH SIMPLE skips NULLs).
CURRENT_OWNER = Case(
    When(type=ActivityType.NOTE, then=F("owner_id")),
    When(status__in=CURRENT_STATUSES, then=F("owner_id")),
    default=None,
)
# One "when" per row for schedules and date filters: a task's due time ("undated" last), a
# meeting's start, a note's creation.
OPPORTUNITY_KEY = Coalesce(F("opportunity_id"), Value(NO_OPPORTUNITY))
SCHEDULE_SORT = Case(
    When(type=ActivityType.TASK, then=Coalesce(F("due_at"), Value(UNDATED))),
    When(type=ActivityType.MEETING, then=F("starts_at")),
    default=F("created_at"),
    output_field=models.DateTimeField(),
)


def _set_exactly_when(status: ActivityStatus, at: str, by: str) -> Q:
    """`at` and `by` are set exactly while the status is `status`. Written NULL-safe: a note
    (status NULL) must satisfy the second branch, never slip through a NULL comparison
    (a CHECK passes on NULL). Django's negation adds `status IS NOT NULL` itself."""
    return Q(
        status__isnull=False, status=status, **{f"{at}__isnull": False, f"{by}__isnull": False}
    ) | (~Q(status=status) & Q(**{f"{at}__isnull": True, f"{by}__isnull": True}))


class Activity(UUIDPrimaryKeyModel, TimeStampedModel):
    type = models.CharField(max_length=16, choices=ActivityType.choices)
    # Fixed for the activity's lifetime (so is the opportunity): its history is filed under
    # this lead's timeline, and the database binds the two (module docstring).
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name="+", db_index=False)
    opportunity = models.ForeignKey(
        Opportunity,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_index=False,
    )
    # The responsible user and the authorization key of every read path. While the activity
    # is current work it is always the lead's owner (database-enforced); nobody chooses it.
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    # Who created it (a note's author). Never changes, not even when the lead changes hands.
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)

    title = models.CharField(max_length=TITLE_MAX_LENGTH, blank=True, default="")
    # A task's description, a meeting's agenda, a note's body.
    description = models.TextField(blank=True, default="")
    status = models.CharField(  # noqa: DJ001 — NULL for notes, which have no status
        max_length=16, choices=ActivityStatus.choices, null=True, blank=True
    )
    priority = models.CharField(  # noqa: DJ001 — tasks only
        max_length=8, choices=Priority.choices, null=True, blank=True
    )
    due_at = models.DateTimeField(null=True, blank=True)  # tasks, optional
    starts_at = models.DateTimeField(null=True, blank=True)  # meetings, required
    ends_at = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=LOCATION_MAX_LENGTH, blank=True, default="")
    meeting_url = models.CharField(max_length=MEETING_URL_MAX_LENGTH, blank=True, default="")

    # Who closed it and when: historical attribution, kept exactly while closed.
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+", db_index=False
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+", db_index=False
    )

    # Activities are never deleted; archiving hides them from lists, timelines and counts.
    archived_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    current_owner_id = models.GeneratedField(
        expression=CURRENT_OWNER, output_field=models.UUIDField(null=True), db_persist=True
    )
    schedule_sort = models.GeneratedField(
        expression=SCHEDULE_SORT, output_field=models.DateTimeField(), db_persist=True
    )
    # The opportunity link with "none" as a value: part of the key every timeline entry
    # about this activity references (see TimelineEntry), so neither can disagree.
    opportunity_key = models.GeneratedField(
        expression=OPPORTUNITY_KEY, output_field=models.UUIDField(), db_persist=True
    )
    # created_at again, as the organisation-wide newest/oldest sort key. A separate column
    # so the organisation-wide index can never serve (and be chosen for) one owner's list:
    # PostgreSQL walked it for owners holding a large share of the rows, filtering the whole
    # organisation when their matches were rare or old (Phase 4 review, P1: 48 ms at 403k
    # activities, 380-500 ms at 2M; the owner's own index answers in under a millisecond).
    created_sort = models.GeneratedField(
        expression=ExpressionWrapper(F("created_at"), output_field=models.DateTimeField()),
        output_field=models.DateTimeField(),
        db_persist=True,
    )

    class Meta:
        db_table = "activities_activity"
        # Every index serves a measured query (docs/database.md#activities_activity).
        indexes = [
            # One owner's tasks / meetings / notes by status, in schedule order (the
            # Activities page's tabs; open, due today and overdue counts; today's and
            # upcoming meetings).
            models.Index(
                F("owner"),
                F("type"),
                F("status"),
                F("schedule_sort"),
                F("id"),
                name="activities_owner_sched_idx",
                condition=Q(archived_at__isnull=True),
            ),
            # One owner's activities of any type or status in schedule order (the "All" and
            # "any status" views by due/start, open-and-scheduled work, date ranges): without
            # it, a bitmap scan and a sort of the owner's whole history (9-24 ms at 30k).
            models.Index(
                F("owner"),
                F("schedule_sort"),
                F("id"),
                name="activities_owner_when_idx",
                condition=Q(archived_at__isnull=True),
            ),
            # The same for one type of any status (tasks / meetings, "any status" by
            # due/start), and for current work only (open tasks and scheduled meetings, the
            # "Open and scheduled" and "upcoming" views): the index above holds every closed
            # activity too, which a page of open work had to filter past (2M activities:
            # 38 ms and 124 ms for the heaviest owner, 0.5 s organisation-wide; Phase 4 review).
            # Current work stays small however much history accumulates.
            models.Index(
                F("owner"),
                F("type"),
                F("schedule_sort"),
                F("id"),
                name="activities_owner_type_when_idx",
                condition=Q(archived_at__isnull=True),
            ),
            models.Index(
                F("owner"),
                F("schedule_sort"),
                F("id"),
                name="activities_owner_current_idx",
                condition=Q(archived_at__isnull=True, status__in=CURRENT_STATUSES),
            ),
            # One owner's activities, newest first (the "All" view and the archived view).
            models.Index(
                F("owner"),
                F("created_at").desc(),
                F("id").desc(),
                name="activities_owner_created_idx",
            ),
            # The same, organisation-wide (administrators).
            models.Index(
                F("type"),
                F("status"),
                F("schedule_sort"),
                F("id"),
                name="activities_sched_idx",
                condition=Q(archived_at__isnull=True),
            ),
            models.Index(
                F("schedule_sort"),
                F("id"),
                name="activities_when_idx",
                condition=Q(archived_at__isnull=True),
            ),
            models.Index(
                F("type"),
                F("schedule_sort"),
                F("id"),
                name="activities_type_when_idx",
                condition=Q(archived_at__isnull=True),
            ),
            models.Index(
                F("schedule_sort"),
                F("id"),
                name="activities_current_idx",
                condition=Q(archived_at__isnull=True, status__in=CURRENT_STATUSES),
            ),
            # Organisation-wide newest/oldest (no owner, lead or opportunity filter), on its
            # own sort column (see created_sort). Ascending: rows arrive newest last, so a
            # descending index would split pages at its left edge on every insert (review:
            # 28 MB against 15 MB at 400k); a backward scan serves "newest first".
            models.Index(F("created_sort"), F("id"), name="activities_created_idx"),
            # A lead's activities by due/start (lead page's open work; the lead filter, also
            # organisation-wide: 64 ms walking the schedule index before, review), the
            # reassignment's lock query, and the ownership foreign key's lookups when a lead's
            # owner changes. Not partial, so the archived view and the key checks use it too.
            models.Index(F("lead"), F("schedule_sort"), F("id"), name="activities_lead_idx"),
            # An opportunity's activities (opportunity page, the opportunity filter).
            models.Index(
                F("opportunity"),
                F("created_at").desc(),
                F("id").desc(),
                name="activities_opportunity_idx",
                condition=Q(opportunity__isnull=False),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(type__in=ActivityType.values), name="activities_activity_type_valid"
            ),
            # Each type's statuses; notes have none. (Every comparison of a nullable column
            # is paired with IS NOT NULL: a CHECK passes on NULL.)
            models.CheckConstraint(
                condition=Q(type=ActivityType.TASK, status__isnull=False, status__in=TASK_STATUSES)
                | Q(type=ActivityType.MEETING, status__isnull=False, status__in=MEETING_STATUSES)
                | Q(type=ActivityType.NOTE, status__isnull=True),
                name="activities_activity_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    type=ActivityType.TASK, priority__isnull=False, priority__in=Priority.values
                )
                | (~Q(type=ActivityType.TASK) & Q(priority__isnull=True)),
                name="activities_activity_priority_only_tasks",
            ),
            # Tasks and meetings have a title (with something visible in it); a note is its
            # body alone, which must have something in it.
            models.CheckConstraint(
                condition=Q(type=ActivityType.NOTE, title="")
                | (~Q(type=ActivityType.NOTE) & Q(title__regex=r"\S")),
                name="activities_activity_title_matches_type",
            ),
            models.CheckConstraint(
                condition=~Q(type=ActivityType.NOTE) | Q(description__regex=r"\S"),
                name="activities_activity_note_has_body",
            ),
            models.CheckConstraint(
                condition=LessThanOrEqual(Length("description"), DESCRIPTION_MAX_LENGTH),
                name="activities_activity_description_length",
            ),
            models.CheckConstraint(
                condition=Q(type=ActivityType.TASK) | Q(due_at__isnull=True),
                name="activities_activity_due_only_tasks",
            ),
            models.CheckConstraint(
                condition=Q(due_at__isnull=True)
                | Q(due_at__gte=EARLIEST_TIME, due_at__lt=LATEST_TIME),
                name="activities_activity_due_range",
            ),
            # Meetings have both times, end after start; nothing else has times.
            models.CheckConstraint(
                condition=Q(
                    type=ActivityType.MEETING,
                    starts_at__isnull=False,
                    ends_at__isnull=False,
                    ends_at__gt=F("starts_at"),
                )
                | (~Q(type=ActivityType.MEETING) & Q(starts_at__isnull=True, ends_at__isnull=True)),
                name="activities_activity_meeting_times",
            ),
            # At most 24 hours, measured as an absolute duration: "starts_at + 1 day" would
            # follow the session time zone's daylight-saving rules (review). NULL for the
            # types without times, whose presence the CHECK above decides.
            models.CheckConstraint(
                condition=LessThanOrEqual(
                    ExpressionWrapper(
                        F("ends_at") - F("starts_at"), output_field=models.DurationField()
                    ),
                    MAX_MEETING_DURATION,
                ),
                name="activities_activity_meeting_duration",
            ),
            models.CheckConstraint(
                condition=Q(starts_at__isnull=True)
                | Q(starts_at__gte=EARLIEST_TIME, starts_at__lt=LATEST_TIME),
                name="activities_activity_starts_range",
            ),
            models.CheckConstraint(
                condition=Q(type=ActivityType.MEETING) | Q(location="", meeting_url=""),
                name="activities_activity_place_only_meetings",
            ),
            models.CheckConstraint(
                condition=Q(meeting_url="") | Q(meeting_url__startswith="https://"),
                name="activities_activity_meeting_url_https",
            ),
            models.CheckConstraint(
                condition=_set_exactly_when(
                    ActivityStatus.COMPLETED, "completed_at", "completed_by"
                ),
                name="activities_activity_completed_matches_status",
            ),
            models.CheckConstraint(
                condition=_set_exactly_when(
                    ActivityStatus.CANCELLED, "cancelled_at", "cancelled_by"
                ),
                name="activities_activity_cancelled_matches_status",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="activities_activity_version_positive"
            ),
            models.CheckConstraint(
                condition=Q(schedule_sort__isnull=False),
                name="activities_activity_schedule_sort_present",
            ),
            # Trivially unique; the target of the timeline's key, so an activity's history is
            # always filed under its own lead and opportunity and named for its own type, and
            # none of those can change once it has history (review).
            models.UniqueConstraint(
                fields=["id", "lead", "opportunity_key", "type"],
                name="activities_activity_timeline_key",
            ),
        ]

    def __str__(self) -> str:
        return f"Activity({self.pk})"  # never the title or body: this string can reach logs

    @property
    def is_current(self) -> bool:
        return self.type == ActivityType.NOTE or self.status in CURRENT_STATUSES

    def is_completable(self, now: datetime) -> bool:
        """Could it be completed now (rules of services.complete_activity, permissions
        aside): an open task, or a scheduled meeting that has started; not archived."""
        if self.archived_at is not None:
            return False
        if self.type == ActivityType.TASK:
            return self.status == ActivityStatus.OPEN
        if self.type == ActivityType.MEETING:
            return (
                self.status == ActivityStatus.SCHEDULED
                and self.starts_at is not None
                and self.starts_at <= now
            )
        return False

    def is_overdue(self, now: datetime) -> bool:
        """Past its due time (a task) or its end (a meeting) while still open or scheduled.
        Computed when read, never stored: it changes by time passing alone."""
        if self.archived_at is not None:
            return False
        if self.type == ActivityType.TASK:
            return (
                self.status == ActivityStatus.OPEN and self.due_at is not None and self.due_at < now
            )
        if self.type == ActivityType.MEETING:
            return (
                self.status == ActivityStatus.SCHEDULED
                and self.ends_at is not None
                and self.ends_at < now
            )
        return False


class TimelineKind(models.TextChoices):
    LEAD_CREATED = "lead.created", "Lead created"
    LEAD_STATUS_CHANGED = "lead.status_changed", "Lead status changed"
    LEAD_REASSIGNED = "lead.reassigned", "Lead reassigned"
    LEAD_ARCHIVED = "lead.archived", "Lead archived"
    LEAD_RESTORED = "lead.restored", "Lead restored"
    OPPORTUNITY_CREATED = "opportunity.created", "Opportunity created"
    OPPORTUNITY_STAGE_CHANGED = "opportunity.stage_changed", "Opportunity stage changed"
    OPPORTUNITY_WON = "opportunity.won", "Opportunity won"
    OPPORTUNITY_LOST = "opportunity.lost", "Opportunity lost"
    OPPORTUNITY_REOPENED = "opportunity.reopened", "Opportunity reopened"
    TASK_CREATED = "task.created", "Task created"
    TASK_COMPLETED = "task.completed", "Task completed"
    TASK_CANCELLED = "task.cancelled", "Task cancelled"
    TASK_REOPENED = "task.reopened", "Task reopened"
    MEETING_SCHEDULED = "meeting.scheduled", "Meeting scheduled"
    MEETING_RESCHEDULED = "meeting.rescheduled", "Meeting rescheduled"
    MEETING_COMPLETED = "meeting.completed", "Meeting completed"
    MEETING_CANCELLED = "meeting.cancelled", "Meeting cancelled"
    MEETING_REOPENED = "meeting.reopened", "Meeting reopened"
    NOTE_ADDED = "note.added", "Note added"


LEAD_KINDS = tuple(k for k in TimelineKind if k.value.startswith("lead."))
OPPORTUNITY_KINDS = tuple(k for k in TimelineKind if k.value.startswith("opportunity."))
ACTIVITY_KINDS = tuple(k for k in TimelineKind if k not in LEAD_KINDS + OPPORTUNITY_KINDS)


class TimelineEntry(AppendOnlyModel):
    """One event in a lead's history: written in the transaction of the change it records,
    never edited (ORM guard + PostgreSQL trigger). Who did it is a stable reference (the
    actor's id); what it was is a small safe snapshot in `data` (status and stage names as
    they were, owner ids, meeting times), never titles, bodies or contact details, which are
    read from the live record and only when the reader may see it (ADR-0021)."""

    id = models.BigAutoField(primary_key=True)
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name="+", db_index=False)
    opportunity = models.ForeignKey(
        Opportunity,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_index=False,
    )
    activity = models.ForeignKey(
        Activity,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_index=False,
    )
    kind = models.CharField(max_length=40, choices=TimelineKind.choices)
    # NULL = the system (e.g. a Phase 2 lead whose creator is unknown to the backfill).
    actor = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+", db_index=False
    )
    occurred_at = models.DateTimeField(default=timezone.now)
    data = models.JSONField(default=dict, blank=True)
    # For an activity's entries: its opportunity ("none" as a value) and the type its kind
    # names ("task" in "task.completed"); the key onto the activity compares both.
    opportunity_key = models.GeneratedField(
        expression=OPPORTUNITY_KEY, output_field=models.UUIDField(), db_persist=True
    )
    subject_type = models.GeneratedField(
        expression=Case(
            When(
                activity__isnull=False,
                then=Func(
                    F("kind"),
                    Value("."),
                    Value(1),
                    function="split_part",
                    output_field=models.CharField(),
                ),
            ),
            default=None,
        ),
        output_field=models.CharField(max_length=16, null=True),
        db_persist=True,
    )

    class Meta:
        db_table = "activities_timeline_entry"
        indexes = [
            # A lead's timeline, newest first (keyset).
            models.Index(
                F("lead"), F("occurred_at").desc(), F("id").desc(), name="timeline_lead_idx"
            ),
            # An opportunity's timeline.
            models.Index(
                F("opportunity"),
                F("occurred_at").desc(),
                F("id").desc(),
                name="timeline_opportunity_idx",
                condition=Q(opportunity__isnull=False),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=TimelineKind.values), name="timeline_kind_valid"
            ),
            # Lead events name only the lead; opportunity events their opportunity; activity
            # events their activity (and its opportunity, if it has one).
            models.CheckConstraint(
                condition=Q(kind__in=LEAD_KINDS, opportunity__isnull=True, activity__isnull=True)
                | Q(kind__in=OPPORTUNITY_KINDS, opportunity__isnull=False, activity__isnull=True)
                | Q(kind__in=ACTIVITY_KINDS, activity__isnull=False),
                name="timeline_subject_matches_kind",
            ),
        ]

    def __str__(self) -> str:
        return f"TimelineEntry({self.pk}, {self.kind})"
