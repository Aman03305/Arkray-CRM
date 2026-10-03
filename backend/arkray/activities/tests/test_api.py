"""The activities HTTP API: shapes, strict input, filters, orderings, pagination, the
lifecycle actions and the summary, in every kind of workspace."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from arkray.activities.models import Activity, ActivityStatus
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
)
from tests.helpers import signed_in

from .conftest import activities_url, activity_url, iso, summary_url

pytestmark = pytest.mark.django_db

SHARED_KEYS = {
    "id",
    "type",
    "title",
    "status",
    "priority",
    "due_at",
    "starts_at",
    "ends_at",
    "is_overdue",
    "completable",
    "lead",
    "opportunity",
    "owner",
    "created_by",
    "completed_at",
    "cancelled_at",
    "archived_at",
    "version",
    "created_at",
    "updated_at",
}


def post(client, body, workspace="me", **headers):
    return client.post(activities_url(workspace), body, format="json", headers=headers)


def ids(response):
    assert response.status_code == 200, response.content
    return [row["id"] for row in response.json()["results"]]


class TestCreate:
    def test_a_task(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        due = timezone.now() + timedelta(days=2)
        response = post(
            user_a_client,
            {
                "type": "task",
                "lead": str(lead.pk),
                "title": "Call back",
                "due_at": iso(due),
                "priority": "high",
                "description": "Ask about the tender",
            },
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert set(body) == SHARED_KEYS | {
            "description",
            "location",
            "meeting_url",
            "completed_by",
            "cancelled_by",
        }
        assert (body["type"], body["status"], body["priority"]) == ("task", "open", "high")
        assert body["lead"] == {
            "id": str(lead.pk),
            "display_name": lead.display_name,
            "organization_name": lead.organization_name,
            "restricted": False,
        }
        assert body["owner"]["id"] == str(user_a.pk) == body["created_by"]["id"]
        assert response["Location"] == f"/api/v1/workspaces/me/activities/{body['id']}"

    def test_a_meeting_and_a_note(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        start = timezone.now() + timedelta(days=1)
        meeting = post(
            user_a_client,
            {
                "type": "meeting",
                "lead": str(lead.pk),
                "title": "Demo",
                "starts_at": iso(start),
                "ends_at": iso(start + timedelta(hours=1)),
                "location": "Andheri office",
                "meeting_url": "https://meet.example/abc",
            },
        )
        assert meeting.status_code == 201, meeting.content
        assert meeting.json()["status"] == "scheduled"
        note = post(user_a_client, {"type": "note", "lead": str(lead.pk), "description": "Hi"})
        assert note.status_code == 201
        assert (note.json()["status"], note.json()["title"]) == (None, "")

    def test_offsets_are_honoured_and_naive_times_refused(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        response = post(
            user_a_client,
            {
                "type": "task",
                "lead": str(lead.pk),
                "title": "x",
                "due_at": "2026-10-03T00:15:00+05:30",
            },
        )
        assert response.json()["due_at"] == "2026-10-02T18:45:00Z"
        naive = post(
            user_a_client,
            {"type": "task", "lead": str(lead.pk), "title": "x", "due_at": "2026-10-03T00:15:00"},
        )
        assert naive.status_code == 400
        assert "due_at" in naive.json()["error"]["details"]

    @pytest.mark.parametrize(
        "extra",
        [
            {"owner": "SELF"},
            {"created_by": "SELF"},
            {"status": "completed"},
            {"completed_at": "2026-10-01T10:00:00Z"},
            {"cancelled_at": "2026-10-01T10:00:00Z"},
            {"archived_at": "2026-10-01T10:00:00Z"},
            {"version": 7},
            {"is_superuser": True},
            {"capabilities": ["*"]},
            {"organization": "x"},
            {"actor_id": "x"},
            {"id": str(uuid.uuid4())},
        ],
    )
    def test_system_fields_are_refused(self, user_a, user_a_client, user_b, extra):
        lead = LeadFactory(owner=user_a)
        body = {"type": "task", "lead": str(lead.pk), "title": "x", **extra}
        for key, value in body.items():
            if value == "SELF":
                body[key] = str(user_b.pk)
        response = post(user_a_client, body)
        assert response.status_code == 400
        assert not Activity.objects.exists()

    def test_idempotent_retries_replay(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        body = {"type": "note", "lead": str(lead.pk), "description": "once"}
        key = str(uuid.uuid4())
        first = post(user_a_client, body, **{"Idempotency-Key": key})
        second = post(user_a_client, body, **{"Idempotency-Key": key})
        assert (first.status_code, second.status_code) == (201, 201)
        assert second["Idempotent-Replayed"] == "true"
        assert first.json()["id"] == second.json()["id"]
        assert post(user_a_client, body, **{"Idempotency-Key": "nope"}).status_code == 400

    def test_query_parameters_on_a_create_are_refused(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.post(
            f"{activities_url()}?owner={uuid.uuid4()}",
            {"type": "note", "lead": str(lead.pk), "description": "x"},
            format="json",
        )
        assert response.status_code == 400


class TestList:
    def test_rows_are_bounded_and_complete(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        NoteFactory(lead=lead, description="x" * 5000)
        row = user_a_client.get(activities_url()).json()["results"][0]
        assert set(row) == SHARED_KEYS | {"preview", "preview_truncated"}
        assert (len(row["preview"]), row["preview_truncated"]) == (240, True)

    def test_filters(self, user_a, user_a_client):
        lead, other = LeadFactory(owner=user_a), LeadFactory(owner=user_a)
        opportunity = OpportunityFactory(lead=lead)
        now = timezone.now()
        overdue = TaskFactory(lead=lead, due_at=now - timedelta(hours=1))
        later = TaskFactory(lead=other, due_at=now + timedelta(days=3))
        done = TaskFactory(lead=lead, status=ActivityStatus.COMPLETED)
        meeting = MeetingFactory(opportunity=opportunity)
        cancelled_meeting = MeetingFactory(lead=lead, status=ActivityStatus.CANCELLED)
        note = NoteFactory(lead=lead)
        archived = NoteFactory(lead=lead, archived_at=now)
        url = activities_url()
        assert set(ids(user_a_client.get(url, {"type": "task"}))) == {
            str(overdue.pk),
            str(later.pk),
            str(done.pk),
        }
        assert set(ids(user_a_client.get(url, {"status": "open"}))) == {
            str(overdue.pk),
            str(later.pk),
        }
        assert ids(user_a_client.get(url, {"overdue": "true"})) == [str(overdue.pk)]
        assert set(ids(user_a_client.get(url, {"current": "true"}))) == {
            str(overdue.pk),
            str(later.pk),
            str(meeting.pk),
        }
        assert set(ids(user_a_client.get(url, {"type": "meeting", "status": "cancelled"}))) == {
            str(cancelled_meeting.pk)
        }
        assert ids(user_a_client.get(url, {"opportunity": str(opportunity.pk)})) == [
            str(meeting.pk)
        ]
        assert str(later.pk) not in ids(user_a_client.get(url, {"lead": str(lead.pk)}))
        assert ids(user_a_client.get(url, {"type": "note"})) == [str(note.pk)]
        assert ids(user_a_client.get(url, {"archived": "true"})) == [str(archived.pk)]
        # Upcoming: current work from now on (the overdue task isn't; undated tasks are).
        assert set(ids(user_a_client.get(url, {"upcoming": "true"}))) == {
            str(later.pk),
            str(meeting.pk),
        }
        assert set(ids(user_a_client.get(url, {"cancelled": "false"}))) == {
            str(overdue.pk),
            str(later.pk),
            str(done.pk),
            str(meeting.pk),
            str(note.pk),  # notes have no status, so they are never "cancelled"
        }
        assert ids(user_a_client.get(url, {"cancelled": "true"})) == [str(cancelled_meeting.pk)]

    def test_the_date_range_uses_the_business_day(self, user_a, user_a_client):
        """00:15 IST on 3 Oct is still 2 Oct in UTC, but it belongs to 3 Oct."""
        lead = LeadFactory(owner=user_a)
        just_after_midnight = TaskFactory(
            lead=lead, due_at=datetime(2026, 10, 2, 18, 45, tzinfo=UTC)
        )
        just_before = TaskFactory(lead=lead, due_at=datetime(2026, 10, 2, 18, 15, tzinfo=UTC))
        url = activities_url()
        assert ids(
            user_a_client.get(url, {"date_from": "2026-10-03", "date_to": "2026-10-03"})
        ) == [str(just_after_midnight.pk)]
        assert ids(
            user_a_client.get(url, {"date_from": "2026-10-02", "date_to": "2026-10-02"})
        ) == [str(just_before.pk)]

    @pytest.mark.parametrize(
        "params",
        [
            {"type": "meeting", "status": "open"},
            {"type": "note", "status": "completed"},
            {"overdue": "true", "type": "meeting"},
            {"current": "true", "status": "open"},
            {"upcoming": "true", "status": "scheduled"},
            {"upcoming": "true", "overdue": "true"},
            {"cancelled": "false", "status": "open"},
            {"cancelled": "maybe"},
            {"date_from": "2026-10-05", "date_to": "2026-10-01"},
            {"date_from": "1999-12-31"},
            {"owner": str(uuid.uuid4())},  # organisation-wide only
            {"ordering": "title"},
            {"page_size": 101},
            {"q": "anything"},
            {"owner_id": "x"},
            {"status__in": "open"},
        ],
    )
    def test_invalid_or_unknown_parameters_are_400(self, user_a_client, params):
        assert user_a_client.get(activities_url(), params).status_code == 400

    def test_orderings_and_pages(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        now = timezone.now()
        soon = TaskFactory(lead=lead, due_at=now + timedelta(days=1))
        later = TaskFactory(lead=lead, due_at=now + timedelta(days=5))
        undated = TaskFactory(lead=lead)
        url = activities_url()
        assert ids(user_a_client.get(url, {"type": "task", "ordering": "scheduled"})) == [
            str(soon.pk),
            str(later.pk),
            str(undated.pk),
        ]
        assert ids(user_a_client.get(url, {"type": "task", "ordering": "-scheduled"}))[0] == str(
            undated.pk
        )
        assert ids(user_a_client.get(url))[0] == str(undated.pk)  # newest first
        seen, response = [], user_a_client.get(url, {"page_size": 1, "ordering": "scheduled"})
        while True:
            body = response.json()
            seen += [r["id"] for r in body["results"]]
            if not body["next"]:
                break
            response = user_a_client.get(body["next"])
        assert seen == [str(soon.pk), str(later.pk), str(undated.pk)]
        assert user_a_client.get(url, {"cursor": "forged"}).status_code == 400

    def test_is_overdue_is_computed_when_read(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        now = timezone.now()
        TaskFactory(lead=lead, due_at=now - timedelta(minutes=1))
        MeetingFactory(
            lead=lead, starts_at=now - timedelta(hours=2), ends_at=now - timedelta(hours=1)
        )
        TaskFactory(lead=lead, due_at=now - timedelta(minutes=1), status=ActivityStatus.COMPLETED)
        rows = user_a_client.get(activities_url()).json()["results"]
        assert sorted(r["is_overdue"] for r in rows) == [False, True, True]

    def test_the_owner_filter_narrows_the_organisation(self, admin_client, user_a, user_b):
        mine = TaskFactory(lead=LeadFactory(owner=user_a))
        TaskFactory(lead=LeadFactory(owner=user_b))
        assert ids(admin_client.get(activities_url("all"), {"owner": str(user_a.pk)})) == [
            str(mine.pk)
        ]

    def test_a_users_workspace_shows_exactly_that_users_activities(
        self, admin_client, user_a, user_b
    ):
        mine = TaskFactory(lead=LeadFactory(owner=user_a))
        TaskFactory(lead=LeadFactory(owner=user_b))
        assert ids(admin_client.get(activities_url(str(user_a.pk)))) == [str(mine.pk)]


class TestDetailAndActions:
    def test_detail_edit_and_conflicts(self, user_a, user_a_client):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        detail = user_a_client.get(activity_url(task.pk))
        assert detail.status_code == 200
        edited = user_a_client.patch(
            activity_url(task.pk), {"version": 1, "title": "Renamed"}, format="json"
        )
        assert (edited.status_code, edited.json()["version"]) == (200, 2)
        stale = user_a_client.patch(
            activity_url(task.pk), {"version": 1, "title": "Again"}, format="json"
        )
        assert stale.status_code == 409
        for field, value in [
            ("owner", str(user_a.pk)),
            ("type", "note"),
            ("lead", str(task.lead_id)),
            ("status", "completed"),
            ("opportunity", str(uuid.uuid4())),
        ]:
            response = user_a_client.patch(
                activity_url(task.pk), {"version": 2, field: value}, format="json"
            )
            assert response.status_code == 400, field

    def test_lifecycle_actions(self, user_a, user_a_client):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        completed = user_a_client.post(
            activity_url(task.pk, action="complete"), {"version": 1}, format="json"
        )
        assert completed.json()["status"] == "completed"
        assert completed.json()["completed_by"]["id"] == str(user_a.pk)
        again = user_a_client.post(
            activity_url(task.pk, action="complete"), {"version": 1}, format="json"
        )
        assert again.status_code == 200
        reopened = user_a_client.post(
            activity_url(task.pk, action="reopen"), {"version": 2}, format="json"
        )
        assert reopened.json()["status"] == "open"
        cancelled = user_a_client.post(
            activity_url(task.pk, action="cancel"), {"version": 3}, format="json"
        )
        assert cancelled.json()["status"] == "cancelled"
        refused = user_a_client.post(
            activity_url(task.pk, action="complete"), {"version": 4}, format="json"
        )
        assert refused.status_code == 422
        archived = user_a_client.post(
            activity_url(task.pk, action="archive"), {"version": 4}, format="json"
        )
        assert archived.json()["archived_at"] is not None
        restored = user_a_client.post(
            activity_url(task.pk, action="restore"), {"version": 5}, format="json"
        )
        assert restored.json()["archived_at"] is None
        assert (
            user_a_client.post(
                activity_url(task.pk, action="complete"),
                {"version": 6, "completed_at": "x"},
                format="json",
            ).status_code
            == 400
        )
        assert (
            user_a_client.post(
                activity_url(task.pk, action="complete"), {}, format="json"
            ).status_code
            == 400
        )

    def test_a_notes_author_only_edit_is_a_403_for_others(self, admin, user_a, user_a_client):
        note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=admin)
        response = user_a_client.patch(
            activity_url(note.pk), {"version": 1, "description": "Changed"}, format="json"
        )
        assert response.status_code == 403
        assert "author" in response.json()["error"]["message"]

    def test_a_sales_user_can_not_write_in_another_workspace(self, user_a_client, user_b):
        task = TaskFactory(lead=LeadFactory(owner=user_b))
        response = user_a_client.post(
            activity_url(task.pk, str(user_b.pk), "complete"), {"version": 1}, format="json"
        )
        assert response.status_code == 404  # the workspace itself can't be opened


class TestSummary:
    def test_figures(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        now = timezone.now()
        TaskFactory(lead=lead, due_at=now - timedelta(days=2))  # open, overdue
        TaskFactory(lead=lead)  # open, undated
        TaskFactory(lead=lead, status=ActivityStatus.COMPLETED)
        TaskFactory(lead=lead, archived_at=now)
        MeetingFactory(lead=lead, starts_at=now + timedelta(days=3))  # upcoming
        TaskFactory(lead=LeadFactory(owner=UserFactory()), due_at=now - timedelta(days=1))
        body = user_a_client.get(summary_url()).json()
        assert body["open_tasks"] == 2
        assert body["overdue_tasks"] == 1
        assert body["upcoming_meetings"] == 1
        assert set(body) == {
            "open_tasks",
            "tasks_due_today",
            "overdue_tasks",
            "meetings_today",
            "upcoming_meetings",
        }

    def test_the_summary_takes_no_parameters(self, user_a_client):
        assert user_a_client.get(summary_url(), {"owner": str(uuid.uuid4())}).status_code == 400


def test_admin_in_a_users_workspace_creates_for_them(admin, user_a):
    lead = LeadFactory(owner=user_a)
    response = post(
        signed_in(admin),
        {"type": "note", "lead": str(lead.pk), "description": "Admin note"},
        workspace=str(user_a.pk),
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["owner"]["id"], body["created_by"]["id"]) == (str(user_a.pk), str(admin.pk))


def test_completable_says_what_complete_would_accept(user_a, user_a_client):
    lead = LeadFactory(owner=user_a)
    now = timezone.now()
    open_task = TaskFactory(lead=lead)
    started = MeetingFactory(
        lead=lead, starts_at=now - timedelta(minutes=5), ends_at=now + timedelta(hours=1)
    )
    future = MeetingFactory(lead=lead, starts_at=now + timedelta(hours=1))
    done = TaskFactory(lead=lead, status=ActivityStatus.COMPLETED)
    note = NoteFactory(lead=lead)
    archived = TaskFactory(lead=lead, archived_at=now)
    rows = {
        r["id"]: r["completable"] for r in user_a_client.get(activities_url()).json()["results"]
    }
    assert rows == {
        str(open_task.pk): True,
        str(started.pk): True,
        str(future.pk): False,
        str(done.pk): False,
        str(note.pk): False,
    }
    assert user_a_client.get(activity_url(archived.pk)).json()["completable"] is False
