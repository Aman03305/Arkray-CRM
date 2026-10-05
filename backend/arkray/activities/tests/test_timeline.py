"""The lead and opportunity timelines (docs/activities.md#timeline): authoritative entries
written in the transaction of each change, read newest first with keyset pagination, each
entry visible only while the record it is about is visible in the same workspace, and
historical entries that keep their meaning when names, owners and stages change."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone

from arkray.activities import services, timeline
from arkray.activities.models import TimelineEntry, TimelineKind
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from arkray.leads.models import LeadStatus
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import Stage
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    OpportunityFactory,
    TaskFactory,
    default_stage,
)
from tests.helpers import signed_in

from .conftest import (
    lead_timeline_url,
    meeting_fields,
    note_fields,
    opportunity_timeline_url,
    task_fields,
)

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
ORG = AccessScope.organization


def entries(client, url, **params):
    response = client.get(url, params)
    assert response.status_code == 200, response.content
    return response.json()["results"]


def walk(client, url, page_size=2):
    seen, response = [], client.get(url, {"page_size": page_size})
    while True:
        assert response.status_code == 200, response.content
        body = response.json()
        seen.extend(body["results"])
        if not body["next"]:
            return seen
        response = client.get(body["next"])


def create(actor, scope, kind, lead=None, opportunity=None, **fields):
    defaults = {"task": task_fields, "meeting": meeting_fields, "note": note_fields}[kind]()
    return services.create_activity(
        actor=actor,
        scope=scope,
        activity_type=kind,
        fields={**defaults, **fields},
        lead_id=lead.pk if lead else None,
        opportunity_id=opportunity.pk if opportunity else None,
    ).activity


def new_lead(actor, scope=None, **fields):
    return lead_services.create_lead(
        actor=actor, scope=scope or OWN(actor.pk), fields={"first_name": "Asha", **fields}
    ).lead


class TestWhatIsRecorded:
    def test_a_leads_whole_story_newest_first(self, user_a, user_a_client):
        scope = OWN(user_a.pk)
        lead = new_lead(user_a)
        lead_services.change_status(
            actor=user_a, scope=scope, lead_id=lead.pk, version=1, status="contacted"
        )
        opportunity = pipeline_services.create_opportunity(
            actor=user_a,
            scope=scope,
            lead_id=lead.pk,
            fields={"value": Decimal("100000"), "instrument_name": "Adams 8180 T"},
        ).opportunity
        pipeline_services.move_opportunity(
            actor=user_a,
            scope=scope,
            opportunity_id=opportunity.pk,
            version=1,
            stage_id=default_stage("proposal").pk,
        )
        task = create(user_a, scope, "task", lead=lead)
        create(user_a, scope, "meeting", opportunity=opportunity)
        create(user_a, scope, "note", lead=lead)
        services.complete_activity(actor=user_a, scope=scope, activity_id=task.pk, version=1)

        rows = entries(user_a_client, lead_timeline_url(lead.pk))
        assert [r["kind"] for r in rows] == [
            "task.completed",
            "note.added",
            "meeting.scheduled",
            "task.created",
            "opportunity.stage_changed",
            "opportunity.created",
            "lead.status_changed",
            "lead.created",
        ]
        by_kind = {r["kind"]: r for r in rows}
        assert by_kind["lead.status_changed"]["details"] == {
            "from": "new",
            "from_name": "New",
            "to": "contacted",
            "to_name": "Contacted",
        }
        assert by_kind["opportunity.stage_changed"]["details"] == {
            "from_stage": "New",
            "to_stage": "Proposal",
        }
        # Named after the lead (the customer) and the instrument.
        assert by_kind["opportunity.created"]["opportunity"]["title"] == "Asha — Adams 8180 T"
        assert by_kind["meeting.scheduled"]["opportunity"]["id"] == str(opportunity.pk)
        assert by_kind["task.created"]["activity"]["title"] == "Send the revised quotation"
        assert by_kind["lead.created"]["details"]["owner"]["full_name"] == "Rahul Sharma"
        assert all(r["actor"]["full_name"] == "Rahul Sharma" for r in rows)

    def test_conversion_reads_as_an_opportunity_created_by_conversion(self, user_a, user_a_client):
        lead = new_lead(user_a)
        pipeline_services.convert_lead(
            actor=user_a,
            scope=OWN(user_a.pk),
            lead_id=lead.pk,
            lead_version=1,
            fields={"value": Decimal("1")},
        )
        rows = entries(user_a_client, lead_timeline_url(lead.pk))
        created = next(r for r in rows if r["kind"] == "opportunity.created")
        assert created["details"]["via_conversion"] is True
        assert "lead.status_changed" in [r["kind"] for r in rows]

    @pytest.mark.parametrize(
        ("target", "kind"),
        [
            ("won", "opportunity.won"),
            ("lost", "opportunity.lost"),
            ("qualified", "opportunity.stage_changed"),
        ],
    )
    def test_outcomes_have_their_own_kinds(self, user_a, target, kind):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        pipeline_services.move_opportunity(
            actor=user_a,
            scope=OWN(user_a.pk),
            opportunity_id=opportunity.pk,
            version=1,
            stage_id=default_stage(target).pk,
        )
        assert TimelineEntry.objects.get(opportunity=opportunity).kind == kind

    def test_reopening_an_opportunity(self, user_a):
        won = OpportunityFactory(lead=LeadFactory(owner=user_a), stage_key="won")
        pipeline_services.move_opportunity(
            actor=user_a,
            scope=OWN(user_a.pk),
            opportunity_id=won.pk,
            version=1,
            stage_id=default_stage("negotiation").pk,
            negotiated_price=Decimal("1000"),
        )
        assert TimelineEntry.objects.get(opportunity=won).kind == "opportunity.reopened"

    def test_archive_and_restore_of_the_lead(self, user_a):
        lead = LeadFactory(owner=user_a)
        lead_services.archive_lead(actor=user_a, scope=OWN(user_a.pk), lead_id=lead.pk, version=1)
        lead_services.restore_lead(actor=user_a, scope=OWN(user_a.pk), lead_id=lead.pk, version=2)
        assert list(
            TimelineEntry.objects.filter(lead=lead).order_by("id").values_list("kind", flat=True)
        ) == ["lead.archived", "lead.restored"]

    def test_only_allowlisted_safe_values_are_ever_recorded(self, user_a):
        lead = LeadFactory(owner=user_a)
        with pytest.raises(ValueError, match="not allowed"):
            timeline.record(
                TimelineKind.NOTE_ADDED,
                lead_id=lead.pk,
                actor_id=user_a.pk,
                at=timezone.now(),
                data={"body": "a whole note"},
            )
        with pytest.raises(ValueError, match="not allowed"):
            timeline.record(
                TimelineKind.LEAD_CREATED,
                lead_id=lead.pk,
                actor_id=user_a.pk,
                at=timezone.now(),
                data={"email": "x@example.test"},
            )


class TestHistoricalIntegrity:
    def test_renamed_statuses_and_stages_keep_their_old_names_on_the_timeline(
        self, user_a, user_a_client
    ):
        scope = OWN(user_a.pk)
        lead = new_lead(user_a)
        lead_services.change_status(
            actor=user_a, scope=scope, lead_id=lead.pk, version=1, status="contacted"
        )
        opportunity = OpportunityFactory(lead=lead)
        pipeline_services.move_opportunity(
            actor=user_a,
            scope=scope,
            opportunity_id=opportunity.pk,
            version=1,
            stage_id=default_stage("proposal").pk,
        )
        LeadStatus.objects.filter(key="contacted").update(name="Reached")
        Stage.objects.filter(pk=default_stage("proposal").pk).update(name="Offer sent")
        rows = {r["kind"]: r for r in entries(user_a_client, lead_timeline_url(lead.pk))}
        assert rows["lead.status_changed"]["details"]["to_name"] == "Contacted"
        assert rows["opportunity.stage_changed"]["details"]["to_stage"] == "Proposal"

    def test_people_keep_their_identity_and_show_their_current_name(self, admin, user_a, user_b):
        lead = LeadFactory(owner=user_a)
        lead_services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=user_b.pk
        )
        user_a.first_name = "Rahul K."
        user_a.save()
        row = entries(signed_in(user_b), lead_timeline_url(lead.pk))[0]
        assert row["kind"] == "lead.reassigned"
        assert row["details"]["from_owner"] == {
            "id": str(user_a.pk),
            "full_name": "Rahul K. Sharma",
            "is_active": True,
        }
        assert row["details"]["to_owner"]["id"] == str(user_b.pk)
        assert row["actor"]["id"] == str(admin.pk)

    def test_who_completed_a_task_stays_on_the_timeline_after_the_lead_moves(
        self, admin, user_a, user_b
    ):
        lead = LeadFactory(owner=user_a)
        task = TaskFactory(lead=lead)
        services.complete_activity(
            actor=user_a, scope=OWN(user_a.pk), activity_id=task.pk, version=1
        )
        lead_services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=user_b.pk
        )
        rows = entries(signed_in(admin), lead_timeline_url(lead.pk, "all"))
        completed = next(r for r in rows if r["kind"] == "task.completed")
        assert completed["actor"]["id"] == str(user_a.pk)


class TestVisibility:
    def test_another_users_lead_timeline_is_not_found_like_a_missing_one(self, user_a, user_b):
        theirs = LeadFactory(owner=user_b)
        client = signed_in(user_a)
        real = client.get(lead_timeline_url(theirs.pk))
        missing = client.get(lead_timeline_url(uuid.uuid4()))
        assert real.status_code == missing.status_code == 404
        assert real.json()["error"]["code"] == missing.json()["error"]["code"]

    def test_after_a_reassignment_each_side_sees_only_what_is_theirs(self, admin, user_a, user_b):
        lead = LeadFactory(owner=user_a)
        scope_a = OWN(user_a.pk)
        won = OpportunityFactory(lead=lead)
        pipeline_services.move_opportunity(
            actor=user_a,
            scope=scope_a,
            opportunity_id=won.pk,
            version=1,
            stage_id=default_stage("won").pk,
        )
        meeting = MeetingFactory(lead=lead, starts_at=timezone.now() - timedelta(days=1))
        services.complete_activity(actor=user_a, scope=scope_a, activity_id=meeting.pk, version=1)
        open_task = create(user_a, scope_a, "task", lead=lead, title="Follow up")
        create(user_a, scope_a, "note", lead=lead, description="Prefers mornings")
        lead.refresh_from_db()  # the completed meeting recorded a contact: a new version
        lead_services.reassign_lead(
            actor=admin,
            scope=ORG(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )

        b_rows = entries(signed_in(user_b), lead_timeline_url(lead.pk))
        b_kinds = [r["kind"] for r in b_rows]
        # B sees the lead's own history and the current work that moved to them...
        assert "lead.reassigned" in b_kinds
        assert {"task.created", "note.added"} <= set(b_kinds)
        # ...but not A's completed meeting or A's won opportunity.
        assert "meeting.completed" not in b_kinds
        assert "meeting.scheduled" not in b_kinds
        assert not any(r["kind"].startswith("opportunity.") for r in b_rows)
        assert str(won.pk) not in str(b_rows)
        assert str(meeting.pk) not in str(b_rows)
        # A can no longer open the lead's timeline at all.
        assert signed_in(user_a).get(lead_timeline_url(lead.pk)).status_code == 404
        # A's won opportunity timeline still shows A's own history of it.
        a_opp = entries(signed_in(user_a), opportunity_timeline_url(won.pk))
        assert [r["kind"] for r in a_opp] == ["opportunity.won"]
        # The administrator sees everything organisation-wide.
        all_kinds = {
            r["kind"] for r in entries(signed_in(admin), lead_timeline_url(lead.pk, "all"))
        }
        assert {"meeting.completed", "opportunity.won", "task.created"} <= all_kinds
        assert open_task.pk

    def test_an_open_task_on_a_won_opportunity_shows_the_opportunity_as_restricted(
        self, admin, user_a, user_b
    ):
        lead = LeadFactory(owner=user_a)
        won = OpportunityFactory(lead=lead, stage_key="won")
        create(user_a, OWN(user_a.pk), "task", opportunity=won)
        lead_services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=user_b.pk
        )
        row = next(
            r
            for r in entries(signed_in(user_b), lead_timeline_url(lead.pk))
            if r["kind"] == "task.created"
        )
        assert row["opportunity"] == {"id": None, "restricted": True}

    def test_archived_activities_and_opportunities_leave_the_lead_timeline(
        self, user_a, user_a_client
    ):
        lead = LeadFactory(owner=user_a)
        scope = OWN(user_a.pk)
        note = create(user_a, scope, "note", lead=lead, description="Wrong lead, sorry")
        opportunity = pipeline_services.create_opportunity(
            actor=user_a,
            scope=scope,
            lead_id=lead.pk,
            fields={"value": Decimal("1")},
        ).opportunity
        services.archive_activity(actor=user_a, scope=scope, activity_id=note.pk, version=1)
        pipeline_services.archive_opportunity(
            actor=user_a, scope=scope, opportunity_id=opportunity.pk, version=1
        )
        rows = entries(user_a_client, lead_timeline_url(lead.pk))
        assert rows == []
        assert "Wrong lead" not in str(rows)
        # The archived opportunity's own page still shows its history.
        assert [
            r["kind"] for r in entries(user_a_client, opportunity_timeline_url(opportunity.pk))
        ] == ["opportunity.created"]

    def test_the_opportunity_timeline_holds_no_lead_events(self, user_a, user_a_client):
        lead = new_lead(user_a)
        opportunity = OpportunityFactory(lead=lead)
        create(user_a, OWN(user_a.pk), "note", opportunity=opportunity)
        create(user_a, OWN(user_a.pk), "note", lead=lead)
        rows = entries(user_a_client, opportunity_timeline_url(opportunity.pk))
        assert [r["kind"] for r in rows] == ["note.added"]

    def test_note_previews_are_bounded(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        long = "word " * 1000
        create(user_a, OWN(user_a.pk), "note", lead=lead, description=long)
        row = entries(user_a_client, lead_timeline_url(lead.pk))[0]
        assert len(row["activity"]["preview"]) == 240
        assert row["activity"]["preview_truncated"] is True
        assert "description" not in row["activity"]


class TestPagination:
    def test_pages_cover_every_entry_once_in_order(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        for i in range(7):
            create(user_a, OWN(user_a.pk), "note", lead=lead, description=f"n{i}")
        seen = walk(user_a_client, lead_timeline_url(lead.pk), page_size=3)
        assert [r["activity"]["preview"] for r in seen] == [f"n{i}" for i in reversed(range(7))]
        assert len({r["id"] for r in seen}) == 7

    @pytest.mark.parametrize("cursor", ["garbage", "a" * 2000, "eyJvIjoiLWNyZWF0ZWRfYXQifQ"])
    def test_a_malformed_cursor_is_a_400(self, user_a, user_a_client, cursor):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.get(lead_timeline_url(lead.pk), {"cursor": cursor})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"

    def test_page_size_is_bounded_and_parameters_allowlisted(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        assert user_a_client.get(lead_timeline_url(lead.pk), {"page_size": 51}).status_code == 400
        assert (
            user_a_client.get(lead_timeline_url(lead.pk), {"kind": "note.added"}).status_code == 400
        )

    def test_a_cursor_from_one_lead_does_not_open_another_leads_timeline(self, user_a, user_b):
        mine = LeadFactory(owner=user_a)
        for _ in range(3):
            create(user_a, OWN(user_a.pk), "note", lead=mine)
        page = signed_in(user_a).get(lead_timeline_url(mine.pk), {"page_size": 1}).json()
        cursor = parse_qs(urlparse(page["next"]).query)["cursor"][0]
        theirs = LeadFactory(owner=user_b)
        response = signed_in(user_a).get(lead_timeline_url(theirs.pk), {"cursor": cursor})
        assert response.status_code == 404
