"""Negotiation stages (docs/pipeline.md#negotiation): entering one requires the agreed price
and the agreed CPT (ADR-0029), on every path (no API bypass); prices are exact decimals,
appended with their CPT to an append-only history (never overwritten), and asked again
whenever the deal re-enters negotiation."""

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
from tests.helpers import key_header, signed_in

from .conftest import board_url, convert_url, opportunities_url, opportunity_url

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
CPT = "Rs 18 per test"


def move(client, opportunity_id, stage, version, **body):
    return client.post(
        opportunity_url(opportunity_id, action="move"),
        {"stage": str(stage.pk), "version": version, **body},
        format="json",
    )


def revise(client, opportunity_id, version, price, agreed_cpt=CPT):
    return client.post(
        opportunity_url(opportunity_id, action="negotiated-prices"),
        {"version": version, "price": price, "agreed_cpt": agreed_cpt},
        format="json",
    )


def prices(opportunity_id):
    return list(
        NegotiationPrice.objects.filter(opportunity_id=opportunity_id)
        .order_by("id")
        .values_list("price", "source", "stage_name")
    )


def terms(opportunity_id):
    return list(
        NegotiationPrice.objects.filter(opportunity_id=opportunity_id)
        .order_by("id")
        .values_list("price", "agreed_cpt")
    )


