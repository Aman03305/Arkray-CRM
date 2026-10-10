"""Global search through the API: what each kind of record matches, what a result shows,
which records are left out, and that results never carry more than a list row does."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from arkray.activities.models import ActivityStatus
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline.models import StageCategory
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
)
from tests.helpers import signed_in

from .conftest import GROUPS, found, ids, search, search_url

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(user_a):
    return signed_in(user_a)


class TestShape:
    def test_every_group_is_present_with_results_and_a_more_flag(self, client):
        body = search(client, "nothing-matches-this")
        assert set(body) == {"query", "terms", *GROUPS}
        for group in GROUPS:
            assert body[group] == {"results": [], "has_more": False}
        assert body["query"] == "nothing-matches-this"
        assert body["terms"] == ["nothing-matches-this"]

    def test_the_query_is_echoed_cleaned_with_the_words_searched(self, client):
        body = search(client, "  Rahul   S  Sharma ")
        assert (body["query"], body["terms"]) == ("Rahul S Sharma", ["Rahul", "Sharma"])

    def test_results_carry_only_what_a_row_shows(self, client, user_a):
        """No contact data, amounts, descriptions, agendas, versions or authors."""
        lead = LeadFactory(
            owner=user_a,
            first_name="Shape",
            last_name="Lead",
            organization_name="Shape Org",
            email="shape.private@example.test",
            phone="+91 98765 43210",
            description="SHAPE-PRIVATE-DESCRIPTION",
        )
        OpportunityFactory(lead=lead, title="Shape deal", description="SHAPE-PRIVATE-DEAL-TEXT")
        TaskFactory(lead=lead, title="Shape task", description="SHAPE-PRIVATE-TASK-TEXT")
        MeetingFactory(
            lead=lead,
            title="Shape meeting",
            description="SHAPE-PRIVATE-AGENDA",
            meeting_url="https://meet.example/shape-private",
        )
        NoteFactory(lead=lead, description="Shape note")
        body = search(client, "shape")
        assert {g: len(body[g]["results"]) for g in GROUPS} == dict.fromkeys(GROUPS, 1)
        assert set(body["leads"]["results"][0]) == {
            "id",
            "display_name",
            "organization_name",
            "status",
            "owner",
        }
        assert set(body["opportunities"]["results"][0]) == {
            "id",
            "title",
            "status",
            "stage",
            "account_name",
            "customer_name",
            "lead",
            "owner",
            "customer_restricted",
        }
        assert set(body["tasks"]["results"][0]) == {
            "id",
            "title",
            "status",
            "priority",
            "due_at",
            "is_overdue",
            "lead",
            "owner",
        }
        assert set(body["meetings"]["results"][0]) == {
            "id",
            "title",
            "status",
            "starts_at",
            "ends_at",
            "location",
            "is_overdue",
            "lead",
            "owner",
        }
        assert set(body["notes"]["results"][0]) == {
            "id",
            "preview",
            "preview_truncated",
            "preview_starts_mid_text",
            "created_at",
            "lead",
        }
        text = str(body)
        for private in ("SHAPE-PRIVATE", "shape.private", "98765", "100000", "version"):
            assert private not in text

    def test_a_lead_result(self, client, user_a):
        lead = LeadFactory(owner=user_a, first_name="Rahul", last_name="Mehta")
        (row,) = search(client, "Mehta")["leads"]["results"]
        assert row == {
            "id": str(lead.pk),
            "display_name": "Rahul Mehta",
            "organization_name": "Apollo Diagnostics",
            "status": {"key": "new", "name": "New", "category": "open"},
            "owner": {"id": str(user_a.pk), "full_name": "Rahul Sharma", "is_active": True},
        }

    def test_an_opportunity_result_names_its_stage_and_lead(self, client, user_a):
        lead = LeadFactory(owner=user_a, first_name="Deal", last_name="Lead")
        deal = OpportunityFactory(lead=lead, title="Analyser upgrade", stage_key="proposal")
        (row,) = search(client, "analyser upgrade")["opportunities"]["results"]
        assert row["id"] == str(deal.pk)
        assert (row["status"], row["stage"]["name"]) == ("open", "Proposal")
        assert row["lead"] == {
            "id": str(lead.pk),
            "display_name": "Deal Lead",
            "organization_name": "Apollo Diagnostics",
            "restricted": False,
        }

    def test_task_and_meeting_results_say_when_and_whether_overdue(self, client, user_a):
        lead = LeadFactory(owner=user_a)
        past = timezone.now() - timedelta(days=2)
        TaskFactory(lead=lead, title="Overdue quotation", due_at=past)
        MeetingFactory(
            lead=lead,
            title="Missed review",
            starts_at=past,
            ends_at=past + timedelta(hours=1),
            location="Mumbai",
        )
        body = search(client, "quotation")
        assert body["tasks"]["results"][0]["is_overdue"] is True
        meeting = search(client, "review")["meetings"]["results"][0]
        assert (meeting["location"], meeting["is_overdue"]) == ("Mumbai", True)


class TestWhatMatches:
    def test_leads_match_names_organisation_email_and_phone_digits(self, client, user_a):
        lead = LeadFactory(
            owner=user_a,
            first_name="Kavya",
            last_name="Menon",
            organization_name="Lotus Labs",
            email="kavya@lotus.example",
            phone="+91 98765 43210",
        )
        for q in ("kavya", "MENON", "lotus labs", "kavya@lotus", "98765-43210", "9876543210"):
            assert ids(search(client, q), "leads") == [str(lead.pk)], q

    def test_leads_are_not_matched_by_description_or_city(self, client, user_a):
        """The Leads list's own rule, unchanged: contact and name fields only."""
        LeadFactory(owner=user_a, description="Mentions Coimbatore", city="Coimbatore")
        assert search(client, "Coimbatore")["leads"]["results"] == []

    def test_opportunities_match_the_title_only(self, client, user_a):
        lead = LeadFactory(owner=user_a, first_name="Zubin", last_name="Lead")
        OpportunityFactory(lead=lead, title="Reagent supply", description="zubinnote")
        assert ids(search(client, "reagent"), "opportunities") != []
        # Not by the lead's name, the description or the amount.
        assert search(client, "zubin")["opportunities"]["results"] == []
        assert search(client, "zubinnote")["opportunities"]["results"] == []
        assert search(client, "100000")["opportunities"]["results"] == []

    def test_tasks_match_the_title_not_the_description(self, client, user_a):
        lead = LeadFactory(owner=user_a)
        task = TaskFactory(lead=lead, title="Send brochure", description="secret-task-detail")
        assert ids(search(client, "brochure"), "tasks") == [str(task.pk)]
        assert search(client, "secret-task-detail")["tasks"]["results"] == []

    def test_meetings_match_title_and_location_not_the_agenda(self, client, user_a):
        lead = LeadFactory(owner=user_a)
        meeting = MeetingFactory(
            lead=lead, title="Morning demo", location="Pune office", description="agenda-text"
        )
        for q in ("morning", "pune", "demo pune", "morning office"):
            assert ids(search(client, q), "meetings") == [str(meeting.pk)], q
        assert search(client, "agenda-text")["meetings"]["results"] == []

    def test_a_word_never_matches_across_title_and_location(self, client, user_a):
        MeetingFactory(lead=LeadFactory(owner=user_a), title="Demo", location="Online")
        assert search(client, "demo online")["meetings"]["results"] != []
        assert search(client, "demoonline")["meetings"]["results"] == []

    def test_notes_match_their_body(self, client, user_a):
        note = NoteFactory(lead=LeadFactory(owner=user_a), description="Prefers morning calls.")
        assert ids(search(client, "morning calls"), "notes") == [str(note.pk)]
        assert ids(search(client, "prefers"), "notes") == [str(note.pk)]

    def test_activities_are_not_matched_by_their_leads_name(self, client, user_a):
        lead = LeadFactory(owner=user_a, first_name="Ishaan", last_name="Lead")
        TaskFactory(lead=lead, title="Call back")
        MeetingFactory(lead=lead, title="Catch up")
        NoteFactory(lead=lead, description="Short note")
        body = search(client, "Ishaan")
        assert ids(body, "leads") == [str(lead.pk)]
        assert body["tasks"]["results"] == body["meetings"]["results"] == []
        assert body["notes"]["results"] == []

    def test_every_word_must_match(self, client, user_a):
        lead = LeadFactory(owner=user_a, first_name="Rahul", last_name="Verma")
        LeadFactory(owner=user_a, first_name="Rahul", last_name="Kapoor")
        assert ids(search(client, "rahul verma"), "leads") == [str(lead.pk)]
        assert ids(search(client, "verma rahul"), "leads") == [str(lead.pk)]

    def test_case_and_unicode_forms_fold_alike(self, client, user_a):
        lead = LeadFactory(owner=user_a, first_name="Zoë", last_name="Ångström")
        for q in ("ZOË", "zoë", "Zoe" + chr(0x308), "ångström"):
            assert ids(search(client, q), "leads") == [str(lead.pk)], q


