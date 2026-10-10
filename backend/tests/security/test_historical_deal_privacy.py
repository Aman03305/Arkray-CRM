"""A closed deal stays with the salesperson who worked it after its lead is reassigned, but
without the customer's identity and contact details (privacy remediation P2-10;
docs/authorization.md#historical-deals; pipeline.customer).

Checked before and after the reassignment, on every surface that shows a deal: its page,
lists and board, archived deals, global search, activity and timeline links, Ask Arkray's
tools and an administrator's support session (which sees what the user sees). The new owner
and an administrator organisation-wide see everything.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from arkray.ai import tools
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from arkray.pipeline.models import CustomField, FieldType, Opportunity
from tests.factories import LeadFactory, OpportunityFactory, TaskFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

PERSON = "Ravi Kumar"
PHONE = "+91 98111 55555"
EMAIL = "ravi.kumar@home.example"
ADDRESS = "14 Lake Road, Pune"
PII = (PERSON, "Ravi", PHONE, EMAIL, ADDRESS)


def leaked(payload: Any) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return [value for value in PII if value in text]


@pytest.fixture
def deal(user_a, admin):
    lead = LeadFactory(owner=user_a, first_name="Ravi", last_name="Kumar", organization_name="")
    opportunity = OpportunityFactory(
        lead=lead,
        stage_key="won",
        title=f"{PERSON} — Adams 8380 V-lite",
        account_name="Apollo Labs",
        customer_name=PERSON,
        contact_phone=PHONE,
        contact_email=EMAIL,
        address=ADDRESS,
        instrument_name="Adams 8380 V-lite",
        value=Decimal("950000"),
    )
    notes_field = CustomField.objects.create(
        pipeline=opportunity.pipeline, name="Lab contact", field_type=FieldType.TEXT, position=90
    )
    volume_field = CustomField.objects.create(
        pipeline=opportunity.pipeline, name="Tests a day", field_type=FieldType.NUMBER, position=91
    )
    Opportunity.objects.filter(pk=opportunity.pk).update(
        custom_fields={str(notes_field.pk): f"Ask for {PERSON}", str(volume_field.pk): "300"}
    )
    task = TaskFactory(
        lead=lead, opportunity=opportunity, owner=user_a, status="completed", title="Install done"
    )
    return {"lead": lead, "opportunity": opportunity, "task": task, "volume": str(volume_field.pk)}


def reassign(admin, deal, to):
    lead = deal["lead"]
    lead.refresh_from_db()
    lead_services.reassign_lead(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        lead_id=lead.pk,
        version=lead.version,
        owner_id=to.pk,
    )


def page(client: Any, deal: dict[str, Any]) -> dict[str, Any]:
    response = client.get(f"/api/v1/workspaces/me/opportunities/{deal['opportunity'].pk}")
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def test_before_reassignment_the_owner_sees_the_customer(user_a, deal):
    body = page(signed_in(user_a), deal)
    assert body["customer_restricted"] is False
    assert (body["customer_name"], body["contact_email"]) == (PERSON, EMAIL)


def test_after_reassignment_the_page_hides_the_customer(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    body = page(signed_in(user_a), deal)
    assert body["customer_restricted"] is True
    assert body["lead"] == {"id": None, "restricted": True}
    for field in ("customer_name", "contact_phone", "contact_email", "address"):
        assert body[field] == ""
    assert body["title"] == "Apollo Labs — Adams 8380 V-lite"
    assert body["account_name"] == "Apollo Labs"
    assert body["value"] == "950000.00"  # the sales history stays
    assert body["custom_fields"] == {deal["volume"]: "300"}  # free text left out
    assert not leaked(body), leaked(body)


def test_a_person_only_account_is_not_shown_either(admin, user_a, user_b, deal):
    Opportunity.objects.filter(pk=deal["opportunity"].pk).update(account_name=PERSON)
    reassign(admin, deal, user_b)
    body = page(signed_in(user_a), deal)
    assert body["account_name"] == ""
    assert body["title"] == "Customer restricted — Adams 8380 V-lite"
    assert not leaked(body)


def test_lists_board_and_archived_lists_hide_it(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    client = signed_in(user_a)
    listing = client.get("/api/v1/workspaces/me/opportunities?status=won").json()
    board = client.get("/api/v1/workspaces/me/pipeline-board").json()
    assert listing["results"][0]["customer_restricted"] is True
    assert not leaked(listing)
    assert not leaked(board)
    Opportunity.objects.filter(pk=deal["opportunity"].pk).update(archived_at=timezone.now())
    archived = client.get("/api/v1/workspaces/me/opportunities?archived=true").json()
    assert archived["results"]
    assert not leaked(archived)


def test_search_never_matches_it(admin, user_a, user_b, deal):
    client = signed_in(user_a)
    before = client.get("/api/v1/workspaces/me/search?q=Ravi").json()
    assert [o["id"] for o in before["opportunities"]["results"]] == [str(deal["opportunity"].pk)]
    reassign(admin, deal, user_b)
    for term in ("Ravi", "Kumar"):  # the customer's name finds nothing any more
        after = client.get(f"/api/v1/workspaces/me/search?q={term}").json()
        assert after["opportunities"]["results"] == [], term
        assert not leaked({k: v for k, v in after.items() if k not in ("query", "terms")})
    # Its organisation still finds it, shown without the customer.
    found = client.get("/api/v1/workspaces/me/search?q=Apollo").json()["opportunities"]["results"]
    assert [o["id"] for o in found] == [str(deal["opportunity"].pk)]
    assert found[0]["customer_restricted"] is True
    assert found[0]["title"] == "Apollo Labs — Adams 8380 V-lite"
    assert not leaked(found)


def test_activity_and_timeline_links_hide_it(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    client = signed_in(user_a)
    activities = client.get("/api/v1/workspaces/me/activities?status=completed").json()
    rows = [r for r in activities["results"] if r["id"] == str(deal["task"].pk)]
    assert rows
    assert rows[0]["opportunity"]["title"] == "Apollo Labs — Adams 8380 V-lite"
    timeline = client.get(
        f"/api/v1/workspaces/me/opportunities/{deal['opportunity'].pk}/timeline"
    ).json()
    assert not leaked(activities)
    assert not leaked(timeline)


def test_ask_arkray_tools_hide_it(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    outcome = tools.execute(ctx, "get_record", {"ref": f"opportunity:{deal['opportunity'].pk}"})
    assert not outcome.is_error, outcome.content
    assert not leaked(json.loads(outcome.content))
    assert not leaked([record.label for record in ctx.records.values()])


@pytest.mark.parametrize("query", ["Apollo", "Adams 8380", "Ravi Kumar"])
def test_ask_arkray_search_never_names_the_customer(admin, user_a, user_b, deal, query):
    """Backend review P1: find_records cited the deal by its full title."""
    reassign(admin, deal, user_b)
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    outcome = tools.execute(ctx, "find_records", {"query": query})
    assert not outcome.is_error, outcome.content
    content = json.loads(outcome.content)
    del content["query"]  # the user's own words, echoed
    assert not leaked(content)
    assert not leaked([record.label for record in ctx.records.values()])
    if query == "Apollo":  # still found by its organisation, under its neutral name
        assert [o["title"] for o in content["opportunities"]] == ["Apollo Labs — Adams 8380 V-lite"]


def test_ask_arkray_retrieval_never_returns_it(admin, user_a, user_b, deal):
    """Backend review P1: retrieval re-read the deal's title and text for its old owner."""
    from arkray.pipeline import selectors

    Opportunity.objects.filter(pk=deal["opportunity"].pk).update(
        description=f"{PERSON} wants the install before March."
    )
    ids = [deal["opportunity"].pk]
    assert selectors.knowledge_documents(AccessScope.own(user_a.pk), ids)
    reassign(admin, deal, user_b)
    assert selectors.knowledge_documents(AccessScope.own(user_a.pk), ids) == []
    assert selectors.knowledge_documents(AccessScope.own(user_b.pk), ids) == []  # not their deal
    found = selectors.knowledge_documents(AccessScope.organization(admin.pk), ids)
    assert [doc.source_id for doc in found] == ids


def test_a_support_session_sees_what_the_user_sees(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    client = signed_in(admin)
    started = client.post(
        "/api/v1/admin/support-sessions",
        {"user": str(user_a.pk), "reason": "Ticket 1"},
        format="json",
    )
    assert started.status_code in (200, 201)
    body = client.get(
        f"/api/v1/workspaces/{user_a.pk}/opportunities/{deal['opportunity'].pk}"
    ).json()
    assert body["customer_restricted"] is True
    assert not leaked(body)


def test_an_administrator_organisation_wide_sees_everything(admin, user_b, deal):
    reassign(admin, deal, user_b)
    body = (
        signed_in(admin)
        .get(f"/api/v1/workspaces/all/opportunities/{deal['opportunity'].pk}")
        .json()
    )
    assert body["customer_restricted"] is False
    assert body["customer_name"] == PERSON
