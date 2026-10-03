"""Lead reassignment moves the lead's OPEN opportunities with it, in the same transaction
(docs/pipeline.md#ownership). Won and lost opportunities keep the owner who closed them;
historical attribution (created_by, audit actors, stage-history actors) never changes."""

from __future__ import annotations

import pytest
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.core.errors import NotFoundError
from arkray.leads import services as lead_services
from arkray.pipeline import events, selectors, services
from arkray.pipeline.models import Opportunity, StageHistory
from arkray.pipeline.selectors import OpportunityFilters
from tests.factories import LeadFactory
from tests.helpers import collected

pytestmark = pytest.mark.django_db

ORG = AccessScope.organization
OWN = AccessScope.own


@pytest.fixture
def world(admin, user_a, user_b, stages):
    """The brief's scenario: a lead of A with two open, one won and one lost opportunity,
    all created (and closed) by A through the services."""
    lead = LeadFactory(owner=user_a)
    scope = OWN(user_a.pk)

    def new(title, stage=None):
        result = services.create_opportunity(
            actor=user_a,
            scope=scope,
            lead_id=lead.pk,
            fields={"title": title, "value": 100000},
            stage_id=stage.pk if stage else None,
        )
        return result.opportunity

    open_1, open_2 = new("Open 1"), new("Open 2", stages["proposal"])
    won = services.move_opportunity(
        actor=user_a,
        scope=scope,
        opportunity_id=new("Won").pk,
        version=1,
        stage_id=stages["won"].pk,
    )
    lost = services.move_opportunity(
        actor=user_a,
        scope=scope,
        opportunity_id=new("Lost").pk,
        version=1,
        stage_id=stages["lost"].pk,
    )
    return lead, open_1, open_2, won, lost


def reassign(admin, lead, to):
    lead.refresh_from_db()
    return lead_services.reassign_lead(
        actor=admin, scope=ORG(admin.pk), lead_id=lead.pk, version=lead.version, owner_id=to.pk
    )


def owners(*opportunities):
    return [Opportunity.objects.get(pk=o.pk).owner_id for o in opportunities]


def test_open_opportunities_follow_closed_ones_stay(world, admin, user_a, user_b):
    lead, open_1, open_2, won, lost = world
    audit_before = list(AuditEvent.objects.values_list("id", "actor_id", "action"))
    with collected(events.OpportunityOwnerChanged) as changed:
        moved_lead = reassign(admin, lead, user_b)

    assert moved_lead.owner_id == user_b.pk
    assert owners(open_1, open_2) == [user_b.pk, user_b.pk]
    assert owners(won, lost) == [user_a.pk, user_a.pk]
    # Versions: the moved ones changed (a client holding the old version gets a 409).
    assert Opportunity.objects.get(pk=open_1.pk).version == open_1.version + 1
    assert Opportunity.objects.get(pk=won.pk).version == won.version
    # Historical attribution is untouched.
    assert set(Opportunity.objects.values_list("created_by_id", flat=True)) == {user_a.pk}
    assert set(StageHistory.objects.values_list("actor_id", flat=True)) == {user_a.pk}
    assert (
        list(
            AuditEvent.objects.filter(id__in=[a[0] for a in audit_before]).values_list(
                "id", "actor_id", "action"
            )
        )
        == audit_before
    )
    # The move itself is audited per opportunity, by the admin, about A's workspace.
    moves = AuditEvent.objects.filter(action="opportunity.owner_changed").order_by("target_id")
    assert sorted(m.target_id for m in moves) == sorted([str(open_1.pk), str(open_2.pk)])
    for event in moves:
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)
        assert event.metadata == {
            "from_owner_id": str(user_a.pk),
            "to_owner_id": str(user_b.pk),
            "lead_id": str(lead.pk),
            "reason": "lead_reassigned",
        }
    assert sorted(e.opportunity_id for e in changed) == sorted([open_1.pk, open_2.pk])


def test_what_each_user_sees_afterwards(world, admin, user_a, user_b):
    lead, open_1, open_2, won, lost = world
    reassign(admin, lead, user_b)
    a, b = OWN(user_a.pk), OWN(user_b.pk)
    with pytest.raises(NotFoundError):
        selectors.opportunity_detail(a, open_1.pk)
    assert selectors.opportunity_detail(b, open_1.pk).owner_id == user_b.pk
    # A keeps what A closed, but no longer sees the lead through it.
    assert selectors.opportunity_detail(a, won.pk).lead.owner_id == user_b.pk
    listed_a = {o.pk for o in selectors.opportunity_list(a, OpportunityFilters(lead_id=lead.pk))}
    listed_b = {o.pk for o in selectors.opportunity_list(b, OpportunityFilters(lead_id=lead.pk))}
    assert listed_a == {won.pk, lost.pk}
    assert listed_b == {open_1.pk, open_2.pk}
    assert selectors.pipeline_totals(a, OpportunityFilters()).open_count == 0
    assert selectors.pipeline_totals(b, OpportunityFilters()).open_count == 2


def test_archived_open_opportunities_move_too(world, admin, user_a, user_b):
    lead, open_1, *_ = world
    Opportunity.objects.filter(pk=open_1.pk).update(archived_at=timezone.now())
    reassign(admin, lead, user_b)
    assert owners(open_1) == [user_b.pk]


def test_a_failure_while_moving_them_rolls_the_whole_reassignment_back(
    world, admin, user_a, user_b
):
    lead, open_1, open_2, won, lost = world

    def fail(_event):
        raise RuntimeError("could not move an opportunity")

    with subscribed(events.OpportunityOwnerChanged, fail), pytest.raises(RuntimeError):
        reassign(admin, lead, user_b)
    lead.refresh_from_db()
    assert lead.owner_id == user_a.pk
    assert owners(open_1, open_2, won, lost) == [user_a.pk] * 4
    assert not AuditEvent.objects.filter(
        action__in=["lead.reassigned", "opportunity.owner_changed"]
    ).exists()


def test_back_and_forth(world, admin, user_a, user_b):
    lead, open_1, open_2, *_ = world
    reassign(admin, lead, user_b)
    reassign(admin, lead, user_a)
    assert owners(open_1, open_2) == [user_a.pk, user_a.pk]
    assert AuditEvent.objects.filter(action="opportunity.owner_changed").count() == 4


def test_reassigning_to_the_current_owner_moves_nothing(world, admin, user_a):
    lead, *_ = world
    reassign(admin, lead, user_a)
    assert not AuditEvent.objects.filter(action="opportunity.owner_changed").exists()


def test_the_lead_has_no_open_opportunities(admin, user_a, user_b, stages):
    lead = LeadFactory(owner=user_a)
    reassign(admin, lead, user_b)
    assert not AuditEvent.objects.filter(action="opportunity.owner_changed").exists()
