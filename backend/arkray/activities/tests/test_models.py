"""Database-level invariants of the activity tables: they hold even for writes that bypass
the services (raw SQL, bulk updates, a future bug). Each CHECK is proven by a raw UPDATE of
an otherwise valid row that breaks exactly that rule (PostgreSQL reports the first failing
constraint by name, so each case is built to break only its own)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from arkray.activities.models import UNDATED, Activity, ActivityStatus, TimelineEntry
from arkray.core.errors import AppendOnlyViolation
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


def violates(constraint: str, sql: str, params=()) -> None:
    """The statement fails on `constraint`, checked immediately (foreign keys are otherwise
    checked at commit, and tests never commit)."""
    with (  # noqa: PT012 — the statement under test needs the preceding SET
        pytest.raises(IntegrityError, match=constraint),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(sql, params)


def update(activity, assignments: str, params=()) -> tuple[str, list[object]]:
    sql = f"UPDATE activities_activity SET {assignments} WHERE id = %s"  # noqa: S608 — test constants
    return sql, [*params, activity.pk]


def accepted(sql: str, params=()) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(sql, params)


class TestTypeMatrix:
    @pytest.mark.parametrize(
        ("assignments", "params", "constraint"),
        [
            ("status = 'scheduled'", (), "activities_activity_status_valid"),
            ("status = NULL", (), "activities_activity_status_valid"),
            ("priority = NULL", (), "activities_activity_priority_only_tasks"),
            ("priority = 'urgent'", (), "activities_activity_priority_only_tasks"),
            ("title = ''", (), "activities_activity_title_matches_type"),
            ("due_at = '1999-12-31T23:00:00Z'", (), "activities_activity_due_range"),
            ("due_at = '2100-01-01T00:00:00Z'", (), "activities_activity_due_range"),
            ("starts_at = now()", (), "activities_activity_meeting_times"),
            ("location = 'Office'", (), "activities_activity_place_only_meetings"),
            (
                "meeting_url = 'https://meet.example/x'",
                (),
                "activities_activity_place_only_meetings",
            ),
            ("status = 'completed'", (), "activities_activity_completed_matches_status"),
            ("completed_at = now()", (), "activities_activity_completed_matches_status"),
            ("status = 'cancelled'", (), "activities_activity_cancelled_matches_status"),
            ("version = 0", (), "activities_activity_version_positive"),
            ("description = %s", ("x" * 10_001,), "activities_activity_description_length"),
        ],
    )
    def test_task_rules(self, assignments, params, constraint):
        task = TaskFactory()
        violates(constraint, *update(task, assignments, params))

    @pytest.mark.parametrize(
        ("assignments", "constraint"),
        [
            ("ends_at = starts_at", "activities_activity_meeting_times"),
            ("ends_at = starts_at - interval '1 minute'", "activities_activity_meeting_times"),
            (
                "ends_at = starts_at + interval '24 hours 1 second'",
                "activities_activity_meeting_duration",
            ),
            ("starts_at = NULL", "activities_activity_meeting_times"),
            ("ends_at = NULL", "activities_activity_meeting_times"),
            (
                "starts_at = '2100-01-01T00:00:00Z', ends_at = '2100-01-01T01:00:00Z'",
                "activities_activity_starts_range",
            ),
            ("meeting_url = 'http://meet.example/x'", "activities_activity_meeting_url_https"),
            ("meeting_url = 'javascript:alert(1)'", "activities_activity_meeting_url_https"),
            ("priority = 'high'", "activities_activity_priority_only_tasks"),
            ("due_at = now()", "activities_activity_due_only_tasks"),
            ("status = 'open'", "activities_activity_status_valid"),
            ("title = ''", "activities_activity_title_matches_type"),
        ],
    )
    def test_meeting_rules(self, assignments, constraint):
        violates(constraint, *update(MeetingFactory(), assignments))

    def test_a_meeting_of_exactly_24_hours_is_allowed(self):
        meeting = MeetingFactory()
        accepted(*update(meeting, "ends_at = starts_at + interval '24 hours'"))

    @pytest.mark.parametrize(
        ("assignments", "constraint"),
        [
            ("status = 'open'", "activities_activity_status_valid"),
            (
                "status = 'completed', completed_at = now(), completed_by_id = owner_id",
                "activities_activity_status_valid",
            ),
            ("title = 'A title'", "activities_activity_title_matches_type"),
            ("description = ''", "activities_activity_note_has_body"),
            ("priority = 'low'", "activities_activity_priority_only_tasks"),
            ("due_at = now()", "activities_activity_due_only_tasks"),
            ("location = 'Office'", "activities_activity_place_only_meetings"),
            # A note has no status (NULL): completion data must still be refused. A CHECK
            # passes on NULL, so the constraint is written to never compare NULL (review).
            (
                "completed_at = now(), completed_by_id = owner_id",
                "activities_activity_completed_matches_status",
            ),
            ("completed_at = now()", "activities_activity_completed_matches_status"),
            (
                "cancelled_at = now(), cancelled_by_id = owner_id",
                "activities_activity_cancelled_matches_status",
            ),
        ],
    )
    def test_note_rules(self, assignments, constraint):
        violates(constraint, *update(NoteFactory(), assignments))

    def test_type_must_be_known(self):
        """An unknown type breaks the status matrix too (reported first, by name); either
        refusal is the database's."""
        note = NoteFactory()
        violates("activities_activity_(status|type)_valid", *update(note, "type = 'call'"))

    def test_completion_and_cancellation_must_be_complete(self):
        task = TaskFactory(status=ActivityStatus.COMPLETED)
        violates(
            "activities_activity_completed_matches_status",
            *update(task, "completed_by_id = NULL"),
        )
        violates(
            "activities_activity_completed_matches_status",
            *update(task, "status = 'open'"),
        )
        cancelled = MeetingFactory(status=ActivityStatus.CANCELLED)
        violates(
            "activities_activity_cancelled_matches_status",
            *update(cancelled, "cancelled_at = NULL"),
        )


