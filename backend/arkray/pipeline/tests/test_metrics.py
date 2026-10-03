"""The pipeline's money figures are exact (docs/pipeline.md#money).

Pipeline value = SUM(value) and weighted pipeline = SUM(value x probability / 100), over
OPEN, non-archived opportunities in the caller's scope, computed by PostgreSQL on NUMERIC
and rounded once to paise (half away from zero). No float anywhere.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest
from django.utils import timezone

from arkray.core.access import AccessScope
from arkray.pipeline import metrics, selectors
from arkray.pipeline.models import MAX_VALUE, Opportunity
from arkray.pipeline.selectors import OpportunityFilters, PipelineTotals
from tests.factories import LeadFactory, OpportunityFactory

from .conftest import board_url, summary_url

pytestmark = pytest.mark.django_db

D = Decimal
NO_FILTERS = OpportunityFilters()


def totals(user) -> PipelineTotals:
    return selectors.pipeline_totals(AccessScope.own(user.pk), NO_FILTERS)


def opportunity(user, stages, stage, value, probability=None, **extra):
    lead = extra.pop("lead", None) or LeadFactory(owner=user)
    overrides = {}
    if probability is not None and D(probability) != stages[stage].probability:
        overrides = {"probability": D(probability), "probability_overridden": True}
    return OpportunityFactory(lead=lead, stage=stages[stage], value=D(value), **overrides, **extra)


class TestTheBriefsWorkedExample:
    def test_open_opportunities_only(self, user_a, stages):
        opportunity(user_a, stages, "proposal", "1000000", 50)  # A
        opportunity(user_a, stages, "negotiation", "500000", 80)  # B (override)
        opportunity(user_a, stages, "won", "200000")  # C: won, never in the open pipeline
        assert totals(user_a) == PipelineTotals(D("1500000.00"), D("900000.00"), 2)

    def test_the_api_reports_them_as_exact_decimal_strings(self, user_a_client, user_a, stages):
        opportunity(user_a, stages, "proposal", "1000000", 50)
        opportunity(user_a, stages, "negotiation", "500000", 80)
        opportunity(user_a, stages, "won", "200000")
        body = user_a_client.get(summary_url()).json()
        assert body == {
            "currency": "INR",
            "totals": {
                "pipeline_value": "1500000.00",
                "weighted_pipeline": "900000.00",
                "open_count": 2,
            },
        }
        board = user_a_client.get(board_url()).json()
        assert board["totals"] == body["totals"]
        won = next(c for c in board["columns"] if c["stage"]["key"] == "won")
        assert (won["count"], won["total_value"], won["weighted_value"]) == (
            1,
            "200000.00",
            "200000.00",
        )

    def test_one_million_at_fifty_percent_is_half_a_million(self, user_a, stages):
        """The requirements' own example (docs/database.md#aggregate-queries)."""
        opportunity(user_a, stages, "proposal", "1000000", 50)
        assert totals(user_a) == PipelineTotals(D("1000000.00"), D("500000.00"), 1)


class TestWhatCounts:
    def test_nothing_is_zero_not_null(self, user_a):
        assert totals(user_a) == PipelineTotals(D("0.00"), D("0.00"), 0)

    def test_a_zero_value_opportunity_counts_but_adds_nothing(self, user_a, stages):
        opportunity(user_a, stages, "new", "0")
        assert totals(user_a) == PipelineTotals(D("0.00"), D("0.00"), 1)

    def test_lost_won_and_archived_never_count(self, user_a, stages):
        opportunity(user_a, stages, "lost", "700000")
        opportunity(user_a, stages, "won", "300000")
        opportunity(user_a, stages, "new", "400000", archived_at=timezone.now())
        opportunity(user_a, stages, "qualified", "100000")
        assert totals(user_a) == PipelineTotals(D("100000.00"), D("25000.00"), 1)

    def test_archived_opportunities_cant_even_be_asked_for(self, user_a):
        with pytest.raises(ValueError, match="Archived"):
            selectors.pipeline_totals(AccessScope.own(user_a.pk), OpportunityFilters(archived=True))


