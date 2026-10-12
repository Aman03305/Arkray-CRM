"""Regressions of the deep security audit's pipeline findings: L-4 (removing a custom field
hid other users' values), L-8 (a choice field accepted two options with one id) and L-9 (a
move changed the lost reason under a legal hold), and of their review: P3-1 (a restricted
previous owner deleted a hidden value by removing its field) and P3-3 (a reopen cleared the
lost reason under a hold)."""

from __future__ import annotations

from typing import Any

import pytest

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.models import HoldSubject, LegalHold
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline.models import CustomField, Opportunity, StageHistory
from tests.factories import LeadFactory, OpportunityFactory, default_stage
from tests.helpers import key_header, signed_in

pytestmark = pytest.mark.django_db

WS = "/api/v1/workspaces/me"


# --- L-4 -------------------------------------------------------------------------------------
@pytest.fixture
def shared_pipeline(user_a, user_b, admin):
    """User A's own pipeline with a text field, and a deal in it whose lead an administrator
    reassigned to user B: the deal (open) follows the lead to B, and stays in A's pipeline."""
    a = signed_in(user_a)
    created = a.post(
        f"{WS}/pipelines",
        {
            "name": "Mine",
            "stages": [
                {"name": "Open", "type": "open", "probability": "10"},
                {"name": "Won", "type": "won"},
                {"name": "Lost", "type": "lost"},
            ],
            "custom_fields": [{"name": "Secret note", "type": "text"}],
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    pipeline = created.json()
    field_id = pipeline["custom_fields"][0]["id"]
    deal = a.post(
        f"{WS}/opportunities",
        {
            "customer_name": "Rahul",
            "value": "100",
            "pipeline": pipeline["id"],
            "custom_fields": {field_id: "B will need this"},
        },
        format="json",
        headers=key_header(),
    )
    assert deal.status_code == 201, deal.content
    opportunity = Opportunity.objects.get(pk=deal.json()["id"])
    lead = Lead.objects.get(pk=opportunity.lead_id)
    lead_services.reassign_lead(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        lead_id=lead.pk,
        version=lead.version,
        owner_id=user_b.pk,
    )
    return {"client": a, "pipeline": pipeline, "field": field_id, "deal": opportunity}


def _remove_fields(client: Any, pipeline_id: str, **extra: Any) -> Any:
    version = client.get(f"{WS}/pipelines/{pipeline_id}").json()["version"]
    return client.put(
        f"{WS}/pipelines/{pipeline_id}/fields",
        {"version": version, "custom_fields": [], **extra},
        format="json",
    )


@pytest.mark.parametrize("delete", [False, True])
def test_a_pipeline_owner_cannot_hide_other_users_values(user_b, shared_pipeline, delete):
    extra = {"delete_removed_values": True} if delete else {}
    response = _remove_fields(shared_pipeline["client"], shared_pipeline["pipeline"]["id"], **extra)
    assert response.status_code == 403, response.content
    assert CustomField.objects.get(pk=shared_pipeline["field"]).is_active
    seen = signed_in(user_b).get(f"{WS}/opportunities/{shared_pipeline['deal'].pk}").json()
    assert seen["custom_fields"] == {shared_pipeline["field"]: "B will need this"}


def test_a_pipeline_owner_still_removes_a_field_only_their_deals_use(user_a):
    a = signed_in(user_a)
    created = a.post(
        f"{WS}/pipelines",
        {
            "name": "Mine",
            "stages": [{"name": "Open", "type": "open", "probability": "10"}],
            "custom_fields": [{"name": "Secret note", "type": "text"}],
        },
        format="json",
    ).json()
    response = _remove_fields(a, created["id"])
    assert response.status_code == 200, response.content
    assert not CustomField.objects.get(pk=created["custom_fields"][0]["id"]).is_active


def test_an_administrator_organisation_wide_removes_it(admin, shared_pipeline):
    client = signed_in(admin)
    pipeline_id = shared_pipeline["pipeline"]["id"]
    version = client.get(f"/api/v1/workspaces/all/pipelines/{pipeline_id}").json()["version"]
    response = client.put(
        f"/api/v1/workspaces/all/pipelines/{pipeline_id}/fields",
        {"version": version, "custom_fields": []},
        format="json",
    )
    assert response.status_code == 200, response.content


# --- L-8 -------------------------------------------------------------------------------------
def test_a_choice_field_refuses_one_option_id_twice(user_a):
    client = signed_in(user_a)
    created = client.post(
        f"{WS}/pipelines",
        {
            "name": "Mine",
            "stages": [{"name": "New", "type": "open", "probability": "10"}],
            "custom_fields": [
                {
                    "name": "Grade",
                    "type": "single_select",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ],
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    body = created.json()
    field = body["custom_fields"][0]
    a_id = field["options"][0]["id"]
    before = CustomField.objects.get(pk=field["id"]).options
    response = client.put(
        f"{WS}/pipelines/{body['id']}/fields",
        {
            "version": body["version"],
            "custom_fields": [
                {
                    "id": field["id"],
                    "name": "Grade",
                    "type": "single_select",
                    "options": [{"id": a_id, "label": "A"}, {"id": a_id, "label": "B"}],
                }
            ],
        },
        format="json",
    )
    assert response.status_code == 400, response.content
    assert response.json()["error"]["details"] == {
        "fields[0].options": ["This choice is listed twice."]
    }
    assert CustomField.objects.get(pk=field["id"]).options == before


# --- L-9 -------------------------------------------------------------------------------------
def test_a_move_cannot_write_a_lost_reason_under_a_legal_hold(user_a, admin):
    opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage_key="new")
    LegalHold.objects.create(
        subject_type=HoldSubject.LEAD,
        subject_id=opportunity.lead_id,
        reference="CASE-1",
        placed_by=admin.pk,
    )
    client = signed_in(user_a)
    lost = default_stage("lost")
    row = Opportunity.objects.filter(pk=opportunity.pk).values().get()
    history, audit = StageHistory.objects.count(), AuditEvent.objects.count()
    response = client.post(
        f"{WS}/opportunities/{opportunity.pk}/move",
        {"version": opportunity.version, "stage": str(lost.pk), "lost_reason": "new reason"},
        format="json",
    )
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "legal_hold"
    assert response.json()["error"]["details"] == {"fields": ["lost_reason"]}
    assert Opportunity.objects.filter(pk=opportunity.pk).values().get() == row
    assert (StageHistory.objects.count(), AuditEvent.objects.count()) == (history, audit)
    # Moves go on under a hold: without a lost reason, it is lost.
    moved = client.post(
        f"{WS}/opportunities/{opportunity.pk}/move",
        {"version": opportunity.version, "stage": str(lost.pk)},
        format="json",
    )
    assert moved.status_code == 200, moved.content


# --- review P3-1 ---------------------------------------------------------------------------------
SECRET = "Ravi Kumar, 98111 55555"


@pytest.fixture
def kept_deal(user_a, user_b, admin):
    """Rahul's own pipeline: his open deal and his closed (won) deal, each holding a text
    value; the closed deal's lead then goes to Priya, so its customer is hidden from him."""
    from arkray.pipeline.models import Pipeline, Stage

    rahul = signed_in(user_a)
    created = rahul.post(
        f"{WS}/pipelines",
        {
            "name": "Tenders",
            "stages": [
                {"name": "Qualify", "type": "open", "probability": "10"},
                {"name": "Won", "type": "won"},
                {"name": "Lost", "type": "lost"},
            ],
            "custom_fields": [{"name": "Contact notes", "type": "text"}],
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    pipeline = Pipeline.objects.get(pk=created.json()["id"])
    field = CustomField.objects.get(pipeline=pipeline, name="Contact notes")
    closed = OpportunityFactory(
        lead=LeadFactory(owner=user_a), stage=Stage.objects.get(pipeline=pipeline, name="Won")
    )
    own = OpportunityFactory(
        lead=LeadFactory(owner=user_a), stage=Stage.objects.get(pipeline=pipeline, name="Qualify")
    )
    for deal, value in ((closed, SECRET), (own, "Rahul's own note")):
        Opportunity.objects.filter(pk=deal.pk).update(custom_fields={str(field.pk): value})
    lead = Lead.objects.get(pk=closed.lead_id)
    lead_services.reassign_lead(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        lead_id=lead.pk,
        version=lead.version,
        owner_id=user_b.pk,
    )
    assert Opportunity.objects.get(pk=closed.pk).owner_id == user_a.pk  # kept by its closer
    return {"client": rahul, "pipeline": pipeline, "field": field, "closed": closed, "own": own}


def _values(deal: Any) -> dict[str, Any]:
    return dict(Opportunity.objects.get(pk=deal.pk).custom_fields)


@pytest.mark.parametrize("delete", [True, False])
def test_the_previous_owner_cannot_remove_a_hidden_value_with_its_field(kept_deal, delete):
    from tests.helpers import drain_outbox

    extra = {"delete_removed_values": True} if delete else {}
    response = _remove_fields(kept_deal["client"], str(kept_deal["pipeline"].pk), **extra)
    drain_outbox()
    assert response.status_code == 403, response.content
    assert CustomField.objects.get(pk=kept_deal["field"].pk).is_active
    assert _values(kept_deal["closed"]) == {str(kept_deal["field"].pk): SECRET}


def test_the_purge_job_leaves_a_deal_whose_customer_is_elsewhere(kept_deal, user_a):
    """The workspace's job (queued before the deal's lead was reassigned, or replayed from the
    ledger) deletes only the values of deals whose customer is the workspace owner's too."""
    from arkray.pipeline import field_values

    field = kept_deal["field"]
    CustomField.objects.filter(pk=field.pk).update(is_active=False)
    done = field_values.purge(field.pk, owner_id=user_a.pk, replay=True)
    assert done["rows"] == 1
    assert _values(kept_deal["own"]) == {}
    assert _values(kept_deal["closed"]) == {str(field.pk): SECRET}
    # Organisation-wide (an administrator's request), every deal's value goes.
    field_values.purge(field.pk, replay=True)
    assert _values(kept_deal["closed"]) == {}


# --- review P3-3 ---------------------------------------------------------------------------------
def _lost_with_reason(user_a):
    opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a), stage_key="new")
    client = signed_in(user_a)
    lost = client.post(
        f"{WS}/opportunities/{opportunity.pk}/move",
        {
            "version": opportunity.version,
            "stage": str(default_stage("lost").pk),
            "lost_reason": "Chose Zeta",
        },
        format="json",
    )
    assert lost.status_code == 200, lost.content
    return client, Opportunity.objects.get(pk=opportunity.pk)


def test_a_reopen_cannot_clear_the_lost_reason_under_a_legal_hold(user_a, admin):
    client, opportunity = _lost_with_reason(user_a)
    LegalHold.objects.create(
        subject_type=HoldSubject.LEAD,
        subject_id=opportunity.lead_id,
        reference="CASE-1",
        placed_by=admin.pk,
    )
    row = Opportunity.objects.filter(pk=opportunity.pk).values().get()
    history = StageHistory.objects.count()
    response = client.post(
        f"{WS}/opportunities/{opportunity.pk}/move",
        {"version": opportunity.version, "stage": str(default_stage("new").pk)},
        format="json",
    )
    assert response.status_code == 409, response.content
    assert response.json()["error"]["details"] == {"fields": ["lost_reason"]}
    assert Opportunity.objects.filter(pk=opportunity.pk).values().get() == row
    assert StageHistory.objects.count() == history


def test_without_a_hold_a_reopen_still_clears_it(user_a):
    client, opportunity = _lost_with_reason(user_a)
    response = client.post(
        f"{WS}/opportunities/{opportunity.pk}/move",
        {"version": opportunity.version, "stage": str(default_stage("new").pk)},
        format="json",
    )
    assert response.status_code == 200, response.content
    assert Opportunity.objects.get(pk=opportunity.pk).lost_reason == ""