class TestOwnershipKey:
    """Current work (open task, scheduled meeting, note) is owned by its lead's owner;
    completed and cancelled work may keep a previous owner (checked at commit)."""

    @pytest.mark.parametrize("factory", [TaskFactory, MeetingFactory, NoteFactory])
    def test_current_work_cannot_belong_to_anyone_but_the_leads_owner(self, factory):
        activity = factory()
        violates(
            "activities_activity_current_owner_fk",
            *update(activity, "owner_id = %s", (UserFactory().pk,)),
        )

    @pytest.mark.parametrize(
        ("factory", "status"),
        [
            (TaskFactory, ActivityStatus.COMPLETED),
            (TaskFactory, ActivityStatus.CANCELLED),
            (MeetingFactory, ActivityStatus.COMPLETED),
            (MeetingFactory, ActivityStatus.CANCELLED),
        ],
    )
    def test_closed_work_keeps_its_historical_owner(self, factory, status):
        activity = factory(status=status)
        other = UserFactory()
        accepted(*update(activity, "owner_id = %s", (other.pk,)))
        activity.refresh_from_db()
        assert activity.owner_id == other.pk
        assert activity.current_owner_id is None

    def test_reassigning_a_lead_alone_leaves_its_current_work_behind_and_is_refused(self):
        task = TaskFactory()
        violates(
            "activities_activity_current_owner_fk",
            "UPDATE leads_lead SET owner_id = %s WHERE id = %s",
            [UserFactory().pk, task.lead_id],
        )

    def test_reopening_by_raw_sql_without_moving_the_owner_is_refused(self):
        lead = LeadFactory()
        previous = UserFactory()
        done = TaskFactory(lead=lead, owner=previous, status=ActivityStatus.COMPLETED)
        violates(
            "activities_activity_current_owner_fk",
            *update(done, "status = 'open', completed_at = NULL, completed_by_id = NULL"),
        )


