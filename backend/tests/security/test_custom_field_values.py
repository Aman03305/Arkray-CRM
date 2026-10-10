"""Deleting a removed custom field's stored values (privacy remediation P2-11;
pipeline.field_values; docs/privacy.md#custom-fields).

Removal keeps values hidden (reversible); deleting them is a separate, confirmed, audited and
ledgered decision, carried out by a job that removes the key row by row atomically, so a
concurrent edit of the deal's other values survives. Legal holds keep their deals' values.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import ConflictError
from arkray.core.models import HoldSubject, LegalHold, OutboxEvent
from arkray.pipeline import configuration, field_values, services
from arkray.pipeline.models import CustomField, FieldType, Opportunity, Pipeline
from tests.factories import LeadFactory, OpportunityFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(admin):
    pipeline = Pipeline.objects.get(is_default=True)
    notes = CustomField.objects.create(
        pipeline=pipeline, name="Contact notes", field_type=FieldType.TEXT, position=50
    )
    volume = CustomField.objects.create(
        pipeline=pipeline, name="Tests a day", field_type=FieldType.NUMBER, position=51
    )
    deals = []
    for index in range(5):
        deal = OpportunityFactory(lead=LeadFactory(), pipeline=pipeline)
        Opportunity.objects.filter(pk=deal.pk).update(
            custom_fields={str(notes.pk): f"Mobile 98111 0000{index}", str(volume.pk): "300"}
        )
        deals.append(deal)
    pipeline.refresh_from_db()
    return {"pipeline": pipeline, "notes": notes, "volume": volume, "deals": deals}


def keep_only_volume(admin, setup, **kwargs):
    pipeline = setup["pipeline"]
    pipeline.refresh_from_db()
    volume = setup["volume"]
    return configuration.replace_fields(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        pipeline_id=pipeline.pk,
        version=pipeline.version,
        fields=[{"id": volume.pk, "name": volume.name, "type": "number"}],
        **kwargs,
    )


def values_of(field: CustomField) -> list[Any]:
    return [
        d.custom_fields.get(str(field.pk))
        for d in Opportunity.objects.filter(pipeline=field.pipeline_id)
    ]


def test_removing_a_field_keeps_its_values_by_default(admin, setup):
    keep_only_volume(admin, setup)
    drain_outbox()
    assert sum(v is not None for v in values_of(setup["notes"])) == 5  # kept, hidden


def test_removing_with_deletion_deletes_every_value_and_keeps_the_rest(admin, setup):
    keep_only_volume(admin, setup, delete_removed_values=True)
    assert AuditEvent.objects.filter(action=field_values.AUDIT_REQUESTED).count() == 1
    assert OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).count() == 1
    drain_outbox()
    assert all(v is None for v in values_of(setup["notes"]))
    assert values_of(setup["volume"]).count("300") == 5  # the other field untouched
    done = AuditEvent.objects.get(action=field_values.AUDIT_PURGED)
    assert done.metadata == {"rows": 5, "held": 0}
    assert "98111" not in json.dumps(list(AuditEvent.objects.values("metadata")))


def test_a_removed_fields_values_are_deleted_later_on_confirmation(admin, setup):
    keep_only_volume(admin, setup)
    client = signed_in(admin)
    url = (
        f"/api/v1/workspaces/all/pipelines/{setup['pipeline'].pk}/fields/"
        f"{setup['notes'].pk}/delete-values"
    )
    assert client.post(url, {"confirm_name": "wrong"}, format="json").status_code == 400
    assert client.post(url, {"confirm_name": "contact NOTES"}, format="json").status_code == 202
    drain_outbox()
    assert all(v is None for v in values_of(setup["notes"]))


def test_an_active_fields_values_cannot_be_deleted(admin, setup):
    url = (
        f"/api/v1/workspaces/all/pipelines/{setup['pipeline'].pk}/fields/"
        f"{setup['notes'].pk}/delete-values"
    )
    response = signed_in(admin).post(url, {"confirm_name": "Contact notes"}, format="json")
    assert response.status_code == 422
    assert sum(v is not None for v in values_of(setup["notes"])) == 5


def test_a_sales_user_cannot_delete_a_shared_pipelines_values(user_a, admin, setup):
    keep_only_volume(admin, setup)
    url = (
        f"/api/v1/workspaces/me/pipelines/{setup['pipeline'].pk}/fields/"
        f"{setup['notes'].pk}/delete-values"
    )
    response = signed_in(user_a).post(url, {"confirm_name": "Contact notes"}, format="json")
    assert response.status_code in (403, 404)


def test_a_legal_hold_keeps_its_deals_values(admin, setup):
    held = setup["deals"][0]
    LegalHold.objects.create(
        subject_type=HoldSubject.LEAD,
        subject_id=held.lead_id,
        reference="CASE-5",
        placed_by=admin.pk,
    )
    keep_only_volume(admin, setup, delete_removed_values=True)
    drain_outbox()
    held.refresh_from_db()
    assert held.custom_fields.get(str(setup["notes"].pk))
    assert AuditEvent.objects.get(action=field_values.AUDIT_PURGED).metadata == {
        "rows": 4,
        "held": 1,
    }


def test_a_failed_change_deletes_nothing(admin, setup):
    """Rollback: the request is part of the configuration change; refused, nothing is queued
    or recorded and the field stays."""
    pipeline = setup["pipeline"]
    with pytest.raises(ConflictError):  # a stale version
        configuration.replace_fields(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            pipeline_id=pipeline.pk,
            version=pipeline.version + 7,
            fields=[],
            delete_removed_values=True,
        )
    assert not OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).exists()
    assert CustomField.objects.get(pk=setup["notes"].pk).is_active


def test_a_concurrent_edit_of_other_values_survives_the_deletion(admin, setup):
    deal = setup["deals"][1]
    keep_only_volume(admin, setup, delete_removed_values=True)
    # A writer changes the remaining field while the job hasn't run yet.
    deal.refresh_from_db()
    services.update_opportunity(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        opportunity_id=deal.pk,
        version=deal.version,
        changes={"custom_fields": {str(setup["volume"].pk): "450"}},
    )
    drain_outbox()
    deal.refresh_from_db()
    assert deal.custom_fields == {str(setup["volume"].pk): "450"}


def test_the_deletion_is_idempotent(admin, setup):
    keep_only_volume(admin, setup, delete_removed_values=True)
    drain_outbox()
    assert field_values.purge(setup["notes"].pk) == {"rows": 0, "held": 0}
