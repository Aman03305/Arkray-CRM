"""ADR-0028: the New Opportunity workflow. Nobody names an opportunity or picks a lead: the
opportunity is named after its customer and instrument, its instrument is one of a fixed list
(checked here, not only in the browser), it has an Expected CPT, and creating it creates its
lead in the same transaction. That lead is what the dashboard, search and Ask Arkray count
and show, in the creator's workspace only."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import InvalidInputError
from arkray.leads import selectors as lead_selectors
from arkray.leads.models import Lead
from arkray.pipeline import instruments, naming, services
from arkray.pipeline.models import TITLE_MAX_LENGTH, Opportunity, StageHistory
from tests.factories import LeadFactory, OpportunityFactory, default_stage
from tests.helpers import signed_in

from .conftest import opportunities_url, opportunity_url

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
ORG = AccessScope.organization
FOR_USER = AccessScope.for_user
INSTRUMENTS = ["Adams 8380 V-lite", "Adams 8180 V", "Adams 8180 T", "PCBA with Printer"]
DEAL = {
    "account_name": "ABC Diagnostics",
    "customer_name": "ABC Diagnostics Mumbai",
    "contact_phone": "9876543210",
    "address": "Mumbai",
    "instrument_name": "Adams 8380 V-lite",
    "work_load": "300 tests/day",
    "value": "850000",
    "expected_cpt": "Rs 18 per test",
    "opportunity_date": "2026-10-05",
    "expected_close_date": "2026-12-15",
}

# The same deal as the services take it (the API's serializers turn text into these types).
SERVICE_DEAL = {
    **DEAL,
    "value": Decimal("850000"),
    "opportunity_date": date(2026, 10, 5),
    "expected_close_date": date(2026, 12, 15),
}


def post(client, body=None, workspace="me", **headers):
    return client.post(
        opportunities_url(workspace), body or DEAL, format="json", headers=headers or None
    )


def dashboard(client, workspace="me") -> dict[str, Any]:
    response = client.get(f"/api/v1/workspaces/{workspace}/dashboard")
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def counts() -> tuple[int, int, int]:
    return Lead.objects.count(), Opportunity.objects.count(), StageHistory.objects.count()


# --- the opportunity's name ------------------------------------------------------------------
class TestNaming:
    def test_customer_and_instrument(self):
        title = naming.opportunity_title(
            customer_name="ABC Diagnostics", account_name="X", instrument_name="Adams 8380 V-lite"
        )
        assert title == "ABC Diagnostics — Adams 8380 V-lite"

    def test_the_account_when_there_is_no_customer_name(self):
        title = naming.opportunity_title(
            customer_name="", account_name="ABC Diagnostics", instrument_name="Adams 8180 T"
        )
        assert title == "ABC Diagnostics — Adams 8180 T"

    def test_no_instrument_is_the_customer_alone(self):
        assert naming.opportunity_title(
            customer_name="XYZ Laboratory", account_name="XYZ", instrument_name=""
        ) == ("XYZ Laboratory")

    def test_a_long_name_still_fits_and_keeps_the_instrument(self):
        title = naming.opportunity_title(
            customer_name="Lab " * 60, account_name="", instrument_name="PCBA with Printer"
        )
        assert len(title) <= TITLE_MAX_LENGTH
        assert title.endswith(f"{naming.ELLIPSIS}{naming.SEPARATOR}PCBA with Printer")

    def test_a_long_legacy_instrument_text_fits_too(self):
        title = naming.opportunity_title(
            customer_name="C" * 200, account_name="", instrument_name="I" * 200
        )
        assert len(title) <= TITLE_MAX_LENGTH
        assert naming.SEPARATOR in title

    def test_never_empty(self):
        with pytest.raises(ValueError, match="customer or account name"):
            naming.opportunity_title(customer_name=" ", account_name="", instrument_name="X")


# --- the instruments ------------------------------------------------------------------------
class TestInstruments:
    def test_exactly_the_four_in_order(self):
        assert list(instruments.INSTRUMENTS) == INSTRUMENTS

    def test_case_and_spacing_find_the_listed_spelling(self):
        assert instruments.canonical("  adams   8380 v-LITE ") == "Adams 8380 V-lite"
        assert instruments.canonical("Adams 9999") is None

    def test_the_api_serves_the_list(self, user_a_client):
        response = user_a_client.get("/api/v1/config/opportunity-options")
        assert response.status_code == 200
        assert response.json() == {"instruments": [{"name": name} for name in INSTRUMENTS]}

    def test_the_list_needs_a_signed_in_user(self, api_client):
        assert api_client.get("/api/v1/config/opportunity-options").status_code in (401, 403)

    @pytest.mark.parametrize("instrument", INSTRUMENTS)
    def test_each_can_be_chosen(self, user_a_client, stages, instrument):
        response = post(user_a_client, {**DEAL, "instrument_name": instrument})
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["instrument_name"] == instrument
        assert body["title"] == f"ABC Diagnostics Mumbai — {instrument}"

    def test_the_listed_spelling_is_stored(self, user_a_client, stages):
        response = post(user_a_client, {**DEAL, "instrument_name": "pcba WITH printer"})
        assert response.json()["instrument_name"] == "PCBA with Printer"

    def test_any_other_instrument_is_refused_and_nothing_is_made(self, user_a_client, stages):
        before = counts()
        response = post(user_a_client, {**DEAL, "instrument_name": "Adams 9999 Turbo"})
        assert response.status_code == 400
        details = response.json()["error"]["details"]
        assert details["instrument_name"] == [instruments.UNKNOWN_INSTRUMENT]
        assert counts() == before

    def test_an_edit_to_another_instrument_is_refused(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = user_a_client.patch(
            opportunity_url(opportunity.pk),
            {"version": 1, "instrument_name": "<script>"},
            format="json",
        )
        assert response.status_code == 400
        assert "instrument_name" in response.json()["error"]["details"]

    def test_an_older_opportunitys_own_text_stays_until_changed(self, user_a, stages):
        opportunity = OpportunityFactory(
            lead=LeadFactory(owner=user_a), instrument_name="HbA1c analyser"
        )
        scope = OWN(user_a.pk)
        same = services.update_opportunity(
            actor=user_a,
            scope=scope,
            opportunity_id=opportunity.pk,
            version=1,
            changes={"instrument_name": "HbA1c analyser", "work_load": "50/day"},
        )
        assert (same.instrument_name, same.work_load) == ("HbA1c analyser", "50/day")
        changed = services.update_opportunity(
            actor=user_a,
            scope=scope,
            opportunity_id=opportunity.pk,
            version=same.version,
            changes={"instrument_name": "Adams 8180 V"},
        )
        assert changed.instrument_name == "Adams 8180 V"

    def test_clearing_is_allowed(self, user_a, stages):
        opportunity = OpportunityFactory(
            lead=LeadFactory(owner=user_a), instrument_name="Adams 8180 V"
        )
        cleared = services.update_opportunity(
            actor=user_a,
            scope=OWN(user_a.pk),
            opportunity_id=opportunity.pk,
            version=1,
            changes={"instrument_name": ""},
        )
        assert cleared.instrument_name == ""


# --- creating: no name, no lead -------------------------------------------------------------
class TestCreate:
    def test_no_lead_and_no_name_are_needed(self, user_a_client, user_a, stages):
        before = Lead.objects.count()
        response = post(user_a_client)
        assert response.status_code == 201, response.content
        body = response.json()
        opportunity = Opportunity.objects.get(pk=body["id"])
        lead = Lead.objects.get(pk=opportunity.lead_id)
        assert Lead.objects.count() == before + 1
        assert body["title"] == "ABC Diagnostics Mumbai — Adams 8380 V-lite"
        assert body["lead"]["id"] == str(lead.pk)
        assert body["expected_cpt"] == "Rs 18 per test"
        assert opportunity.stage_id == default_stage("new").pk  # the first open stage
        assert opportunity.owner_id == lead.owner_id == lead.created_by_id == user_a.pk

    def test_the_lead_is_made_from_the_customer_details(self, user_a_client, stages):
        body = post(
            user_a_client, {**DEAL, "contact_email": "lab@abc.example", "address": "Plot 4\nMIDC"}
        ).json()
        lead = Lead.objects.get(pk=body["lead"]["id"])
        assert (lead.display_name, lead.organization_name) == (
            "ABC Diagnostics Mumbai",
            "ABC Diagnostics",
        )
        assert (lead.phone, lead.email) == ("9876543210", "lab@abc.example")
        assert (lead.address_line_1, lead.address_line_2) == ("Plot 4", "MIDC")
        # The deal's own details stay on the deal.
        assert lead.description == ""
        assert lead.created_at is not None

    def test_an_address_that_doesnt_fit_the_leads_lines_stays_on_the_deal(
        self, user_a_client, stages
    ):
        address = "Plot 4\nMIDC\nAndheri East, Mumbai 400093"
        body = post(user_a_client, {**DEAL, "address": address}).json()
        lead = Lead.objects.get(pk=body["lead"]["id"])
        assert (lead.address_line_1, lead.address_line_2) == ("", "")
        assert body["address"] == address

    def test_the_lead_is_converted_as_it_has_its_opportunity(self, user_a_client, stages):
        """Backend review: these leads stayed "New", so "how many leads converted?" said none.
        Converted means "has an opportunity" (ADR-0019), as conversion leaves a lead."""
        body = post(user_a_client).json()
        lead = Lead.objects.select_related("status").get(pk=body["lead"]["id"])
        assert lead.status.category == "converted"
        change = AuditEvent.objects.get(action="lead.status_changed")
        assert (change.target_id, change.metadata["to"]) == (str(lead.pk), lead.status_id)
        # What Ask Arkray's get_lead_summary reports per status (the same selector).
        breakdown = lead_selectors.status_breakdown(OWN(lead.owner_id))
        assert [(row.category, row.count) for row in breakdown] == [("converted", 1)]

    @pytest.mark.parametrize(
        "name",
        [
            " ".join(["Diagnosticsxx"] * 14)[:200].strip(),  # 13-letter words, 200 characters
            "A" * 99 + " " + "B" * 100,  # the only space that fits both halves
            "Lab " * 40,
        ],
    )
    def test_a_long_customer_name_reads_the_same_on_the_lead(self, user_a_client, stages, name):
        """Backend review: the cut fell before the last 100 characters and the tail was lost."""
        body = post(user_a_client, {**DEAL, "customer_name": name}).json()
        lead = Lead.objects.get(pk=body["lead"]["id"])
        assert lead.display_name == body["customer_name"] == " ".join(name.split())
        assert len(lead.first_name) <= 100
        assert len(lead.last_name) <= 100

    def test_a_long_name_without_a_usable_space_loses_nothing(self, user_a_client, stages):
        name = "न" * 150  # 150 characters, no space
        body = post(user_a_client, {**DEAL, "customer_name": name}).json()
        lead = Lead.objects.get(pk=body["lead"]["id"])
        assert lead.first_name + lead.last_name == name

    def test_the_previous_release_can_still_insert(self, user_a, stages):
        """Backend review (P2): 0008 must keep the column's DEFAULT. The previous release's
        INSERT never names expected_cpt; during a rolling deploy or after a rollback it must
        still work."""
        from django.db import connection

        source = OpportunityFactory(lead=LeadFactory(owner=user_a))
        columns = [
            f.column
            for f in Opportunity._meta.concrete_fields
            if not f.generated and f.column not in ("id", "expected_cpt")
        ]
        names = ", ".join(columns)
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO pipeline_opportunity (id, {names}) "  # noqa: S608 — model columns
                f"SELECT gen_random_uuid(), {names} FROM pipeline_opportunity WHERE id = %s "
                "RETURNING expected_cpt",
                [source.pk],
            )
            assert cursor.fetchone() == ("",)

    def test_a_typed_name_is_refused(self, user_a_client, stages):
        before = counts()
        response = post(user_a_client, {**DEAL, "title": "My name"})
        assert response.status_code == 400
        assert counts() == before

    def test_into_the_chosen_pipeline_and_stage(self, user_a_client, pipeline, stages):
        body = post(
            user_a_client,
            {**DEAL, "pipeline": str(pipeline.pk), "stage": str(stages["qualified"].pk)},
        ).json()
        assert (body["pipeline"]["id"], body["stage"]["id"]) == (
            str(pipeline.pk),
            str(stages["qualified"].pk),
        )
        board = user_a_client.get("/api/v1/workspaces/me/pipeline-board").json()
        qualified = str(stages["qualified"].pk)
        column = next(c for c in board["columns"] if c["stage"]["id"] == qualified)
        assert [card["id"] for card in column["cards"]] == [body["id"]]

    def test_a_refused_opportunity_leaves_no_lead(self, user_a_client, stages):
        before = counts()
        # A negotiation stage needs the negotiated price: refused after the lead was made.
        response = post(user_a_client, {**DEAL, "stage": str(stages["negotiation"].pk)})
        assert response.status_code == 400
        assert counts() == before

    def test_a_failure_after_the_lead_rolls_both_back(self, user_a, stages):
        before = counts()
        with (
            mock.patch.object(services, "_history", side_effect=RuntimeError("disk full")),
            pytest.raises(RuntimeError),
        ):
            services.create_opportunity(
                actor=user_a,
                scope=OWN(user_a.pk),
                lead_id=None,
                fields=SERVICE_DEAL,
            )
        assert counts() == before
        assert not AuditEvent.objects.filter(action__in=["lead.created", "opportunity.created"])

    def test_a_failed_lead_makes_no_opportunity(self, user_a, stages):
        before = counts()
        with pytest.raises(InvalidInputError):
            services.create_opportunity(
                actor=user_a,
                scope=OWN(user_a.pk),
                lead_id=None,
                fields={"value": Decimal("1"), "contact_phone": "12"},
            )
        assert counts() == before

    def test_a_retry_with_the_same_key_is_one_lead_and_one_opportunity(self, user_a_client, stages):
        key = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
        first = post(user_a_client, **{"Idempotency-Key": key})
        again = post(user_a_client, **{"Idempotency-Key": key})
        assert (first.status_code, again.status_code) == (201, 201)
        assert again.headers["Idempotent-Replayed"] == "true"
        assert first.json()["id"] == again.json()["id"]
        assert Lead.objects.count() == Opportunity.objects.count() == 1

    def test_audited_as_one_creation_without_customer_details(self, admin, user_a, stages):
        services.create_opportunity(
            actor=admin,
            scope=FOR_USER(admin.pk, user_a.pk),
            lead_id=None,
            fields=SERVICE_DEAL,
        )
        lead_event = AuditEvent.objects.get(action="lead.created")
        created = AuditEvent.objects.get(action="opportunity.created")
        opportunity = Opportunity.objects.get()
        for event in (lead_event, created):
            assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)
        assert lead_event.target_id == str(opportunity.lead_id)
        assert created.target_id == str(opportunity.pk)
        assert created.metadata["lead_id"] == str(opportunity.lead_id)
        assert created.metadata["lead_created"] is True
        assert (created.metadata["pipeline"], created.metadata["stage"]) == ("sales", "new")
        text = json.dumps([lead_event.metadata, created.metadata])
        for value in ("ABC Diagnostics", "9876543210", "Mumbai", "850000", "Rs 18"):
            assert value not in text


# --- afterwards: the same lead, never another ---------------------------------------------
class TestAfterwards:
    @pytest.fixture
    def created(self, user_a_client, stages):
        body = post(user_a_client).json()
        return Opportunity.objects.get(pk=body["id"])

    def test_editing_renames_after_the_customer_and_keeps_the_lead(self, user_a_client, created):
        response = user_a_client.patch(
            opportunity_url(created.pk),
            {"version": 1, "customer_name": "ABC Labs Pune", "instrument_name": "Adams 8180 T"},
            format="json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["title"] == "ABC Labs Pune — Adams 8180 T"
        assert response.json()["lead"]["id"] == str(created.lead_id)
        assert Lead.objects.count() == 1
        event = AuditEvent.objects.filter(action="opportunity.updated").get()
        assert event.metadata["fields"] == ["customer_name", "instrument_name", "title"]

    def test_other_edits_keep_the_name(self, user_a_client, created):
        response = user_a_client.patch(
            opportunity_url(created.pk),
            {"version": 1, "address": "Pune", "expected_cpt": "Rs 20 per test"},
            format="json",
        )
        assert response.json()["title"] == created.title
        assert response.json()["expected_cpt"] == "Rs 20 per test"
        event = AuditEvent.objects.filter(action="opportunity.updated").get()
        assert event.metadata["fields"] == ["address", "expected_cpt"]
        assert "Rs 20" not in json.dumps(event.metadata)

    def test_an_older_typed_name_is_kept_until_customer_or_instrument_changes(self, user_a, stages):
        older = OpportunityFactory(lead=LeadFactory(owner=user_a), title="Annual reagent deal")
        scope = OWN(user_a.pk)
        kept = services.update_opportunity(
            actor=user_a,
            scope=scope,
            opportunity_id=older.pk,
            version=1,
            changes={"value": Decimal("5")},
        )
        assert kept.title == "Annual reagent deal"
        renamed = services.update_opportunity(
            actor=user_a,
            scope=scope,
            opportunity_id=older.pk,
            version=kept.version,
            changes={"instrument_name": "Adams 8180 V"},
        )
        assert renamed.title == f"{older.customer_name} — Adams 8180 V"

    def test_a_typed_name_is_refused_on_edit(self, user_a_client, created):
        response = user_a_client.patch(
            opportunity_url(created.pk), {"version": 1, "title": "Renamed"}, format="json"
        )
        assert response.status_code == 400

    def test_no_other_lead_is_made_by_moves_prices_notes_or_custom_values(
        self, user_a_client, user_a, created, stages
    ):
        before = Lead.objects.count()
        moved = user_a_client.post(
            opportunity_url(created.pk, action="move"),
            {"stage": str(stages["negotiation"].pk), "version": 1, "negotiated_price": "800000"},
            format="json",
        )
        assert moved.status_code == 200, moved.content
        priced = user_a_client.post(
            opportunity_url(created.pk, action="negotiated-prices"),
            {"price": "790000", "version": moved.json()["version"]},
            format="json",
        )
        assert priced.status_code == 200, priced.content
        noted = user_a_client.post(
            "/api/v1/workspaces/me/activities",
            {"type": "note", "opportunity": str(created.pk), "description": "Demo went well."},
            format="json",
        )
        assert noted.status_code == 201, noted.content
        edited = user_a_client.patch(
            opportunity_url(created.pk),
            {"version": priced.json()["version"], "custom_fields": {}},
            format="json",
        )
        assert edited.status_code == 200, edited.content
        assert Lead.objects.count() == before
        assert Opportunity.objects.get(pk=created.pk).lead_id == created.lead_id


# --- the dashboard -----------------------------------------------------------------------------
class TestDashboard:
    def test_one_opportunity_is_one_more_lead_and_one_more_new_lead_today(
        self, user_a_client, user_a, stages
    ):
        LeadFactory.create_batch(3, owner=user_a)
        before = dashboard(user_a_client)["leads"]
        post(user_a_client)
        after = dashboard(user_a_client)["leads"]
        assert after == {"total": before["total"] + 1, "new_today": before["new_today"] + 1}

    def test_the_new_lead_is_listed_with_its_opportunity(self, user_a_client, stages):
        body = post(user_a_client).json()
        rows = dashboard(user_a_client)["new_leads"]
        assert rows[0]["id"] == body["lead"]["id"]
        assert rows[0]["display_name"] == "ABC Diagnostics Mumbai"
        assert rows[0]["opportunity"] == {
            "id": body["id"],
            "title": body["title"],
            "instrument_name": "Adams 8380 V-lite",
        }
        assert "phone" not in rows[0]
        assert "email" not in rows[0]

    def test_a_lead_whose_only_opportunity_is_archived_lists_none(self, user_a_client, stages):
        body = post(user_a_client).json()
        archived = user_a_client.post(
            opportunity_url(body["id"], action="archive"), {"version": 1}, format="json"
        )
        assert archived.status_code == 200
        rows = dashboard(user_a_client)["new_leads"]
        # The lead is still a lead (archiving a deal never archives or deletes its lead).
        assert rows[0]["id"] == body["lead"]["id"]
        assert rows[0]["opportunity"] is None

    def test_only_in_the_creators_workspace_and_the_organisations(
        self, user_a_client, user_a, user_b, admin_client, stages
    ):
        b_client = signed_in(user_b)
        b_before = dashboard(b_client)["leads"]
        org_before = dashboard(admin_client, "all")["leads"]
        body = post(user_a_client).json()
        assert dashboard(b_client)["leads"] == b_before
        assert [r["id"] for r in dashboard(b_client)["new_leads"]] == []
        org = dashboard(admin_client, "all")
        assert org["leads"]["new_today"] == org_before["new_today"] + 1
        assert org["new_leads"][0]["opportunity"]["id"] == body["id"]
        selected = dashboard(admin_client, str(user_a.pk))
        assert selected["new_leads"][0]["id"] == body["lead"]["id"]


# --- who may see the lead --------------------------------------------------------------------
class TestAccess:
    @pytest.fixture
    def made(self, user_a_client, stages):
        return post(user_a_client).json()

    def test_the_creator_opens_the_lead_and_its_opportunity(self, user_a_client, made):
        lead_id = made["lead"]["id"]
        assert user_a_client.get(f"/api/v1/workspaces/me/leads/{lead_id}").status_code == 200
        listed = user_a_client.get(f"/api/v1/workspaces/me/opportunities?lead={lead_id}").json()
        assert [row["id"] for row in listed["results"]] == [made["id"]]

    def test_another_user_finds_nothing(self, user_a, user_b, made):
        client = signed_in(user_b)
        lead_id = made["lead"]["id"]
        for url in (
            f"/api/v1/workspaces/me/leads/{lead_id}",
            f"/api/v1/workspaces/me/opportunities/{made['id']}",
            f"/api/v1/workspaces/{user_a.pk}/leads/{lead_id}",
            f"/api/v1/workspaces/{user_a.pk}/opportunities/{made['id']}",
            f"/api/v1/workspaces/all/leads/{lead_id}",
        ):
            assert client.get(url).status_code == 404, url
        listed = client.get(f"/api/v1/workspaces/me/opportunities?lead={lead_id}").json()
        assert listed["results"] == []
        assert client.get("/api/v1/workspaces/me/search?q=ABC").json()["leads"]["results"] == []

    def test_an_administrator_sees_it_organisation_wide_and_in_the_users_workspace(
        self, admin_client, user_a, made
    ):
        lead_id = made["lead"]["id"]
        for workspace in ("all", str(user_a.pk)):
            response = admin_client.get(f"/api/v1/workspaces/{workspace}/leads/{lead_id}")
            assert response.status_code == 200, workspace

    def test_a_support_session_creates_in_the_users_name_recording_the_administrator(
        self, admin, admin_client, user_a, stages
    ):
        started = admin_client.post(
            "/api/v1/admin/support-sessions",
            {"user": str(user_a.pk), "reason": "Customer asked for help with a deal"},
            format="json",
        )
        assert started.status_code == 201, started.content
        response = post(admin_client, workspace=str(user_a.pk))
        assert response.status_code == 201, response.content
        lead = Lead.objects.get(pk=response.json()["lead"]["id"])
        assert (lead.owner_id, lead.created_by_id) == (user_a.pk, admin.pk)
        for action in ("lead.created", "opportunity.created"):
            event = AuditEvent.objects.get(action=action)
            assert (event.actor_id, event.subject_user_id, str(event.support_session_id)) == (
                admin.pk,
                user_a.pk,
                started.json()["id"],
            )

    def test_a_sales_user_cant_create_in_someone_elses_workspace(self, user_b, user_a, stages):
        before = counts()
        response = post(signed_in(user_b), workspace=str(user_a.pk))
        assert response.status_code == 404
        assert counts() == before


# --- search and Ask Arkray (its router and tools: arkray/ai/tests/test_opportunity_leads.py) --
class TestSearchAndAsk:
    def test_search_finds_the_lead_and_the_opportunity_each_once(self, user_a_client, stages):
        made = post(user_a_client).json()
        found = user_a_client.get("/api/v1/workspaces/me/search?q=ABC Diagnostics").json()
        assert [r["id"] for r in found["leads"]["results"]] == [made["lead"]["id"]]
        assert [r["id"] for r in found["opportunities"]["results"]] == [made["id"]]

    @pytest.mark.usefixtures("ai_on")
    def test_ask_counts_new_leads_as_the_dashboard_does(self, user_a_client, stages):
        post(user_a_client)
        post(user_a_client, {**DEAL, "instrument_name": "PCBA with Printer"})
        figure = dashboard(user_a_client)["leads"]["new_today"]
        answer = user_a_client.post(
            "/api/v1/workspaces/me/ask",
            {"question": "How many new leads were created today?"},
            format="json",
        ).json()["answer"]
        assert answer["provenance"]["mode"] == "router"
        facts = {fact["label"]: fact["value"] for fact in answer["facts"]}
        assert facts["New leads today"] == str(figure) == "2"  # two deals: two leads, not four
        text = " ".join(part.get("text", "") for b in answer["blocks"] for part in b["parts"])
        assert "You have 2 new leads today." in text

    @pytest.mark.usefixtures("ai_on")
    def test_ask_lists_an_instruments_opportunities(self, user_a_client, stages):
        made = post(user_a_client).json()
        answer = user_a_client.post(
            "/api/v1/workspaces/me/ask",
            {"question": "Show opportunities for Adams 8380 V-lite"},
            format="json",
        ).json()["answer"]
        assert answer["provenance"]["mode"] == "router"
        assert [s["id"] for s in answer["sources"]] == [made["id"]]
