"""Lead conversion (docs/pipeline.md#conversion): Convert creates the lead's opportunity and
sets the lead's status to Converted in one transaction. "Converted" always means the lead
has an opportunity: the plain status change and lead creation are refused without one."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.core.errors import BusinessRuleViolation, ConflictError, NotFoundError
from arkray.core.idempotency import IdempotencyKeyReused
from arkray.identity import services as identity_services
from arkray.leads import services as lead_services
from arkray.leads.events import LeadStatusChanged
from arkray.leads.models import Lead, LeadStatus
from arkray.pipeline import events, services
from arkray.pipeline.models import Opportunity, StageHistory
from tests.factories import LeadFactory, OpportunityFactory
from tests.helpers import collected

from .conftest import convert_url

pytestmark = pytest.mark.django_db

OWN = AccessScope.own
KEY = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
FIELDS = {"value": Decimal("1250000")}


def convert(actor, scope, lead, version=None, **kwargs):
    return services.convert_lead(
        actor=actor,
        scope=scope,
        lead_id=lead.pk,
        lead_version=lead.version if version is None else version,
        fields=kwargs.pop("fields", FIELDS),
        **kwargs,
    )


class TestConvert:
    def test_creates_one_opportunity_and_converts_the_lead_atomically(self, user_a, stages):
        lead = LeadFactory(owner=user_a, status_id="qualified")
        with (
            collected(events.OpportunityCreated) as created,
            collected(LeadStatusChanged) as status_changed,
        ):
            result = convert(user_a, OWN(user_a.pk), lead)
        opportunity = result.opportunity
        assert result.replayed is False
        assert Opportunity.objects.filter(lead=lead).count() == 1
        assert (opportunity.owner_id, opportunity.created_by_id) == (user_a.pk, user_a.pk)
        assert opportunity.pipeline.is_default
        assert (opportunity.stage_id, opportunity.status) == (stages["new"].pk, "open")
        assert opportunity.value == Decimal("1250000.00")
        assert (result.lead.status.key, result.lead.version) == ("converted", 2)
        assert [e.via_conversion for e in created] == [True]
        assert [(e.from_status, e.to_status) for e in status_changed] == [
            ("qualified", "converted")
        ]
        assert StageHistory.objects.filter(opportunity=opportunity).count() == 1
        assert list(AuditEvent.objects.order_by("id").values_list("action", flat=True)) == [
            "opportunity.created",
            "lead.status_changed",
            "lead.converted",
        ]
        converted = AuditEvent.objects.get(action="lead.converted")
        assert converted.metadata == {
            "workspace": "self",
            "opportunity_id": str(opportunity.pk),
            "from_status": "qualified",
            "to_status": "converted",
        }
        assert AuditEvent.objects.get(action="opportunity.created").metadata["via"] == "conversion"

    def test_into_a_chosen_stage(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        result = convert(user_a, OWN(user_a.pk), lead, stage_id=stages["proposal"].pk)
        assert result.opportunity.stage_id == stages["proposal"].pk

    def test_an_administrator_converts_for_the_leads_owner(self, admin, user_a, stages):
        lead = LeadFactory(owner=user_a)
        result = convert(admin, AccessScope.for_user(admin.pk, user_a.pk), lead)
        assert (result.opportunity.owner_id, result.opportunity.created_by_id) == (
            user_a.pk,
            admin.pk,
        )
        event = AuditEvent.objects.get(action="lead.converted")
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)

    def test_converting_twice_is_refused_and_creates_nothing(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        convert(user_a, OWN(user_a.pk), lead)
        lead.refresh_from_db()
        with pytest.raises(BusinessRuleViolation, match="already been converted"):
            convert(user_a, OWN(user_a.pk), lead)
        assert Opportunity.objects.count() == 1

    def test_a_double_click_without_a_key_converts_once(self, user_a, stages):
        """The second click carries the version the page showed: stale now (409)."""
        lead = LeadFactory(owner=user_a)
        convert(user_a, OWN(user_a.pk), lead, version=1)
        with pytest.raises(ConflictError):
            convert(user_a, OWN(user_a.pk), lead, version=1)
        assert Opportunity.objects.count() == 1

    def test_with_a_key_a_retry_replays_the_first_conversion(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        first = convert(user_a, OWN(user_a.pk), lead, version=1, idempotency_key=KEY)
        again = convert(user_a, OWN(user_a.pk), lead, version=1, idempotency_key=KEY)
        assert (first.replayed, again.replayed) == (False, True)
        assert again.opportunity.pk == first.opportunity.pk
        assert again.lead.status.key == "converted"
        assert Opportunity.objects.count() == 1
        assert AuditEvent.objects.filter(action="lead.converted").count() == 1

    def test_a_key_reused_for_a_different_conversion_is_refused(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        convert(user_a, OWN(user_a.pk), lead, version=1, idempotency_key=KEY)
        with pytest.raises(IdempotencyKeyReused):
            convert(
                user_a,
                OWN(user_a.pk),
                lead,
                version=1,
                idempotency_key=KEY,
                fields={**FIELDS, "value": Decimal("1")},
            )

    def test_stale_lead_version(self, user_a, stages):
        lead = LeadFactory(owner=user_a, version=3)
        with pytest.raises(ConflictError):
            convert(user_a, OWN(user_a.pk), lead, version=2)
        assert not Opportunity.objects.exists()

    def test_archived_leads_cant_be_converted(self, user_a, stages):
        lead = LeadFactory(owner=user_a, archived_at=timezone.now())
        with pytest.raises(BusinessRuleViolation, match="archived"):
            convert(user_a, OWN(user_a.pk), lead)

    def test_someone_elses_lead_does_not_exist(self, user_a, user_b, stages):
        lead = LeadFactory(owner=user_b)
        with pytest.raises(NotFoundError):
            convert(user_a, OWN(user_a.pk), lead)
        lead.refresh_from_db()
        assert (lead.status_id, Opportunity.objects.count()) == ("new", 0)

    def test_a_deactivated_owners_lead_isnt_converted(self, admin, user_a, stages):
        lead = LeadFactory(owner=user_a)
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        with pytest.raises(BusinessRuleViolation, match="deactivated"):
            convert(admin, AccessScope.organization(admin.pk), lead)

    def test_without_an_active_converted_status(self, user_a, stages):
        LeadStatus.objects.filter(category="converted").update(is_active=False)
        with pytest.raises(BusinessRuleViolation, match="configured"):
            convert(user_a, OWN(user_a.pk), LeadFactory(owner=user_a))
        assert not Opportunity.objects.exists()


class TestNoPartialConversion:
    def test_a_failure_after_the_opportunity_was_created_undoes_everything(
        self, user_a, stages, monkeypatch
    ):
        lead = LeadFactory(owner=user_a, status_id="qualified")

        def fail(**_):
            raise RuntimeError("the database went away halfway through")

        monkeypatch.setattr(services.lead_services, "change_status", fail)
        with pytest.raises(RuntimeError, match="halfway"):
            convert(user_a, OWN(user_a.pk), lead, idempotency_key=KEY)
        lead.refresh_from_db()
        assert (lead.status_id, lead.version) == ("qualified", 1)
        assert not Opportunity.objects.exists()
        assert not StageHistory.objects.exists()
        assert not AuditEvent.objects.exists()
        # The key wasn't consumed: a retry performs the conversion.
        monkeypatch.undo()
        assert convert(user_a, OWN(user_a.pk), lead, idempotency_key=KEY).replayed is False

    def test_a_failing_subscriber_undoes_everything(self, user_a, stages):
        lead = LeadFactory(owner=user_a)

        def boom(_event):
            raise RuntimeError("subscriber failed")

        with subscribed(LeadStatusChanged, boom), pytest.raises(RuntimeError):
            convert(user_a, OWN(user_a.pk), lead)
        lead.refresh_from_db()
        assert (lead.status_id, Opportunity.objects.count(), AuditEvent.objects.count()) == (
            "new",
            0,
            0,
        )


class TestConvertedAlwaysHasAnOpportunity:
    def change_status(self, user, lead, status):
        return lead_services.change_status(
            actor=user, scope=OWN(user.pk), lead_id=lead.pk, version=lead.version, status=status
        )

    def test_the_plain_status_change_needs_an_opportunity(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        with pytest.raises(BusinessRuleViolation, match="Convert"):
            self.change_status(user_a, lead, "converted")
        lead.refresh_from_db()
        assert (lead.status_id, AuditEvent.objects.count()) == ("new", 0)

    def test_with_an_existing_opportunity_the_status_can_be_set(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        OpportunityFactory(lead=lead)
        assert self.change_status(user_a, lead, "converted").status_id == "converted"

    def test_an_archived_or_closed_opportunity_still_counts(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        OpportunityFactory(lead=lead, stage=stages["lost"], archived_at=timezone.now())
        assert self.change_status(user_a, lead, "converted").status_id == "converted"

    def test_a_converted_lead_can_be_corrected_and_converted_again(self, user_a, stages):
        lead = LeadFactory(owner=user_a)
        convert(user_a, OWN(user_a.pk), lead)
        lead.refresh_from_db()
        corrected = self.change_status(user_a, lead, "qualified")
        assert corrected.status_id == "qualified"
        again = convert(user_a, OWN(user_a.pk), corrected)
        assert again.lead.status.key == "converted"
        assert Opportunity.objects.filter(lead=lead).count() == 2

    def test_a_new_lead_cant_start_as_converted(self, user_a, stages):
        with pytest.raises(BusinessRuleViolation):
            lead_services.create_lead(
                actor=user_a,
                scope=OWN(user_a.pk),
                fields={"first_name": "Asha"},
                status="converted",
            )
        assert not Lead.objects.exists()


class TestApi:
    def test_convert_endpoint(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.post(
            convert_url(lead.pk),
            {
                "version": 1,
                "value": "1250000.00",
                "stage": str(stages["proposal"].pk),
            },
            format="json",
            headers={"Idempotency-Key": KEY},
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["lead"]["status"]["key"] == "converted"
        assert body["opportunity"]["value"] == "1250000.00"
        # Named after the lead (no customer or instrument given): nobody types the name.
        assert body["opportunity"]["title"] == lead.display_name
        assert body["opportunity"]["stage"]["key"] == "proposal"
        assert body["opportunity"]["lead"] == {
            "id": str(lead.pk),
            "display_name": lead.display_name,
            "organization_name": lead.organization_name,
            "restricted": False,
        }
        assert response["Location"] == (
            f"/api/v1/workspaces/me/opportunities/{body['opportunity']['id']}"
        )
        replay = user_a_client.post(
            convert_url(lead.pk),
            {
                "version": 1,
                "value": "1250000.00",
                "stage": str(stages["proposal"].pk),
            },
            format="json",
            headers={"Idempotency-Key": KEY},
        )
        assert (replay.status_code, replay["Idempotent-Replayed"]) == (201, "true")
        assert replay.json()["opportunity"]["id"] == body["opportunity"]["id"]

    @pytest.mark.parametrize(
        "extra",
        [
            {"owner": "x"},
            {"status": "won"},
            {"lead": "x"},
            {"created_by": "x"},
            {"closed_at": "x"},
            {"title": "x"},  # the name is derived, never typed (ADR-0028)
        ],
    )
    def test_strict_body(self, user_a_client, user_a, stages, extra):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.post(
            convert_url(lead.pk), {"version": 1, **FIELDS, "value": "1", **extra}, format="json"
        )
        assert response.status_code == 400
        assert not Opportunity.objects.exists()

    def test_the_status_endpoint_explains_how_to_convert(self, user_a_client, user_a, stages):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.post(
            f"/api/v1/workspaces/me/leads/{lead.pk}/status",
            {"status": "converted", "version": 1},
            format="json",
        )
        assert response.status_code == 422
        assert "Convert" in response.json()["error"]["message"]

    def test_creating_a_lead_as_converted_is_refused(self, user_a_client, stages):
        response = user_a_client.post(
            "/api/v1/workspaces/me/leads",
            {"first_name": "Asha", "status": "converted"},
            format="json",
        )
        assert response.status_code == 422
