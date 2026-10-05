"""Activity services, called directly (as a future import or Ask Arkray tool would): every
rule holds without the HTTP layer. Ownership, authorization, per-type fields, the
lifecycle (complete, cancel, reopen and their no-op repeats), archive, audit, domain
events and the timeline entries each operation writes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from arkray.activities import events, services
from arkray.activities.models import Activity, ActivityStatus, TimelineEntry
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from arkray.leads import services as lead_services
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
)
from tests.helpers import collected

from .conftest import meeting_fields, note_fields, task_fields

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
ORG = AccessScope.organization
FOR = AccessScope.for_user


def create(actor, scope, kind="task", **kwargs):
    fields = kwargs.pop("fields", None)
    if fields is None:
        fields = {"task": task_fields, "meeting": meeting_fields, "note": note_fields}[kind]()
    return services.create_activity(
        actor=actor, scope=scope, activity_type=kind, fields=fields, **kwargs
    ).activity


def kinds(lead) -> list[str]:
    return list(
        TimelineEntry.objects.filter(lead=lead)
        .order_by("occurred_at", "id")
        .values_list("kind", flat=True)
    )


class TestCreate:
    def test_a_task_belongs_to_the_leads_owner_and_starts_open(self, user_a):
        lead = LeadFactory(owner=user_a)
        task = create(user_a, OWN(user_a.pk), lead_id=lead.pk)
        assert (task.type, task.status, task.priority) == ("task", "open", "normal")
        assert (task.owner_id, task.created_by_id, task.lead_id) == (user_a.pk, user_a.pk, lead.pk)
        assert task.opportunity_id is None
        assert task.version == 1

    def test_a_meeting_starts_scheduled_and_a_note_has_no_status(self, user_a):
        lead = LeadFactory(owner=user_a)
        meeting = create(user_a, OWN(user_a.pk), "meeting", lead_id=lead.pk)
        note = create(user_a, OWN(user_a.pk), "note", lead_id=lead.pk)
        assert meeting.status == "scheduled"
        assert (note.status, note.title, note.priority) == (None, "", None)

    def test_an_admin_in_a_users_workspace_creates_for_that_user_as_themselves(self, admin, user_a):
        lead = LeadFactory(owner=user_a)
        note = create(admin, FOR(admin.pk, user_a.pk), "note", lead_id=lead.pk)
        # Historical integrity: the author stays the admin; the note is the lead's owner's.
        assert (note.owner_id, note.created_by_id) == (user_a.pk, admin.pk)
        event = AuditEvent.objects.get(action="note.created")
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)

    def test_organisation_wide_the_owner_is_still_the_leads(self, admin, user_b):
        lead = LeadFactory(owner=user_b)
        task = create(admin, ORG(admin.pk), lead_id=lead.pk)
        assert (task.owner_id, task.created_by_id) == (user_b.pk, admin.pk)

    def test_an_opportunity_implies_its_lead(self, user_a):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        task = create(user_a, OWN(user_a.pk), opportunity_id=opportunity.pk)
        assert (task.lead_id, task.opportunity_id) == (opportunity.lead_id, opportunity.pk)

    def test_a_lead_and_its_opportunity_together(self, user_a):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        task = create(
            user_a, OWN(user_a.pk), lead_id=opportunity.lead_id, opportunity_id=opportunity.pk
        )
        assert task.opportunity_id == opportunity.pk

    def test_an_opportunity_of_another_of_my_leads_is_refused(self, user_a):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        other = LeadFactory(owner=user_a)
        with pytest.raises(InvalidInputError) as caught:
            create(user_a, OWN(user_a.pk), lead_id=other.pk, opportunity_id=opportunity.pk)
        assert "opportunity" in caught.value.details
        assert not Activity.objects.exists()

    def test_a_link_is_required(self, user_a):
        with pytest.raises(InvalidInputError) as caught:
            create(user_a, OWN(user_a.pk))
        assert "lead" in caught.value.details

    @pytest.mark.parametrize("link", ["lead", "opportunity"])
    def test_someone_elses_record_is_not_found_exactly_like_a_missing_one(
        self, user_a, user_b, link
    ):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_b))
        target = {"lead": opportunity.lead_id, "opportunity": opportunity.pk}[link]
        with pytest.raises(NotFoundError):
            create(user_a, OWN(user_a.pk), **{f"{link}_id": target})
        assert not Activity.objects.exists()
        assert not TimelineEntry.objects.filter(kind="task.created").exists()

    def test_my_lead_with_someone_elses_opportunity_is_not_found(self, user_a, user_b):
        mine = LeadFactory(owner=user_a)
        theirs = OpportunityFactory(lead=LeadFactory(owner=user_b))
        with pytest.raises(NotFoundError):
            create(user_a, OWN(user_a.pk), lead_id=mine.pk, opportunity_id=theirs.pk)

    def test_a_closed_opportunity_whose_lead_moved_on_takes_no_new_work_here(
        self, admin, user_a, user_b
    ):
        lead = LeadFactory(owner=user_a)
        won = OpportunityFactory(lead=lead, stage_key="won")
        lead_services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=user_b.pk
        )
        with pytest.raises(BusinessRuleViolation, match="belongs to someone else"):
            create(user_a, OWN(user_a.pk), opportunity_id=won.pk)
        # An administrator may: the new work is the lead's current owner's.
        task = create(admin, ORG(admin.pk), opportunity_id=won.pk)
        assert task.owner_id == user_b.pk

    def test_archived_leads_and_opportunities_take_no_new_activities(self, user_a):
        lead = LeadFactory(owner=user_a, archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="customer's record is archived"):
            create(user_a, OWN(user_a.pk), lead_id=lead.pk)
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="opportunity is archived"):
            create(user_a, OWN(user_a.pk), opportunity_id=opportunity.pk)

    def test_a_deactivated_owners_lead_must_be_reassigned_first(self, admin):
        gone = UserFactory(is_active=False)
        lead = LeadFactory(owner=gone)
        with pytest.raises(BusinessRuleViolation, match="deactivated"):
            create(admin, ORG(admin.pk), "note", lead_id=lead.pk)

    def test_viewing_a_workspace_is_not_writing_to_it(self, user_a):
        """A sales user can't write anywhere but their own workspace (authorize_write)."""
        lead = LeadFactory(owner=user_a)
        with pytest.raises(PermissionDeniedError):
            create(user_a, ORG(user_a.pk), lead_id=lead.pk)

    def test_unknown_type(self, user_a):
        with pytest.raises(InvalidInputError) as caught:
            create(user_a, OWN(user_a.pk), "call", fields={}, lead_id=LeadFactory(owner=user_a).pk)
        assert "type" in caught.value.details


