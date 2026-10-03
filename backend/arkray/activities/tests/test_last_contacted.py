"""Lead.last_contacted_at (docs/activities.md#last-contacted): a completed meeting is a
customer interaction and advances it to the meeting's start time, never backwards (MAX).
Scheduling, tasks, notes and edits never touch it; only an authorised domain operation can.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from arkray.activities import services
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import NotFoundError
from arkray.leads.events import LeadUpdated
from arkray.leads.models import Lead
from tests.factories import LeadFactory, MeetingFactory, NoteFactory, TaskFactory
from tests.helpers import collected

from .conftest import meeting_fields, note_fields, task_fields

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
NOW = timezone.now


def complete(actor, meeting, scope=None):
    meeting.refresh_from_db()
    return services.complete_activity(
        actor=actor,
        scope=scope or OWN(actor.pk),
        activity_id=meeting.pk,
        version=meeting.version,
    )


def past_meeting(lead, days_ago: float):
    start = NOW() - timedelta(days=days_ago)
    return MeetingFactory(lead=lead, starts_at=start, ends_at=start + timedelta(hours=1))


def contacted(lead):
    return Lead.objects.get(pk=lead.pk).last_contacted_at


def test_completing_a_meeting_records_the_contact_at_its_start(user_a):
    lead = LeadFactory(owner=user_a)
    meeting = past_meeting(lead, 2)
    with collected(LeadUpdated) as updates:
        complete(user_a, meeting)
    assert contacted(lead) == meeting.starts_at
    assert Lead.objects.get(pk=lead.pk).version == 2  # a lead change: open edit forms get a 409
    assert [u.fields for u in updates] == [("last_contacted_at",)]
    audit = AuditEvent.objects.get(action="lead.updated")
    assert audit.metadata == {
        "workspace": "self",
        "fields": ["last_contacted_at"],
        "via": "meeting_completed",
        "via_id": str(meeting.pk),
    }


def test_last_contact_never_moves_backwards(user_a):
    """The brief's example: A on 1 Oct, B on 3 Oct, then an older C (25 Sep) is marked
    completed: the lead stays at 3 Oct."""
    lead = LeadFactory(owner=user_a)
    a, b, c = past_meeting(lead, 5), past_meeting(lead, 3), past_meeting(lead, 11)
    complete(user_a, a)
    complete(user_a, b)
    complete(user_a, c)
    assert contacted(lead) == b.starts_at


def test_an_older_meeting_does_not_bump_the_lead_version(user_a):
    lead = LeadFactory(owner=user_a, last_contacted_at=NOW() - timedelta(hours=1))
    complete(user_a, past_meeting(lead, 3))
    stored = Lead.objects.get(pk=lead.pk)
    assert stored.version == 1
    assert not AuditEvent.objects.filter(action="lead.updated").exists()


def test_a_manual_later_contact_is_respected(user_a):
    later = NOW() - timedelta(minutes=10)
    lead = LeadFactory(owner=user_a, last_contacted_at=later)
    complete(user_a, past_meeting(lead, 1))
    assert contacted(lead) == later


def test_scheduling_tasks_notes_and_edits_never_count_as_contact(user_a):
    lead = LeadFactory(owner=user_a)
    scope = OWN(user_a.pk)
    for kind, fields in [
        ("task", task_fields()),
        ("meeting", meeting_fields(start=NOW() - timedelta(hours=3))),  # even a past one
        ("note", note_fields()),
    ]:
        services.create_activity(
            actor=user_a, scope=scope, activity_type=kind, fields=fields, lead_id=lead.pk
        )
    task = TaskFactory(lead=lead)
    services.complete_activity(actor=user_a, scope=scope, activity_id=task.pk, version=1)
    note = NoteFactory(lead=lead)
    services.update_activity(
        actor=user_a, scope=scope, activity_id=note.pk, version=1, changes={"description": "x"}
    )
    meeting = past_meeting(lead, 1)
    services.cancel_activity(actor=user_a, scope=scope, activity_id=meeting.pk, version=1)
    assert contacted(lead) is None
    assert Lead.objects.get(pk=lead.pk).version == 1


def test_reopening_a_completed_meeting_keeps_the_recorded_contact(user_a):
    lead = LeadFactory(owner=user_a)
    meeting = past_meeting(lead, 1)
    complete(user_a, meeting)
    services.reopen_activity(actor=user_a, scope=OWN(user_a.pk), activity_id=meeting.pk, version=2)
    assert contacted(lead) == meeting.starts_at


def test_an_admin_completing_in_a_users_workspace_records_it_as_themselves(admin, user_a):
    lead = LeadFactory(owner=user_a)
    meeting = past_meeting(lead, 1)
    complete(admin, meeting, AccessScope.for_user(admin.pk, user_a.pk))
    audit = AuditEvent.objects.get(action="lead.updated")
    assert (audit.actor_id, audit.subject_user_id) == (admin.pk, user_a.pk)
    assert contacted(lead) == meeting.starts_at


def test_an_archived_leads_open_meeting_can_still_be_completed_and_counts(user_a):
    """Like an archived lead's open opportunities, its scheduled meetings can still be
    closed (pipeline precedent); the interaction happened, so it is recorded."""
    lead = LeadFactory(owner=user_a, archived_at=NOW())
    meeting = past_meeting(lead, 1)
    complete(user_a, meeting)
    assert contacted(lead) == meeting.starts_at


def test_another_users_lead_can_not_be_touched_by_completing_a_guessed_meeting(user_a, user_b):
    lead = LeadFactory(owner=user_b)
    theirs = past_meeting(lead, 1)
    with pytest.raises(NotFoundError):
        services.complete_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=theirs.pk, version=1
        )
    assert contacted(lead) is None
    assert Lead.objects.get(pk=lead.pk).version == 1