class TestRelationshipKeys:
    def test_an_activitys_lead_is_its_opportunitys_lead(self):
        owner = UserFactory()
        opportunity = OpportunityFactory(lead=LeadFactory(owner=owner))
        task = TaskFactory(opportunity=opportunity)
        assert task.lead_id == opportunity.lead_id
        other_lead = LeadFactory(owner=owner)  # same owner: only the relationship is wrong
        violates(
            "activities_activity_opportunity_lead_fk",
            *update(task, "lead_id = %s", (other_lead.pk,)),
        )

    def test_a_lead_only_activity_needs_no_opportunity(self):
        accepted(*update(TaskFactory(), "opportunity_id = NULL"))


class TestGeneratedColumns:
    def test_schedule_sort_is_due_start_or_creation(self):
        due = timezone.now() + timedelta(days=3)
        task = TaskFactory(due_at=due)
        undated = TaskFactory()
        meeting = MeetingFactory()
        note = NoteFactory()
        for activity in (task, undated, meeting, note):
            activity.refresh_from_db()
        assert task.schedule_sort == due
        assert undated.schedule_sort == UNDATED
        assert meeting.schedule_sort == meeting.starts_at
        assert note.schedule_sort == note.created_at

    def test_current_owner_is_set_only_for_current_work(self):
        rows = [
            TaskFactory(),
            MeetingFactory(),
            NoteFactory(),
            TaskFactory(status=ActivityStatus.COMPLETED),
            MeetingFactory(status=ActivityStatus.CANCELLED),
        ]
        for activity in rows:
            activity.refresh_from_db()
        assert [a.current_owner_id is not None for a in rows] == [True, True, True, False, False]


class TestTimelineTable:
    def entry(self, **extra):
        lead = extra.pop("lead", None) or LeadFactory()
        return TimelineEntry.objects.create(
            lead=lead, kind="lead.created", actor=lead.owner, data={}, **extra
        )

    def test_entries_are_append_only_even_for_raw_sql(self):
        entry = self.entry()
        for sql in (
            "UPDATE activities_timeline_entry SET kind = 'lead.archived' WHERE id = %s",
            "DELETE FROM activities_timeline_entry WHERE id = %s",
        ):
            with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():  # noqa: SIM117
                with connection.cursor() as cursor:
                    cursor.execute(sql, [entry.pk])
        with pytest.raises(AppendOnlyViolation):
            entry.save()
        with pytest.raises(AppendOnlyViolation):
            TimelineEntry.objects.filter(pk=entry.pk).delete()

    def test_the_subject_matches_the_kind(self):
        task = TaskFactory()
        with pytest.raises(IntegrityError, match="timeline_subject_matches_kind"):  # noqa: SIM117
            with transaction.atomic():
                self.entry(lead=task.lead, activity=task)
        with pytest.raises(IntegrityError, match="timeline_subject_matches_kind"):  # noqa: SIM117
            with transaction.atomic():
                TimelineEntry.objects.create(lead=task.lead, kind="task.created", data={})
        with pytest.raises(IntegrityError, match="timeline_kind_valid"), transaction.atomic():
            TimelineEntry.objects.create(lead=task.lead, kind="task.deleted", data={})

    def test_an_activitys_history_is_filed_under_its_own_lead(self):
        task = TaskFactory()
        other = LeadFactory()
        violates(
            "activities_timeline_activity_fk",
            "INSERT INTO activities_timeline_entry (lead_id, activity_id, kind, occurred_at, data)"
            " VALUES (%s, %s, 'task.created', now(), '{}')",
            [other.pk, task.pk],
        )

    def test_an_opportunitys_history_is_filed_under_its_own_lead(self):
        opportunity = OpportunityFactory()
        violates(
            "activities_timeline_opportunity_lead_fk",
            "INSERT INTO activities_timeline_entry (lead_id, opportunity_id, kind, occurred_at,"
            " data) VALUES (%s, %s, 'opportunity.created', now(), '{}')",
            [LeadFactory().pk, opportunity.pk],
        )


