"""The pipeline HTTP API: shapes, strict input, filters, sorts, pagination, bounds."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from arkray.pipeline.models import Opportunity, Stage
from tests.factories import LeadFactory, OpportunityFactory, UserFactory
from tests.helpers import key_header

from .conftest import board_url, opportunities_url, opportunity_url, summary_url

pytestmark = pytest.mark.django_db


def create_body(lead, **extra):
    return {"lead": str(lead.pk), "value": "1200000", **extra}


class TestConfig:
    def test_pipelines_with_ordered_stages(self, user_a_client, stages):
        body = user_a_client.get("/api/v1/config/pipelines").json()
        (pipeline,) = body["results"]
        assert (pipeline["key"], pipeline["name"], pipeline["is_default"]) == (
            "sales",
            "Sales Pipeline",
            True,
        )
        assert [(s["key"], s["probability"], s["category"]) for s in pipeline["stages"]] == [
            ("new", "10.00", "open"),
            ("qualified", "25.00", "open"),
            ("proposal", "50.00", "open"),
            ("negotiation", "75.00", "open"),
            ("won", "100.00", "won"),
            ("lost", "0.00", "lost"),
        ]

    def test_retired_stages_are_listed_but_flagged(self, user_a_client, stages):
        Stage.objects.filter(pk=stages["qualified"].pk).update(is_active=False)
        body = user_a_client.get("/api/v1/config/pipelines").json()
        flags = {s["key"]: s["is_active"] for s in body["results"][0]["stages"]}
        assert flags["qualified"] is False


class TestCreateAndDetail:
    def test_create(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.post(
            opportunities_url(),
            create_body(
                lead,
                stage=str(stages["proposal"].pk),
                probability="62.5",
                expected_close_date="2026-12-31",
                description="Two analysers",
            ),
            format="json",
            headers=key_header(),
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert response["Location"] == f"/api/v1/workspaces/me/opportunities/{body['id']}"
        assert body["value"] == "1200000.00"
        assert (body["probability"], body["probability_overridden"]) == ("62.50", True)
        assert body["weighted_value"] == "750000.00"
        assert body["expected_close_date"] == "2026-12-31"
        assert body["stage"]["key"] == "proposal"
        assert body["pipeline"]["key"] == "sales"
        assert body["status"] == "open"
        assert body["owner"]["id"] == str(user_a.pk)
        assert body["lead"]["id"] == str(lead.pk)
        assert body["title"] == lead.display_name  # derived: the lead's name, no instrument
        assert body["version"] == 1
        detail = user_a_client.get(opportunity_url(body["id"])).json()
        assert detail == body

    def test_the_title_is_derived_never_sent(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        refused = user_a_client.post(
            opportunities_url(),
            create_body(lead, title="Hospital Analyzer Project"),
            format="json",
            headers=key_header(),
        )
        assert refused.status_code == 400
        assert refused.json()["error"]["details"] == {
            "non_field_errors": ["Unknown field(s): title."]
        }
        assert not Opportunity.objects.exists()
        created = user_a_client.post(
            opportunities_url(),
            create_body(lead, account_name="City Hospital", instrument_name="PCBA with Printer"),
            format="json",
            headers=key_header(),
        )
        assert created.status_code == 201, created.content
        # The customer name defaults to the lead's, which names it before the account.
        assert created.json()["title"] == f"{lead.display_name} — PCBA with Printer"

    def test_integer_amounts_are_accepted(self, user_a_client, user_a, stages):
        response = user_a_client.post(
            opportunities_url(),
            create_body(LeadFactory(owner=user_a), value=1200000),
            format="json",
            headers=key_header(),
        )
        assert (response.status_code, response.json()["value"]) == (201, "1200000.00")

    @pytest.mark.parametrize(
        "value",
        [
            1200000.5,  # a JSON number with a fraction: a binary float
            1e6,
            "1e6",
            "1.2E+6",
            "-1",
            "+100",
            " 100",
            "1,00,000",
            "100.001",
            "NaN",
            "Infinity",
            "0x10",
            "".join(chr(0x0661 + i) for i in range(3)),  # Arabic-Indic digits: Decimal() reads them
            "".join(chr(0xFF11 + i) for i in range(3)),  # full-width digits
            "1000000000000",  # 13 whole digits
            True,
            None,
            [],
            {},
        ],
        ids=lambda v: repr(v)[:20],
    )
    def test_amounts_are_only_plain_decimal_strings(self, user_a_client, user_a, stages, value):
        response = user_a_client.post(
            opportunities_url(),
            create_body(LeadFactory(owner=user_a), value=value),
            format="json",
            headers=key_header(),
        )
        assert response.status_code == 400, response.content
        assert "value" in response.json()["error"]["details"]
        assert not Opportunity.objects.exists()

    def test_nan_as_a_json_literal_is_malformed(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        body = f'{{"lead": "{lead.pk}", "value": NaN}}'
        response = user_a_client.post(
            opportunities_url(), body, content_type="application/json", headers=key_header()
        )
        assert response.status_code == 400
        assert not Opportunity.objects.exists()

    @pytest.mark.parametrize("probability", ["100.01", "101", "-1", 50.5, "1e1", "50.555"])
    def test_probability_is_a_percentage(self, user_a_client, user_a, stages, probability):
        response = user_a_client.post(
            opportunities_url(),
            create_body(LeadFactory(owner=user_a), probability=probability),
            format="json",
            headers=key_header(),
        )
        assert response.status_code == 400
        assert "probability" in response.json()["error"]["details"]

    @pytest.mark.parametrize(
        "date_value",
        ["2026-02-30", "30/09/2026", "2026-09-30T00:00:00Z", "1999-12-31", "2100-01-01"],
    )
    def test_expected_close_is_a_plain_business_date(
        self, user_a_client, user_a, stages, date_value
    ):
        response = user_a_client.post(
            opportunities_url(),
            create_body(LeadFactory(owner=user_a), expected_close_date=date_value),
            format="json",
            headers=key_header(),
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "extra",
        [
            {"owner": "00000000-0000-4000-8000-000000000000"},
            {"status": "won"},
            {"closed_at": "2026-01-01T00:00:00Z"},
            {"created_by": "00000000-0000-4000-8000-000000000000"},
            {"version": 5},
            {"probability_overridden": True},
            {"weighted_value": "1"},
            {"archived_at": None},
            {"category": "won"},
        ],
    )
    def test_system_fields_are_refused(self, user_a_client, user_a, stages, extra):
        response = user_a_client.post(
            opportunities_url(),
            create_body(LeadFactory(owner=user_a), **extra),
            format="json",
            headers=key_header(),
        )
        assert response.status_code == 400
        assert not Opportunity.objects.exists()

    def test_unknown_lead_is_a_404(self, user_a_client, stages):
        response = user_a_client.post(
            opportunities_url(),
            {"lead": "5a1e4d2c-0000-4000-8000-00000000abcd", "value": "1"},
            format="json",
            headers=key_header(),
        )
        assert response.status_code == 404

    def test_idempotent_replay(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        headers = {"Idempotency-Key": "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"}
        first = user_a_client.post(
            opportunities_url(), create_body(lead), format="json", headers=headers
        )
        again = user_a_client.post(
            opportunities_url(), create_body(lead), format="json", headers=headers
        )
        assert (first.status_code, again.status_code) == (201, 201)
        assert again["Idempotent-Replayed"] == "true"
        assert first.json()["id"] == again.json()["id"]
        reused = user_a_client.post(
            opportunities_url(), create_body(lead, value="1"), format="json", headers=headers
        )
        assert (reused.status_code, reused.json()["error"]["code"]) == (
            422,
            "idempotency_key_reused",
        )
        bad = user_a_client.post(
            opportunities_url(), create_body(lead), format="json", headers={"Idempotency-Key": "1"}
        )
        assert bad.status_code == 400


class TestEditMoveArchiveHistory:
    def test_patch(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = user_a_client.patch(
            opportunity_url(opportunity.pk),
            {"version": 1, "value": "2000000.10", "probability": "70", "expected_close_date": None},
            format="json",
        )
        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["value"], body["probability"], body["weighted_value"], body["version"]) == (
            "2000000.10",
            "70.00",
            "1400000.07",
            2,
        )
        reset = user_a_client.patch(
            opportunity_url(opportunity.pk), {"version": 2, "probability": None}, format="json"
        )
        assert (reset.json()["probability"], reset.json()["probability_overridden"]) == (
            "50.00",
            False,
        )

    def test_patch_needs_the_version(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        assert (
            user_a_client.patch(
                opportunity_url(opportunity.pk), {"description": "x"}, format="json"
            ).status_code
            == 400
        )
        stale = user_a_client.patch(
            opportunity_url(opportunity.pk), {"version": 9, "description": "x"}, format="json"
        )
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "conflict")

    @pytest.mark.parametrize(
        "field",
        ["stage", "status", "owner", "lead", "pipeline", "closed_at", "archived_at", "title"],
    )
    def test_patch_cant_touch_state(self, user_a_client, user_a, stages, field):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = user_a_client.patch(
            opportunity_url(opportunity.pk),
            {"version": 1, field: str(stages["won"].pk)},
            format="json",
        )
        assert response.status_code == 400
        opportunity.refresh_from_db()
        assert (opportunity.status, opportunity.version) == ("open", 1)

    def test_move_and_history(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = user_a_client.post(
            opportunity_url(opportunity.pk, action="move"),
            {"stage": str(stages["lost"].pk), "version": 1, "lost_reason": "Budget cut"},
            format="json",
        )
        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["status"], body["probability"], body["lost_reason"]) == (
            "lost",
            "0.00",
            "Budget cut",
        )
        assert body["closed_at"] is not None
        closed_twice = user_a_client.post(
            opportunity_url(opportunity.pk, action="move"),
            {"stage": str(stages["won"].pk), "version": 2},
            format="json",
        )
        assert (closed_twice.status_code, closed_twice.json()["error"]["code"]) == (
            422,
            "business_rule_violation",
        )
        history = user_a_client.get(opportunity_url(opportunity.pk, action="history")).json()
        assert [(h["from_stage_name"], h["to_stage_name"]) for h in history["results"]] == [
            ("Proposal", "Lost")
        ]
        assert history["results"][0]["actor"]["id"] == str(user_a.pk)
        assert history["results"][0]["lost_reason"] == "Budget cut"

    def test_move_to_an_unknown_stage(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = user_a_client.post(
            opportunity_url(opportunity.pk, action="move"),
            {"stage": "5a1e4d2c-0000-4000-8000-00000000abcd", "version": 1},
            format="json",
        )
        assert response.status_code == 400
        assert "stage" in response.json()["error"]["details"]

    def test_history_is_paginated(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        keys = ["qualified", "proposal", "negotiation", "proposal", "negotiation"]
        for version, key in enumerate(keys, start=1):
            body = {"stage": str(stages[key].pk), "version": version}
            if key == "negotiation":
                body["negotiated_price"] = "1000"
                body["agreed_cpt"] = "Rs 18"
            assert (
                user_a_client.post(
                    opportunity_url(opportunity.pk, action="move"), body, format="json"
                ).status_code
                == 200
            )
        first = user_a_client.get(
            opportunity_url(opportunity.pk, action="history"), {"page_size": 3}
        ).json()
        assert len(first["results"]) == 3
        second = user_a_client.get(first["next"]).json()
        assert len(second["results"]) == 2
        assert [h["to_stage_name"] for h in first["results"] + second["results"]] == [
            "Negotiation",
            "Proposal",
            "Negotiation",
            "Proposal",
            "Qualified",
        ]

    def test_history_has_no_write_methods(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        url = opportunity_url(opportunity.pk, action="history")
        for method in ("post", "patch", "put", "delete"):
            assert getattr(user_a_client, method)(url, {}, format="json").status_code == 405

    def test_there_is_no_delete(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        assert user_a_client.delete(opportunity_url(opportunity.pk)).status_code == 405
        assert Opportunity.objects.filter(pk=opportunity.pk).exists()

    def test_archive_and_restore(self, user_a_client, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        archived = user_a_client.post(
            opportunity_url(opportunity.pk, action="archive"), {"version": 1}, format="json"
        ).json()
        assert archived["archived_at"] is not None
        assert user_a_client.get(opportunities_url()).json()["results"] == []
        listed = user_a_client.get(opportunities_url(), {"archived": "true"}).json()["results"]
        assert [o["id"] for o in listed] == [str(opportunity.pk)]
        assert user_a_client.get(opportunity_url(opportunity.pk)).status_code == 200  # still opens
        restored = user_a_client.post(
            opportunity_url(opportunity.pk, action="restore"), {"version": 2}, format="json"
        ).json()
        assert restored["archived_at"] is None


class TestBoard:
    def test_every_stage_in_order_with_counts_values_and_cards(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        OpportunityFactory(lead=lead, stage=stages["proposal"], value=Decimal("300000"))
        OpportunityFactory(lead=lead, stage=stages["proposal"], value=Decimal("100000.50"))
        OpportunityFactory(lead=lead, stage=stages["won"], value=Decimal("50000"))
        body = user_a_client.get(board_url()).json()
        assert body["pipeline"]["key"] == "sales"
        assert body["currency"] == "INR"
        assert [c["stage"]["key"] for c in body["columns"]] == [
            "new",
            "qualified",
            "proposal",
            "negotiation",
            "won",
            "lost",
        ]
        proposal = body["columns"][2]
        assert (proposal["count"], proposal["total_value"], proposal["weighted_value"]) == (
            2,
            "400000.50",
            "200000.25",
        )
        assert proposal["ordering"] == "expected_close"
        assert body["columns"][4]["ordering"] == "-closed_at"
        card = proposal["cards"][0]
        assert set(card) == {
            "id",
            "title",
            "lead",
            "owner",
            "stage_id",
            "status",
            "value",
            "probability",
            "probability_overridden",
            "weighted_value",
            "expected_close_date",
            "closed_at",
            "archived_at",
            "account_name",
            "negotiated_price",
            "agreed_cpt",
            "version",
            "created_at",
            "updated_at",
            "customer_restricted",
        }
        assert "description" not in card
        assert body["totals"] == {
            "pipeline_value": "400000.50",
            "weighted_pipeline": "200000.25",
            "open_count": 2,
        }

    def test_card_order_is_deterministic(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        today = date(2026, 10, 1)
        OpportunityFactory(lead=lead, title="Later", expected_close_date=today + timedelta(days=9))
        OpportunityFactory(lead=lead, title="Undated")
        OpportunityFactory(lead=lead, title="Soon", expected_close_date=today)
        tie_old = OpportunityFactory(
            lead=lead, title="Tie old", expected_close_date=today + timedelta(days=5)
        )
        OpportunityFactory(
            lead=lead, title="Tie new", expected_close_date=today + timedelta(days=5)
        )
        Opportunity.objects.filter(pk=tie_old.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )
        won_old = OpportunityFactory(lead=lead, stage=stages["won"], title="Won long ago")
        OpportunityFactory(lead=lead, stage=stages["won"], title="Won today")
        Opportunity.objects.filter(pk=won_old.pk).update(
            closed_at=timezone.now() - timedelta(days=30)
        )
        body = user_a_client.get(board_url()).json()
        new = next(c for c in body["columns"] if c["stage"]["key"] == "new")
        assert [c["title"] for c in new["cards"]] == [
            "Soon",
            "Tie old",
            "Tie new",
            "Later",
            "Undated",
        ]
        won = next(c for c in body["columns"] if c["stage"]["key"] == "won")
        assert [c["title"] for c in won["cards"]] == ["Won today", "Won long ago"]

    def test_a_crowded_stage_is_bounded_and_continues_in_the_list(
        self, user_a_client, user_a, stages
    ):
        lead = LeadFactory(owner=user_a)
        Opportunity.objects.bulk_create(
            [
                OpportunityFactory.build(lead=lead, stage=stages["new"], title=f"Deal {i:03}")
                for i in range(45)
            ]
        )
        body = user_a_client.get(board_url(), {"cards_per_stage": 20}).json()
        new = body["columns"][0]
        assert (new["count"], len(new["cards"])) == (45, 20)
        assert new["next"] is not None
        seen = [c["id"] for c in new["cards"]]
        link = new["next"]
        while link:
            page = user_a_client.get(link).json()
            seen += [c["id"] for c in page["results"]]
            link = page["next"]
        assert len(seen) == len(set(seen)) == 45  # no gaps, no repeats
        assert body["columns"][1]["next"] is None

    @pytest.mark.parametrize("cards", ["51", "-1", "x"])
    def test_cards_per_stage_is_bounded(self, user_a_client, stages, cards):
        assert user_a_client.get(board_url(), {"cards_per_stage": cards}).status_code == 400

    def test_summaries_only(self, user_a_client, user_a, stages):
        OpportunityFactory(lead=LeadFactory(owner=user_a))
        body = user_a_client.get(board_url(), {"cards_per_stage": 0}).json()
        assert body["columns"][0]["count"] == 1
        assert body["columns"][0]["cards"] == []

    def test_retired_stages_show_only_while_they_hold_opportunities(
        self, user_a_client, user_a, stages
    ):
        Stage.objects.filter(pk=stages["qualified"].pk).update(is_active=False)
        keys = [c["stage"]["key"] for c in user_a_client.get(board_url()).json()["columns"]]
        assert "qualified" not in keys
        OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["qualified"])
        column = next(
            c
            for c in user_a_client.get(board_url()).json()["columns"]
            if c["stage"]["key"] == "qualified"
        )
        assert (column["stage"]["is_active"], column["count"]) == (False, 1)

    def test_filters_apply_to_cards_counts_and_totals(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        OpportunityFactory(lead=lead, expected_close_date=date(2026, 10, 15), value=Decimal("100"))
        OpportunityFactory(lead=lead, expected_close_date=date(2027, 1, 15), value=Decimal("200"))
        body = user_a_client.get(
            board_url(), {"expected_close_from": "2026-10-01", "expected_close_to": "2026-10-31"}
        ).json()
        assert body["columns"][0]["count"] == 1
        assert body["totals"]["pipeline_value"] == "100.00"
        assert body["columns"][0]["next"] is None

    def test_unknown_pipeline(self, user_a_client, stages):
        response = user_a_client.get(
            board_url(), {"pipeline": "5a1e4d2c-0000-4000-8000-00000000abcd"}
        )
        assert response.status_code == 400
        assert "pipeline" in response.json()["error"]["details"]


class TestListFiltersAndSorts:
    @pytest.fixture
    def rows(self, user_a, stages):
        lead, other_lead = LeadFactory(owner=user_a), LeadFactory(owner=user_a)
        now = timezone.now()
        made = {}
        for title, stage, value, close, lead_, age in [
            ("A", "new", "300", date(2026, 11, 1), lead, 5),
            ("B", "proposal", "100", date(2026, 10, 1), lead, 4),
            ("C", "negotiation", "200", None, other_lead, 3),
            ("D", "won", "400", date(2026, 9, 1), lead, 2),
            ("E", "lost", "50", None, other_lead, 1),
        ]:
            opportunity = OpportunityFactory(
                lead=lead_,
                stage=stages[stage],
                title=title,
                value=Decimal(value),
                expected_close_date=close,
            )
            Opportunity.objects.filter(pk=opportunity.pk).update(
                created_at=now - timedelta(days=age), updated_at=now - timedelta(days=6 - age)
            )
            made[title] = opportunity
        return made, lead, other_lead

    def titles(self, client, **params):
        response = client.get(opportunities_url(), params)
        assert response.status_code == 200, response.content
        return [o["title"] for o in response.json()["results"]]

    @pytest.mark.parametrize(
        ("ordering", "expected"),
        [
            ("-created_at", ["E", "D", "C", "B", "A"]),
            ("created_at", ["A", "B", "C", "D", "E"]),
            ("-value", ["D", "A", "C", "B", "E"]),
            ("value", ["E", "B", "C", "A", "D"]),
            ("expected_close", ["D", "B", "A", "C", "E"]),
            ("-updated_at", ["A", "B", "C", "D", "E"]),
        ],
    )
    def test_orderings(self, user_a_client, rows, ordering, expected):
        assert self.titles(user_a_client, ordering=ordering) == expected

    def test_closed_first_by_latest_close(self, user_a_client, rows):
        assert self.titles(user_a_client, ordering="-closed_at")[:2] in (["D", "E"], ["E", "D"])

    def test_filters(self, user_a_client, rows, stages):
        _, _, other_lead = rows
        assert self.titles(user_a_client, status="open") == ["C", "B", "A"]
        assert self.titles(user_a_client, stage=str(stages["proposal"].pk)) == ["B"]
        assert self.titles(user_a_client, lead=str(other_lead.pk)) == ["E", "C"]
        assert self.titles(
            user_a_client, expected_close_from="2026-10-01", expected_close_to="2026-10-31"
        ) == ["B"]
        assert self.titles(user_a_client, probability_min="50", probability_max="75") == ["C", "B"]

    @pytest.mark.parametrize(
        "params",
        [
            {"owner__email": "x"},
            {"value__gt": "1"},
            {"search": "x"},
            {"status": "pending"},
            {"ordering": "title"},
            {"ordering": "owner"},
            {"page_size": "101"},
            {"expected_close_from": "2026-10-02", "expected_close_to": "2026-10-01"},
            {"probability_min": "80", "probability_max": "10"},
            {"probability_min": "101"},
            {"cursor": "forged"},
            {"owner": "5a1e4d2c-0000-4000-8000-00000000abcd"},  # only organisation-wide
        ],
    )
    def test_anything_else_is_a_400(self, user_a_client, stages, params):
        assert user_a_client.get(opportunities_url(), params).status_code == 400

    def test_pages_never_overlap(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        Opportunity.objects.bulk_create(
            [OpportunityFactory.build(lead=lead, value=Decimal(i % 3)) for i in range(23)]
        )
        seen: list[str] = []
        response = user_a_client.get(
            opportunities_url(), {"ordering": "-value", "page_size": 5}
        ).json()
        while True:
            seen += [o["id"] for o in response["results"]]
            if not response["next"]:
                break
            response = user_a_client.get(response["next"]).json()
        assert len(seen) == len(set(seen)) == 23


class TestAdminWorkspaces:
    def test_organisation_wide_owner_filter(self, admin_client, user_a, user_b, stages):
        OpportunityFactory(lead=LeadFactory(owner=user_a), title="A's")
        OpportunityFactory(lead=LeadFactory(owner=user_b), title="B's")
        listed = admin_client.get(opportunities_url("all"), {"owner": str(user_a.pk)}).json()
        assert [o["title"] for o in listed["results"]] == ["A's"]
        board = admin_client.get(board_url("all"), {"owner": str(user_b.pk)}).json()
        assert board["totals"]["open_count"] == 1
        everyone = admin_client.get(summary_url("all")).json()
        assert everyone["totals"]["open_count"] == 2

    def test_a_users_workspace_shows_that_user_only(self, admin_client, user_a, user_b, stages):
        OpportunityFactory(lead=LeadFactory(owner=user_a), title="A's", value=Decimal("100"))
        OpportunityFactory(lead=LeadFactory(owner=user_b), title="B's", value=Decimal("999"))
        board = admin_client.get(board_url(str(user_a.pk))).json()
        cards = [c["title"] for col in board["columns"] for c in col["cards"]]
        assert cards == ["A's"]
        assert board["totals"]["pipeline_value"] == "100.00"
        assert (
            admin_client.get(
                opportunities_url(str(user_a.pk)), {"owner": str(user_b.pk)}
            ).status_code
            == 400
        )

    def test_an_admin_moves_a_users_opportunity(self, admin_client, admin, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = admin_client.post(
            opportunity_url(opportunity.pk, str(user_a.pk), "move"),
            {"stage": str(stages["won"].pk), "version": 1},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["owner"]["id"] == str(user_a.pk)

    def test_a_deactivated_users_pipeline_stays_viewable(self, admin_client, admin, stages):
        gone = UserFactory(is_active=False)
        OpportunityFactory(lead=LeadFactory(owner=gone), stage=stages["won"])
        board = admin_client.get(board_url(str(gone.pk))).json()
        assert board["columns"][4]["count"] == 1
