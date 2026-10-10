"""Correcting a customer's details on request (privacy remediation P2-6;
pipeline.corrections; docs/privacy.md#correction).

The lead (the canonical customer record) is corrected, and every deal's copy that still holds
the old value follows; deliberately different copies and everything commercial stay. One
transaction, optimistic concurrency, audited by field names only, re-indexed for Ask Arkray,
and search and the dashboard see the corrected text.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import ConflictError
from arkray.core.models import OutboxEvent
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import corrections, services
from arkray.pipeline.models import Opportunity
from tests.factories import LeadFactory, OpportunityFactory
from tests.helpers import run_concurrently, signed_in

pytestmark = pytest.mark.django_db

OLD_EMAIL = "ravi.k@old-mail.example"
NEW_EMAIL = "ravi.kumar@new-mail.example"


@pytest.fixture
def customer(user_a):
    lead = LeadFactory(
        owner=user_a,
        first_name="Ravi",
        last_name="Kumaar",  # misspelt
        organization_name="Apollo Labs",
        email=OLD_EMAIL,
        phone="+91 98111 22333",
        address_line_1="12 Old Road",
        address_line_2="Pune",
    )
    common = {
        "lead": lead,
        "account_name": "Apollo Labs",
        "customer_name": "Ravi Kumaar",
        "contact_email": OLD_EMAIL,
        "contact_phone": "+91 98111 22333",
        "address": "12 Old Road\nPune",
        "instrument_name": "Adams 8380 V-lite",
    }
    open_deal = OpportunityFactory(title="Ravi Kumaar — Adams 8380 V-lite", **common)
    won_deal = OpportunityFactory(
        title="Ravi Kumaar — Adams 8380 V-lite", stage_key="won", value=Decimal("777000"), **common
    )
    different = OpportunityFactory(
        **{
            **common,
            "account_name": "Apollo Hospitals Purchase Dept",
            "customer_name": "Dr. Mehta",
            "contact_email": "purchase@apollo.example",
            "contact_phone": "+91 20 2612 0000",
            "address": "Purchase Office, Apollo Hospitals",
            "title": "Dr. Mehta — Adams 8380 V-lite",
        }
    )
    return {"lead": lead, "open": open_deal, "won": won_deal, "different": different}


def correct(client, lead, workspace="me", **changes):
    lead.refresh_from_db()
    return client.post(
        f"/api/v1/workspaces/{workspace}/leads/{lead.pk}/correction",
        {"version": lead.version, **changes},
        format="json",
    )


def test_the_lead_and_its_copies_are_corrected_together(user_a, customer):
    response = correct(
        signed_in(user_a),
        customer["lead"],
        last_name="Kumar",
        email=NEW_EMAIL,
        address_line_1="7 New Street",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["corrected"] == ["address_line_1", "email", "last_name"]
    assert body["opportunities"] == 2
    lead = Lead.objects.get(pk=customer["lead"].pk)
    assert (lead.last_name, lead.email, lead.display_name) == ("Kumar", NEW_EMAIL, "Ravi Kumar")
    for key in ("open", "won"):
        deal = Opportunity.objects.get(pk=customer[key].pk)
        assert deal.customer_name == "Ravi Kumar"
        assert deal.contact_email == NEW_EMAIL
        assert deal.address == "7 New Street\nPune"
        assert deal.title == "Ravi Kumar — Adams 8380 V-lite"  # the derived name follows
        assert deal.version == customer[key].version + 1
    won = Opportunity.objects.get(pk=customer["won"].pk)
    assert (won.value, won.status) == (Decimal("777000.00"), "won")  # commercial history kept
    different = Opportunity.objects.get(pk=customer["different"].pk)
    assert (different.customer_name, different.contact_email) == (
        "Dr. Mehta",
        "purchase@apollo.example",
    )
    assert different.version == customer["different"].version


def test_audited_by_field_names_only(user_a, customer):
    correct(signed_in(user_a), customer["lead"], email=NEW_EMAIL)
    corrected = AuditEvent.objects.get(action=corrections.AUDIT_LEAD_CORRECTED)
    assert corrected.metadata == {"workspace": "self", "fields": ["email"]}
    copies = AuditEvent.objects.filter(action=corrections.AUDIT_OPPORTUNITY_CORRECTED)
    assert copies.count() == 2
    trail = json.dumps(list(AuditEvent.objects.values("metadata")), default=str)
    assert NEW_EMAIL not in trail
    assert OLD_EMAIL not in trail


def test_a_stale_version_is_refused_and_changes_nothing(user_a, customer):
    lead = customer["lead"]
    response = signed_in(user_a).post(
        f"/api/v1/workspaces/me/leads/{lead.pk}/correction",
        {"version": lead.version + 5, "email": NEW_EMAIL},
        format="json",
    )
    assert response.status_code == 409
    assert Lead.objects.get(pk=lead.pk).email == OLD_EMAIL


@pytest.mark.parametrize(
    "field", [{"status": "converted"}, {"description": "x"}, {"owner": "x"}, {"source": "web"}]
)
def test_only_customer_details_can_be_corrected(user_a, customer, field):
    response = correct(signed_in(user_a), customer["lead"], **field)
    assert response.status_code == 400


def test_a_closed_deals_previous_owner_cannot_correct_the_customer(admin, user_a, user_b, customer):
    lead = customer["lead"]
    lead_services.reassign_lead(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        lead_id=lead.pk,
        version=lead.version,
        owner_id=user_b.pk,
    )
    assert correct(signed_in(user_a), lead, email=NEW_EMAIL).status_code == 404
    # The new owner can, and the kept deal's copy follows (still hidden from its owner).
    assert correct(signed_in(user_b), lead, email=NEW_EMAIL).status_code == 200
    assert Opportunity.objects.get(pk=customer["won"].pk).contact_email == NEW_EMAIL
    page = signed_in(user_a).get(f"/api/v1/workspaces/me/opportunities/{customer['won'].pk}")
    assert page.json()["contact_email"] == ""


def test_an_administrator_corrects_organisation_wide(admin, customer):
    response = correct(signed_in(admin), customer["lead"], workspace="all", email=NEW_EMAIL)
    assert response.status_code == 200
    assert response.json()["opportunities"] == 2


def test_an_archived_lead_can_still_be_corrected(user_a, customer):
    Lead.objects.filter(pk=customer["lead"].pk).update(
        archived_at=customer["lead"].created_at, version=customer["lead"].version
    )
    assert correct(signed_in(user_a), customer["lead"], email=NEW_EMAIL).status_code == 200


def test_an_erased_lead_cannot_be_corrected(user_a, customer):
    Lead.objects.filter(pk=customer["lead"].pk).update(first_name="[erased]", last_name="")
    assert correct(signed_in(user_a), customer["lead"], email=NEW_EMAIL).status_code == 422


def test_a_stale_edit_of_a_corrected_deal_is_refused(user_a, customer):
    stale = customer["open"].version
    correct(signed_in(user_a), customer["lead"], email=NEW_EMAIL)
    with pytest.raises(ConflictError):
        services.update_opportunity(
            actor=user_a,
            scope=AccessScope.own(user_a.pk),
            opportunity_id=customer["open"].pk,
            version=stale,
            changes={"contact_email": OLD_EMAIL},
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_concurrent_corrections_one_wins_the_other_conflicts():
    from tests.factories import UserFactory

    owner = UserFactory()
    lead = LeadFactory(owner=owner, email=OLD_EMAIL)
    OpportunityFactory(lead=lead, contact_email=OLD_EMAIL, customer_name=lead.display_name)
    version = lead.version

    def attempt(email):
        return lambda: corrections.correct_customer(
            actor=owner,
            scope=AccessScope.own(owner.pk),
            lead_id=lead.pk,
            version=version,
            changes={"email": email},
        )

    results = run_concurrently(attempt("a@one.example"), attempt("b@two.example"))
    assert sum(isinstance(r, ConflictError) for r in results) == 1
    winner = next(r for r in results if isinstance(r, corrections.Correction))
    lead.refresh_from_db()
    assert lead.email == winner.lead.email
    assert Opportunity.objects.get(lead=lead).contact_email == lead.email


@pytest.mark.usefixtures("ai_on")
def test_search_dashboard_and_ask_arkray_follow(user_a, customer):
    client = signed_in(user_a)
    correct(client, customer["lead"], last_name="Kumar")
    found = client.get("/api/v1/workspaces/me/search?q=Kumar").json()
    assert {o["id"] for o in found["opportunities"]["results"]} >= {
        str(customer["open"].pk),
        str(customer["won"].pk),
    }
    old = client.get("/api/v1/workspaces/me/search?q=Kumaar").json()
    assert old["opportunities"]["results"] == []
    assert old["leads"]["results"] == []
    dashboard = client.get("/api/v1/workspaces/me/dashboard").json()
    assert "Kumaar" not in json.dumps(dashboard)
    topics = set(OutboxEvent.objects.values_list("topic", flat=True))
    assert topics & {"ai.index_lead", "ai.index_source"}, topics


def test_a_lead_under_legal_hold_cannot_be_corrected(admin, user_a, customer):
    """Backend review P3: a correction overwrote a held lead's details."""
    from arkray.core.models import HoldSubject, LegalHold

    hold = LegalHold.objects.create(
        subject_type=HoldSubject.LEAD,
        subject_id=customer["lead"].pk,
        reference="MATTER-7",
        placed_by=admin.pk,
    )
    response = correct(signed_in(user_a), customer["lead"], email=NEW_EMAIL)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "legal_hold"
    customer["lead"].refresh_from_db()
    assert customer["lead"].email == OLD_EMAIL
    LegalHold.objects.filter(pk=hold.pk).update(released_at=hold.placed_at, released_by=admin.pk)
    assert correct(signed_in(user_a), customer["lead"], email=NEW_EMAIL).status_code == 200
