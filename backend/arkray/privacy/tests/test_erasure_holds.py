"""An erasure leaves the Ask Arkray conversations a legal hold on someone else keeps (deep
security audit M-5; ai.subjects.held_conversations): a held user's, and one that also
touches another held customer. The rest of the person's data is erased all the same, and a
ledger replay applies the same rule (holds restored with a backup are the current state),
except that a hold on the replayed lead itself never keeps its conversations."""

from __future__ import annotations

from typing import Any

import pytest

from arkray.ai.models import Conversation
from arkray.audit.models import AuditEvent
from arkray.core.models import HoldSubject, LegalHold
from arkray.leads.models import Lead
from arkray.privacy import services
from arkray.privacy.tests.test_erasure import asked
from tests.factories import LeadFactory, OpportunityFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ai_on")]


def hold(subject_type: HoldSubject, subject_id: Any, admin: Any) -> LegalHold:
    return LegalHold.objects.create(
        subject_type=subject_type, subject_id=subject_id, reference="CASE-7", placed_by=admin.pk
    )


@pytest.fixture
def world(user_a, user_b, admin):
    person = LeadFactory(owner=user_a, first_name="Rahul", last_name="Sharma")
    deal = OpportunityFactory(lead=person, stage_key="new")
    other = LeadFactory(owner=user_a, first_name="Meera", last_name="Iyer")
    other_deal = OpportunityFactory(lead=other, stage_key="new")
    hold(HoldSubject.LEAD, other.pk, admin)
    hold(HoldSubject.USER, user_b.pk, admin)

    def cites(*ids: Any) -> dict[str, Any]:
        return {"citations": [{"ref": f"opportunity:{pk}", "snippet": "a deal"} for pk in ids]}

    return {
        "person": person,
        # Only about the person: erased.
        "only": asked(user_a, "What did Rahul Sharma ask?", cites(deal.pk)).conversation_id,
        # About the person and a held customer: kept, for the other matter.
        "both": asked(user_a, "Compare them", cites(deal.pk, other_deal.pk)).conversation_id,
        # A held user's: kept.
        "held_user": asked(user_b, "Rahul Sharma's lab?", cites(deal.pk)).conversation_id,
    }


def remaining(world: dict[str, Any]) -> set[str]:
    left = set(Conversation.objects.values_list("pk", flat=True))
    return {key for key in ("only", "both", "held_user") if world[key] in left}


def test_conversations_a_hold_keeps_survive_the_erasure(world, admin):
    assert services.preview(world["person"].pk).conversations == 1
    done = services.erase(world["person"].pk, operator_id=admin.pk)
    assert done.conversations == 1
    assert remaining(world) == {"both", "held_user"}
    event = AuditEvent.objects.get(action="lead.erased", target_id=str(world["person"].pk))
    assert event.metadata["conversations_held"] == 2
    # The rest of the person's data is erased all the same.
    assert Lead.objects.get(pk=world["person"].pk).first_name == "[erased]"


def test_a_replay_applies_the_same_rule_but_not_a_hold_on_the_lead_itself(world, admin):
    # A backup restored from before the erasure brought back a (since released) hold on the
    # person: their erasure was authorised, so it doesn't keep their conversations.
    hold(HoldSubject.LEAD, world["person"].pk, admin)
    services.erase(world["person"].pk, operator_id=admin.pk, replay=True)
    assert remaining(world) == {"both", "held_user"}


def test_without_holds_every_conversation_touching_the_person_goes(world, admin):
    LegalHold.objects.all().delete()
    services.erase(world["person"].pk, operator_id=admin.pk)
    assert remaining(world) == set()