class TestFieldsPerType:
    @pytest.mark.parametrize(
        ("kind", "fields", "field"),
        [
            ("task", {"title": "x", "location": "Office"}, "location"),
            ("task", {"title": "x", "starts_at": timezone.now()}, "starts_at"),
            ("meeting", {**meeting_fields(), "priority": "high"}, "priority"),
            ("meeting", {**meeting_fields(), "due_at": timezone.now()}, "due_at"),
            ("note", {"description": "x", "title": "Sneaky"}, "title"),
            ("note", {"description": "x", "due_at": timezone.now()}, "due_at"),
        ],
    )
    def test_a_type_accepts_only_its_own_fields(self, user_a, kind, fields, field):
        with pytest.raises(InvalidInputError) as caught:
            create(
                user_a, OWN(user_a.pk), kind, fields=fields, lead_id=LeadFactory(owner=user_a).pk
            )
        assert field in caught.value.details

    @pytest.mark.parametrize(
        ("kind", "fields", "field"),
        [
            ("task", {}, "title"),
            ("task", {"title": "   "}, "title"),
            ("meeting", {"title": "x", "starts_at": timezone.now()}, "ends_at"),
            ("note", {}, "description"),
            ("note", {"description": " \n\t "}, "description"),
        ],
    )
    def test_required_fields(self, user_a, kind, fields, field):
        with pytest.raises(InvalidInputError) as caught:
            create(
                user_a, OWN(user_a.pk), kind, fields=fields, lead_id=LeadFactory(owner=user_a).pk
            )
        assert field in caught.value.details

    def test_meeting_times(self, user_a):
        lead = LeadFactory(owner=user_a)
        start = timezone.now() + timedelta(days=1)
        for end, message in [
            (start, "after the start"),
            (start - timedelta(minutes=5), "after the start"),
            (start + timedelta(hours=24, seconds=1), "24 hours"),
        ]:
            with pytest.raises(InvalidInputError, match="invalid") as caught:
                create(
                    user_a,
                    OWN(user_a.pk),
                    "meeting",
                    fields={"title": "x", "starts_at": start, "ends_at": end},
                    lead_id=lead.pk,
                )
            assert message in caught.value.details["ends_at"][0]

    def test_text_is_normalised_and_hidden_characters_refused(self, user_a):
        lead = LeadFactory(owner=user_a)
        task = create(
            user_a, OWN(user_a.pk), fields={"title": "  Call   back\tsoon "}, lead_id=lead.pk
        )
        assert task.title == "Call back soon"
        with pytest.raises(InvalidInputError) as caught:
            create(
                user_a, OWN(user_a.pk), "note", fields={"description": "hi‮evil"}, lead_id=lead.pk
            )
        assert "description" in caught.value.details

    @pytest.mark.parametrize(
        "url",
        [
            "http://meet.example/x",
            "javascript:alert(1)",
            "ftp://files.example",
            "https://user:secret@meet.example/x",
            "not a url",
        ],
    )
    def test_meeting_links_are_plain_https(self, user_a, url):
        with pytest.raises(InvalidInputError) as caught:
            create(
                user_a,
                OWN(user_a.pk),
                "meeting",
                fields=meeting_fields(meeting_url=url),
                lead_id=LeadFactory(owner=user_a).pk,
            )
        assert "meeting_url" in caught.value.details

    def test_dates_must_be_aware_and_in_range(self, user_a):
        lead = LeadFactory(owner=user_a)
        naive = timezone.now().replace(tzinfo=None)
        with pytest.raises(InvalidInputError):
            create(user_a, OWN(user_a.pk), fields=task_fields(due_at=naive), lead_id=lead.pk)
        with pytest.raises(InvalidInputError, match="invalid"):
            create(
                user_a,
                OWN(user_a.pk),
                fields=task_fields(due_at=timezone.now().replace(year=2100)),
                lead_id=lead.pk,
            )


