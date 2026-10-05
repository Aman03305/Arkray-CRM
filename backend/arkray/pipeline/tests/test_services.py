"""Opportunity use cases, called directly (as an import, a job or an Ask Arkray tool would):
the services enforce scope, capabilities, ownership and the stage rules themselves."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from arkray.identity import services as identity_services
from arkray.leads import services as lead_services
from arkray.pipeline import events, services
from arkray.pipeline.models import Opportunity, Pipeline, Stage, StageHistory
from tests.factories import AdminFactory, LeadFactory, OpportunityFactory
from tests.helpers import collected

pytestmark = pytest.mark.django_db

D = Decimal
OWN = AccessScope.own
ORG = AccessScope.organization
FIELDS = {"value": D("1200000")}


def create(actor, scope, lead, **kwargs):
    fields = {**FIELDS, **kwargs.pop("fields", {})}
    return services.create_opportunity(
        actor=actor, scope=scope, lead_id=lead.pk, fields=fields, **kwargs
    ).opportunity


def move(actor, scope, opportunity, stage, version=None, **kwargs):
    # Entering a negotiation stage needs the negotiated price (tested on its own in
    # test_negotiation.py); these tests are about other rules, so they supply one.
    if stage.is_negotiation:
        kwargs.setdefault("negotiated_price", Decimal("1000.00"))
    return services.move_opportunity(
        actor=actor,
        scope=scope,
        opportunity_id=opportunity.pk,
        version=opportunity.version if version is None else version,
        stage_id=stage.pk,
        **kwargs,
    )


def history(opportunity):
    return list(
        StageHistory.objects.filter(opportunity=opportunity)
        .order_by("id")
        .values_list("from_stage_name", "to_stage_name", "from_status", "to_status")
    )


def actions(target_id):
    return list(
        AuditEvent.objects.filter(target_id=str(target_id))
        .order_by("id")
        .values_list("action", flat=True)
    )


class TestCreate:
    def test_defaults_owner_from_the_lead_pipeline_and_first_open_stage(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        opportunity = create(user_a, OWN(user_a.pk), lead)
        assert opportunity.owner_id == user_a.pk
        assert opportunity.created_by_id == user_a.pk
        assert opportunity.stage_id == stages["new"].pk
        assert opportunity.pipeline.is_default
        assert (opportunity.status, opportunity.probability) == ("open", D("10.00"))
        assert opportunity.probability_overridden is False
        assert opportunity.value == D("1200000.00")
        assert opportunity.weighted_value == D("120000.00")
        assert opportunity.closed_at is None
        # Named after the lead (its customer details default to the lead's), no instrument.
        assert opportunity.title == opportunity.customer_name == lead.display_name
        assert history(opportunity) == [("", "New", "", "open")]
        assert actions(opportunity.pk) == ["opportunity.created"]

    def test_an_admin_creating_in_a_users_workspace_creates_it_for_that_user(
        self, admin, user_a, stages
    ):
        lead = LeadFactory(owner=user_a)
        opportunity = create(admin, AccessScope.for_user(admin.pk, user_a.pk), lead)
        assert (opportunity.owner_id, opportunity.created_by_id) == (user_a.pk, admin.pk)
        event = AuditEvent.objects.get(action="opportunity.created")
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)
        assert event.metadata["workspace"] == "user"

    def test_organisation_wide_creation_follows_the_leads_owner(self, admin, user_b, stages):
        lead = LeadFactory(owner=user_b)
        assert create(admin, ORG(admin.pk), lead).owner_id == user_b.pk

    def test_a_lead_outside_the_scope_does_not_exist(self, user_a, user_b, stages):
        with pytest.raises(NotFoundError):
            create(user_a, OWN(user_a.pk), LeadFactory(owner=user_b))
        assert not Opportunity.objects.exists()

    def test_writing_in_someone_elses_workspace_needs_manage_any(self, user_a, user_b, stages):
        """A direct service call with a delegated scope the actor could never have obtained
        from resolve_workspace is still refused by authorize_write."""
        with pytest.raises(PermissionDeniedError):
            create(user_a, AccessScope.for_user(user_a.pk, user_b.pk), LeadFactory(owner=user_b))
        assert not Opportunity.objects.exists()

    def test_in_a_chosen_stage_with_a_probability_override(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        opportunity = create(
            user_a,
            OWN(user_a.pk),
            lead,
            stage_id=stages["proposal"].pk,
            fields={"probability": D("65"), "expected_close_date": date(2026, 12, 31)},
        )
        assert (opportunity.probability, opportunity.probability_overridden) == (D("65"), True)
        assert opportunity.expected_close_date == date(2026, 12, 31)

    def test_asking_for_the_stage_default_is_not_an_override(self, user_a, stages):
        opportunity = create(
            user_a,
            OWN(user_a.pk),
            LeadFactory(owner=user_a),
            stage_id=stages["proposal"].pk,
            fields={"probability": D("50.00")},
        )
        assert opportunity.probability_overridden is False

    def test_created_already_won_is_closed_at_one_hundred_percent(self, user_a, stages):
        with collected(events.OpportunityWon) as won:
            opportunity = create(
                user_a, OWN(user_a.pk), LeadFactory(owner=user_a), stage_id=stages["won"].pk
            )
        assert (opportunity.status, opportunity.probability) == ("won", D("100.00"))
        assert opportunity.closed_at is not None
        assert [e.opportunity_id for e in won] == [opportunity.pk]

    def test_a_closed_stage_has_a_fixed_probability(self, user_a, stages):
        with pytest.raises(InvalidInputError) as error:
            create(
                user_a,
                OWN(user_a.pk),
                LeadFactory(owner=user_a),
                stage_id=stages["lost"].pk,
                fields={"probability": D("20")},
            )
        assert "probability" in error.value.details

    def test_a_lost_reason_only_for_a_lost_stage(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        with pytest.raises(InvalidInputError) as error:
            create(user_a, OWN(user_a.pk), lead, fields={"lost_reason": "Too expensive"})
        assert "lost_reason" in error.value.details
        lost = create(
            user_a,
            OWN(user_a.pk),
            lead,
            stage_id=stages["lost"].pk,
            fields={"lost_reason": "Too expensive"},
        )
        assert lost.lost_reason == "Too expensive"

    @pytest.mark.parametrize(
        ("fields", "field"),
        [
            # The customer name, which the derived title is made of, follows the text rules.
            ({"customer_name": "   "}, "customer_name"),
            ({"customer_name": "x" * 201}, "customer_name"),
            ({"customer_name": "Bad\u202ename"}, "customer_name"),  # a bidi override
            ({"instrument_name": "HbA1c analyser"}, "instrument_name"),  # not on the list
            ({"value": D("-1")}, "value"),
            ({"value": D("1000000000000")}, "value"),
            ({"value": D("1.001")}, "value"),
            ({"value": 1250000.5}, "value"),  # a float: never
            ({"value": D("NaN")}, "value"),
            ({"value": D("Infinity")}, "value"),
            ({"value": True}, "value"),
            ({"probability": D("100.01")}, "probability"),
            ({"probability": D("-0.01")}, "probability"),
            ({"probability": 50.5}, "probability"),
            ({"expected_close_date": date(1999, 12, 31)}, "expected_close_date"),
            ({"expected_close_date": date(2100, 1, 1)}, "expected_close_date"),
            ({"expected_close_date": timezone.now()}, "expected_close_date"),
            ({"description": "x" * 5001}, "description"),
        ],
    )
    def test_invalid_fields(self, user_a, stages, fields, field):
        with pytest.raises(InvalidInputError) as error:
            create(user_a, OWN(user_a.pk), LeadFactory(owner=user_a), fields=fields)
        assert field in error.value.details

    def test_the_value_is_required(self, user_a, stages):
        """The value is the only required field for an existing lead's opportunity: its name
        is derived (naming.py), never asked for."""
        with pytest.raises(InvalidInputError) as error:
            services.create_opportunity(
                actor=user_a, scope=OWN(user_a.pk), lead_id=LeadFactory(owner=user_a).pk, fields={}
            )
        assert set(error.value.details) == {"value"}

    def test_the_title_cant_be_set_it_is_derived(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        with pytest.raises(InvalidInputError) as error:
            create(user_a, OWN(user_a.pk), lead, fields={"title": "Hospital Analyzer Project"})
        assert error.value.details == {"non_field_errors": ["These fields can't be set: title."]}
        assert not Opportunity.objects.exists()
        opportunity = create(
            user_a,
            OWN(user_a.pk),
            lead,
            fields={"customer_name": "City Hospital", "instrument_name": "adams  8180 v"},
        )
        # The instrument in the list's spelling, after the customer and an em dash.
        assert opportunity.instrument_name == "Adams 8180 V"
        assert opportunity.title == "City Hospital — Adams 8180 V"

    def test_system_fields_cant_be_passed(self, user_a, user_b, stages):
        with pytest.raises(InvalidInputError):
            create(user_a, OWN(user_a.pk), LeadFactory(owner=user_a), fields={"owner": user_b.pk})

    def test_an_archived_lead_gets_no_new_opportunities(self, user_a, stages):
        lead = LeadFactory(owner=user_a, archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="archived"):
            create(user_a, OWN(user_a.pk), lead)

    def test_a_deactivated_owner_gets_no_new_opportunities(self, admin, user_a, stages):
        lead = LeadFactory(owner=user_a)
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        with pytest.raises(BusinessRuleViolation, match="deactivated"):
            create(admin, ORG(admin.pk), lead)

    @pytest.mark.parametrize("which", ["inactive_stage", "other_pipeline", "inactive_pipeline"])
    def test_only_active_stages_of_the_chosen_pipeline(self, user_a, stages, which):
        other = Pipeline.objects.create(key="other", name="Other")
        foreign = Stage.objects.create(
            pipeline=other, key="x", name="X", position=1, probability=5, category="open"
        )
        kwargs = {}
        if which == "inactive_stage":
            Stage.objects.filter(pk=stages["qualified"].pk).update(is_active=False)
            kwargs = {"stage_id": stages["qualified"].pk}
        elif which == "other_pipeline":
            kwargs = {"stage_id": foreign.pk}
        else:
            Pipeline.objects.filter(pk=other.pk).update(is_active=False)
            kwargs = {"pipeline_id": other.pk}
        with pytest.raises(InvalidInputError):
            create(user_a, OWN(user_a.pk), LeadFactory(owner=user_a), **kwargs)

    def test_another_pipelines_stage_by_explicit_pipeline(self, user_a, stages):
        other = Pipeline.objects.create(key="other", name="Other")
        first = Stage.objects.create(
            pipeline=other, key="intro", name="Intro", position=1, probability=5, category="open"
        )
        opportunity = create(
            user_a, OWN(user_a.pk), LeadFactory(owner=user_a), pipeline_id=other.pk
        )
        assert (opportunity.pipeline_id, opportunity.stage_id) == (other.pk, first.pk)

    def test_idempotent_create(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        key = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
        first = services.create_opportunity(
            actor=user_a, scope=OWN(user_a.pk), lead_id=lead.pk, fields=FIELDS, idempotency_key=key
        )
        again = services.create_opportunity(
            actor=user_a, scope=OWN(user_a.pk), lead_id=lead.pk, fields=FIELDS, idempotency_key=key
        )
        assert (first.replayed, again.replayed) == (False, True)
        assert first.opportunity.pk == again.opportunity.pk
        assert Opportunity.objects.count() == 1


class TestEdit:
    def edit(self, actor, scope, opportunity, version=None, **changes):
        return services.update_opportunity(
            actor=actor,
            scope=scope,
            opportunity_id=opportunity.pk,
            version=opportunity.version if version is None else version,
            changes=changes,
        )

    def test_changes_fields_and_audits_their_names_only(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        updated = self.edit(
            user_a,
            OWN(user_a.pk),
            opportunity,
            customer_name="Lab upgrade",
            value=D("2500000.50"),
            description="Confidential: CFO wants a discount",
        )
        # A new customer name renames it (the derived title is audited as changed too).
        assert (updated.customer_name, updated.title, updated.value, updated.version) == (
            "Lab upgrade",
            "Lab upgrade",
            D("2500000.50"),
            2,
        )
        event = AuditEvent.objects.get(action="opportunity.updated")
        assert event.metadata == {
            "workspace": "self",
            "fields": ["customer_name", "description", "title", "value"],
        }
        assert "Confidential" not in str(event.metadata)
        assert "Lab upgrade" not in str(event.metadata)

    def test_probability_override_and_back_to_the_stage_default(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        overridden = self.edit(user_a, OWN(user_a.pk), opportunity, probability=D("62.5"))
        assert (overridden.probability, overridden.probability_overridden) == (D("62.5"), True)
        reset = self.edit(user_a, OWN(user_a.pk), overridden, probability=None)
        assert (reset.probability, reset.probability_overridden) == (D("50"), False)
        assert AuditEvent.objects.filter(action="opportunity.updated").count() == 2

    def test_closed_opportunities_keep_their_fixed_probability(self, user_a, stages):
        won = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["won"])
        with pytest.raises(InvalidInputError):
            self.edit(user_a, OWN(user_a.pk), won, probability=D("90"))
        # but a won deal's final amount can be corrected
        assert self.edit(user_a, OWN(user_a.pk), won, value=D("190000")).value == D("190000")

    def test_lost_reason_only_while_lost(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        with pytest.raises(InvalidInputError):
            self.edit(user_a, OWN(user_a.pk), OpportunityFactory(lead=lead), lost_reason="x")
        lost = OpportunityFactory(lead=lead, stage=stages["lost"])
        assert self.edit(user_a, OWN(user_a.pk), lost, lost_reason="Budget").lost_reason == "Budget"

    def test_no_change_is_a_no_op(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), description="Same")
        assert self.edit(user_a, OWN(user_a.pk), opportunity, description="Same").version == 1
        assert not AuditEvent.objects.exists()

    def test_stale_version_conflicts(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), version=3)
        with pytest.raises(ConflictError):
            self.edit(user_a, OWN(user_a.pk), opportunity, version=2, description="Late")

    def test_archived_is_read_only(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="Restore"):
            self.edit(user_a, OWN(user_a.pk), opportunity, description="x")

    @pytest.mark.parametrize(
        "field",
        ["owner", "stage", "status", "closed_at", "lead", "pipeline", "created_by", "title"],
    )
    def test_system_fields_are_not_editable(self, user_a, user_b, stages, field):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        with pytest.raises(InvalidInputError):
            self.edit(user_a, OWN(user_a.pk), opportunity, **{field: user_b.pk})


class TestStageTransitions:
    def test_open_to_open_adopts_the_new_stages_probability(self, user_a, stages):
        opportunity = OpportunityFactory(
            lead=LeadFactory(owner=user_a),
            stage=stages["proposal"],
            probability=D("65"),
            probability_overridden=True,
        )
        moved = move(user_a, OWN(user_a.pk), opportunity, stages["negotiation"])
        assert (moved.stage_id, moved.status, moved.version) == (
            stages["negotiation"].pk,
            "open",
            2,
        )
        assert (moved.probability, moved.probability_overridden) == (D("75"), False)
        assert history(moved) == [("Proposal", "Negotiation", "open", "open")]
        assert actions(moved.pk) == ["opportunity.stage_changed"]
        event = AuditEvent.objects.get(action="opportunity.stage_changed")
        assert event.metadata == {
            "workspace": "self",
            "from": "proposal",
            "to": "negotiation",
            "from_status": "open",
            "to_status": "open",
            "negotiated_price_recorded": True,
        }

    def test_moving_backwards_is_allowed(self, user_a, stages):
        opportunity = OpportunityFactory(
            lead=LeadFactory(owner=user_a), stage=stages["negotiation"]
        )
        assert move(user_a, OWN(user_a.pk), opportunity, stages["new"]).probability == D("10")

    def test_won(self, user_a, stages):
        opportunity = OpportunityFactory(
            lead=LeadFactory(owner=user_a),
            stage=stages["negotiation"],
            probability=D("80"),
            probability_overridden=True,
        )
        with (
            collected(events.OpportunityWon) as won,
            collected(events.OpportunityStageChanged) as changed,
        ):
            closed = move(user_a, OWN(user_a.pk), opportunity, stages["won"])
        assert (closed.status, closed.probability, closed.probability_overridden) == (
            "won",
            D("100"),
            False,
        )
        assert closed.closed_at is not None
        assert Opportunity.objects.filter(pk=closed.pk).exists()  # closed, not removed
        assert actions(closed.pk) == ["opportunity.won"]
        assert [e.opportunity_id for e in won] == [closed.pk]
        assert [(e.from_status, e.to_status) for e in changed] == [("open", "won")]

    def test_lost_with_an_optional_reason(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        with collected(events.OpportunityLost) as lost_events:
            lost = move(
                user_a, OWN(user_a.pk), opportunity, stages["lost"], lost_reason="  Chose a rival  "
            )
        assert (lost.status, lost.probability, lost.lost_reason) == (
            "lost",
            D("0"),
            "Chose a rival",
        )
        assert lost.closed_at is not None
        assert actions(lost.pk) == ["opportunity.lost"]
        assert "rival" not in str(AuditEvent.objects.get(action="opportunity.lost").metadata)
        row = StageHistory.objects.get(opportunity=lost, to_status="lost")
        assert row.lost_reason == "Chose a rival"
        assert len(lost_events) == 1

    def test_a_lost_reason_is_only_for_lost(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        with pytest.raises(InvalidInputError):
            move(user_a, OWN(user_a.pk), opportunity, stages["won"], lost_reason="x")

    def test_reopen_clears_closing_and_restores_the_stage_probability(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        opportunity = OpportunityFactory(lead=lead, stage=stages["proposal"])
        lost = move(user_a, OWN(user_a.pk), opportunity, stages["lost"], lost_reason="Budget")
        reopened = move(user_a, OWN(user_a.pk), lost, stages["negotiation"])
        assert (reopened.status, reopened.probability, reopened.probability_overridden) == (
            "open",
            D("75"),
            False,
        )
        assert (reopened.closed_at, reopened.lost_reason) == (None, "")
        assert history(reopened) == [
            ("Proposal", "Lost", "open", "lost"),
            ("Lost", "Negotiation", "lost", "open"),
        ]
        # The earlier history is untouched, the loss reason kept where it was recorded.
        assert (
            StageHistory.objects.get(opportunity=reopened, to_status="lost").lost_reason == "Budget"
        )
        assert actions(reopened.pk) == ["opportunity.lost", "opportunity.reopened"]

    def test_closed_to_closed_needs_a_reopen_first(self, user_a, stages):
        won = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["won"])
        with pytest.raises(BusinessRuleViolation, match="Reopen"):
            move(user_a, OWN(user_a.pk), won, stages["lost"])

    def test_moving_to_the_current_stage_is_a_no_op_even_with_an_old_version(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), version=4)
        again = move(user_a, OWN(user_a.pk), opportunity, stages["new"], version=1)
        assert again.version == 4
        assert not StageHistory.objects.exists()
        assert not AuditEvent.objects.exists()

    def test_stale_version_conflicts(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), version=2)
        with pytest.raises(ConflictError):
            move(user_a, OWN(user_a.pk), opportunity, stages["proposal"], version=1)

    def test_archived_opportunities_dont_move(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation):
            move(user_a, OWN(user_a.pk), opportunity, stages["proposal"])

    def test_only_active_stages_of_the_same_pipeline(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        other = Pipeline.objects.create(key="other", name="Other")
        foreign = Stage.objects.create(
            pipeline=other, key="x", name="X", position=1, probability=5, category="open"
        )
        with pytest.raises(InvalidInputError):
            move(user_a, OWN(user_a.pk), opportunity, foreign)
        Stage.objects.filter(pk=stages["qualified"].pk).update(is_active=False)
        with pytest.raises(InvalidInputError):
            move(user_a, OWN(user_a.pk), opportunity, stages["qualified"])

    def test_someone_elses_opportunity_does_not_exist(self, user_a, user_b, stages):
        theirs = OpportunityFactory(lead=LeadFactory(owner=user_b))
        with pytest.raises(NotFoundError):
            move(user_a, OWN(user_a.pk), theirs, stages["won"])
        theirs.refresh_from_db()
        assert theirs.status == "open"

    def test_history_survives_renaming_and_retiring_a_stage(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["proposal"])
        move(user_a, OWN(user_a.pk), opportunity, stages["negotiation"])
        Stage.objects.filter(pk=stages["proposal"].pk).update(name="Quotation", is_active=False)
        assert history(opportunity) == [("Proposal", "Negotiation", "open", "open")]

    def test_every_transition_is_one_history_row_and_one_audit_event(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        path = ["qualified", "proposal", "negotiation", "won", "proposal", "lost", "new"]
        for key in path:
            opportunity = move(user_a, OWN(user_a.pk), opportunity, stages[key])
        assert StageHistory.objects.filter(opportunity=opportunity).count() == len(path)
        assert AuditEvent.objects.filter(target_id=str(opportunity.pk)).count() == len(path)
        assert opportunity.version == 1 + len(path)


class TestReopenAfterTheLeadChangedHands:
    def setup_closed_then_reassigned(self, admin, user_a, user_b, stages):
        lead = LeadFactory(owner=user_a)
        won = OpportunityFactory(lead=lead, stage=stages["won"])
        lead_services.reassign_lead(
            actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=1, owner_id=user_b.pk
        )
        won.refresh_from_db()
        assert won.owner_id == user_a.pk  # closed: kept by who won it
        return won

    def test_the_previous_owner_cant_reopen_it_into_someone_elses_hands(
        self, admin, user_a, user_b, stages
    ):
        won = self.setup_closed_then_reassigned(admin, user_a, user_b, stages)
        with pytest.raises(BusinessRuleViolation, match="administrator"):
            move(user_a, OWN(user_a.pk), won, stages["negotiation"])

    def test_an_administrator_reopens_it_for_the_leads_current_owner(
        self, admin, user_a, user_b, stages
    ):
        won = self.setup_closed_then_reassigned(admin, user_a, user_b, stages)
        with collected(events.OpportunityOwnerChanged) as changed:
            reopened = move(admin, ORG(admin.pk), won, stages["negotiation"])
        assert (reopened.status, reopened.owner_id) == ("open", user_b.pk)
        assert [e.reason for e in changed] == ["reopened"]
        event = AuditEvent.objects.get(action="opportunity.owner_changed")
        assert event.metadata["reason"] == "reopened"
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)


class TestArchive:
    def test_archive_and_restore_are_audited_and_idempotent(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
        scope = OWN(user_a.pk)
        archived = services.archive_opportunity(
            actor=user_a, scope=scope, opportunity_id=opportunity.pk, version=1
        )
        assert archived.archived_at is not None
        again = services.archive_opportunity(
            actor=user_a, scope=scope, opportunity_id=opportunity.pk, version=1
        )
        assert again.version == archived.version == 2
        restored = services.restore_opportunity(
            actor=user_a, scope=scope, opportunity_id=opportunity.pk, version=2
        )
        assert (restored.archived_at, restored.version) == (None, 3)
        assert actions(opportunity.pk) == ["opportunity.archived", "opportunity.restored"]

    def test_closed_is_not_archived(self, user_a, stages):
        won = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stages["won"])
        archived = services.archive_opportunity(
            actor=user_a, scope=OWN(user_a.pk), opportunity_id=won.pk, version=1
        )
        assert (archived.status, archived.archived_at is not None) == ("won", True)

    def test_stale_version(self, user_a, stages):
        opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), version=2)
        with pytest.raises(ConflictError):
            services.archive_opportunity(
                actor=user_a, scope=OWN(user_a.pk), opportunity_id=opportunity.pk, version=1
            )


def test_owners_of_new_opportunities_follow_the_lead(user_a, user_b, stages):
    """An owner is named only for a new customer record (ADR-0027, test_customer_record.py),
    never for an existing lead's opportunity, and is never an editable field."""
    assert "owner" not in services.validation.EDITABLE_FIELDS
    with pytest.raises(InvalidInputError) as caught:
        create(user_a, OWN(user_a.pk), LeadFactory(owner=user_a), owner_id=user_b.pk)
    assert caught.value.details == {"owner": [services.OWNER_FOLLOWS_LEAD]}


def test_created_by_is_the_actor_even_organisation_wide(user_b, stages):
    admin = AdminFactory()
    opportunity = create(admin, ORG(admin.pk), LeadFactory(owner=user_b))
    assert (opportunity.created_by_id, opportunity.owner_id) == (admin.pk, user_b.pk)