class TestEnteringNegotiation:
    def test_the_terms_are_required_and_a_refusal_leaves_the_deal_where_it_was(
        self, user_a, user_a_client, stages
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(user_a_client, opp.pk, stages["negotiation"], 1)
        assert response.status_code == 400
        # Both are asked for at once.
        assert response.json()["error"]["details"] == {
            "negotiated_price": ["Enter the agreed price."],
            "agreed_cpt": ["Enter the agreed CPT."],
        }
        opp.refresh_from_db()
        # "Cancelled": nothing changed, not even the version; no history, no audit.
        assert (opp.stage_id, opp.version, opp.negotiated_price) == (
            stages["proposal"].pk,
            1,
            None,
        )
        assert StageHistory.objects.filter(opportunity=opp).count() == 0
        assert not AuditEvent.objects.filter(target_id=str(opp.pk)).exists()

    @pytest.mark.parametrize(
        ("body", "missing"),
        [
            ({"negotiated_price": "1200000"}, "agreed_cpt"),
            ({"negotiated_price": "1200000", "agreed_cpt": ""}, "agreed_cpt"),
            ({"negotiated_price": "1200000", "agreed_cpt": "   "}, "agreed_cpt"),
            ({"agreed_cpt": CPT}, "negotiated_price"),
        ],
        ids=["no-cpt", "blank-cpt", "spaces-cpt", "no-price"],
    )
    def test_the_price_alone_or_the_cpt_alone_is_not_enough(
        self, user_a, user_a_client, stages, body, missing
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(user_a_client, opp.pk, stages["negotiation"], 1, **body)
        assert response.status_code == 400
        assert list(response.json()["error"]["details"]) == [missing]
        opp.refresh_from_db()
        assert (opp.stage_id, opp.version) == (stages["proposal"].pk, 1)
        assert terms(opp.pk) == []

    def test_with_both_it_moves_and_records_them(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            1,
            negotiated_price="1200000",
            agreed_cpt="  Rs 18 per test ",
        )
        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["stage"]["type"], body["negotiated_price"], body["agreed_cpt"]) == (
            "negotiation",
            "1200000.00",
            "Rs 18 per test",  # one line, trimmed, as typed otherwise
        )
        assert body["negotiated_at"] is not None
        assert prices(opp.pk) == [(Decimal("1200000.00"), "stage_entry", "Negotiation")]
        row = NegotiationPrice.objects.get(opportunity_id=opp.pk)
        assert (row.actor_id, row.subject_user_id, row.currency) == (user_a.pk, None, "INR")
        assert row.agreed_cpt == "Rs 18 per test"
        assert row.opportunity_version == body["version"]
        assert user_a_client.get(opportunity_url(opp.pk)).json()["agreed_cpt"] == "Rs 18 per test"

    def test_the_audit_says_they_were_recorded_never_what(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            1,
            negotiated_price="1200000",
            agreed_cpt="CPT 4321",
        )
        event = AuditEvent.objects.get(target_id=str(opp.pk), action="opportunity.stage_changed")
        assert event.metadata["negotiated_price_recorded"] is True
        assert event.metadata["agreed_cpt_recorded"] is True
        assert "1200000" not in str(event.metadata)
        assert "4321" not in str(event.metadata)

    @pytest.mark.parametrize(
        "price", [1200000.5, "1,200,000", "-5", "1e6", "12.345", "NaN", "", None, True]
    )
    def test_only_exact_amounts(self, user_a, user_a_client, stages, price):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(
            user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price=price, agreed_cpt=CPT
        )
        assert response.status_code == 400
        assert Opportunity.objects.get(pk=opp.pk).stage_id == stages["proposal"].pk

    @pytest.mark.parametrize(
        "cpt",
        ["x" * 101, "Rs 18" + chr(0x1B) + "[31m", None, ["Rs 18"], {"rs": 18}, 18, 18.5, True],
        # A number is refused, not re-spelled as text (18.50 would arrive as "18.5"): R104.
        ids=["too-long", "control-character", "null", "list", "object", "int", "float", "bool"],
    )
    def test_the_cpt_is_one_line_of_text(self, user_a, user_a_client, stages, cpt):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            1,
            negotiated_price="10",
            agreed_cpt=cpt,
        )
        assert response.status_code == 400
        assert "agreed_cpt" in response.json()["error"]["details"]
        assert Opportunity.objects.get(pk=opp.pk).stage_id == stages["proposal"].pk

    @pytest.mark.parametrize(
        ("cpt", "kept"),
        [("Rs 18\nper   test", "Rs 18 per test"), ("18", "18")],
        ids=["lines-joined", "a-number-as-text"],
    )
    def test_the_cpt_is_kept_as_one_line_like_expected_cpt(
        self, user_a, user_a_client, stages, cpt, kept
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            1,
            negotiated_price="10",
            agreed_cpt=cpt,
        )
        assert response.status_code == 200, response.content
        assert terms(opp.pk) == [(Decimal("10.00"), kept)]

    def test_a_cpt_of_100_characters_fits(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            1,
            negotiated_price="10",
            agreed_cpt="x" * 100,
        )
        assert response.status_code == 200, response.content
        assert terms(opp.pk) == [(Decimal("10.00"), "x" * 100)]

    @pytest.mark.parametrize("price", ["0.01", "1050000.10", "999999999999.99", "0"])
    def test_decimals_are_kept_exactly(self, user_a, user_a_client, stages, price):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = move(
            user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price=price, agreed_cpt=CPT
        )
        assert response.status_code == 200, response.content
        expected = Decimal(price).quantize(Decimal("0.01"))
        assert NegotiationPrice.objects.get(opportunity_id=opp.pk).price == expected
        assert response.json()["negotiated_price"] == format(expected, "f")

    @pytest.mark.parametrize(
        ("body", "refused"),
        [
            ({"negotiated_price": "10"}, ["negotiated_price"]),
            ({"agreed_cpt": CPT}, ["agreed_cpt"]),
            ({"negotiated_price": "10", "agreed_cpt": CPT}, ["negotiated_price", "agreed_cpt"]),
        ],
        ids=["price", "cpt", "both"],
    )
    def test_no_other_stage_takes_them(self, user_a, user_a_client, stages, body, refused):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = move(user_a_client, opp.pk, stages["proposal"], 1, **body)
        assert response.status_code == 400
        assert list(response.json()["error"]["details"]) == refused

    def test_a_blank_cpt_on_another_stage_is_no_cpt(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a))
        response = move(user_a_client, opp.pk, stages["proposal"], 1, agreed_cpt="")
        assert response.status_code == 200, response.content
        assert response.json()["agreed_cpt"] == ""

    def test_leaving_and_re_entering_asks_again_and_keeps_the_history(
        self, user_a, user_a_client, stages
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        v = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            1,
            negotiated_price="1200000",
            agreed_cpt="Rs 20",
        )
        v = move(user_a_client, opp.pk, stages["proposal"], v.json()["version"])
        assert v.status_code == 200
        # The last terms stay on record.
        assert (v.json()["negotiated_price"], v.json()["agreed_cpt"]) == ("1200000.00", "Rs 20")
        again = move(user_a_client, opp.pk, stages["negotiation"], v.json()["version"])
        assert again.status_code == 400  # asked again
        assert set(again.json()["error"]["details"]) == {"negotiated_price", "agreed_cpt"}
        again = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            v.json()["version"],
            negotiated_price="1100000",
            agreed_cpt="Rs 18",
        )
        assert again.status_code == 200
        assert prices(opp.pk) == [
            (Decimal("1200000.00"), "stage_entry", "Negotiation"),
            (Decimal("1100000.00"), "stage_entry", "Negotiation"),
        ]
        assert terms(opp.pk) == [
            (Decimal("1200000.00"), "Rs 20"),
            (Decimal("1100000.00"), "Rs 18"),
        ]

    def test_reopening_into_negotiation_asks_too(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["lost"])
        refused = move(user_a_client, opp.pk, stages["negotiation"], opp.version)
        assert refused.status_code == 400
        assert set(refused.json()["error"]["details"]) == {"negotiated_price", "agreed_cpt"}
        response = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            opp.version,
            negotiated_price="900000",
            agreed_cpt=CPT,
        )
        assert response.status_code == 200, response.content
        assert terms(opp.pk) == [(Decimal("900000.00"), CPT)]

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

    @pytest.mark.parametrize(
        ("body", "refused"),
        [
            ({"negotiated_price": "9", "agreed_cpt": "Rs 9"}, {"negotiated_price", "agreed_cpt"}),
            ({"agreed_cpt": "Rs 9"}, {"agreed_cpt"}),
        ],
        ids=["both", "cpt"],
    )
    def test_the_same_stage_with_terms_is_refused_not_dropped(
        self, user_a, user_a_client, stages, body, refused
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10", agreed_cpt=CPT)
        response = move(user_a_client, opp.pk, stages["negotiation"], 2, **body)
        assert response.status_code == 400
        assert set(response.json()["error"]["details"]) == refused
        assert terms(opp.pk) == [(Decimal("10.00"), CPT)]

    def test_the_service_has_no_bypass(self, user_a, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        for given in ({}, {"negotiated_price": "10"}, {"agreed_cpt": CPT}):
            with pytest.raises(InvalidInputError):
                services.move_opportunity(
                    actor=user_a,
                    scope=OWN(user_a.pk),
                    opportunity_id=opp.pk,
                    version=1,
                    stage_id=stages["negotiation"].pk,
                    **given,
                )
        assert Opportunity.objects.get(pk=opp.pk).stage_id == stages["proposal"].pk

    def test_creating_in_a_negotiation_stage_needs_both(self, user_a, user_a_client, stages):
        lead = LeadFactory(owner=user_a)
        body = {
            "lead": str(lead.pk),
            "value": "1500000",
            "stage": str(stages["negotiation"].pk),
        }
        refused = user_a_client.post(opportunities_url(), body, format="json", headers=key_header())
        assert refused.status_code == 400
        assert set(refused.json()["error"]["details"]) == {"negotiated_price", "agreed_cpt"}
        refused = user_a_client.post(
            opportunities_url(),
            {**body, "negotiated_price": "1400000"},
            format="json",
            headers=key_header(),
        )
        assert refused.status_code == 400
        assert set(refused.json()["error"]["details"]) == {"agreed_cpt"}
        response = user_a_client.post(
            opportunities_url(),
            {**body, "negotiated_price": "1400000", "agreed_cpt": CPT},
            format="json",
            headers=key_header(),
        )
        assert response.status_code == 201, response.content
        assert response.json()["agreed_cpt"] == CPT
        assert prices(response.json()["id"]) == [(Decimal("1400000.00"), "creation", "Negotiation")]
        assert terms(response.json()["id"]) == [(Decimal("1400000.00"), CPT)]

    def test_creating_elsewhere_refuses_a_cpt(self, user_a, user_a_client, stages):
        response = user_a_client.post(
            opportunities_url(),
            {
                "lead": str(LeadFactory(owner=user_a).pk),
                "value": "1500000",
                "stage": str(stages["proposal"].pk),
                "agreed_cpt": CPT,
            },
            format="json",
            headers=key_header(),
        )
        assert response.status_code == 400
        assert set(response.json()["error"]["details"]) == {"agreed_cpt"}

    def test_a_new_customer_created_in_negotiation_is_not_created_without_both(
        self, user_a, user_a_client, stages
    ):
        # The UI's path (no lead: one is made in the same transaction): refused, nothing kept.
        body = {
            "customer_name": "ABC Diagnostics",
            "value": "1500000",
            "stage": str(stages["negotiation"].pk),
            "negotiated_price": "1400000",
        }
        before = Opportunity.objects.count()
        refused = user_a_client.post(opportunities_url(), body, format="json", headers=key_header())
        assert refused.status_code == 400
        assert Opportunity.objects.count() == before
        response = user_a_client.post(
            opportunities_url(), {**body, "agreed_cpt": CPT}, format="json", headers=key_header()
        )
        assert response.status_code == 201, response.content
        assert terms(response.json()["id"]) == [(Decimal("1400000.00"), CPT)]

    def test_converting_into_a_negotiation_stage_needs_both(self, user_a, user_a_client, stages):
        lead = LeadFactory(owner=user_a)
        body = {
            "version": lead.version,
            "value": "1500000",
            "stage": str(stages["negotiation"].pk),
            "negotiated_price": "1450000",
        }
        refused = user_a_client.post(convert_url(lead.pk), body, format="json")
        assert refused.status_code == 400
        assert set(refused.json()["error"]["details"]) == {"agreed_cpt"}
        response = user_a_client.post(
            convert_url(lead.pk), {**body, "agreed_cpt": CPT}, format="json"
        )
        assert response.status_code == 201, response.content
        assert terms(response.json()["opportunity"]["id"]) == [(Decimal("1450000.00"), CPT)]

    def test_an_idempotent_create_with_another_cpt_is_not_a_replay(
        self, user_a, user_a_client, stages
    ):
        body = {
            "customer_name": "ABC Diagnostics",
            "value": "1500000",
            "stage": str(stages["negotiation"].pk),
            "negotiated_price": "1400000",
            "agreed_cpt": CPT,
        }
        headers = {"Idempotency-Key": "6f1c2b9e-3d4a-4c5b-8e7f-0a1b2c3d4e5f"}
        first = user_a_client.post(opportunities_url(), body, format="json", headers=headers)
        assert first.status_code == 201, first.content
        replay = user_a_client.post(opportunities_url(), body, format="json", headers=headers)
        assert replay.status_code == 201
        assert replay["Idempotent-Replayed"] == "true"
        # The same key with other terms is never a replay of the first creation.
        other = user_a_client.post(
            opportunities_url(), {**body, "agreed_cpt": "Rs 12"}, format="json", headers=headers
        )
        assert (other.status_code, other.json()["error"]["code"]) == (
            422,
            "idempotency_key_reused",
        )


class TestRevisions:
    def test_the_history_is_appended_never_overwritten(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        version = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            1,
            negotiated_price="1200000",
            agreed_cpt="Rs 20",
        ).json()["version"]
        for price, cpt in (("1100000", "Rs 19"), ("1050000", "Rs 18")):
            response = revise(user_a_client, opp.pk, version, price, cpt)
            assert response.status_code == 200, response.content
            version = response.json()["version"]
        assert (response.json()["negotiated_price"], response.json()["agreed_cpt"]) == (
            "1050000.00",
            "Rs 18",
        )
        listed = user_a_client.get(opportunity_url(opp.pk, action="negotiated-prices")).json()
        assert [r["price"] for r in listed["results"]] == ["1050000.00", "1100000.00", "1200000.00"]
        assert [r["agreed_cpt"] for r in listed["results"]] == ["Rs 18", "Rs 19", "Rs 20"]
        assert [r["source"] for r in listed["results"]] == ["revision", "revision", "stage_entry"]
        assert listed["results"][0]["actor"]["id"] == str(user_a.pk)
        assert (
            AuditEvent.objects.filter(action="opportunity.negotiated_price_recorded").count() == 2
        )
        audit = AuditEvent.objects.filter(action="opportunity.negotiated_price_recorded").first()
        # Amounts and CPTs live in the history only.
        assert "1100000" not in str(audit.metadata)
        assert "Rs 19" not in str(audit.metadata)

    def test_board_cards_and_list_rows_show_the_latest_cpt(self, user_a, user_a_client, stages):
        """The board shows the agreed CPT below the agreed price: the latest one, from the
        history, without a query per card."""
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        other = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        body = move(
            user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10", agreed_cpt=CPT
        ).json()
        revise(user_a_client, opp.pk, body["version"], "9", "Rs 16 per test")
        board = user_a_client.get(board_url()).json()
        cards = {c["id"]: c for column in board["columns"] for c in column["cards"]}
        listed = {r["id"]: r for r in user_a_client.get(opportunities_url()).json()["results"]}
        for shown in (cards, listed):
            assert (shown[str(opp.pk)]["negotiated_price"], shown[str(opp.pk)]["agreed_cpt"]) == (
                "9.00",
                "Rs 16 per test",
            )
            assert (
                shown[str(other.pk)]["negotiated_price"],
                shown[str(other.pk)]["agreed_cpt"],
            ) == (
                None,
                "",
            )

    def test_the_cpt_alone_can_change(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        body = move(
            user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10", agreed_cpt=CPT
        ).json()
        response = revise(user_a_client, opp.pk, body["version"], "10.00", "Rs 16 per test")
        assert response.status_code == 200, response.content
        assert response.json()["version"] == body["version"] + 1
        assert response.json()["agreed_cpt"] == "Rs 16 per test"
        assert terms(opp.pk) == [(Decimal("10.00"), CPT), (Decimal("10.00"), "Rs 16 per test")]

    def test_the_same_terms_again_are_a_harmless_retry(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        body = move(
            user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10", agreed_cpt=CPT
        ).json()
        response = revise(user_a_client, opp.pk, 1, "10.00", f" {CPT} ")  # an old version
        assert response.status_code == 200
        assert response.json()["version"] == body["version"]
        assert len(prices(opp.pk)) == 1

    @pytest.mark.parametrize(
        ("body", "missing"),
        [
            ({"price": "9"}, "agreed_cpt"),
            ({"price": "9", "agreed_cpt": ""}, "agreed_cpt"),
            ({"agreed_cpt": CPT}, "price"),
        ],
        ids=["no-cpt", "blank-cpt", "no-price"],
    )
    def test_both_are_required(self, user_a, user_a_client, stages, body, missing):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10", agreed_cpt=CPT)
        response = user_a_client.post(
            opportunity_url(opp.pk, action="negotiated-prices"),
            {"version": 2, **body},
            format="json",
        )
        assert response.status_code == 400
        assert missing in response.json()["error"]["details"]
        assert len(prices(opp.pk)) == 1

    def test_the_service_requires_both(self, user_a, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["negotiation"])
        with pytest.raises(InvalidInputError) as raised:
            services.record_negotiated_price(
                actor=user_a, scope=OWN(user_a.pk), opportunity_id=opp.pk, version=1, price=10
            )
        assert raised.value.details == {"agreed_cpt": ["Enter the agreed CPT."]}

    def test_a_deal_negotiated_before_the_cpt_was_asked_for_gets_one(
        self, user_a, user_a_client, stages
    ):
        # A price recorded by the previous release has no CPT (the column's default): the
        # next revision adds one, even at the same price.
        opp = OpportunityFactory(
            lead=LeadFactory(owner=user_a),
            stage=stages["negotiation"],
            negotiated_price=Decimal("10.00"),
            negotiated_at="2026-01-01T00:00:00Z",
        )
        NegotiationPrice.objects.create(
            opportunity=opp,
            price=Decimal("10.00"),
            currency="INR",
            stage=stages["negotiation"],
            stage_name="Negotiation",
            source="stage_entry",
            opportunity_version=1,
            actor=user_a,
        )
        response = revise(user_a_client, opp.pk, 1, "10", CPT)
        assert response.status_code == 200, response.content
        assert (response.json()["version"], response.json()["agreed_cpt"]) == (2, CPT)
        assert terms(opp.pk) == [(Decimal("10.00"), ""), (Decimal("10.00"), CPT)]

    def test_only_while_in_negotiation(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = revise(user_a_client, opp.pk, 1, "10")
        assert response.status_code == 422

    def test_a_stale_version_conflicts(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10", agreed_cpt=CPT)
        response = revise(user_a_client, opp.pk, 1, "9")
        assert response.status_code == 409

    def test_history_rows_are_append_only_in_the_database(self, user_a, user_a_client, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10", agreed_cpt=CPT)
        row = NegotiationPrice.objects.get(opportunity_id=opp.pk)
        with pytest.raises(AppendOnlyViolation):
            row.save()
        with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pipeline_negotiation_price SET agreed_cpt = 'x' WHERE id = %s", [row.pk]
            )


class TestWhoRecordedIt:
    def test_an_administrator_in_a_users_workspace_is_the_actor_the_user_the_subject(
        self, user_a, admin, admin_client, stages
    ):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        response = admin_client.post(
            f"/api/v1/workspaces/{user_a.pk}/opportunities/{opp.pk}/move",
            {
                "stage": str(stages["negotiation"].pk),
                "version": 1,
                "negotiated_price": "500",
                "agreed_cpt": CPT,
            },
            format="json",
        )
        assert response.status_code == 200, response.content
        row = NegotiationPrice.objects.get(opportunity_id=opp.pk)
        assert (row.actor_id, row.subject_user_id, row.agreed_cpt) == (admin.pk, user_a.pk, CPT)

    def test_another_users_prices_dont_exist(self, user_a, user_b, stages):
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        signed_in(user_a).post(
            opportunity_url(opp.pk, action="move"),
            {
                "stage": str(stages["negotiation"].pk),
                "version": 1,
                "negotiated_price": "9",
                "agreed_cpt": CPT,
            },
            format="json",
        )
        priya = signed_in(user_b)
        assert priya.get(opportunity_url(opp.pk, action="negotiated-prices")).status_code == 404
        response = revise(priya, opp.pk, 2, "1")
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
                actor=user_a,
                scope=OWN(user_a.pk),
                opportunity_id=opp.pk,
                version=1,
                price=10,
                agreed_cpt=CPT,
            )


class TestThePreviousRelease:
    """During a rolling deploy or after a rollback the previous release runs beside this one:
    it never names agreed_cpt."""

    def test_it_can_still_insert_a_price(self, user_a, user_a_client, stages):
        # pipeline.0009 keeps the column's DEFAULT.
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a_client, opp.pk, stages["negotiation"], 1, negotiated_price="10", agreed_cpt=CPT)
        row = NegotiationPrice.objects.get(opportunity_id=opp.pk)
        columns = ", ".join(
            f.column
            for f in NegotiationPrice._meta.concrete_fields
            if f.column not in ("id", "agreed_cpt")
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO pipeline_negotiation_price ({columns}) "  # noqa: S608 — model columns
                f"SELECT {columns} FROM pipeline_negotiation_price WHERE id = %s "
                "RETURNING agreed_cpt",
                [row.pk],
            )
            assert cursor.fetchone() == ("",)

    def test_a_price_it_records_never_shows_an_older_cpt(self, user_a, user_a_client, stages):
        """Review P2: the CPT shown (and offered in Update price & CPT) is the one recorded
        with the latest price, never one that went with an earlier price."""
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        body = move(
            user_a_client,
            opp.pk,
            stages["negotiation"],
            1,
            negotiated_price="1200000",
            agreed_cpt="Rs 18 per test",
        ).json()
        assert body["agreed_cpt"] == "Rs 18 per test"
        # The previous release records a revision: a price row without a CPT, and its copy.
        NegotiationPrice.objects.create(
            opportunity=opp,
            price=Decimal("900000.00"),
            currency="INR",
            stage=stages["negotiation"],
            stage_name="Negotiation",
            source="revision",
            opportunity_version=body["version"] + 1,
            actor=user_a,
        )
        Opportunity.objects.filter(pk=opp.pk).update(
            negotiated_price=Decimal("900000.00"), version=body["version"] + 1
        )
        shown = user_a_client.get(opportunity_url(opp.pk)).json()
        assert (shown["negotiated_price"], shown["agreed_cpt"]) == ("900000.00", "")


class TestIdempotencyAcrossTheDeploy:
    def test_a_request_without_a_cpt_keeps_its_digest(self, user_a, stages):
        """Review P3: a create sent to the previous release and retried on this one (same
        key, same body: no CPT) replays instead of being refused as a reused key."""
        from arkray.core import idempotency

        captured = []
        real = idempotency.request_digest

        def spy(*parts):
            captured.append(parts)
            return real(*parts)

        lead = LeadFactory(owner=user_a)
        fields = {"value": Decimal("1500000")}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(idempotency, "request_digest", spy)
            services.create_opportunity(
                actor=user_a, scope=OWN(user_a.pk), lead_id=lead.pk, fields=fields
            )
            services.create_opportunity(
                actor=user_a,
                scope=OWN(user_a.pk),
                lead_id=lead.pk,
                fields=fields,
                stage_id=stages["negotiation"].pk,
                negotiated_price=Decimal("10"),
                agreed_cpt=CPT,
            )
        without, with_cpt = captured
        # The previous release's parts, exactly: ..., pipeline, stage, price.
        assert without[-1] == "None"
        assert len(with_cpt) == len(without) + 1
        assert with_cpt[-2:] == ("10.00", CPT)
