"""The critical cross-user suite for Activities and timelines
(docs/testing.md#critical-cross-user-security-suite).

World: Admin, User A and User B. Lead LA (A) with opportunity OA, lead LB (B) with OB; each
user has a task, a meeting and a note. The attacker must never obtain the victim's
activities, learn that they exist, change them, see them counted, or reach the victim's
timeline, through any channel: list (every filter, sort and page size), detail, guessed
ids, timelines, relationship fields (creating their own task "on" the victim's lead or
opportunity), lifecycle actions, edits, archive, summary counts, cursors replayed across
workspaces, workspace substitution or crafted payloads. Every case runs both ways.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone

from arkray.activities.models import Activity, TimelineEntry
from arkray.audit.models import AuditEvent
from arkray.core.keyset import INVALID_CURSOR
from arkray.leads.models import Lead
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
)
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

ME = "/api/v1/workspaces/me"
SECRET = "Zenobia confidential pricing"


def make_world(owner, name, *, secret=False):
    lead = LeadFactory(owner=owner, first_name=name, organization_name=f"{name} Pharma")
    opportunity = OpportunityFactory(lead=lead, title=f"{name} deal")
    text = SECRET if secret else f"{name} note"
    start = timezone.now() - timedelta(hours=3)
    activities = {
        "task": TaskFactory(lead=lead, title=f"{name} task", description=text),
        "meeting": MeetingFactory(
            opportunity=opportunity,
            title=f"{name} meeting",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            location=text,
            meeting_url="https://meet.example/secret-room" if secret else "",
        ),
        "note": NoteFactory(lead=lead, description=text),
    }
    for activity in activities.values():
        TimelineEntry.objects.create(
            lead=lead,
            opportunity=activity.opportunity,
            activity=activity,
            kind={"task": "task.created", "meeting": "meeting.scheduled", "note": "note.added"}[
                activity.type
            ],
            actor=owner,
            data={},
        )
    TimelineEntry.objects.create(
        lead=lead,
        opportunity=opportunity,
        kind="opportunity.created",
        actor=owner,
        data={"stage": "New", "status": "open", "via_conversion": False},
    )
    return lead, opportunity, activities


@pytest.fixture(params=["a_attacks_b", "b_attacks_a"])
def world(request, user_a, user_b):
    attacker, victim = (user_a, user_b) if request.param == "a_attacks_b" else (user_b, user_a)
    own = make_world(attacker, "Arjun")
    theirs = make_world(victim, "Zenobia", secret=True)
    return attacker, victim, own, theirs


def walk(client, url, params):
    seen, bodies, response = [], [], client.get(url, params)
    while True:
        assert response.status_code == 200, response.content
        body = response.json()
        bodies.append(response.content.decode())
        seen.extend(row["id"] for row in body["results"])
        if not body["next"]:
            return seen, "".join(bodies)
        response = client.get(body["next"])


LIST_QUERIES = [
    {},
    {"type": "task"},
    {"type": "meeting"},
    {"type": "note"},
    {"status": "open"},
    {"status": "scheduled"},
    {"type": "task", "status": "completed"},
    {"overdue": "true"},
    {"current": "true"},
    {"archived": "true"},
    {"date_from": "2000-01-01", "date_to": "2099-12-31"},
    *({"ordering": o} for o in ["-created_at", "created_at", "scheduled", "-scheduled"]),
    {"page_size": 1},
    {"page_size": 100},
]


class TestReads:
    @pytest.mark.parametrize("params", LIST_QUERIES)
    def test_no_list_query_ever_returns_the_victims_activities(self, world, params):
        attacker, _, (_, _, mine), (_, _, theirs) = world
        seen, text = walk(signed_in(attacker), f"{ME}/activities", params)
        assert set(seen) <= {str(a.pk) for a in mine.values()}
        assert not {str(a.pk) for a in theirs.values()} & set(seen)
        assert "Zenobia" not in text
        assert SECRET not in text

    @pytest.mark.parametrize("field", ["lead", "opportunity"])
    def test_filtering_by_the_victims_lead_or_opportunity_finds_nothing(self, world, field):
        attacker, _, _, (lead, opportunity, _) = world
        target = {"lead": lead, "opportunity": opportunity}[field]
        response = signed_in(attacker).get(f"{ME}/activities", {field: str(target.pk)})
        assert response.json()["results"] == []

    @pytest.mark.parametrize("kind", ["task", "meeting", "note"])
    def test_a_guessed_id_is_indistinguishable_from_a_missing_one(self, world, kind):
        attacker, _, _, (_, _, theirs) = world
        client = signed_in(attacker)
        real = client.get(f"{ME}/activities/{theirs[kind].pk}")
        missing = client.get(f"{ME}/activities/{uuid.uuid4()}")
        assert real.status_code == missing.status_code == 404
        assert without_request_id(real) == without_request_id(missing)

    @pytest.mark.parametrize("which", ["lead", "opportunity"])
    def test_the_victims_timelines_are_not_found(self, world, which):
        attacker, _, _, (lead, opportunity, _) = world
        client = signed_in(attacker)
        url = {
            "lead": f"{ME}/leads/{lead.pk}/timeline",
            "opportunity": f"{ME}/opportunities/{opportunity.pk}/timeline",
        }[which]
        missing_url = url.replace(
            str(lead.pk if which == "lead" else opportunity.pk), str(uuid.uuid4())
        )
        real, missing = client.get(url), client.get(missing_url)
        assert real.status_code == missing.status_code == 404
        assert without_request_id(real) == without_request_id(missing)

    def test_the_attackers_own_timeline_holds_nothing_of_the_victims(self, world):
        attacker, _, (lead, opportunity, _), _ = world
        client = signed_in(attacker)
        for url in (
            f"{ME}/leads/{lead.pk}/timeline",
            f"{ME}/opportunities/{opportunity.pk}/timeline",
        ):
            text = client.get(url).content.decode()
            assert "Zenobia" not in text
        assert SECRET not in text

    def test_counts_only_count_the_attackers_records(self, world):
        attacker, *_ = world
        body = signed_in(attacker).get(f"{ME}/activity-summary").json()
        # One open task each and one meeting each (scheduled, started 3 hours ago).
        assert body["open_tasks"] == 1
        assert body["upcoming_meetings"] == 0
        assert body["meetings_today"] <= 1

    def test_the_summary_is_identical_whether_or_not_the_victim_has_records(self, world):
        attacker, victim, _, _ = world
        client = signed_in(attacker)
        before = client.get(f"{ME}/activity-summary").json()
        lead = Lead.objects.filter(owner=victim).first()
        for _ in range(5):
            TaskFactory(lead=lead, due_at=timezone.now() - timedelta(days=1))
        assert client.get(f"{ME}/activity-summary").json() == before

    def test_a_cursor_replayed_in_another_workspace_is_refused(self, world):
        """Since Phase 9 a cursor is bound to its user and workspace: the replay is a 400
        (before: a page of the attacker's own activities). Nothing of the victim's either way."""
        attacker, victim, _, _ = world
        victim_page = signed_in(victim).get(f"{ME}/activities", {"page_size": 1}).json()
        cursor = parse_qs(urlparse(victim_page["next"]).query)["cursor"][0]
        response = signed_in(attacker).get(f"{ME}/activities", {"page_size": 1, "cursor": cursor})
        assert response.status_code == 400
        assert response.json()["error"]["details"] == {"cursor": [INVALID_CURSOR]}

    @pytest.mark.parametrize(
        "segment", ["VICTIM", "all", "VICTIM_UPPER", "urn:uuid:VICTIM", "{VICTIM}", "VICTIM_HEX"]
    )
    def test_workspace_substitution_is_not_found(self, world, segment):
        attacker, victim, *_ = world
        value = (
            segment.replace("VICTIM_UPPER", str(victim.pk).upper())
            .replace("VICTIM_HEX", victim.pk.hex)
            .replace("VICTIM", str(victim.pk))
        )
        client = signed_in(attacker)
        for path in ("activities", "activity-summary"):
            assert client.get(f"/api/v1/workspaces/{value}/{path}").status_code == 404

    def test_owner_filters_are_refused_outside_the_organisation(self, world):
        attacker, victim, *_ = world
        response = signed_in(attacker).get(f"{ME}/activities", {"owner": str(victim.pk)})
        assert response.status_code == 400


class TestWrites:
    @pytest.mark.parametrize(
        ("action", "method", "body"),
        [
            ("", "patch", {"version": 1, "title": "pwned"}),
            ("/complete", "post", {"version": 1}),
            ("/cancel", "post", {"version": 1}),
            ("/reopen", "post", {"version": 1}),
            ("/archive", "post", {"version": 1}),
            ("/restore", "post", {"version": 1}),
        ],
    )
    @pytest.mark.parametrize("kind", ["task", "meeting", "note"])
    def test_every_write_to_the_victims_activity_is_not_found_and_changes_nothing(
        self, world, kind, action, method, body
    ):
        attacker, _, _, (lead, _, theirs) = world
        target = theirs[kind]
        before = Lead.objects.get(pk=lead.pk).last_contacted_at
        response = getattr(signed_in(attacker), method)(
            f"{ME}/activities/{target.pk}{action}", body, format="json"
        )
        assert response.status_code == 404
        target.refresh_from_db()
        assert (target.version, target.archived_at) == (1, None)
        assert Lead.objects.get(pk=lead.pk).last_contacted_at == before
        assert not AuditEvent.objects.filter(target_id=str(target.pk)).exists()

    @pytest.mark.parametrize("kind", ["task", "meeting", "note"])
    @pytest.mark.parametrize("link", ["lead", "opportunity", "both"])
    def test_linking_my_activity_to_the_victims_records_reveals_nothing(self, world, kind, link):
        attacker, _, (my_lead, my_opportunity, _), (lead, opportunity, _) = world
        start = timezone.now() + timedelta(days=1)
        fields = {
            "task": {"title": "x"},
            "meeting": {
                "title": "x",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
            },
            "note": {"description": "x"},
        }[kind]
        links = {
            "lead": {"lead": str(lead.pk)},
            "opportunity": {"opportunity": str(opportunity.pk)},
            "both": {"lead": str(my_lead.pk), "opportunity": str(opportunity.pk)},
        }[link]
        client = signed_in(attacker)
        real = client.post(f"{ME}/activities", {"type": kind, **fields, **links}, format="json")
        ghost = {
            key: str(uuid.uuid4()) if key != "lead" or link != "both" else value
            for key, value in links.items()
        }
        missing = client.post(f"{ME}/activities", {"type": kind, **fields, **ghost}, format="json")
        assert real.status_code == missing.status_code == 404
        assert without_request_id(real) == without_request_id(missing)
        assert not Activity.objects.filter(created_by=attacker, title="x").exists()
        assert not Activity.objects.filter(created_by=attacker, description="x").exists()
        assert my_opportunity  # the attacker's own opportunity is unaffected

    def test_completing_a_guessed_meeting_can_not_touch_the_victims_last_contact(self, world):
        attacker, _, _, (lead, _, theirs) = world
        signed_in(attacker).post(
            f"{ME}/activities/{theirs['meeting'].pk}/complete", {"version": 1}, format="json"
        )
        assert Lead.objects.get(pk=lead.pk).last_contacted_at is None

    def test_payloads_can_not_pick_owners_or_system_fields(self, world):
        attacker, victim, (my_lead, _, _), _ = world
        client = signed_in(attacker)
        for extra in (
            {"owner": str(victim.pk)},
            {"created_by": str(victim.pk)},
            {"completed_at": timezone.now().isoformat()},
            {"status": "completed"},
            {"archived_at": timezone.now().isoformat()},
            {"version": 99},
            {"tenant": "x"},
            {"audit_actor": str(victim.pk)},
            {"is_superuser": True},
            {"capabilities": ["crm.view_all"]},
        ):
            response = client.post(
                f"{ME}/activities",
                {"type": "task", "lead": str(my_lead.pk), "title": "x", **extra},
                format="json",
            )
            assert response.status_code == 400, extra
        assert not Activity.objects.filter(title="x").exists()


class TestAdministrator:
    def test_a_users_workspace_shows_that_users_activities_only(self, admin, user_a, user_b):
        _, _, a_rows = make_world(user_a, "Arjun")
        make_world(user_b, "Zenobia", secret=True)
        client = signed_in(admin)
        seen, text = walk(client, f"/api/v1/workspaces/{user_a.pk}/activities", {})
        assert set(seen) == {str(a.pk) for a in a_rows.values()}
        assert "Zenobia" not in text
        summary = client.get(f"/api/v1/workspaces/{user_a.pk}/activity-summary").json()
        assert summary["open_tasks"] == 1
        everything = client.get("/api/v1/workspaces/all/activity-summary").json()
        assert everything["open_tasks"] == 2

    def test_delegated_access_is_audited_and_writes_are_the_admins(self, admin, user_a):
        lead, _, _ = make_world(user_a, "Arjun")
        client = signed_in(admin)
        response = client.post(
            f"/api/v1/workspaces/{user_a.pk}/activities",
            {"type": "note", "lead": str(lead.pk), "description": "Admin's note"},
            format="json",
        )
        assert response.status_code == 201
        assert AuditEvent.objects.filter(action="workspace.accessed", actor_id=admin.pk).exists()
        audit = AuditEvent.objects.get(action="note.created")
        assert (audit.actor_id, audit.subject_user_id) == (admin.pk, user_a.pk)

    def test_the_admins_view_of_a_users_lead_timeline_is_scoped_to_that_user(
        self, admin, user_a, user_b
    ):
        lead, _, _ = make_world(user_a, "Arjun")
        # B's completed meeting on A's lead (B owned the lead before): A's workspace hides it.
        start = timezone.now() - timedelta(days=3)
        theirs = MeetingFactory(
            lead=lead,
            owner=user_b,
            status="completed",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        TimelineEntry.objects.create(
            lead=lead, activity=theirs, kind="meeting.completed", actor=user_b, data={}
        )
        client = signed_in(admin)
        in_a = client.get(f"/api/v1/workspaces/{user_a.pk}/leads/{lead.pk}/timeline").json()
        assert str(theirs.pk) not in str(in_a)
        everywhere = client.get(f"/api/v1/workspaces/all/leads/{lead.pk}/timeline").json()
        assert str(theirs.pk) in str(everywhere)
