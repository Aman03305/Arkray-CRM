"""What crosses between the previous and the new owner of a reassigned lead, through every
activities and timeline channel (docs/activities.md#lead-reassignment).

The lead, its notes (archived ones too) and its open work follow the lead to the new owner;
completed work and won opportunities stay with whoever had them. Neither side may learn
anything of what stayed with the other, or write through what it kept.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from arkray.activities import services
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from arkray.pipeline import services as pipeline_services
from tests.factories import LeadFactory, MeetingFactory, OpportunityFactory, default_stage
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

ME = "/api/v1/workspaces/me"
OWN = AccessScope.own
ORG = AccessScope.organization


def walk(client, url):
    """Every page of `url`, one row per page, as one text."""
    pages, response = [], client.get(url, {"page_size": 1})
    while True:
        assert response.status_code == 200, response.content
        pages.append(response.content.decode())
        if not response.json()["next"]:
            return "".join(pages)
        response = client.get(response.json()["next"])


@pytest.fixture
def story(admin, user_a, user_b):
    """The lead belonged to A: A won an opportunity on it, completed a meeting with a secret
    agenda on that opportunity, wrote a note then archived it, and left an open task. The
    administrator reassigns the lead to B, then adds a task on the won opportunity in the
    organisation workspace (it belongs to B, the lead's owner)."""
    lead = LeadFactory(owner=user_a, first_name="Leadname", organization_name="LeadOrg")
    scope_a = OWN(user_a.pk)
    opportunity = OpportunityFactory(lead=lead, title="A-secret-deal")
    pipeline_services.move_opportunity(
        actor=user_a,
        scope=scope_a,
        opportunity_id=opportunity.pk,
        version=1,
        stage_id=default_stage("won").pk,
    )
    start = timezone.now() - timedelta(days=1)
    meeting = MeetingFactory(
        opportunity=opportunity,
        title="A-meeting-title",
        description="A-secret-agenda",
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        location="A-room",
    )
    services.complete_activity(actor=user_a, scope=scope_a, activity_id=meeting.pk, version=1)
    note = services.create_activity(
        actor=user_a,
        scope=scope_a,
        activity_type="note",
        lead_id=lead.pk,
        fields={"description": "A-archived-note-text"},
    ).activity
    services.archive_activity(actor=user_a, scope=scope_a, activity_id=note.pk, version=1)
    task = services.create_activity(
        actor=user_a,
        scope=scope_a,
        activity_type="task",
        opportunity_id=opportunity.pk,
        fields={"title": "Open-follow-up"},
    ).activity
    lead.refresh_from_db()
    lead_services.reassign_lead(
        actor=admin,
        scope=ORG(admin.pk),
        lead_id=lead.pk,
        version=lead.version,
        owner_id=user_b.pk,
    )
    admin_task = services.create_activity(
        actor=admin,
        scope=ORG(admin.pk),
        activity_type="task",
        opportunity_id=opportunity.pk,
        fields={"title": "Admin-task-for-B"},
    ).activity
    return lead, opportunity, meeting, note, task, admin_task


def test_the_new_owner_sees_nothing_of_the_previous_owners_retained_work(story, user_b):
    lead, opportunity, meeting, note, _, _ = story
    client = signed_in(user_b)
    text = walk(client, f"{ME}/leads/{lead.pk}/timeline")
    for secret in (
        "A-secret-deal",
        "A-meeting-title",
        "A-secret-agenda",
        "A-room",
        "A-archived-note-text",
        str(meeting.pk),
        str(opportunity.pk),
        str(note.pk),
    ):
        assert secret not in text, secret
    assert "Open-follow-up" in text  # current work followed the lead (by design)
    lists = walk(client, f"{ME}/activities")
    assert "A-meeting-title" not in lists
    assert str(opportunity.pk) not in lists
    assert client.get(f"{ME}/opportunities/{opportunity.pk}/timeline").status_code == 404
    assert client.get(f"{ME}/activities/{meeting.pk}").status_code == 404


def test_the_previous_owner_sees_nothing_of_the_lead_or_the_new_owners_work(story, user_a):
    lead, opportunity, meeting, note, task, admin_task = story
    client = signed_in(user_a)
    detail = client.get(f"{ME}/activities/{meeting.pk}")
    assert detail.status_code == 200
    assert detail.json()["lead"] == {"id": None, "restricted": True}
    body = detail.content.decode()
    assert "Leadname" not in body
    assert "LeadOrg" not in body
    assert str(lead.pk) not in body
    text = walk(client, f"{ME}/opportunities/{opportunity.pk}/timeline")
    for secret in (
        "Leadname",
        "LeadOrg",
        "Open-follow-up",
        "Admin-task-for-B",
        str(task.pk),
        str(admin_task.pk),
        str(lead.pk),
    ):
        assert secret not in text, secret
    lists = walk(client, f"{ME}/activities")
    assert "Open-follow-up" not in lists
    assert "Admin-task-for-B" not in lists
    assert "Leadname" not in lists
    # The archived note followed the lead: A can no longer reach the note A wrote.
    assert client.get(f"{ME}/activities/{note.pk}").status_code == 404
    assert client.get(f"{ME}/leads/{lead.pk}/timeline").status_code == 404


def test_the_previous_owner_can_not_write_through_retained_records(story, user_a):
    lead, opportunity, _, _, task, _ = story
    client = signed_in(user_a)
    alone = client.post(
        f"{ME}/activities",
        {"type": "task", "title": "x", "opportunity": str(opportunity.pk)},
        format="json",
    )
    assert alone.status_code == 422  # A closed it and still sees it: a reason, not a 404
    real = client.post(
        f"{ME}/activities",
        {"type": "task", "title": "x", "lead": str(lead.pk), "opportunity": str(opportunity.pk)},
        format="json",
    )
    ghost = client.post(
        f"{ME}/activities",
        {
            "type": "task",
            "title": "x",
            "lead": str(uuid.uuid4()),
            "opportunity": str(opportunity.pk),
        },
        format="json",
    )
    assert real.status_code == ghost.status_code == 404
    assert without_request_id(real) == without_request_id(ghost)
    for action in ("complete", "cancel"):
        response = client.post(f"{ME}/activities/{task.pk}/{action}", {"version": 2}, format="json")
        assert response.status_code == 404


def test_the_new_owner_can_read_a_note_the_previous_owner_archived(story, user_b):
    """By design: notes, archived ones included, follow the lead to its new owner."""
    _, _, _, note, _, _ = story
    assert signed_in(user_b).get(f"{ME}/activities/{note.pk}").status_code == 200
