"""An erasure can't be undone through the CRM, and the erasure tool removes whatever was typed
back in (deep security audit M-1; docs/privacy.md#erasure).

The erased lead is marked (`Lead.erased_at`): it can't be restored, edited or moved to another
status, nothing new is added for it, and the personal text of its deals and activities (the
fields an erasure redacts) can't be written again: 422 `lead_erased`, with nothing written,
versioned or audited. Everything that isn't the person's data (value, stage moves without
free text, task priority) still changes. Text re-entered before this rule (or by a previous
release) is removed by running the erasure again, which is refused only once nothing is left.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.db import connection

from arkray.activities.models import Activity
from arkray.audit.models import AuditEvent
from arkray.core import ledger
from arkray.leads.models import Lead
from arkray.pipeline.models import (
    CustomField,
    FieldType,
    NegotiationPrice,
    NegotiationSource,
    Opportunity,
    StageHistory,
)
from arkray.privacy import services as privacy
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    default_stage,
)
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

ERASED = "[erased]"
WS = "/api/v1/workspaces/me"


@pytest.fixture
def erased(user_a, admin):
    lead = LeadFactory(
        owner=user_a, first_name="Rahul", last_name="Sharma", email="rahul@clinic.example"
    )
    deal = OpportunityFactory(
        lead=lead,
        stage_key="new",
        customer_name="Rahul Sharma",
        account_name="Sharma Diagnostics",
        contact_email="rahul@clinic.example",
        value=Decimal("500000.00"),
    )
    negotiating = OpportunityFactory(lead=lead, stage_key="negotiation", customer_name="Rahul")
    text_field = CustomField.objects.create(
        pipeline=deal.pipeline, name="Lab contact", field_type=FieldType.TEXT, position=90
    )
    task = TaskFactory(lead=lead, opportunity=deal, title="Call Rahul")
    meeting = MeetingFactory(lead=lead, location="Koregaon office")
    written = NoteFactory(lead=lead, opportunity=deal, description="Rahul likes mornings.")
    privacy.erase(lead.pk, operator_id=admin.pk)
    for record in (lead, deal, negotiating, task, meeting, written):
        record.refresh_from_db()
    assert lead.erased_at is not None
    assert lead.archived_at is not None
    return {
        "lead": lead,
        "deal": deal,
        "negotiating": negotiating,
        "task": task,
        "meeting": meeting,
        "note": written,
        "text_field": str(text_field.pk),
        "client": signed_in(user_a),
    }


def snapshot(*records: Any) -> list[dict[str, Any]]:
    """Every column of the records, and the audit trail's length: what "nothing was written"
    means."""
    rows = [type(r).objects.filter(pk=r.pk).values().get() for r in records]
    return [*rows, {"audit": AuditEvent.objects.count()}]


def refused(response: Any) -> None:
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "lead_erased"


# --- the lead itself --------------------------------------------------------------------------
def test_an_erased_lead_cannot_be_restored(erased):
    lead = erased["lead"]
    before = snapshot(lead)
    response = erased["client"].post(
        f"{WS}/leads/{lead.pk}/restore", {"version": lead.version}, format="json"
    )
    refused(response)
    assert snapshot(lead) == before


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("", {"first_name": "Rahul", "email": "rahul@clinic.example"}),
        ("", {"description": "Rahul is back"}),
        ("/status", {"status": "qualified"}),
    ],
)
def test_an_erased_lead_cannot_be_edited(erased, path, body):
    lead = erased["lead"]
    before = snapshot(lead)
    method = erased["client"].patch if not path else erased["client"].post
    refused(method(f"{WS}/leads/{lead.pk}{path}", {"version": lead.version, **body}, format="json"))
    assert snapshot(lead) == before


def test_its_customer_details_cannot_be_corrected_either(erased, user_a):
    lead = erased["lead"]
    # Even if its placeholder was overwritten before the marker existed (the old check was
    # the placeholder name alone).
    Lead.objects.filter(pk=lead.pk).update(first_name="Rahul")
    lead.refresh_from_db()
    response = erased["client"].post(
        f"{WS}/leads/{lead.pk}/correction",
        {"version": lead.version, "email": "new@clinic.example"},
        format="json",
    )
    refused(response)


# --- its deals --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "change",
    [
        {"customer_name": "Rahul Sharma"},
        {"account_name": "Sharma Diagnostics"},
        {"contact_email": "rahul@clinic.example"},
        {"contact_phone": "+91 98000 00001"},
        {"address": "12 Koregaon Park"},
        {"description": "Rahul's home: Koregaon"},
        {"work_load": "Rahul's lab, 300 a day"},
        {"expected_cpt": "Rs 18 for Rahul"},
        {"value": "600000.00", "customer_name": "Rahul Sharma"},  # refused as a whole
        "custom",
    ],
)
def test_its_deals_personal_text_cannot_be_written_again(erased, change):
    deal = erased["deal"]
    if change == "custom":
        change = {"custom_fields": {erased["text_field"]: "Ask for Rahul"}}
    before = snapshot(deal)
    response = erased["client"].patch(
        f"{WS}/opportunities/{deal.pk}", {"version": deal.version, **change}, format="json"
    )
    refused(response)
    assert response.json()["error"]["details"]["fields"]
    assert snapshot(deal) == before


def test_its_deals_other_details_still_change(erased):
    deal = erased["deal"]
    response = erased["client"].patch(
        f"{WS}/opportunities/{deal.pk}",
        # The redacted values sent back unchanged (an edit form) are not a change.
        {"version": deal.version, "value": "600000.00", "customer_name": ERASED},
        format="json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["value"] == "600000.00"


def test_a_move_cannot_write_a_lost_reason(erased):
    deal = erased["deal"]
    lost = default_stage("lost")
    before = snapshot(deal)
    history = StageHistory.objects.count()
    response = erased["client"].post(
        f"{WS}/opportunities/{deal.pk}/move",
        {"version": deal.version, "stage": str(lost.pk), "lost_reason": "Rahul chose Zeta"},
        format="json",
    )
    refused(response)
    assert snapshot(deal) == before
    assert StageHistory.objects.count() == history
    # Without one, the deal moves on.
    response = erased["client"].post(
        f"{WS}/opportunities/{deal.pk}/move",
        {"version": deal.version, "stage": str(lost.pk)},
        format="json",
    )
    assert response.status_code == 200, response.content


def test_agreed_terms_cannot_be_recorded(erased):
    deal, negotiating = erased["deal"], erased["negotiating"]
    before = snapshot(deal, negotiating)
    prices = NegotiationPrice.objects.count()
    moved = erased["client"].post(
        f"{WS}/opportunities/{deal.pk}/move",
        {
            "version": deal.version,
            "stage": str(default_stage("negotiation").pk),
            "negotiated_price": "450000.00",
            "agreed_cpt": "Rs 18 per test, Rahul's lab",
        },
        format="json",
    )
    refused(moved)
    revised = erased["client"].post(
        f"{WS}/opportunities/{negotiating.pk}/negotiated-prices",
        {"version": negotiating.version, "price": "400000.00", "agreed_cpt": "Rs 17 for Rahul"},
        format="json",
    )
    refused(revised)
    assert snapshot(deal, negotiating) == before
    assert NegotiationPrice.objects.count() == prices


# --- its activities ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("record", "change"),
    [
        ("task", {"title": "Call Rahul Sharma 9800000001"}),
        ("task", {"description": "Rahul's mobile: 9800000001"}),
        ("meeting", {"title": "Demo for Rahul"}),
        ("meeting", {"location": "Rahul's home, Koregaon"}),
        ("meeting", {"meeting_url": "https://meet.example/rahul"}),
    ],
)
def test_its_activities_text_cannot_be_written_again(erased, record, change):
    activity = erased[record]
    before = snapshot(activity)
    response = erased["client"].patch(
        f"{WS}/activities/{activity.pk}", {"version": activity.version, **change}, format="json"
    )
    refused(response)
    assert snapshot(activity) == before


def test_its_notes_cannot_be_rewritten(erased):
    written = erased["note"]
    before = snapshot(written)
    response = erased["client"].patch(
        f"{WS}/activities/{written.pk}",
        {"version": written.version, "description": "Rahul likes mornings."},
        format="json",
    )
    assert response.status_code == 422, response.content
    assert snapshot(written) == before


def test_its_activities_other_details_still_change(erased):
    task = erased["task"]
    response = erased["client"].patch(
        f"{WS}/activities/{task.pk}", {"version": task.version, "priority": "high"}, format="json"
    )
    assert response.status_code == 200, response.content


@pytest.mark.parametrize("link", ["lead", "opportunity"])
def test_nothing_new_is_added_for_it(erased, link):
    target = erased["lead"] if link == "lead" else erased["deal"]
    count = Activity.objects.count()
    response = erased["client"].post(
        f"{WS}/activities",
        {"type": "note", link: str(target.pk), "description": "Rahul's home address ..."},
        format="json",
    )
    refused(response)
    assert Activity.objects.count() == count


# --- erasing again ----------------------------------------------------------------------------
def test_erasing_again_is_refused_when_nothing_is_left(erased, admin):
    with pytest.raises(privacy.AlreadyErased):
        privacy.erase(erased["lead"].pk, operator_id=admin.pk)
    with pytest.raises(privacy.AlreadyErased):
        privacy.preview(erased["lead"].pk)


def reenter(erased: dict[str, Any], *, lead_too: bool = True) -> None:
    """What the old code let an owner type back in (the audit's reproduction), written
    directly: the services refuse it now."""
    if lead_too:
        lead = Lead.objects.get(pk=erased["lead"].pk)
        lead.first_name, lead.email, lead.archived_at = "Rahul", "rahul@clinic.example", None
        lead.save()
    Opportunity.objects.filter(pk=erased["deal"].pk).update(
        customer_name="Rahul Sharma", contact_email="rahul@clinic.example"
    )
    Activity.objects.filter(pk=erased["task"].pk).update(title="Call Rahul Sharma 9800000001")
    negotiating = erased["negotiating"]
    NegotiationPrice.objects.create(
        opportunity=negotiating,
        price=Decimal("400000.00"),
        agreed_cpt="Rs 17 for Rahul",
        currency="INR",
        stage=negotiating.stage,
        stage_name=negotiating.stage.name,
        source=NegotiationSource.REVISION,
        opportunity_version=negotiating.version,
        actor_id=negotiating.owner_id,
    )


@pytest.mark.parametrize("replay", [False, True])
def test_re_entered_text_is_erased_again(erased, admin, replay, monkeypatch):
    first = erased["lead"].erased_at
    reenter(erased)
    assert privacy.preview(erased["lead"].pk).opportunities == 2
    if replay:  # a ledger replay re-applies it, and appends nothing new to the ledger
        monkeypatch.setattr(ledger, "append", lambda *a, **k: pytest.fail("appended"))
    privacy.erase(erased["lead"].pk, operator_id=admin.pk, replay=replay)

    lead = Lead.objects.get(pk=erased["lead"].pk)
    assert (lead.first_name, lead.email) == (ERASED, "")
    assert lead.archived_at is not None
    assert lead.erased_at == first  # the first erasure's time stays
    deal = Opportunity.objects.get(pk=erased["deal"].pk)
    assert (deal.customer_name, deal.contact_email) == (ERASED, "")
    assert Activity.objects.get(pk=erased["task"].pk).title == ERASED
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT agreed_cpt FROM pipeline_negotiation_price WHERE opportunity_id = %s",
            [erased["negotiating"].pk],
        )
        assert {row[0] for row in cursor.fetchall()} == {""}
    assert AuditEvent.objects.filter(action="lead.erased", target_id=str(lead.pk)).count() == 2
    with pytest.raises(privacy.AlreadyErased):  # and now nothing is left
        privacy.erase(lead.pk, operator_id=admin.pk)


def test_an_unmarked_lead_erased_by_an_earlier_release_is_erased_and_marked(user_a, admin):
    """A lead the previous release erased during a rolling deploy carries the placeholder but
    no marker: erasing it again marks it (it used to be refused as already erased)."""
    lead = LeadFactory(owner=user_a, first_name="Rahul")
    privacy.erase(lead.pk, operator_id=admin.pk)
    Lead.objects.filter(pk=lead.pk).update(erased_at=None)
    privacy.erase(lead.pk, operator_id=admin.pk)
    assert Lead.objects.get(pk=lead.pk).erased_at is not None


def test_text_re_entered_only_in_its_deals_and_tasks_is_erased_again(erased, admin):
    """The audit's first reproduction: the lead itself still holds only the placeholder (the
    old check looked at nothing else, so it answered "already erased")."""
    reenter(erased, lead_too=False)
    privacy.erase(erased["lead"].pk, operator_id=admin.pk)
    deal = Opportunity.objects.get(pk=erased["deal"].pk)
    assert (deal.customer_name, deal.contact_email) == (ERASED, "")
    assert Activity.objects.get(pk=erased["task"].pk).title == ERASED