class TestEdit:
    def test_changes_only_what_differs_and_audits_field_names(self, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        updated = services.update_activity(
            actor=user_a,
            scope=OWN(user_a.pk),
            activity_id=task.pk,
            version=1,
            changes={"title": task.title, "priority": "high", "description": "Secret pricing"},
        )
        assert (updated.priority, updated.version) == ("high", 2)
        event = AuditEvent.objects.get(action="task.updated")
        assert event.metadata["fields"] == ["description", "priority"]
        assert "Secret pricing" not in str(event.metadata)

    def test_nothing_changed_is_a_no_op(self, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        same = services.update_activity(
            actor=user_a,
            scope=OWN(user_a.pk),
            activity_id=task.pk,
            version=1,
            changes={"title": task.title},
        )
        assert same.version == 1
        assert not AuditEvent.objects.filter(action="task.updated").exists()

    def test_a_stale_version_is_a_conflict(self, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a), version=3)
        with pytest.raises(ConflictError):
            services.update_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_id=task.pk,
                version=2,
                changes={"title": "New"},
            )

    def test_closed_work_is_history_until_reopened(self, user_a):
        done = TaskFactory(lead=LeadFactory(owner=user_a), status=ActivityStatus.COMPLETED)
        with pytest.raises(BusinessRuleViolation, match="Reopen it"):
            services.update_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_id=done.pk,
                version=1,
                changes={"title": "Rewritten"},
            )

    def test_archived_activities_are_read_only(self, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a), archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="archived"):
            services.update_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_id=task.pk,
                version=1,
                changes={"title": "x"},
            )

    def test_only_a_notes_author_may_rewrite_it(self, admin, user_a):
        lead = LeadFactory(owner=user_a)
        by_admin = NoteFactory(lead=lead, created_by=admin)
        with pytest.raises(PermissionDeniedError, match="author"):
            services.update_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_id=by_admin.pk,
                version=1,
                changes={"description": "Not what the admin said"},
            )
        edited = services.update_activity(
            actor=admin,
            scope=FOR(admin.pk, user_a.pk),
            activity_id=by_admin.pk,
            version=1,
            changes={"description": "Clarified"},
        )
        assert (edited.description, edited.created_by_id) == ("Clarified", admin.pk)

    def test_a_type_can_not_change_and_fields_stay_per_type(self, user_a):
        note = NoteFactory(lead=LeadFactory(owner=user_a))
        with pytest.raises(InvalidInputError) as caught:
            services.update_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_id=note.pk,
                version=1,
                changes={"due_at": timezone.now()},
            )
        assert "due_at" in caught.value.details
        with pytest.raises(InvalidInputError):
            services.update_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_id=note.pk,
                version=1,
                changes={"type": "task"},
            )

    def test_rescheduling_a_meeting_is_validated_and_recorded(self, user_a):
        meeting = MeetingFactory(lead=LeadFactory(owner=user_a))
        later = meeting.starts_at + timedelta(days=2)
        with pytest.raises(InvalidInputError):
            services.update_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_id=meeting.pk,
                version=1,
                changes={"starts_at": later},  # now after its end
            )
        moved = services.update_activity(
            actor=user_a,
            scope=OWN(user_a.pk),
            activity_id=meeting.pk,
            version=1,
            changes={"starts_at": later, "ends_at": later + timedelta(minutes=30)},
        )
        entry = TimelineEntry.objects.get(kind="meeting.rescheduled")
        assert entry.data["from_starts_at"] == meeting.starts_at.isoformat()
        assert entry.data["to_starts_at"] == moved.starts_at.isoformat()

    def test_a_required_field_can_not_be_emptied(self, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        with pytest.raises(InvalidInputError) as caught:
            services.update_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_id=task.pk,
                version=1,
                changes={"title": ""},
            )
        assert "title" in caught.value.details


