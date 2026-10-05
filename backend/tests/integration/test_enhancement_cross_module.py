"""Product enhancement phase behaviour that spans modules, kept outside the layered
package (identity may not import the CRM modules, activities may not import privacy):
support sessions stamping CRM writes, and erasure racing an upload."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

import pytest
from django.core.files.storage import storages

from arkray.activities import attachments, storage
from arkray.activities.models import Attachment
from arkray.audit.models import AuditEvent
from arkray.leads.models import Lead
from arkray.pipeline.models import NegotiationPrice
from arkray.privacy import services as privacy
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory, default_stage
from tests.helpers import drain_outbox

pytestmark = pytest.mark.django_db


def test_a_support_sessions_changes_record_the_administrator_the_user_and_the_session(
    admin, admin_client, user_a
):
    """docs/admin-user-workspace.md#support-sessions: never recorded as the user's own act."""
    started = admin_client.post(
        "/api/v1/admin/support-sessions",
        {"user": str(user_a.pk), "reason": "Customer asked for help with a deal"},
        format="json",
    )
    assert started.status_code == 201, started.content
    session = started.json()
    response = admin_client.post(
        f"/api/v1/workspaces/{user_a.pk}/leads", {"first_name": "Asha"}, format="json"
    )
    assert response.status_code == 201, response.content
    lead = Lead.objects.get(pk=response.json()["id"])
    assert (lead.owner_id, lead.created_by_id) == (user_a.pk, admin.pk)
    event = AuditEvent.objects.get(action="lead.created")
    assert (event.actor_id, event.subject_user_id, str(event.support_session_id)) == (
        admin.pk,
        user_a.pk,
        session["id"],
    )
    opp = OpportunityFactory(lead=lead, stage=default_stage("proposal"))
    moved = admin_client.post(
        f"/api/v1/workspaces/{user_a.pk}/opportunities/{opp.pk}/move",
        {"stage": str(default_stage("negotiation").pk), "version": 1, "negotiated_price": "7"},
        format="json",
    )
    assert moved.status_code == 200, moved.content
    price = NegotiationPrice.objects.get()
    assert (price.actor_id, price.subject_user_id, str(price.support_session_id)) == (
        admin.pk,
        user_a.pk,
        session["id"],
    )
    assert price.price == Decimal("7.00")


def test_an_erasure_during_an_upload_still_removes_the_file(user_a, user_a_client, monkeypatch):
    """Enhancement review P1: the purge found no object yet and marked the row purged; the
    upload then wrote it and marked it stored: personal data kept for good, and a 500."""
    lead = LeadFactory(owner=user_a)
    note = NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)
    save = storage.save

    def erased_while_writing(key, content):
        privacy.erase(note.lead_id, operator_id=user_a.pk)
        drain_outbox()  # the purge job runs before the bytes land: nothing to remove yet
        save(key, content)

    monkeypatch.setattr(storage, "save", erased_while_writing)
    response = user_a_client.post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        b"personal",
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote("x.txt", safe=""),
    )
    assert response.status_code == 404
    row = Attachment.objects.get()
    assert row.deleted_at is not None
    assert row.sha256 == attachments.ERASED_SHA256  # the fingerprint goes with the name
    assert storages["attachments"].exists(row.storage_key)
    drain_outbox()
    assert not storages["attachments"].exists(row.storage_key)
    assert Attachment.objects.get().purged_at is not None
