"""The deal's details and custom fields (docs/pipeline.md#opportunities, #custom-fields):
opportunity date vs expected closing date, customer and instrument details, server-side
validation, and per-pipeline custom fields that never change the schema."""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.business_time import business_date
from arkray.pipeline import models as m
from arkray.pipeline.models import CustomField, Opportunity
from tests.factories import LeadFactory, OpportunityFactory
from tests.helpers import key_header, signed_in

from .conftest import opportunities_url, opportunity_url
from .test_configuration import SHORT, created, pipeline_url

pytestmark = pytest.mark.django_db

FULL = {
    "value": "1250000.00",
    "opportunity_date": "2026-10-01",
    "account_name": "City Hospital Labs",
    "customer_name": "Dr. Meera Iyer",
    "contact_phone": "+91 98765 43210",
    "contact_email": "meera@cityhospital.example",
    "address": "12 MG Road\nBengaluru 560001",
    "instrument_name": "Adams 8380 V-lite",
    "work_load": "300 tests/day",
    "expected_cpt": "Rs 42 per test",
    "expected_close_date": "2026-12-15",
}


def create(client, lead, **body):
    return client.post(
        opportunities_url(), {"lead": str(lead.pk), **body}, format="json", headers=key_header()
    )


class TestDealFields:
    def test_every_field_is_stored_and_returned(self, user_a, user_a_client):
        lead = LeadFactory(owner=user_a)
        response = create(user_a_client, lead, **FULL)
        assert response.status_code == 201, response.content
        body = response.json()
        for field, value in FULL.items():
            assert body[field] == value, field
        # Named by the server from the customer and the instrument (naming.py).
        assert body["title"] == "Dr. Meera Iyer — Adams 8380 V-lite"
        card = user_a_client.get(opportunities_url()).json()["results"][0]
        assert card["account_name"] == "City Hospital Labs"
        assert "contact_email" not in card  # cards stay concise

    def test_the_customer_defaults_to_the_lead_and_the_date_to_today(self, user_a, user_a_client):
        person = LeadFactory(owner=user_a, first_name="Asha", last_name="Rao", organization_name="")
        org = LeadFactory(owner=user_a, organization_name="Metro Diagnostics")
        a = create(user_a_client, person, value="1").json()
        b = create(user_a_client, org, value="1").json()
        assert (a["account_name"], a["customer_name"]) == ("Asha Rao", "Asha Rao")
        assert b["account_name"] == "Metro Diagnostics"
        # ...and so does the name derived from them: the lead's name, without an instrument.
        assert (a["title"], b["title"]) == ("Asha Rao", org.display_name)
        assert a["opportunity_date"] == business_date(timezone.now()).isoformat()
        assert a["expected_close_date"] is None  # a different date, never filled in for you

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("contact_email", "not-an-email"),
            ("contact_email", "a?bcc=spy@evil.example"),
            ("contact_phone", "call me"),
            ("opportunity_date", "1999-12-31"),
            ("opportunity_date", "2026-02-30"),
            ("expected_close_date", "2100-01-01"),
            ("account_name", "   "),
            ("customer_name", ""),
            ("account_name", "x" * 201),
            ("instrument_name", "x" * 201),
            ("instrument_name", "HbA1c analyser"),  # not one of the instruments
            ("work_load", "x" * 101),
            ("expected_cpt", "x" * 101),
            ("title", "Typed by the client"),  # the name is derived, never sent
            ("address", "x" * 1001),
            ("account_name", "Lab" + chr(0x202E) + "evil"),
            ("value", 12.5),
        ],
    )
    def test_validated_server_side(self, user_a, user_a_client, field, value):
        lead = LeadFactory(owner=user_a)
        response = create(user_a_client, lead, **{**FULL, field: value})
        assert response.status_code == 400, (field, response.content)
        assert not Opportunity.objects.exists()

    def test_editing_audits_field_names_never_values(self, user_a, user_a_client):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = user_a_client.patch(
            opportunity_url(opp.pk),
            {"version": 1, "customer_name": "Dr. Kumar", "work_load": "500 tests/day"},
            format="json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["title"] == "Dr. Kumar"  # renamed after its new customer
        event = AuditEvent.objects.get(action="opportunity.updated")
        assert event.metadata["fields"] == ["customer_name", "title", "work_load"]
        assert "Kumar" not in json.dumps(event.metadata)

    def test_a_name_cant_be_emptied(self, user_a, user_a_client):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = user_a_client.patch(
            opportunity_url(opp.pk), {"version": 1, "account_name": ""}, format="json"
        )
        assert response.status_code == 400


def field(name, kind, required=False, options=None, field_id=None):
    body = {"name": name, "type": kind, "required": required}
    if options is not None:
        body["options"] = [{"label": o} if isinstance(o, str) else o for o in options]
    if field_id is not None:
        body["id"] = str(field_id)
    return body


ALL_TYPES = [
    field("Tender number", "text", required=True),
    field("Site notes", "long_text"),
    field("Analysers", "number"),
    field("Reagent budget", "currency"),
    field("PO date", "date"),
    field("Existing customer", "boolean"),
    field("Segment", "single_select", options=["Hospital", "Lab", "Clinic"]),
    field("Tests", "multi_select", options=["HbA1c", "Glucose", "Urine"]),
]


def put_fields(client, body, rows, workspace="me"):
    return client.put(
        pipeline_url(body["id"], workspace, "fields"),
        {"version": body["version"], "custom_fields": rows},
        format="json",
    )


@pytest.fixture
def configured(user_a_client):
    body = created(user_a_client, "Tenders", SHORT)
    response = put_fields(user_a_client, body, ALL_TYPES)
    assert response.status_code == 200, response.content
    pipeline = response.json()
    ids = {f["name"]: f["id"] for f in pipeline["custom_fields"]}
    options = {
        f["name"]: {o["label"]: o["id"] for o in f["options"]} for f in pipeline["custom_fields"]
    }
    return pipeline, ids, options


def values(ids, options, **overrides):
    found = {
        ids["Tender number"]: "GEM/2026/B/12345",
        ids["Site notes"]: "Two labs.\nNeeds UPS.",
        ids["Analysers"]: "3",
        ids["Reagent budget"]: "250000.50",
        ids["PO date"]: "2026-11-30",
        ids["Existing customer"]: True,
        ids["Segment"]: options["Segment"]["Hospital"],
        ids["Tests"]: [options["Tests"]["Urine"], options["Tests"]["HbA1c"]],
    }
    found.update({ids[k]: v for k, v in overrides.items()})
    return found


class TestCustomFields:
    def test_definitions_never_change_the_schema(self, configured):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'pipeline_opportunity'"
            )
            (columns,) = cursor.fetchone()
        assert CustomField.objects.count() == len(ALL_TYPES)
        assert columns < 60  # values live in one JSON column

    def test_values_of_every_type_in_canonical_form(self, user_a, user_a_client, configured):
        pipeline, ids, options = configured
        lead = LeadFactory(owner=user_a)
        response = create(
            user_a_client,
            lead,
            value="1",
            pipeline=pipeline["id"],
            custom_fields=values(ids, options),
        )
        assert response.status_code == 201, response.content
        stored = response.json()["custom_fields"]
        assert stored[ids["Reagent budget"]] == "250000.50"
        assert stored[ids["Existing customer"]] is True
        # multi-select ids in the options' order
        assert stored[ids["Tests"]] == [options["Tests"]["HbA1c"], options["Tests"]["Urine"]]

    @pytest.mark.parametrize(
        ("name", "bad"),
        [
            ("Tender number", "<script>alert(1)</script>"),
            ("Tender number", "x" * 501),
            ("Analysers", 2.5),
            ("Analysers", "1e3"),
            ("Analysers", "three"),
            ("Reagent budget", "-1"),
            ("Reagent budget", "1.234"),
            ("PO date", "2026-13-01"),
            ("PO date", "30/11/2026"),
            ("Existing customer", "yes"),
            ("Segment", "nonexistent"),
            ("Tests", ["nonexistent"]),
            ("Tests", "not-a-list"),
        ],
    )
    def test_values_are_validated(self, user_a, user_a_client, configured, name, bad):
        pipeline, ids, options = configured
        response = create(
            user_a_client,
            LeadFactory(owner=user_a),
            value="1",
            pipeline=pipeline["id"],
            custom_fields=values(ids, options, **{name: bad}),
        )
        assert response.status_code == 400, (name, response.content)

    def test_required_at_creation_and_never_cleared(self, user_a, user_a_client, configured):
        pipeline, ids, options = configured
        lead = LeadFactory(owner=user_a)
        missing = values(ids, options)
        del missing[ids["Tender number"]]
        response = create(
            user_a_client,
            lead,
            value="1",
            pipeline=pipeline["id"],
            custom_fields=missing,
        )
        assert response.status_code == 400
        opp = create(
            user_a_client,
            lead,
            value="1",
            pipeline=pipeline["id"],
            custom_fields=values(ids, options),
        ).json()
        cleared = user_a_client.patch(
            opportunity_url(opp["id"]),
            {"version": opp["version"], "custom_fields": {ids["Tender number"]: None}},
            format="json",
        )
        assert cleared.status_code == 400
        # merging: only the given field changes; null clears an optional one
        edited = user_a_client.patch(
            opportunity_url(opp["id"]),
            {"version": opp["version"], "custom_fields": {ids["Site notes"]: None}},
            format="json",
        )
        assert edited.status_code == 200, edited.content
        assert ids["Site notes"] not in edited.json()["custom_fields"]
        assert edited.json()["custom_fields"][ids["Tender number"]] == "GEM/2026/B/12345"
        event = AuditEvent.objects.get(action="opportunity.updated")
        assert event.metadata["custom_fields"] == [ids["Site notes"]]
        assert "Two labs" not in json.dumps(event.metadata)

    def test_unknown_keys_and_another_pipelines_fields_are_refused(
        self, user_a, user_a_client, configured
    ):
        _, ids, _ = configured
        lead = LeadFactory(owner=user_a)
        # a field of the Tenders pipeline on a deal in the default pipeline
        response = create(user_a_client, lead, value="1", custom_fields={ids["Analysers"]: "1"})
        assert response.status_code == 400
        response = create(user_a_client, lead, value="1", custom_fields={"is_admin": True})
        assert response.status_code == 400

    def test_a_removed_field_keeps_its_values_hidden_and_its_type_never_changes(
        self, user_a, user_a_client, configured
    ):
        pipeline, ids, options = configured
        lead = LeadFactory(owner=user_a)
        opp = create(
            user_a_client,
            lead,
            value="1",
            pipeline=pipeline["id"],
            custom_fields=values(ids, options),
        ).json()
        rows = [
            {k: v for k, v in f.items() if k in ("id", "name", "type", "required", "options")}
            for f in pipeline["custom_fields"]
            if f["name"] != "Analysers"
        ]
        response = put_fields(user_a_client, pipeline, rows)
        assert response.status_code == 200, response.content
        assert ids["Analysers"] not in {f["id"] for f in response.json()["custom_fields"]}
        stored = Opportunity.objects.get(pk=opp["id"]).custom_fields
        assert stored[ids["Analysers"]] == "3"
        changed = [dict(r) for r in rows]
        changed[0]["type"] = "number"
        retyped = put_fields(user_a_client, response.json(), changed)
        assert retyped.status_code == 400

    def test_definitions_are_bounded_and_plain_text(self, user_a_client):
        body = created(user_a_client, "Bounded", SHORT)
        too_many = [field(f"F{n}", "text") for n in range(m.MAX_FIELDS_PER_PIPELINE + 1)]
        assert put_fields(user_a_client, body, too_many).status_code == 400
        options = [f"O{n}" for n in range(m.MAX_FIELD_OPTIONS + 1)]
        assert (
            put_fields(user_a_client, body, [field("S", "single_select", options=options)])
        ).status_code == 400
        assert put_fields(user_a_client, body, [field("<img src=x>", "text")]).status_code == 400
        assert (
            put_fields(user_a_client, body, [field("A", "text"), field("a", "date")]).status_code
            == 400
        )
        assert (
            put_fields(user_a_client, body, [field("A", "text", options=["x"])])
        ).status_code == 400
        assert (
            put_fields(user_a_client, body, [{"name": "F", "type": "formula"}]).status_code == 400
        )

    def test_another_user_cant_define_fields_on_my_pipeline(self, user_a_client, user_b):
        body = created(user_a_client, "Mine", SHORT)
        response = put_fields(signed_in(user_b), body, [field("Spy", "text")])
        assert response.status_code == 404

    def test_values_are_bounded_in_total(self, user_a, user_a_client):
        body = created(user_a_client, "Big", SHORT)
        rows = [field(f"Notes {n}", "long_text") for n in range(10)]
        pipeline = put_fields(user_a_client, body, rows).json()
        big = {f["id"]: "x" * 5000 for f in pipeline["custom_fields"]}
        response = create(
            user_a_client,
            LeadFactory(owner=user_a),
            value="1",
            pipeline=pipeline["id"],
            custom_fields=big,
        )
        assert response.status_code == 400


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("-0", "0"), ("000", "0"), ("1.50", "1.5"), ("001", "1"), ("100", "100"), ("-3.250", "-3.25")],
)
def test_numbers_have_one_spelling(typed, stored):
    """Enhancement review P3: "1.50" and "1.5" were stored (and audited) as different."""
    from arkray.pipeline import validation
    from arkray.pipeline.models import CustomField

    field = CustomField(name="Analysers", field_type="number")
    assert validation.clean_custom_value(field, typed) == stored


@pytest.mark.parametrize(("typed", "stored"), [("250000", "250000.00"), ("1.5", "1.50")])
def test_money_is_stored_to_the_paisa(typed, stored):
    from arkray.pipeline import validation
    from arkray.pipeline.models import CustomField

    field = CustomField(name="Reagent budget", field_type="currency")
    assert validation.clean_custom_value(field, typed) == stored