class TestLifecycle:
    def test_completing_a_task_records_who_and_when(self, admin, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        done = services.complete_activity(
            actor=admin, scope=FOR(admin.pk, user_a.pk), activity_id=task.pk, version=1
        )
        assert (done.status, done.completed_by_id, done.owner_id) == (
            "completed",
            admin.pk,
            user_a.pk,
        )
        assert done.completed_at is not None
        assert kinds(task.lead) == ["task.completed"]
        audit = AuditEvent.objects.get(action="task.completed")
        assert (audit.actor_id, audit.subject_user_id) == (admin.pk, user_a.pk)

    def test_completing_twice_is_a_harmless_no_op(self, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        first = services.complete_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=1
        )
        again = services.complete_activity(
            actor=user_a,
            scope=OWN(user_a.pk),
            activity_id=task.pk,
            version=1,  # stale: fine
        )
        assert (again.version, again.completed_at) == (first.version, first.completed_at)
        assert TimelineEntry.objects.filter(kind="task.completed").count() == 1
        assert AuditEvent.objects.filter(action="task.completed").count() == 1

    def test_cancel_and_complete_exclude_each_other_until_reopened(self, user_a):
        lead = LeadFactory(owner=user_a)
        cancelled = TaskFactory(lead=lead, status=ActivityStatus.CANCELLED)
        with pytest.raises(BusinessRuleViolation, match="cancelled"):
            services.complete_activity(
                actor=user_a, scope=OWN(user_a.pk), activity_id=cancelled.pk, version=1
            )
        done = MeetingFactory(
            lead=lead, status=ActivityStatus.COMPLETED, starts_at=timezone.now() - timedelta(days=1)
        )
        with pytest.raises(BusinessRuleViolation, match="completed"):
            services.cancel_activity(
                actor=user_a, scope=OWN(user_a.pk), activity_id=done.pk, version=1
            )

    def test_cancelling_records_who_and_when_and_twice_is_a_no_op(self, user_a):
        meeting = MeetingFactory(lead=LeadFactory(owner=user_a))
        cancelled = services.cancel_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=meeting.pk, version=1
        )
        assert (cancelled.status, cancelled.cancelled_by_id) == ("cancelled", user_a.pk)
        services.cancel_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=meeting.pk, version=1
        )
        assert TimelineEntry.objects.filter(kind="meeting.cancelled").count() == 1

    def test_a_meeting_is_completed_only_once_it_has_started(self, user_a):
        future = MeetingFactory(lead=LeadFactory(owner=user_a))
        with pytest.raises(BusinessRuleViolation, match="hasn't started"):
            services.complete_activity(
                actor=user_a, scope=OWN(user_a.pk), activity_id=future.pk, version=1
            )

    def test_reopening_clears_the_outcome_but_keeps_the_history(self, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        services.complete_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=1
        )
        reopened = services.reopen_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=2
        )
        assert (reopened.status, reopened.completed_at, reopened.completed_by_id) == (
            "open",
            None,
            None,
        )
        assert kinds(task.lead) == ["task.completed", "task.reopened"]
        assert AuditEvent.objects.get(action="task.reopened").metadata["from_status"] == "completed"
        # Reopening open work: nothing to do.
        again = services.reopen_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=1
        )
        assert again.version == reopened.version

    def test_reopening_after_the_lead_moved_on_follows_the_new_owner(self, admin, user_a, user_b):
        lead = LeadFactory(owner=user_b)
        done = TaskFactory(lead=lead, owner=user_a, status=ActivityStatus.COMPLETED)
        with pytest.raises(BusinessRuleViolation, match="belongs to someone else"):
            services.reopen_activity(
                actor=user_a, scope=OWN(user_a.pk), activity_id=done.pk, version=1
            )
        reopened = services.reopen_activity(
            actor=admin, scope=ORG(admin.pk), activity_id=done.pk, version=1
        )
        assert reopened.owner_id == user_b.pk
        changed = AuditEvent.objects.get(action="task.owner_changed")
        assert changed.metadata["reason"] == "reopened"

    def test_reopening_needs_an_unarchived_lead_and_an_active_owner(self, admin, user_a):
        archived = LeadFactory(owner=user_a, archived_at=timezone.now())
        done = TaskFactory(lead=archived, status=ActivityStatus.COMPLETED)
        with pytest.raises(BusinessRuleViolation, match="customer's record is archived"):
            services.reopen_activity(
                actor=admin, scope=ORG(admin.pk), activity_id=done.pk, version=1
            )
        gone = UserFactory(is_active=False)
        cancelled = MeetingFactory(lead=LeadFactory(owner=gone), status=ActivityStatus.CANCELLED)
        with pytest.raises(BusinessRuleViolation, match="deactivated"):
            services.reopen_activity(
                actor=admin, scope=ORG(admin.pk), activity_id=cancelled.pk, version=1
            )

    @pytest.mark.parametrize("operation", ["complete", "cancel", "reopen"])
    def test_notes_have_no_lifecycle(self, user_a, operation):
        note = NoteFactory(lead=LeadFactory(owner=user_a))
        with pytest.raises(BusinessRuleViolation, match="Notes can't"):
            getattr(services, f"{operation}_activity")(
                actor=user_a, scope=OWN(user_a.pk), activity_id=note.pk, version=1
            )

    @pytest.mark.parametrize("operation", ["complete", "cancel"])
    def test_a_stale_version_is_a_conflict(self, user_a, operation):
        task = TaskFactory(lead=LeadFactory(owner=user_a), version=2)
        with pytest.raises(ConflictError):
            getattr(services, f"{operation}_activity")(
                actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=1
            )

    @pytest.mark.parametrize("operation", ["complete", "cancel", "reopen", "archive", "restore"])
    def test_someone_elses_activity_is_not_found(self, user_a, user_b, operation):
        theirs = TaskFactory(lead=LeadFactory(owner=user_b))
        with pytest.raises(NotFoundError):
            getattr(services, f"{operation}_activity")(
                actor=user_a, scope=OWN(user_a.pk), activity_id=theirs.pk, version=1
            )
        theirs.refresh_from_db()
        assert theirs.version == 1


