"""Negotiation stages (docs/pipeline.md#negotiation): entering one requires the negotiated
price, on every path (no API bypass); prices are exact decimals, appended to an append-only
history (never overwritten), and asked again whenever the deal re-enters negotiation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import AppendOnlyViolation, BusinessRuleViolation, InvalidInputError
from arkray.pipeline import services
from arkray.pipeline.models import NegotiationPrice, Opportunity, Stage, StageHistory
from tests.factories import LeadFactory, OpportunityFactory
from tests.helpers import signed_in

from .conftest import convert_url, opportunities_url, opportunity_url

pytestmark = pytest.mark.django_db

OWN = AccessScope.own


def move(client, opportunity_id, stage, version, **body):
    return client.post(
        opportunity_url(opportunity_id, action="move"),
        {"stage": str(stage.pk), "version": version, **body},
        format="json",
    )


def prices(opportunity_id):
    return list(
        NegotiationPrice.objects.filter(opportunity_id=opportunity_id)
        .order_by("id")
        .values_list("price", "source", "stage_name")
    )


class TestEnteringNegotiation:
    def test_the_price_is_required_and_a_refusal_leaves_the_deal_where_it_was(
        self, user_a, user_a_client, stages
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(user_a_client, opp.pk, stages["negotiation"], 1)
        assert response.status_code == 400
        assert "negotiated_price" in response.json()["error"]["details"]
        opp.refresh_from_db()
        # "Cancelled": nothing changed, not even the version; no history, no audit.
        assert (opp.stage_id, opp.version, opp.negotiated_price) == (
            stages["proposal"].pk,
            1,
            None,
        )
        assert StageHistory.objects.filter(opportunity=opp).count() == 0
        assert not AuditEvent.objects.filter(target_id=str(opp.pk)).exists()

    def test_with_a_price_it_moves_and_records_it(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="1200000")
        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["stage"]["type"], body["negotiated_price"]) == ("negotiation", "1200000.00")
        assert body["negotiated_at"] is not None
        assert prices(opp.pk) == [(Decimal("1200000.00"), "stage_entry", "Negotiation")]
        row = NegotiationPrice.objects.get(opportunity_id=opp.pk)
        assert (row.actor_id, row.subject_user_id, row.currency) == (user_a.pk, None, "INR")
        assert row.opportunity_version == body["version"]

    @pytest.mark.parametrize(
        "price", [1200000.5, "1,200,000", "-5", "1e6", "12.345", "NaN", "", None, True]
    )
    def test_only_exact_amounts(self, user_a, user_a_client, stages, price):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price=price)
        assert response.status_code == 400
        assert Opportunity.objects.get(pk=opp.pk).stage_id == stages["proposal"].pk

    @pytest.mark.parametrize("price", ["0.01", "1050000.10", "999999999999.99", "0"])
    def test_decimals_are_kept_exactly(self, user_a, user_a_client, stages, price):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price=price)
        assert response.status_code == 200, response.content
        expected = Decimal(price).quantize(Decimal("0.01"))
        assert NegotiationPrice.objects.get(opportunity_id=opp.pk).price == expected
        assert response.json()["negotiated_price"] == format(expected, "f")

    def test_no_other_stage_takes_a_price(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = move(user_a_client, opp.pk, stages["proposal"], 1, negotiated_price="10")
        assert response.status_code == 400
        assert "negotiated_price" in response.json()["error"]["details"]

    def test_leaving_and_re_entering_asks_again_and_keeps_the_history(
        self, user_a, user_a_client, stages
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        v = move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="1200000")
        v = move(user_a_client, opp.pk, stages["proposal"], v.json()["version"])
        assert v.status_code == 200
        assert v.json()["negotiated_price"] == "1200000.00"  # the last one stays on record
        again = move(user_a_client, opp.pk, stages["negotiation"], v.json()["version"])
        assert again.status_code == 400  # asked again
        again = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            v.json()["version"],
            negotiated_price="1100000",
        )
        assert again.status_code == 200
        assert prices(opp.pk) == [
            (Decimal("1200000.00"), "stage_entry", "Negotiation"),
            (Decimal("1100000.00"), "stage_entry", "Negotiation"),
        ]

    def test_a_renamed_negotiation_stage_still_asks(self, user_a, user_a_client, stages):
        Stage.objects.filter(pk=stages["negotiation"].pk).update(name="Commercial discussion")
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        assert move(user_a_client, opp.pk, stages["negotiation"], 1).status_code == 400

    def test_a_stage_called_negotiation_that_isnt_one_doesnt_ask(
        self, user_a, user_a_client, stages
    ):
        Stage.objects.filter(pk=stages["proposal"].pk).update(name="Price talk")
        Stage.objects.filter(pk=stages["negotiation"].pk).update(is_negotiation=False)
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a))
        assert move(user_a_client, opp.pk, stages["negotiation"], 1).status_code == 200

    def test_the_same_stage_with_a_price_is_refused_not_dropped(
        self, user_a, user_a_client, stages
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10")
        response = move(user_a_client, opp.pk, stages["negotiation"], 2, negotiated_price="9")
        assert response.status_code == 400
        assert len(prices(opp.pk)) == 1

    def test_the_service_has_no_bypass(self, user_a, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        with pytest.raises(InvalidInputError):
            services.move_opportunity(
                actor=user_a,
                scope=OWN(user_a.pk),
                opportunity_id=opp.pk,
                version=1,
                stage_id=stages["negotiation"].pk,
            )

    def test_creating_in_a_negotiation_stage_needs_the_price(self, user_a, user_a_client, stages):
        lead = LeadFactory(owner=user_a)
        body = {
            "lead": str(lead.pk),
            "title": "Analyser",
            "value": "1500000",
            "stage": str(stages["negotiation"].pk),
        }
        assert user_a_client.post(opportunities_url(), body, format="json").status_code == 400
        response = user_a_client.post(
            opportunities_url(), {**body, "negotiated_price": "1400000"}, format="json"
        )
        assert response.status_code == 201, response.content
        assert prices(response.json()["id"]) == [(Decimal("1400000.00"), "creation", "Negotiation")]

    def test_converting_into_a_negotiation_stage_needs_the_price(
        self, user_a, user_a_client, stages
    ):
        lead = LeadFactory(owner=user_a)
        body = {
            "version": lead.version,
            "title": "Analyser",
            "value": "1500000",
            "stage": str(stages["negotiation"].pk),
        }
        assert user_a_client.post(convert_url(lead.pk), body, format="json").status_code == 400
        response = user_a_client.post(
            convert_url(lead.pk), {**body, "negotiated_price": "1450000"}, format="json"
        )
        assert response.status_code == 201, response.content


class TestRevisions:
    def test_the_history_is_appended_never_overwritten(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        version = move(
            user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="1200000"
        ).json()["version"]
        for price in ("1100000", "1050000"):
            response = user_a_client.post(
                opportunity_url(opp.pk, action="negotiated-prices"),
                {"version": version, "price": price},
                format="json",
            )
            assert response.status_code == 200, response.content
            version = response.json()["version"]
        assert response.json()["negotiated_price"] == "1050000.00"
        listed = user_a_client.get(opportunity_url(opp.pk, action="negotiated-prices")).json()
        assert [r["price"] for r in listed["results"]] == ["1050000.00", "1100000.00", "1200000.00"]
        assert [r["source"] for r in listed["results"]] == ["revision", "revision", "stage_entry"]
        assert listed["results"][0]["actor"]["id"] == str(user_a.pk)
        assert (
            AuditEvent.objects.filter(action="opportunity.negotiated_price_recorded").count() == 2
        )
        audit = AuditEvent.objects.filter(action="opportunity.negotiated_price_recorded").first()
        assert "1100000" not in str(audit.metadata)  # amounts live in the history only

    def test_the_same_price_again_is_a_harmless_retry(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        body = move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10").json()
        response = user_a_client.post(
            opportunity_url(opp.pk, action="negotiated-prices"),
            {"version": 1, "price": "10.00"},  # an old version: still no conflict
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["version"] == body["version"]
        assert len(prices(opp.pk)) == 1

    def test_only_while_in_negotiation(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = user_a_client.post(
            opportunity_url(opp.pk, action="negotiated-prices"),
            {"version": 1, "price": "10"},
            format="json",
        )
        assert response.status_code == 422

    def test_a_stale_version_conflicts(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10")
        response = user_a_client.post(
            opportunity_url(opp.pk, action="negotiated-prices"),
            {"version": 1, "price": "9"},
            format="json",
        )
        assert response.status_code == 409

    def test_history_rows_are_append_only_in_the_database(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10")
        row = NegotiationPrice.objects.get(opportunity_id=opp.pk)
        with pytest.raises(AppendOnlyViolation):
            row.save()
        with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pipeline_negotiation_price SET price = 1 WHERE id = %s", [row.pk]
            )


class TestWhoRecordedIt:
    def test_an_administrator_in_a_users_workspace_is_the_actor_the_user_the_subject(
        self, user_a, admin, admin_client, stages
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = admin_client.post(
            f"/api/v1/workspaces/{user_a.pk}/opportunities/{opp.pk}/move",
            {"stage": str(stages["negotiation"].pk), "version": 1, "negotiated_price": "500"},
            format="json",
        )
        assert response.status_code == 200, response.content
        row = NegotiationPrice.objects.get(opportunity_id=opp.pk)
        assert (row.actor_id, row.subject_user_id) == (admin.pk, user_a.pk)

    def test_another_users_prices_dont_exist(self, user_a, user_b, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        signed_in(user_a).post(
            opportunity_url(opp.pk, action="move"),
            {"stage": str(stages["negotiation"].pk), "version": 1, "negotiated_price": "9"},
            format="json",
        )
        priya = signed_in(user_b)
        assert priya.get(opportunity_url(opp.pk, action="negotiated-prices")).status_code == 404
        response = priya.post(
            opportunity_url(opp.pk, action="negotiated-prices"),
            {"version": 2, "price": "1"},
            format="json",
        )
        assert response.status_code == 404
        assert len(prices(opp.pk)) == 1

    def test_archived_deals_are_read_only(self, user_a, stages):
        opp = OpportunityFactory(
            lead=LeadFactory(owner=user_a),
            stage=stages["negotiation"],
            archived_at="2026-01-01T00:00:00Z",
        )
        with pytest.raises(BusinessRuleViolation):
            services.record_negotiated_price(
                actor=user_a, scope=OWN(user_a.pk), opportunity_id=opp.pk, version=1, price=10
            )
