"""No N+1: every activity endpoint costs a constant number of queries, whatever the number
of activities, leads, opportunities, owners and timeline entries, in every kind of
workspace. Pinned exactly, so an accidental extra query per request is noticed too.

Per request: session (1) + user (1) [+ subject user exists-check (1) in a user's
workspace] [+ workspace-access audit insert, once per window] + the endpoint's own queries.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities.models import ActivityStatus, TimelineEntry
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
)

from .conftest import (
    activities_url,
    activity_url,
    lead_timeline_url,
    opportunity_timeline_url,
    summary_url,
)

pytestmark = pytest.mark.django_db


def seed(n, owners):
    """n activities of every type spread over leads and opportunities of `owners`, with
    a timeline entry for each (as the services would write)."""
    leads = [LeadFactory(owner=owner) for owner in owners for _ in range(3)]
    opportunities = [OpportunityFactory(lead=lead) for lead in leads]
    rows = []
    for i in range(n):
        lead = leads[i % len(leads)]
        opportunity = opportunities[i % len(leads)] if i % 2 else None
        factory = (TaskFactory, MeetingFactory, NoteFactory)[i % 3]
        activity = factory(lead=lead, opportunity=opportunity)
        TimelineEntry.objects.create(
            lead=lead,
            opportunity=opportunity,
            activity=activity,
            kind={"task": "task.created", "meeting": "meeting.scheduled", "note": "note.added"}[
                activity.type
            ],
            actor=lead.owner,
            data={},
        )
        rows.append(activity)
    return leads, opportunities, rows


def count(client, url, params=None):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, params or {})
    assert response.status_code == 200, response.content
    return len(queries), response.json()


def warm_up(client, url):
    client.get(url)  # the first delegated access writes the workspace-access audit row


@pytest.mark.parametrize("n", [10, 100])
def test_list_own_workspace(user_a_client, user_a, n):
    seed(n, [user_a])
    queries, body = count(user_a_client, activities_url(), {"page_size": 100})
    assert len(body["results"]) == min(n, 100)
    assert queries == 3  # session, user, activities (lead, opportunity, owner, author joined)


@pytest.mark.parametrize("n", [10, 100])
def test_list_in_a_users_workspace(admin_client, user_a, n, django_capture_on_commit_callbacks):
    seed(n, [user_a])
    with django_capture_on_commit_callbacks(execute=True):
        warm_up(admin_client, activities_url(str(user_a.pk)))
    assert count(admin_client, activities_url(str(user_a.pk)), {"page_size": 100})[0] == 4


@pytest.mark.parametrize("n", [10, 100])
def test_list_organisation_wide_with_many_owners(
    admin_client, n, django_capture_on_commit_callbacks
):
    seed(n, [UserFactory() for _ in range(5)])
    with django_capture_on_commit_callbacks(execute=True):
        warm_up(admin_client, activities_url("all"))
    queries, body = count(admin_client, activities_url("all"), {"page_size": 100})
    assert len(body["results"]) == min(n, 100)
    assert queries == 3


@pytest.mark.parametrize(
    "params",
    [
        {"type": "task", "status": "open", "ordering": "scheduled"},
        {"type": "meeting", "ordering": "-scheduled"},
        {"type": "note"},
        {"overdue": "true"},
        {"current": "true"},
        {"type": "meeting", "upcoming": "true", "ordering": "scheduled"},
        {"type": "meeting", "cancelled": "false"},
        {"date_from": "2026-01-01", "date_to": "2099-12-31"},
        {"archived": "true"},
        {"lead": "00000000-0000-4000-8000-000000000000"},
    ],
)
def test_list_filters_and_sorts_stay_constant(user_a_client, user_a, params):
    seed(60, [user_a])
    assert count(user_a_client, activities_url(), {**params, "page_size": 100})[0] == 3


def test_following_a_cursor_costs_the_same(user_a_client, user_a):
    seed(30, [user_a])
    first = user_a_client.get(activities_url(), {"page_size": 10}).json()
    with CaptureQueriesContext(connection) as queries:
        user_a_client.get(first["next"])
    assert len(queries) == 3


@pytest.mark.parametrize("ordering", ["-created_at", "created_at"])
def test_organisation_wide_cursors_cost_nothing_extra(
    admin_client, ordering, django_capture_on_commit_callbacks
):
    """The organisation-wide orderings' cursor reads the generated created_sort column; it
    is loaded with the rows (review: it was deferred, so building each page's cursors
    re-read it with one more query per boundary row)."""
    seed(30, [UserFactory() for _ in range(3)])
    with django_capture_on_commit_callbacks(execute=True):
        warm_up(admin_client, activities_url("all"))
    params = {"page_size": 10, "ordering": ordering}
    queries, first = count(admin_client, activities_url("all"), params)
    assert queries == 3
    with CaptureQueriesContext(connection) as captured:
        middle = admin_client.get(first["next"]).json()
    assert (len(captured), bool(middle["previous"]), bool(middle["next"])) == (3, True, True)


def test_detail(user_a_client, user_a):
    _, _, rows = seed(3, [user_a])
    # session, user, activity (lead, opportunity, owner, author, completer, canceller)
    assert count(user_a_client, activity_url(rows[1].pk))[0] == 3


@pytest.mark.parametrize("n", [10, 100])
def test_summary(user_a_client, user_a, n):
    seed(n, [user_a])
    # session, user, two aggregates (open tasks; meetings from today on: Phase 5 review)
    assert count(user_a_client, summary_url())[0] == 4


@pytest.mark.parametrize("n", [10, 100])
def test_lead_timeline(user_a_client, user_a, admin, n):
    lead = LeadFactory(owner=user_a)
    opportunity = OpportunityFactory(lead=lead)
    for i in range(n):
        factory = (TaskFactory, MeetingFactory, NoteFactory)[i % 3]
        activity = factory(lead=lead, opportunity=opportunity if i % 2 else None)
        TimelineEntry.objects.create(
            lead=lead,
            activity=activity,
            opportunity=activity.opportunity,
            kind={"task": "task.created", "meeting": "meeting.scheduled", "note": "note.added"}[
                activity.type
            ],
            actor=user_a,
            data={},
        )
    # a reassignment back and forth names people in the snapshots
    other = UserFactory()
    org = AccessScope.organization(admin.pk)
    lead_services.reassign_lead(
        actor=admin, scope=org, lead_id=lead.pk, version=1, owner_id=other.pk
    )
    lead_services.reassign_lead(
        actor=admin, scope=org, lead_id=lead.pk, version=2, owner_id=user_a.pk
    )
    queries, body = count(user_a_client, lead_timeline_url(lead.pk), {"page_size": 50})
    assert len(body["results"]) == min(n + 2, 50)
    # session, user, lead visibility, entries (actor, activity, opportunity joined), the
    # people named in the page's snapshots (one query for all of them)
    assert queries == 5


def test_a_timeline_page_naming_nobody_needs_no_people_query(user_a_client, user_a):
    lead = LeadFactory(owner=user_a)
    for _ in range(5):
        note = NoteFactory(lead=lead)
        TimelineEntry.objects.create(
            lead=lead, activity=note, kind="note.added", actor=user_a, data={}
        )
    assert count(user_a_client, lead_timeline_url(lead.pk))[0] == 4


@pytest.mark.parametrize("n", [10, 100])
def test_opportunity_timeline(user_a_client, user_a, n):
    opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
    for _ in range(n):
        task = TaskFactory(opportunity=opportunity)
        TimelineEntry.objects.create(
            lead=opportunity.lead,
            opportunity=opportunity,
            activity=task,
            kind="task.created",
            actor=user_a,
            data={},
        )
    queries, body = count(
        user_a_client, opportunity_timeline_url(opportunity.pk), {"page_size": 50}
    )
    assert len(body["results"]) == min(n, 50)
    assert queries == 4  # session, user, opportunity visibility, entries


@pytest.mark.parametrize("existing", [1, 25])
def test_writes_cost_the_same_whatever_else_exists(user_a_client, user_a, existing):
    lead = LeadFactory(owner=user_a)
    for _ in range(existing):
        TaskFactory(lead=lead)
        NoteFactory(lead=lead)
    with CaptureQueriesContext(connection) as created:
        response = user_a_client.post(
            activities_url(),
            {"type": "task", "lead": str(lead.pk), "title": "Call"},
            format="json",
        )
    assert response.status_code == 201
    # session, user, savepoint, lead lock, owner share lock, insert, timeline, audit,
    # release, reload
    assert len(created) == 10
    task_id = response.json()["id"]
    with CaptureQueriesContext(connection) as completed:
        assert (
            user_a_client.post(
                activity_url(task_id, action="complete"), {"version": 1}, format="json"
            ).status_code
            == 200
        )
    # session, user, savepoint, lead id, lead lock, activity lock, update, timeline, audit,
    # release, reload
    assert len(completed) == 11


def test_completing_a_meeting_adds_only_the_last_contact_update(user_a_client, user_a):
    lead = LeadFactory(owner=user_a)
    start = timezone.now() - timedelta(hours=2)
    meeting = MeetingFactory(lead=lead, starts_at=start, ends_at=start + timedelta(hours=1))
    with CaptureQueriesContext(connection) as queries:
        assert (
            user_a_client.post(
                activity_url(meeting.pk, action="complete"), {"version": 1}, format="json"
            ).status_code
            == 200
        )
    # + the lead's last contact: re-read under the held lock, update, its audit
    assert len(queries) == 14
    assert MeetingFactory._meta.model.objects.get(pk=meeting.pk).status == ActivityStatus.COMPLETED