class TestArchive:
    def test_archive_and_restore_are_audited_and_idempotent(self, user_a):
        note = NoteFactory(lead=LeadFactory(owner=user_a))
        archived = services.archive_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=note.pk, version=1
        )
        assert archived.archived_at is not None
        services.archive_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=note.pk, version=1
        )
        restored = services.restore_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=note.pk, version=2
        )
        assert restored.archived_at is None
        assert [e.action for e in AuditEvent.objects.order_by("id")] == [
            "note.archived",
            "note.restored",
        ]

    def test_anyone_who_may_write_may_archive_someone_elses_note(self, admin, user_a):
        note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=admin)
        archived = services.archive_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=note.pk, version=1
        )
        assert archived.archived_at is not None

    def test_restoring_needs_the_lead_restored_first(self, user_a):
        lead = LeadFactory(owner=user_a, archived_at=timezone.now())
        note = NoteFactory(lead=lead, archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="record is archived"):
            services.restore_activity(
                actor=user_a, scope=OWN(user_a.pk), activity_id=note.pk, version=1
            )


class TestEventsAndTimeline:
    def test_every_operation_publishes_identifiers_only(self, user_a):
        lead = LeadFactory(owner=user_a)
        scope = OWN(user_a.pk)
        with (
            collected(events.ActivityCreated) as created,
            collected(events.ActivityUpdated) as updated,
            collected(events.ActivityStatusChanged) as changed,
            collected(events.ActivityArchived) as archived,
            collected(events.ActivityRestored) as restored,
        ):
            task = create(user_a, scope, lead_id=lead.pk)
            services.update_activity(
                actor=user_a,
                scope=scope,
                activity_id=task.pk,
                version=1,
                changes={"priority": "low"},
            )
            services.complete_activity(actor=user_a, scope=scope, activity_id=task.pk, version=2)
            services.archive_activity(actor=user_a, scope=scope, activity_id=task.pk, version=3)
            services.restore_activity(actor=user_a, scope=scope, activity_id=task.pk, version=4)
        assert [len(created), len(updated), len(changed), len(archived), len(restored)] == [1] * 5
        assert (changed[0].from_status, changed[0].to_status) == ("open", "completed")
        assert updated[0].fields == ("priority",)
        for event in (*created, *updated, *changed):
            assert event.lead_id == lead.pk
            assert "subject" not in repr(event)

    def test_creation_writes_one_timeline_entry_per_type(self, user_a):
        lead = LeadFactory(owner=user_a)
        scope = OWN(user_a.pk)
        create(user_a, scope, "task", lead_id=lead.pk)
        meeting = create(user_a, scope, "meeting", lead_id=lead.pk)
        create(user_a, scope, "note", lead_id=lead.pk)
        assert kinds(lead) == ["task.created", "meeting.scheduled", "note.added"]
        scheduled = TimelineEntry.objects.get(kind="meeting.scheduled")
        assert scheduled.data == {
            "starts_at": meeting.starts_at.isoformat(),
            "ends_at": meeting.ends_at.isoformat(),
        }
        assert TimelineEntry.objects.get(kind="note.added").data == {}

    def test_audit_never_holds_text(self, user_a):
        lead = LeadFactory(owner=user_a)
        secret = "Confidential: they will pay 20% more"
        create(user_a, OWN(user_a.pk), "note", fields={"description": secret}, lead_id=lead.pk)
        create(
            user_a,
            OWN(user_a.pk),
            "meeting",
            fields=meeting_fields(title=secret, location=secret, meeting_url="https://m.example/x"),
            lead_id=lead.pk,
        )
        for row in AuditEvent.objects.all():
            assert secret not in str(row.metadata)
            assert "m.example" not in str(row.metadata)
        for entry in TimelineEntry.objects.all():
            assert secret not in str(entry.data)