class TestExactness:
    def test_very_large_allowed_values_add_up_exactly(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        for _ in range(25):
            opportunity(user_a, stages, "negotiation", MAX_VALUE, lead=lead)
        expected_value = MAX_VALUE * 25
        expected_weighted = (MAX_VALUE * D("75") * D("0.01") * 25).quantize(D("0.01"))
        assert totals(user_a) == PipelineTotals(expected_value, expected_weighted, 25)
        assert str(expected_value) == "24999999999999.75"  # 25 x ₹999,999,999,999.99

    def test_paise_survive(self, user_a, stages):
        opportunity(user_a, stages, "proposal", "1250000.55", 50)
        opportunity(user_a, stages, "proposal", "0.45", 50)
        assert totals(user_a) == PipelineTotals(D("1250001.00"), D("625000.50"), 2)

    @pytest.mark.parametrize(
        ("value", "probability", "weighted"),
        [
            ("0.01", "50", "0.01"),  # 0.005 -> rounds half away from zero
            ("0.01", "49.99", "0.00"),  # 0.004999
            ("0.03", "50", "0.02"),  # 0.015 -> 0.02
            ("12345.67", "33.33", "4114.81"),  # 4114.811811
            ("999999999999.99", "0.01", "100000000.00"),  # 99999999.999999 -> up
            ("100", "12.5", "12.50"),
            ("1", "0.5", "0.01"),  # 0.005
            ("1", "0.49", "0.00"),
        ],
    )
    def test_rounding_boundaries_of_one_opportunity(
        self, user_a, stages, value, probability, weighted
    ):
        created = opportunity(user_a, stages, "proposal", value, probability)
        row = selectors.opportunity_list(AccessScope.own(user_a.pk), NO_FILTERS).get(pk=created.pk)
        assert row.weighted_value == D(weighted)
        assert metrics.weighted_value(D(value), D(probability)) == D(weighted)
        assert totals(user_a).weighted_pipeline == D(weighted)

    def test_a_total_is_rounded_once_not_summed_from_rounded_rows(self, user_a, stages):
        """Three opportunities of ₹0.03 at 50 %: each is ₹0.015 exactly. Rounded one by one
        they would add up to ₹0.06; the exact sum ₹0.045 is rounded once: ₹0.05."""
        lead = LeadFactory(owner=user_a)
        for _ in range(3):
            opportunity(user_a, stages, "proposal", "0.03", 50, lead=lead)
        rows = selectors.opportunity_list(AccessScope.own(user_a.pk), NO_FILTERS)
        assert [r.weighted_value for r in rows] == [D("0.02")] * 3
        assert totals(user_a).weighted_pipeline == D("0.05")

    def test_sql_and_python_agree_on_a_thousand_random_amounts(self, user_a, stages):
        rng = random.Random(20260930)  # deterministic
        lead = LeadFactory(owner=user_a)
        cases = []
        for _ in range(1000):
            value = D(rng.randint(0, 99_999_999_999_999)) / 100
            probability = D(rng.randint(0, 10_000)) / 100
            cases.append((value, probability))
        Opportunity.objects.bulk_create(
            [
                OpportunityFactory.build(
                    lead=lead,
                    stage=stages["proposal"],
                    value=value,
                    probability=probability,
                    probability_overridden=probability != D("50"),
                )
                for value, probability in cases
            ]
        )
        rows = selectors.opportunity_list(AccessScope.own(user_a.pk), NO_FILTERS)
        by_pair = {(r.value, r.probability): r.weighted_value for r in rows}
        for value, probability in cases:
            assert by_pair[(value, probability)] == metrics.weighted_value(value, probability)
        exact = sum((v * p * D("0.01") for v, p in cases), D(0))
        assert totals(user_a) == PipelineTotals(
            sum((v for v, _ in cases), D(0)),
            exact.quantize(D("0.01"), rounding=ROUND_HALF_UP),
            1000,
        )

    def test_python_refuses_floats(self):
        with pytest.raises(TypeError, match="never float"):
            metrics.weighted_value(1000000.0, D("50"))
        with pytest.raises(TypeError, match="never float"):
            metrics.weighted_value(D("1000000"), 50.0)


class TestFiltersNarrowTheTotals:
    def test_expected_close_and_probability_filters_apply_to_the_totals(self, user_a, stages):
        opportunity(user_a, stages, "proposal", "100", expected_close_date=date(2026, 10, 15))
        opportunity(user_a, stages, "negotiation", "200", expected_close_date=date(2026, 12, 1))
        opportunity(user_a, stages, "new", "400")  # undated
        scope = AccessScope.own(user_a.pk)
        october = OpportunityFilters(
            expected_close_from=date(2026, 10, 1), expected_close_to=date(2026, 10, 31)
        )
        assert selectors.pipeline_totals(scope, october) == PipelineTotals(
            D("100.00"), D("50.00"), 1
        )
        likely = OpportunityFilters(probability_min=D("60"))
        assert selectors.pipeline_totals(scope, likely) == PipelineTotals(
            D("200.00"), D("150.00"), 1
        )