class TestWhatIsLeftOut:
    def test_archived_records_of_every_kind_are_left_out(self, client, user_a):
        now = timezone.now()
        live = LeadFactory(owner=user_a, first_name="Archive", last_name="Live")
        LeadFactory(owner=user_a, first_name="Archive", last_name="Gone", archived_at=now)
        OpportunityFactory(lead=live, title="Archive deal", archived_at=now)
        TaskFactory(lead=live, title="Archive task", archived_at=now)
        MeetingFactory(lead=live, title="Archive meeting", archived_at=now)
        NoteFactory(lead=live, description="Archive note", archived_at=now)
        assert found(search(client, "archive")) == {
            "leads": [str(live.pk)],
            "opportunities": [],
            "tasks": [],
            "meetings": [],
            "notes": [],
        }

    def test_won_lost_completed_and_cancelled_records_are_history_and_stay(self, client, user_a):
        lead = LeadFactory(owner=user_a)
        won = OpportunityFactory(lead=lead, title="History won", stage_key="won")
        lost = OpportunityFactory(lead=lead, title="History lost", stage_key="lost")
        done = TaskFactory(lead=lead, title="History done", status=ActivityStatus.COMPLETED)
        dropped = TaskFactory(lead=lead, title="History dropped", status=ActivityStatus.CANCELLED)
        met = MeetingFactory(lead=lead, title="History met", status=ActivityStatus.COMPLETED)
        body = search(client, "history")
        assert set(ids(body, "opportunities")) == {str(won.pk), str(lost.pk)}
        assert {r["status"] for r in body["opportunities"]["results"]} == {
            StageCategory.WON,
            StageCategory.LOST,
        }
        assert set(ids(body, "tasks")) == {str(done.pk), str(dropped.pk)}
        assert ids(body, "meetings") == [str(met.pk)]
        assert body["meetings"]["results"][0]["status"] == "completed"