class TestIdempotency:
    def test_a_retry_with_the_same_key_replays_the_first_create(self, user_a):
        import uuid

        lead = LeadFactory(owner=user_a)
        key = uuid.uuid4()
        first = services.create_activity(
            actor=user_a,
            scope=OWN(user_a.pk),
            activity_type="note",
            fields=note_fields(),
            lead_id=lead.pk,
            idempotency_key=key,
        )
        second = services.create_activity(
            actor=user_a,
            scope=OWN(user_a.pk),
            activity_type="note",
            fields=note_fields(),
            lead_id=lead.pk,
            idempotency_key=key,
        )
        assert (first.replayed, second.replayed) == (False, True)
        assert first.activity.pk == second.activity.pk
        assert Activity.objects.count() == 1
        assert TimelineEntry.objects.filter(kind="note.added").count() == 1

    def test_the_same_key_for_a_different_request_is_refused(self, user_a):
        import uuid

        from arkray.core.idempotency import IdempotencyKeyReused

        lead = LeadFactory(owner=user_a)
        key = uuid.uuid4()
        services.create_activity(
            actor=user_a,
            scope=OWN(user_a.pk),
            activity_type="note",
            fields=note_fields("a"),
            lead_id=lead.pk,
            idempotency_key=key,
        )
        with pytest.raises(IdempotencyKeyReused):
            services.create_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_type="note",
                fields=note_fields("b"),
                lead_id=lead.pk,
                idempotency_key=key,
            )

    def test_a_replay_after_the_lead_moved_away_says_so(self, admin, user_a, user_b):
        import uuid

        lead = LeadFactory(owner=user_a)
        key = uuid.uuid4()
        services.create_activity(
            actor=user_a,
            scope=OWN(user_a.pk),
            activity_type="task",
            fields=task_fields(),
            lead_id=lead.pk,
            idempotency_key=key,
        )
        lead_services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=user_b.pk
        )
        with pytest.raises(ConflictError, match="earlier request"):
            services.create_activity(
                actor=user_a,
                scope=OWN(user_a.pk),
                activity_type="task",
                fields=task_fields(),
                lead_id=lead.pk,
                idempotency_key=key,
            )