def test_times_are_stored_as_utc_instants():
    meeting = MeetingFactory(
        starts_at=datetime(2026, 10, 3, 0, 15, tzinfo=UTC) + timedelta(hours=-5, minutes=-30),
    )
    meeting.refresh_from_db()
    # 00:15 IST on 3 October is 18:45 UTC on 2 October; whatever the input offset, the
    # stored instant is the same.
    assert meeting.starts_at == datetime(2026, 10, 2, 18, 45, tzinfo=UTC)
    assert Activity.objects.filter(starts_at=datetime(2026, 10, 2, 18, 45, tzinfo=UTC)).exists()


def test_every_type_spec_field_has_a_cleaner():
    from arkray.activities.spec import ALL_FIELDS, SPECS
    from arkray.activities.validation import CLEANERS

    assert set(CLEANERS) == ALL_FIELDS
    assert {spec.type for spec in SPECS.values()} == {"task", "meeting", "note"}
    for spec in SPECS.values():
        assert spec.required <= spec.fields


class TestReviewHardening:
    """Phase 4 domain review (D-2, D-3, D-6): gaps only raw SQL could reach, now closed."""

    def insert_entry(self, lead_id, kind, activity_id=None, opportunity_id=None):
        return (
            "INSERT INTO activities_timeline_entry (lead_id, opportunity_id, activity_id, kind,"
            " occurred_at, data) VALUES (%s, %s, %s, %s, now(), '{}')",
            [lead_id, opportunity_id, activity_id, kind],
        )

    def test_an_entry_is_filed_under_its_activitys_own_opportunity(self):
        owner = UserFactory()
        lead = LeadFactory(owner=owner)
        first, second = OpportunityFactory(lead=lead), OpportunityFactory(lead=lead)
        on_first = TaskFactory(opportunity=first)
        lead_only = TaskFactory(lead=lead)
        for activity, opportunity in [(on_first, second), (on_first, None), (lead_only, first)]:
            violates(
                "activities_timeline_activity_fk",
                *self.insert_entry(
                    lead.pk, "task.created", activity.pk, opportunity.pk if opportunity else None
                ),
            )
        accepted(*self.insert_entry(lead.pk, "task.created", on_first.pk, first.pk))
        accepted(*self.insert_entry(lead.pk, "task.created", lead_only.pk, None))

    def test_an_entry_names_its_activitys_own_type(self):
        note = NoteFactory()
        violates(
            "activities_timeline_activity_fk",
            *self.insert_entry(note.lead_id, "meeting.completed", note.pk),
        )
        accepted(*self.insert_entry(note.lead_id, "note.added", note.pk))

    def test_an_activitys_links_and_type_are_fixed_once_it_has_history(self):
        owner = UserFactory()
        lead = LeadFactory(owner=owner)
        first, second = OpportunityFactory(lead=lead), OpportunityFactory(lead=lead)
        task = TaskFactory(opportunity=first)
        accepted(*self.insert_entry(lead.pk, "task.created", task.pk, first.pk))
        violates(
            "activities_timeline_activity_fk", *update(task, "opportunity_id = %s", (second.pk,))
        )

    def test_the_24_hour_limit_does_not_depend_on_the_session_time_zone(self):
        meeting = MeetingFactory()
        with (  # noqa: PT012 — the statement under test needs the preceding SET
            pytest.raises(IntegrityError, match="activities_activity_meeting_duration"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            # Across a daylight-saving change in this zone, "+ 1 day" would be 25 hours.
            cursor.execute("SET LOCAL TIME ZONE 'America/New_York'")
            cursor.execute(
                "UPDATE activities_activity SET starts_at = '2026-11-01T03:00:00Z',"
                " ends_at = '2026-11-01T03:00:00Z'::timestamptz + interval '25 hours'"
                " WHERE id = %s",
                [meeting.pk],
            )

    @pytest.mark.parametrize(
        ("factory", "assignments", "constraint"),
        [
            (TaskFactory, "title = '   '", "activities_activity_title_matches_type"),
            (MeetingFactory, "title = E'\t\n'", "activities_activity_title_matches_type"),
            (NoteFactory, "description = E' \n\t '", "activities_activity_note_has_body"),
        ],
    )
    def test_text_must_have_something_visible(self, factory, assignments, constraint):
        violates(constraint, *update(factory(), assignments))
