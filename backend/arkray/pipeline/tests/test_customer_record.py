"""ADR-0027: Leads left the UI. An opportunity created without a lead gets a hidden customer
record (a lead) made from its customer details, in the same transaction; its owner changes
through "assign", which reassigns that record. Called through the services (as the API
does) and through the API for the shapes."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.apps import apps

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
from arkray.leads import events as lead_events
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import services
from arkray.pipeline.models import Opportunity
from tests.factories import LeadFactory, OpportunityFactory, TaskFactory, UserFactory
from tests.helpers import collected, key_header, signed_in

from .conftest import opportunities_url, opportunity_url

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
ORG = AccessScope.organization
FOR_USER = AccessScope.for_user
CUSTOMER = {
    "value": Decimal("1200000"),
    "account_name": "Apollo Diagnostics",
    "customer_name": "Asharaf Panka",
    "contact_email": "asharaf@apollo.example",
    "contact_phone": "+91 95607 34655",
}


def create(actor, scope, fields=None, **kwargs):
    return services.create_opportunity(
        actor=actor, scope=scope, lead_id=None, fields=fields or CUSTOMER, **kwargs
    ).opportunity


def reassign(actor, scope, opportunity, owner, version=None):
    return services.reassign_opportunity(
        actor=actor,
        scope=scope,
        opportunity_id=opportunity.pk,
        version=Opportunity.objects.get(pk=opportunity.pk).version if version is None else version,
        owner_id=owner.pk,
    )


class TestCreateWithoutALead:
    def test_makes_the_customer_record_from_the_customer_details(self, user_a, stages):
        with collected(lead_events.LeadCreated) as created:
            opportunity = create(user_a, OWN(user_a.pk))
        lead = Lead.objects.get(pk=opportunity.lead_id)
        assert (lead.first_name, lead.last_name, lead.organization_name) == (
            "Asharaf Panka",
            "",
            "Apollo Diagnostics",
        )
        assert (lead.display_name, lead.email, lead.phone) == (
            "Asharaf Panka",
            "asharaf@apollo.example",
            "+91 95607 34655",
        )
        assert lead.owner_id == lead.created_by_id == opportunity.owner_id == user_a.pk
        assert (opportunity.customer_name, opportunity.account_name) == (
            "Asharaf Panka",
            "Apollo Diagnostics",
        )
        assert opportunity.title == "Asharaf Panka"  # derived: the customer, no instrument
        assert [event.lead_id for event in created] == [lead.pk]
        # Both records are audited, each by its own module.
        assert AuditEvent.objects.filter(target_id=str(lead.pk), action="lead.created").exists()
        assert AuditEvent.objects.filter(
            target_id=str(opportunity.pk), action="opportunity.created"
        ).exists()

    def test_a_long_customer_name_reads_the_same(self, user_a, stages):
        name = " ".join(["Venkatanarasimharajuvaripeta"] * 6)  # 173 characters
        opportunity = create(user_a, OWN(user_a.pk), {**CUSTOMER, "customer_name": name})
        lead = Lead.objects.get(pk=opportunity.lead_id)
        assert len(lead.first_name) <= 100
        assert len(lead.last_name) <= 100
        assert lead.display_name == name

    def test_a_long_name_without_spaces_is_kept_whole(self, user_a, stages):
        name = "X" * 150
        opportunity = create(user_a, OWN(user_a.pk), {**CUSTOMER, "customer_name": name})
        lead = Lead.objects.get(pk=opportunity.lead_id)
        assert (len(lead.first_name), len(lead.last_name)) == (100, 50)

    def test_an_account_alone_is_enough(self, user_a, stages):
        fields = {k: v for k, v in CUSTOMER.items() if k != "customer_name"}
        opportunity = create(user_a, OWN(user_a.pk), fields)
        lead = Lead.objects.get(pk=opportunity.lead_id)
        assert (lead.display_name, opportunity.customer_name, opportunity.title) == (
            "Apollo Diagnostics",
            "Apollo Diagnostics",
            "Apollo Diagnostics",
        )

    def test_someone_to_sell_to_is_required(self, user_a, stages):
        fields = {"value": Decimal("1")}
        with pytest.raises(InvalidInputError) as caught:
            create(user_a, OWN(user_a.pk), fields)
        assert caught.value.details == {"customer_name": [services.CUSTOMER_REQUIRED]}
        assert not Lead.objects.exists()

    def test_nothing_is_left_behind_when_the_opportunity_is_refused(self, user_a, stages):
        with pytest.raises(InvalidInputError):
            create(user_a, OWN(user_a.pk), stage_id=stages["won"].pipeline.pk)  # not a stage
        assert not Lead.objects.exists()
        assert not Opportunity.objects.exists()

    def test_in_ones_own_workspace_the_owner_is_oneself(self, user_a, user_b, stages):
        assert create(user_a, OWN(user_a.pk), owner_id=user_a.pk).owner_id == user_a.pk
        with pytest.raises(InvalidInputError) as caught:
            create(user_a, OWN(user_a.pk), owner_id=user_b.pk)
        assert caught.value.details == {"owner": [services.OWNER_IS_SELF_ONLY]}

    def test_in_a_users_workspace_it_is_theirs(self, admin, user_a, user_b, stages):
        opportunity = create(admin, FOR_USER(admin.pk, user_a.pk))
        assert opportunity.owner_id == Lead.objects.get(pk=opportunity.lead_id).owner_id
        assert opportunity.owner_id == user_a.pk
        with pytest.raises(InvalidInputError) as caught:
            create(admin, FOR_USER(admin.pk, user_a.pk), owner_id=user_b.pk)
        assert caught.value.details == {"owner": [services.OWNER_IS_SUBJECT_ONLY]}

    def test_organisation_wide_the_owner_is_chosen(self, admin, user_b, stages):
        with pytest.raises(InvalidInputError) as caught:
            create(admin, ORG(admin.pk))
        assert caught.value.details == {"owner": [services.OWNER_REQUIRED]}
        opportunity = create(admin, ORG(admin.pk), owner_id=user_b.pk)
        assert opportunity.owner_id == user_b.pk
        assert Lead.objects.get(pk=opportunity.lead_id).created_by_id == admin.pk

    def test_a_deactivated_owner_is_refused(self, admin, stages):
        gone = UserFactory()
        identity_services.deactivate_user(actor_id=admin.pk, user_id=gone.pk)
        with pytest.raises(InvalidInputError):
            create(admin, ORG(admin.pk), owner_id=gone.pk)
        assert not Lead.objects.exists()

    def test_a_deactivated_users_workspace_takes_none(self, admin, stages):
        gone = UserFactory()
        identity_services.deactivate_user(actor_id=admin.pk, user_id=gone.pk)
        with pytest.raises(InvalidInputError) as caught:
            create(admin, FOR_USER(admin.pk, gone.pk))
        assert caught.value.details == {"owner": [lead_services.SUBJECT_NOT_ASSIGNABLE]}
        assert not Lead.objects.exists()
        assert not Opportunity.objects.exists()

    def test_a_sales_user_cant_create_in_someone_elses_workspace(self, user_a, user_b, stages):
        with pytest.raises(PermissionDeniedError):
            create(user_a, FOR_USER(user_a.pk, user_b.pk))

    def test_an_existing_leads_opportunity_takes_no_owner(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        with pytest.raises(InvalidInputError) as caught:
            services.create_opportunity(
                actor=user_a,
                scope=OWN(user_a.pk),
                lead_id=lead.pk,
                owner_id=user_a.pk,
                fields=CUSTOMER,
            )
        assert caught.value.details == {"owner": [services.OWNER_FOLLOWS_LEAD]}

    def test_a_retry_makes_one_record(self, user_a, stages):
        key = "6c1b2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
        first = services.create_opportunity(
            actor=user_a, scope=OWN(user_a.pk), lead_id=None, fields=CUSTOMER, idempotency_key=key
        )
        again = services.create_opportunity(
            actor=user_a, scope=OWN(user_a.pk), lead_id=None, fields=CUSTOMER, idempotency_key=key
        )
        assert (first.replayed, again.replayed) == (False, True)
        assert first.opportunity.pk == again.opportunity.pk
        assert Lead.objects.count() == Opportunity.objects.count() == 1


@pytest.fixture
def customer(user_a, stages):
    """One customer of A with two open opportunities (one archived), a won one, and current
    work on the customer and on an opportunity."""
    lead = LeadFactory(owner=user_a)
    first = OpportunityFactory(lead=lead, title="First")
    second = OpportunityFactory(lead=lead, title="Second", stage_key="proposal")
    won = OpportunityFactory(lead=lead, title="Won", stage_key="won")
    task = TaskFactory(lead=lead, opportunity=second)
    return lead, first, second, won, task


class TestReassign:
    def test_the_customer_and_its_current_work_move(self, admin, user_a, user_b, customer):
        lead, first, second, won, task = customer
        updated = reassign(admin, ORG(admin.pk), first, user_b)
        assert updated.owner_id == user_b.pk
        assert updated.version == first.version + 1
        assert Lead.objects.get(pk=lead.pk).owner_id == user_b.pk
        assert Opportunity.objects.get(pk=second.pk).owner_id == user_b.pk
        # Current work follows (activities' own subscriber; pipeline can't import it).
        assert (
            apps.get_model("activities", "Activity").objects.get(pk=task.pk).owner_id == user_b.pk
        )
        # Won and lost ones keep the owner who closed them.
        assert Opportunity.objects.get(pk=won.pk).owner_id == user_a.pk
        assert AuditEvent.objects.filter(
            target_id=str(lead.pk), action="lead.reassigned", actor_id=admin.pk
        ).exists()

    def test_from_a_users_workspace(self, admin, user_a, user_b, customer):
        _, first, *_ = customer
        updated = reassign(admin, FOR_USER(admin.pk, user_a.pk), first, user_b)
        assert updated.owner_id == user_b.pk

    def test_needs_the_assign_capability(self, user_a, user_b, customer):
        _, first, *_ = customer
        with pytest.raises(PermissionDeniedError):
            reassign(user_a, OWN(user_a.pk), first, user_b)
        assert Opportunity.objects.get(pk=first.pk).owner_id == user_a.pk

    def test_only_open_opportunities(self, admin, user_b, customer):
        *_, won, _ = customer
        with pytest.raises(BusinessRuleViolation, match="only open ones change owner"):
            reassign(admin, ORG(admin.pk), won, user_b)

    def test_archived_ones_are_read_only(self, admin, user_b, customer):
        _, first, *_ = customer
        Opportunity.objects.filter(pk=first.pk).update(archived_at="2026-10-01T10:00:00Z")
        with pytest.raises(BusinessRuleViolation, match="archived"):
            reassign(admin, ORG(admin.pk), first, user_b)

    def test_an_archived_customer_record_is_read_only(self, admin, user_b, customer):
        lead, first, *_ = customer
        Lead.objects.filter(pk=lead.pk).update(archived_at="2026-10-01T10:00:00Z")
        with pytest.raises(BusinessRuleViolation, match="customer's record is archived"):
            reassign(admin, ORG(admin.pk), first, user_b)

    def test_needs_the_version_seen(self, admin, user_b, customer):
        _, first, *_ = customer
        with pytest.raises(ConflictError):
            reassign(admin, ORG(admin.pk), first, user_b, version=first.version + 5)

    def test_the_current_owner_changes_nothing(self, admin, user_a, customer):
        lead, first, *_ = customer
        updated = reassign(admin, ORG(admin.pk), first, user_a, version=99)
        assert (updated.owner_id, updated.version) == (user_a.pk, first.version)
        assert Lead.objects.get(pk=lead.pk).version == lead.version

    def test_only_to_an_active_user(self, admin, customer):
        _, first, *_ = customer
        gone = UserFactory()
        identity_services.deactivate_user(actor_id=admin.pk, user_id=gone.pk)
        with pytest.raises(InvalidInputError):
            reassign(admin, ORG(admin.pk), first, gone)

    def test_outside_the_workspace_it_doesnt_exist(self, admin, user_b, customer):
        _, first, *_ = customer
        with pytest.raises(NotFoundError):
            reassign(admin, FOR_USER(admin.pk, user_b.pk), first, user_b)


class TestApi:
    def body(self, **extra):
        return {
            "value": "1200000",
            "account_name": "Apollo Diagnostics",
            "customer_name": "Asharaf Panka",
            **extra,
        }

    def test_create_without_a_lead(self, user_a_client, user_a, stages):
        response = user_a_client.post(
            opportunities_url(), self.body(), format="json", headers=key_header()
        )
        assert response.status_code == 201
        body = response.json()
        assert body["owner"]["id"] == str(user_a.pk)
        assert body["lead"]["display_name"] == "Asharaf Panka"
        assert body["title"] == "Asharaf Panka"

    def test_organisation_wide_needs_an_owner(self, admin_client, user_b, stages):
        missing = admin_client.post(
            opportunities_url("all"), self.body(), format="json", headers=key_header()
        )
        assert missing.status_code == 400
        assert missing.json()["error"]["details"] == {"owner": [services.OWNER_REQUIRED]}
        made = admin_client.post(
            opportunities_url("all"),
            self.body(owner=str(user_b.pk)),
            format="json",
            headers=key_header(),
        )
        assert (made.status_code, made.json()["owner"]["id"]) == (201, str(user_b.pk))

    def test_assign(self, admin_client, user_a, user_b, customer):
        _, first, *_ = customer
        response = admin_client.post(
            opportunity_url(first.pk, "all", "assign"),
            {"owner": str(user_b.pk), "version": first.version},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["owner"]["id"] == str(user_b.pk)

    def test_assign_is_refused_to_a_sales_user(self, user_a_client, user_b, customer):
        _, first, *_ = customer
        response = user_a_client.post(
            opportunity_url(first.pk, action="assign"),
            {"owner": str(user_b.pk), "version": first.version},
            format="json",
        )
        assert response.status_code == 403

    def test_search_finds_a_deal_by_its_customer(self, user_a, stages):
        OpportunityFactory(
            lead=LeadFactory(owner=user_a),
            title="Analyzer",
            account_name="Zephyr Diagnostics",
            customer_name="Ivo Kestrel",
        )
        client = signed_in(user_a)
        for query in ("Kestrel", "Zephyr", "Analyzer"):
            body = client.get("/api/v1/workspaces/me/search", {"q": query}).json()
            assert [r["title"] for r in body["opportunities"]["results"]] == ["Analyzer"], query
