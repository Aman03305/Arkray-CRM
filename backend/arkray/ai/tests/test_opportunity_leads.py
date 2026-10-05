"""Ask Arkray and the Opportunity/Lead flow (ADR-0028): the router answers "how many new leads
were created today" (the Dashboard's figure) and "opportunities for <instrument>" (the
structured field), leaves "leads I added" to the model, and list_opportunities filters by an
instrument however it is spelled, inside the workspace only. (The pipeline module's tests may
not import this module: tests/architecture layering.)"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from arkray.ai import router, tools
from arkray.ai.tools import ToolContext
from arkray.core.access import AccessScope
from arkray.pipeline import services as pipeline_services

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
INSTRUMENTS = ["Adams 8380 V-lite", "Adams 8180 V", "Adams 8180 T", "PCBA with Printer"]


def create(user, customer="ABC Diagnostics Mumbai", instrument="Adams 8380 V-lite"):
    """An opportunity made as the New Opportunity form makes it (no lead, no name)."""
    return pipeline_services.create_opportunity(
        actor=user,
        scope=OWN(user.pk),
        lead_id=None,
        fields={
            "account_name": "ABC Diagnostics",
            "customer_name": customer,
            "instrument_name": instrument,
            "value": Decimal("850000"),
            "opportunity_date": date(2026, 10, 5),
        },
    ).opportunity


class TestOpportunityLeadQuestions:
    @pytest.mark.parametrize(
        ("question", "intent"),
        [
            ("How many new leads were created today?", "new_leads_today"),
            ("How many leads were created today?", "new_leads_today"),
            ("Which opportunities are expected to close this month?", "deals_closing_this_month"),
        ],
    )
    def test_routed_questions(self, question, intent):
        routed = router.route(question)
        assert routed is not None
        assert routed.intent == intent

    @pytest.mark.parametrize(
        "question",
        ["How many leads have I added today?", "Leads I created today", "new leads we added today"],
    )
    def test_leads_i_added_are_left_to_the_model(self, question):
        """Backend review: the figure counts by owner, not by who added them."""
        assert router.route(question) is None

    def test_ask_takes_an_instrument_however_it_is_spelled(self, user_a):
        create(user_a)
        ctx = ToolContext(scope=OWN(user_a.pk), now=timezone.now())
        spelled = {"instrument": "adams  8380 V-LITE"}
        result = json.loads(tools.execute(ctx, "list_opportunities", spelled).content)
        assert result["total_matching"] == 1

    @pytest.mark.parametrize("instrument", INSTRUMENTS)
    def test_opportunities_for_an_instrument_are_routed(self, instrument):
        routed = router.route(f"Show opportunities for {instrument}")
        assert routed is not None
        assert (routed.intent, routed.instrument) == ("deals_for_instrument", instrument)
        assert routed.calls == (("list_opportunities", {"instrument": instrument, "limit": 10}),)
        open_only = router.route(f"Show open opportunities for {instrument}")
        assert open_only is not None
        assert open_only.calls[0][1]["status"] == "open"

    def test_the_instrument_filter_is_the_field_and_the_workspace(self, user_a, user_b):
        mine = create(user_a)
        create(user_a, instrument="Adams 8180 T")
        create(user_b, customer="B Lab")
        ctx = ToolContext(scope=OWN(user_a.pk), now=timezone.now())
        result = json.loads(
            tools.execute(ctx, "list_opportunities", {"instrument": "Adams 8380 V-lite"}).content
        )
        assert result["total_matching"] == 1
        assert result["opportunities"][0]["ref"] == f"opportunity:{mine.pk}"
        assert result["opportunities"][0]["instrument"] == "Adams 8380 V-lite"
        refused = tools.execute(ctx, "list_opportunities", {"instrument": "Something else"})
        assert refused.is_error
