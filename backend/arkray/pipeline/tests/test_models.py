"""Database-level invariants of the pipeline tables: they hold even for writes that bypass
the services (raw SQL, bulk updates, a future bug)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from arkray.core.errors import AppendOnlyViolation
from arkray.leads.models import Lead
from arkray.pipeline.models import Pipeline, Stage, StageCategory, StageHistory
from tests.factories import LeadFactory, OpportunityFactory, UserFactory

pytestmark = pytest.mark.django_db


def violates(constraint: str, sql: str, params=()) -> None:
    """The statement fails on `constraint`, checked immediately (foreign keys are otherwise
    checked at commit, and tests never commit)."""
    with (  # noqa: PT012 — the statement under test needs the preceding SET
        pytest.raises(IntegrityError, match=constraint),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(sql, params)


class TestSeededPipeline:
    def test_one_default_sales_pipeline_with_six_ordered_stages(self, pipeline):
        assert (pipeline.key, pipeline.name) == ("sales", "Sales Pipeline")
        assert pipeline.is_active
        rows = list(
            pipeline.stages.order_by("position").values_list(
                "key", "name", "probability", "category"
            )
        )
        assert rows == [
            ("new", "New", Decimal("10.00"), "open"),
            ("qualified", "Qualified", Decimal("25.00"), "open"),
            ("proposal", "Proposal", Decimal("50.00"), "open"),
            ("negotiation", "Negotiation", Decimal("75.00"), "open"),
            ("won", "Won", Decimal("100.00"), "won"),
            ("lost", "Lost", Decimal("0.00"), "lost"),
        ]

    def test_nothing_assumes_a_single_pipeline(self, pipeline):
        other = Pipeline.objects.create(key="service", name="Service Contracts")
        Stage.objects.create(
            pipeline=other, key="new", name="New", position=10, probability=5, category="open"
        )
        assert Pipeline.objects.count() == 2
        assert Stage.objects.filter(key="new").count() == 2  # keys are per pipeline

    def test_only_one_default_pipeline(self, pipeline):
        with pytest.raises(IntegrityError, match="pipeline_pipeline_one_default"):
            Pipeline.objects.create(key="other", name="Other", is_default=True)


class TestStageConstraints:
    def stage(self, pipeline, **overrides):
        values = {
            "pipeline": pipeline,
            "key": "demo",
            "name": "Demo",
            "position": 99,
            "probability": Decimal("40"),
            "category": "open",
            **overrides,
        }
        return Stage(**values)

    @pytest.mark.parametrize(
        ("overrides", "constraint"),
        [
            ({"category": "won", "probability": Decimal("90")}, "pipeline_stage_won_is_certain"),
            ({"category": "lost", "probability": Decimal("5")}, "lost_is_impossible"),
            ({"probability": Decimal("100.01")}, "pipeline_stage_probability_range"),
            ({"probability": Decimal("-1")}, "pipeline_stage_probability_range"),
            ({"category": "pending"}, "pipeline_stage_category_valid"),
            ({"key": "Bad Key"}, "pipeline_stage_key_format"),
            ({"name": ""}, "pipeline_stage_name_present"),
            ({"key": "new"}, "pipeline_stage_key_unique"),
            ({"name": "PROPOSAL"}, "pipeline_stage_active_name_unique"),
        ],
    )
    def test_invalid_stages_are_refused(self, pipeline, overrides, constraint):
        with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
            self.stage(pipeline, **overrides).save()

    def test_positions_are_unique_per_pipeline_but_can_be_swapped_in_one_transaction(self, stages):
        with (  # noqa: PT012
            pytest.raises(IntegrityError, match="pipeline_stage_position_unique"),
            transaction.atomic(),
        ):
            Stage.objects.filter(pk=stages["new"].pk).update(position=20)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with transaction.atomic():  # a reorder: checked at commit, so a swap is fine
            Stage.objects.filter(pk=stages["new"].pk).update(position=20)
            Stage.objects.filter(pk=stages["qualified"].pk).update(position=10)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        order = list(Stage.objects.filter(pipeline=stages["new"].pipeline).order_by("position"))
        assert [s.key for s in order[:2]] == ["qualified", "new"]

    def test_a_retired_stage_name_can_be_reused(self, pipeline, stages):
        Stage.objects.filter(pk=stages["proposal"].pk).update(is_active=False)
        self.stage(pipeline, key="proposal_v2", name="Proposal").save()

    def test_a_stage_in_use_cant_change_its_category_or_pipeline(self, stages):
        OpportunityFactory(stage=stages["negotiation"])
        violates(
            "pipeline_opportunity_stage_category_fk",
            "UPDATE pipeline_stage SET category='won', probability=100 WHERE id=%s",
            [stages["negotiation"].pk],
        )
        other = Pipeline.objects.create(key="other", name="Other")
        violates(
            "pipeline_opportunity_stage_category_fk",
            "UPDATE pipeline_stage SET pipeline_id=%s WHERE id=%s",
            [other.pk, stages["negotiation"].pk],
        )

    def test_renaming_or_repricing_a_stage_in_use_rewrites_nothing(self, stages):
        opportunity = OpportunityFactory(stage=stages["proposal"])
        Stage.objects.filter(pk=stages["proposal"].pk).update(
            name="Quotation", probability=Decimal("60")
        )
        opportunity.refresh_from_db()
        assert (opportunity.stage_id, opportunity.probability) == (
            stages["proposal"].pk,
            Decimal("50.00"),
        )


class TestOpportunityConstraints:
    def test_status_always_equals_the_stage_category(self, stages):
        opportunity = OpportunityFactory(stage=stages["new"])
        violates(
            "pipeline_opportunity_stage_category_fk",
            "UPDATE pipeline_opportunity SET status='won', probability=100, closed_at=now() "
            "WHERE id=%s",
            [opportunity.pk],
        )

    def test_the_pipeline_is_the_stages_pipeline(self, stages):
        opportunity = OpportunityFactory(stage=stages["new"])
        other = Pipeline.objects.create(key="other", name="Other")
        violates(
            "pipeline_opportunity_stage_category_fk",
            "UPDATE pipeline_opportunity SET pipeline_id=%s WHERE id=%s",
            [other.pk, opportunity.pk],
        )

    def test_an_open_opportunity_is_owned_by_its_leads_owner(self, stages):
        opportunity = OpportunityFactory(stage=stages["new"])
        stranger = UserFactory()
        violates(
            "pipeline_opportunity_open_owner_fk",
            "UPDATE pipeline_opportunity SET owner_id=%s WHERE id=%s",
            [stranger.pk, opportunity.pk],
        )
        # ... and reassigning the lead alone (without moving it) can't commit either.
        violates(
            "pipeline_opportunity_open_owner_fk",
            "UPDATE leads_lead SET owner_id=%s WHERE id=%s",
            [stranger.pk, opportunity.lead_id],
        )

    def test_a_closed_opportunity_keeps_its_historical_owner(self, stages):
        opportunity = OpportunityFactory(stage=stages["won"])
        newcomer = UserFactory()
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(
                "UPDATE leads_lead SET owner_id=%s WHERE id=%s", [newcomer.pk, opportunity.lead_id]
            )
        opportunity.refresh_from_db()
        assert opportunity.open_owner_id is None
        assert opportunity.owner_id != newcomer.pk

    def test_reopening_under_the_wrong_owner_is_refused(self, stages):
        opportunity = OpportunityFactory(stage=stages["won"])
        Lead.objects.filter(pk=opportunity.lead_id).update(owner=UserFactory())
        violates(
            "pipeline_opportunity_open_owner_fk",
            "UPDATE pipeline_opportunity SET stage_id=%s, status='open', probability=10, "
            "closed_at=NULL WHERE id=%s",
            [stages["new"].pk, opportunity.pk],
        )

    @pytest.mark.parametrize(
        ("sql", "constraint"),
        [
            ("SET value=-0.01", "pipeline_opp_value_non_negative"),
            ("SET value=1000000000000", "numeric field overflow"),
            ("SET probability=100.01", "pipeline_opp_probability_range"),
            ("SET title=''", "pipeline_opp_title_present"),
            ("SET closed_at=now()", "pipeline_opp_closed_at_matches_status"),
            ("SET lost_reason='Price'", "pipeline_opp_lost_reason_only_when_lost"),
            ("SET expected_close_date='1999-12-31'", "pipeline_opp_expected_close_range"),
            ("SET expected_close_date='2100-01-01'", "pipeline_opp_expected_close_range"),
            ("SET version=0", "pipeline_opp_version_positive"),
        ],
    )
    def test_invalid_open_opportunities_are_refused(self, stages, sql, constraint):
        opportunity = OpportunityFactory(stage=stages["new"])
        with pytest.raises(DatabaseError, match=constraint), transaction.atomic():  # noqa: SIM117
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE pipeline_opportunity {sql} WHERE id=%s", [opportunity.pk])

    @pytest.mark.parametrize(
        ("stage_key", "sql", "constraint"),
        [
            ("won", "SET probability=99", "pipeline_opp_won_is_certain"),
            ("lost", "SET probability=1", "pipeline_opp_lost_is_impossible"),
            ("won", "SET closed_at=NULL", "pipeline_opp_closed_at_matches_status"),
            ("won", "SET probability_overridden=true", "pipeline_opp_override_only_while_open"),
            ("won", "SET lost_reason='x'", "pipeline_opp_lost_reason_only_when_lost"),
        ],
    )
    def test_closed_opportunities_have_fixed_semantics(self, stages, stage_key, sql, constraint):
        opportunity = OpportunityFactory(stage=stages[stage_key])
        with pytest.raises(DatabaseError, match=constraint), transaction.atomic():  # noqa: SIM117
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE pipeline_opportunity {sql} WHERE id=%s", [opportunity.pk])

    def test_generated_sort_keys(self, stages):
        undated = OpportunityFactory(stage=stages["new"])
        undated.refresh_from_db()
        assert str(undated.expected_close_sort) == "9999-12-31"
        assert undated.closed_sort.year == 1900
        won = OpportunityFactory(stage=stages["won"])
        won.refresh_from_db()
        assert won.closed_sort == won.closed_at

    def test_money_is_never_a_float(self, stages):
        opportunity = OpportunityFactory(stage=stages["new"], value=Decimal("1250000.50"))
        opportunity.refresh_from_db()
        assert isinstance(opportunity.value, Decimal)
        assert opportunity.value == Decimal("1250000.50")

    def test_opportunities_and_their_leads_are_never_deleted(self, stages):
        opportunity = OpportunityFactory(stage=stages["new"])
        with pytest.raises(IntegrityError), transaction.atomic():
            opportunity.lead.delete()
        with pytest.raises(IntegrityError), transaction.atomic():
            stages["new"].delete()


class TestStageHistoryIsAppendOnly:
    def history(self, stages):
        opportunity = OpportunityFactory(stage=stages["new"])
        return StageHistory.objects.create(
            opportunity=opportunity,
            to_stage=stages["new"],
            to_stage_name="New",
            to_status=StageCategory.OPEN,
            value=opportunity.value,
            probability=opportunity.probability,
            actor=opportunity.owner,
        )

    def test_the_orm_refuses_changes(self, stages):
        row = self.history(stages)
        with pytest.raises(AppendOnlyViolation):
            row.save()
        with pytest.raises(AppendOnlyViolation):
            row.delete()
        with pytest.raises(AppendOnlyViolation):
            StageHistory.objects.filter(pk=row.pk).update(to_stage_name="Edited")
        with pytest.raises(AppendOnlyViolation):
            StageHistory.objects.filter(pk=row.pk).delete()

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE pipeline_stage_history SET to_stage_name='Edited' WHERE id=%s",
            "DELETE FROM pipeline_stage_history WHERE id=%s",
        ],
    )
    def test_the_database_refuses_changes_even_from_raw_sql(self, stages, sql):
        row = self.history(stages)
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():  # noqa: SIM117
            with connection.cursor() as cursor:
                cursor.execute(sql, [row.pk])

    def test_a_row_is_either_a_creation_or_a_complete_transition(self, stages):
        opportunity = OpportunityFactory(stage=stages["new"])
        with pytest.raises(IntegrityError, match="pipeline_history_from_complete"):
            StageHistory.objects.create(
                opportunity=opportunity,
                from_stage=stages["new"],  # a from-stage without its name and status
                to_stage=stages["proposal"],
                to_stage_name="Proposal",
                to_status="open",
                value=1,
                probability=50,
                actor=opportunity.owner,
                occurred_at=timezone.now(),
            )


def test_opportunity_str_never_contains_the_title(stages):
    opportunity = OpportunityFactory(stage=stages["new"], title="Secret Hospital Deal")
    assert "Secret" not in str(opportunity)


def test_leads_have_an_id_owner_key_for_the_ownership_foreign_key():
    lead = LeadFactory()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = 'leads_lead_id_owner_key' "
            "AND conrelid = 'leads_lead'::regclass"
        )
        assert cursor.fetchone() == (1,)
    assert lead.pk