class TestRestrictedLeads:
    """A closed deal or task its owner kept after the lead was reassigned shows the lead as
    "restricted" (Phase 3/4). Search must not undo that: no result reveals the lead, and
    nothing is matched by the lead's name (that would tell whose lead it is)."""

    @pytest.fixture
    def kept(self, user_a, user_b, admin):
        lead = LeadFactory(owner=user_a, first_name="Moved", last_name="Away")
        # Its customer is no longer the owner's to see (pipeline.customer): the deal is found
        # by its organisation and instrument only, and shown without its customer.
        deal = OpportunityFactory(
            lead=lead,
            title="Moved Away",
            account_name="Kept Labs",
            customer_name="Moved Away",
            stage_key="won",
        )
        task = TaskFactory(lead=lead, title="Kept task", status=ActivityStatus.COMPLETED)
        lead_services.reassign_lead(
            actor=admin,
            scope=_organisation(admin),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        return lead, deal, task

    def test_the_kept_records_show_their_lead_as_restricted(self, client, kept):
        _, deal, task = kept
        body = search(client, "kept")
        assert ids(body, "opportunities") == [str(deal.pk)]
        assert ids(body, "tasks") == [str(task.pk)]
        restricted = {"id": None, "restricted": True}
        assert body["opportunities"]["results"][0]["lead"] == restricted
        assert body["opportunities"]["results"][0]["customer_restricted"] is True
        assert body["opportunities"]["results"][0]["title"] == "Kept Labs"
        assert body["tasks"]["results"][0]["lead"] == restricted
        assert "Moved" not in str(body)

    def test_the_moved_leads_name_finds_nothing(self, client, kept):
        assert found(search(client, "Moved Away")) == {g: [] for g in GROUPS}


class TestResultsGrantNothing:
    def test_a_result_reassigned_away_is_not_found_when_opened(self, client, admin, user_a, user_b):
        """Search results are a snapshot, not a grant: the record page re-authorises."""
        lead = LeadFactory(owner=user_a, first_name="Snapshot", last_name="Lead")
        (row,) = search(client, "snapshot")["leads"]["results"]
        lead_services.reassign_lead(
            actor=admin,
            scope=_organisation(admin),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        assert client.get(f"/api/v1/workspaces/me/leads/{row['id']}").status_code == 404
        assert search(client, "snapshot")["leads"]["results"] == []


class TestWorkspaces:
    def test_a_deactivated_users_workspace_stays_searchable_by_an_admin(self, admin):
        gone = UserFactory(is_active=False, first_name="Former", last_name="Rep")
        lead = LeadFactory(owner=gone, first_name="Legacy", last_name="Client")
        body = search(signed_in(admin), "legacy", str(gone.pk))
        assert ids(body, "leads") == [str(lead.pk)]

    def test_an_unopenable_workspace_is_404_before_the_query_is_looked_at(self, client, user_b):
        """No oracle: a sales user can't tell a valid query from an invalid one in a workspace
        they can't open, nor an existing user from a missing one."""
        for q in ("valid", "", "x", "a" * 500, "bad" + chr(0x202E)):
            for workspace in (str(user_b.pk), "all", "5a1e4d2c-0000-4000-8000-00000000abcd"):
                response = client.get(search_url(q, workspace))
                assert response.status_code == 404, (q, workspace)

    def test_own_records_found_in_ones_own_workspace_by_id(self, user_a):
        lead = LeadFactory(owner=user_a, first_name="Selfie")
        body = search(signed_in(user_a), "selfie", str(user_a.pk))
        assert ids(body, "leads") == [str(lead.pk)]


def _organisation(admin):
    from arkray.core.access import AccessScope

    return AccessScope.organization(admin.pk)


def test_the_lead_model_keeps_no_search_history(client, user_a):
    """Search stores nothing: the same rows exist before and after (a stronger check of the
    read-only transaction is in test_read_only.py)."""
    LeadFactory(owner=user_a, first_name="Count")
    before = Lead.objects.count()
    search(client, "count")
    assert Lead.objects.count() == before


class TestRateLimit:
    def test_each_user_has_a_search_budget(self, user_a, user_b, monkeypatch):
        """A runaway client (or script) is held to the search scope's rate; other users and
        other endpoints are unaffected."""
        from rest_framework.throttling import ScopedRateThrottle

        monkeypatch.setattr(
            ScopedRateThrottle,
            "THROTTLE_RATES",
            {**ScopedRateThrottle.THROTTLE_RATES, "search": "3/min"},
        )
        rahul, priya = signed_in(user_a), signed_in(user_b)
        assert [rahul.get(search_url("abc")).status_code for _ in range(4)] == [200, 200, 200, 429]
        limited = rahul.get(search_url("abc"))
        assert limited.json()["error"]["code"] == "rate_limited"
        assert int(limited["Retry-After"]) >= 1
        assert priya.get(search_url("abc")).status_code == 200
        assert rahul.get("/api/v1/workspaces/me/leads").status_code == 200
