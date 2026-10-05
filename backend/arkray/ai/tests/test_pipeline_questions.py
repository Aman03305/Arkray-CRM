"""Ask Arkray and user-defined pipelines (docs/rag-architecture.md#tools): deterministic
figures per pipeline, negotiation by stage *type*, authoritative negotiated prices, and no
other user's personal pipeline shaping an answer or the routing."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from arkray.ai import router, service, tools
from arkray.ai.tools import ToolContext
from arkray.core.access import AccessScope
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import Pipeline, Stage
from tests.factories import LeadFactory, OpportunityFactory, default_stage

pytestmark = pytest.mark.django_db

OWN = AccessScope.own


def run(scope: AccessScope, name: str, args: dict[str, Any] | None = None) -> tuple[Any, bool]:
    ctx = ToolContext(scope=scope, now=timezone.now())
    outcome = tools.execute(ctx, name, args or {})
    return json.loads(outcome.content), outcome.is_error


def personal(owner, name, stages):
    pipeline = Pipeline.objects.create(
        key=f"p{owner.pk.hex[:6]}{len(name)}", name=name, owner=owner
    )
    made = {}
    for position, (stage_name, kind, probability) in enumerate(stages, start=1):
        made[stage_name] = Stage.objects.create(
            pipeline=pipeline,
            key=f"s{position}",
            name=stage_name,
            position=position * 10,
            probability=Decimal(probability),
            category="won" if kind == "won" else "open",
            is_negotiation=kind == "negotiation",
        )
    return pipeline, made


@pytest.fixture
def tender(user_a):
    """Rahul's "Government Tender" pipeline: its negotiation stage is called
    "Commercial discussion"; one deal in it at ₹10,00,000 (60 %), one at ₹5,00,000 in Bid."""
    pipeline, stages = personal(
        user_a,
        "Government Tender",
        [
            ("Bid", "open", "20"),
            ("Commercial discussion", "negotiation", "60"),
            ("Won", "won", "100"),
        ],
    )
    lead = LeadFactory(owner=user_a)
    talking = OpportunityFactory(
        lead=lead,
        title="AIIMS analysers",
        stage=stages["Bid"],
        value=Decimal("1000000"),
    )
    pipeline_services.move_opportunity(
        actor=user_a,
        scope=OWN(user_a.pk),
        opportunity_id=talking.pk,
        version=1,
        stage_id=stages["Commercial discussion"].pk,
        negotiated_price=Decimal("950000.50"),
    )
    talking.refresh_from_db()
    pipeline_services.record_negotiated_price(
        actor=user_a,
        scope=OWN(user_a.pk),
        opportunity_id=talking.pk,
        version=talking.version,
        price=Decimal("925000"),
    )
    OpportunityFactory(
        lead=lead, title="District lab", stage=stages["Bid"], value=Decimal("500000")
    )
    # a deal in the shared default pipeline, which "all pipelines" totals include
    OpportunityFactory(lead=lead, stage=default_stage("proposal"), value=Decimal("200000"))
    return pipeline, stages, talking


class TestPipelineFigures:
    def test_one_pipeline_by_name(self, user_a, tender):
        body, is_error = run(
            OWN(user_a.pk), "get_pipeline_summary", {"pipeline": "government tender"}
        )
        assert not is_error, body
        assert body["pipeline"] == "Government Tender"
        assert body["pipeline_value"]["amount"] == "1500000.00"
        assert body["weighted_pipeline"]["amount"] == "700000.00"  # 6,00,000 + 1,00,000
        assert [s["type"] for s in body["by_stage"][0]["stages"]] == ["open", "negotiation", "won"]

    def test_every_pipeline_by_default(self, user_a, tender):
        body, _ = run(OWN(user_a.pk), "get_pipeline_summary")
        assert body["pipeline"] == "all pipelines"
        assert body["pipeline_value"]["amount"] == "1700000.00"

    def test_another_users_pipeline_doesnt_exist_for_them(self, user_b, tender):
        body, is_error = run(
            OWN(user_b.pk), "get_pipeline_summary", {"pipeline": "Government Tender"}
        )
        assert is_error
        assert "Government Tender" in body["error"]  # echoing their own words...
        assert "Pipelines: Sales Pipeline" in body["error"]  # ...but listing only theirs


class TestNegotiation:
    def test_deals_in_negotiation_by_type_whatever_the_name(self, user_a, tender):
        body, is_error = run(OWN(user_a.pk), "list_opportunities", {"stage_type": "negotiation"})
        assert not is_error, body
        assert body["total_matching"] == 1
        row = body["opportunities"][0]
        assert row["stage"] == "Commercial discussion"
        assert row["negotiated_price"]["amount"] == "925000.00"

    def test_the_negotiation_history_is_authoritative(self, user_a, tender):
        _, _, talking = tender
        body, is_error = run(
            OWN(user_a.pk), "get_negotiation_history", {"ref": f"opportunity:{talking.pk}"}
        )
        assert not is_error, body
        assert body["latest"]["amount"] == "925000.00"
        assert [h["price"]["amount"] for h in body["history"]] == ["925000.00", "950000.50"]

    def test_another_users_history_isnt_found(self, user_b, tender):
        _, _, talking = tender
        body, is_error = run(
            OWN(user_b.pk), "get_negotiation_history", {"ref": f"opportunity:{talking.pk}"}
        )
        assert is_error
        assert "925000" not in json.dumps(body)

    def test_the_record_carries_its_prices(self, user_a, tender):
        _, _, talking = tender
        body, _ = run(OWN(user_a.pk), "get_record", {"ref": f"opportunity:{talking.pk}"})
        assert len(body["negotiation_history"]) == 2
        assert body["negotiated_price"]["amount"] == "925000.00"


class TestRouting:
    def route(self, scope, question):
        return router.route(
            question,
            stage_names=lambda: router.active_stage_names(scope),
            pipeline_names=lambda: router.pipeline_names(scope),
        )

    def test_deals_in_negotiation_route_by_type(self, user_a, tender):
        found = self.route(OWN(user_a.pk), "How many deals are in negotiation?")
        assert found is not None
        assert found.intent == "deals_in_negotiation"
        assert found.calls == (("list_opportunities", {"stage_type": "negotiation", "limit": 10}),)

    def test_a_named_pipelines_value(self, user_a, tender):
        found = self.route(OWN(user_a.pk), "What is the value of my Government Tender pipeline?")
        assert found is not None
        assert (found.intent, found.pipeline) == (
            "pipeline_named",
            "Government Tender",
        )

    def test_another_users_pipeline_name_routes_nowhere(self, user_b, tender):
        assert router.pipeline_names(OWN(user_b.pk)) == ["Sales Pipeline"]
        assert (
            self.route(OWN(user_b.pk), "What is the value of my Government Tender pipeline?")
            is None
        )

    def test_the_routed_answers_are_exact(self, user_a, tender):
        scope = OWN(user_a.pk)
        ctx = tools.ToolContext(scope=scope, now=timezone.now())
        words = service._workspace_words(scope, user_a)
        routed = self.route(scope, "What is the value of my Government Tender pipeline?")
        answer = service.answer_routed(ctx, routed, workspace=words)
        text = json.dumps(answer, ensure_ascii=False)
        assert "₹15,00,000" in text
        assert "₹7,00,000" in text
        routed = self.route(scope, "Which deals are in negotiation?")
        answer = json.dumps(service.answer_routed(ctx, routed, workspace=words), ensure_ascii=False)
        assert "1 opportunity in negotiation" in answer
        assert "negotiated ₹9,25,000" in answer


class TestReviewRegressions:
    """Enhancement review findings, each pinned."""

    def test_a_name_several_pipelines_share_is_never_answered_for_one_of_them(
        self, admin, user_a, user_b, tender
    ):
        """P2: names are unique per owner only; the first match answered for all."""
        personal(user_b, "Government Tender", [("Bid", "open", "20"), ("Won", "won", "100")])
        org = AccessScope.organization(admin.pk)
        body, is_error = run(org, "get_pipeline_summary", {"pipeline": "Government Tender"})
        assert is_error
        assert "Several pipelines" in body["error"]
        assert "Rahul Sharma's" in body["error"]
        assert "Government Tender" not in router.pipeline_names(org)  # not routed either
        # In Rahul's own workspace only his is visible: answered as before.
        mine, is_error = run(
            OWN(user_a.pk), "get_pipeline_summary", {"pipeline": "Government Tender"}
        )
        assert not is_error, mine
        assert mine["pipeline_value"]["amount"] == "1500000.00"

    def test_prices_are_credited_to_whoever_recorded_them(self, admin, user_a, tender):
        """P2: in an administrator's view of Rahul's workspace, Rahul's prices showed as
        "by you" (the administrator), and the administrator's as Rahul's own."""
        _, _, talking = tender
        delegated = AccessScope.for_user(admin.pk, user_a.pk)
        talking.refresh_from_db()
        pipeline_services.record_negotiated_price(
            actor=admin,
            scope=delegated,
            opportunity_id=talking.pk,
            version=talking.version,
            price=Decimal("900000"),
        )
        ref = {"ref": f"opportunity:{talking.pk}"}
        as_admin, _ = run(delegated, "get_negotiation_history", ref)
        assert [h["by"] for h in as_admin["history"]] == ["you", "Rahul Sharma", "Rahul Sharma"]
        as_rahul, _ = run(OWN(user_a.pk), "get_negotiation_history", ref)
        assert [h["by"] for h in as_rahul["history"]] == [admin.full_name, "you", "you"]
