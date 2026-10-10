"""The restore drill's steps inside Django (tests/drills/restore_drill.py runs each in a
fresh interpreter against the database the drill points it at). Synthetic data only."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from django.test import Client
from django.utils import timezone

from arkray.activities import services as activity_services
from arkray.activities import storage
from arkray.activities.models import Attachment
from arkray.ai.models import KnowledgeChunk
from arkray.core.access import AccessScope
from arkray.core.ledger_gate import GATE
from arkray.identity import services as identity_services
from arkray.identity.models import Role, User
from arkray.leads.models import Lead
from arkray.pipeline import configuration
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import CustomField, FieldType, Opportunity, Pipeline
from arkray.privacy import services as privacy_services
from arkray.privacy import staff
from tests.helpers import drain_outbox

CUSTOMER = {"customer_name": "Drill Customer", "account_name": "Drill Labs"}
BYSTANDER = {"customer_name": "Drill Bystander", "account_name": "Bystander Labs"}


def _file(note_id: UUID, uploader: User, content: bytes) -> Attachment:
    key = f"drill/{uuid4().hex}"
    storage.save(key, io.BytesIO(content))
    return Attachment.objects.create(
        note_id=note_id,
        original_name="Drill_Customer_Report.pdf",
        extension="pdf",
        content_type="application/pdf",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_key=key,
        state="stored",
        stored_at=timezone.now(),
        uploaded_by=uploader,
    )


def _deal(actor: User, fields: dict[str, Any]) -> Opportunity:
    return pipeline_services.create_opportunity(
        actor=actor,
        scope=AccessScope.own(actor.pk),
        lead_id=None,
        fields={"value": Decimal("500000"), "contact_email": "drill@example.invalid", **fields},
    ).opportunity


def seed() -> dict[str, Any]:
    password = secrets.token_urlsafe(24)
    admin = User.objects.create_superuser("drill-admin@example.invalid", "Drill", "Admin", password)
    sales = User.objects.create_user(
        "drill-sales@example.invalid", "Drill", "Sales", role=Role.SALES_USER, password=password
    )
    former = User.objects.create_user(
        "drill-former@example.invalid", "Drill", "Former", role=Role.SALES_USER, password=password
    )
    identity_services.deactivate_user(actor_id=admin.pk, user_id=former.pk)
    deal = _deal(sales, CUSTOMER)
    other = _deal(sales, BYSTANDER)
    scope = AccessScope.own(sales.pk)
    note = activity_services.create_activity(
        actor=sales,
        scope=scope,
        activity_type="note",
        fields={"description": "Drill Customer asked about the analyser warranty."},
        lead_id=deal.lead_id,
        opportunity_id=deal.pk,
    ).activity
    other_note = activity_services.create_activity(
        actor=sales,
        scope=scope,
        activity_type="note",
        fields={"description": "Bystander note."},
        lead_id=other.lead_id,
        opportunity_id=other.pk,
    ).activity
    erased_file = _file(note.pk, sales, b"%PDF-1.7 drill customer\n%%EOF\n")
    deleted_file = _file(other_note.pk, sales, b"%PDF-1.7 drill bystander\n%%EOF\n")
    pipeline = Pipeline.objects.get(pk=deal.pipeline_id)
    field = CustomField.objects.create(
        pipeline=pipeline, name="Drill contact notes", field_type=FieldType.TEXT, position=80
    )
    Opportunity.objects.filter(pk=other.pk).update(
        custom_fields={str(field.pk): "Call 98111 00000"}
    )
    drain_outbox(rounds=20)  # Ask Arkray's index
    return {
        "admin_email": admin.email,
        "admin_id": str(admin.pk),
        "sales_id": str(sales.pk),
        "former_id": str(former.pk),
        "lead_id": str(deal.lead_id),
        "bystander_lead_id": str(other.lead_id),
        "erased_file_id": str(erased_file.pk),
        "deleted_file_id": str(deleted_file.pk),
        "field_id": str(field.pk),
        "pipeline_id": str(pipeline.pk),
        "chunks_before": KnowledgeChunk.objects.filter(lead_id=deal.lead_id).count(),
    }


def erase_after_backup(raw: str) -> dict[str, Any]:
    seeded = json.loads(raw)
    admin = User.objects.get(pk=seeded["admin_id"])
    sales = User.objects.get(pk=seeded["sales_id"])
    privacy_services.erase(UUID(seeded["lead_id"]), operator_id=admin.pk)
    from arkray.activities import attachments

    attachments.delete(
        actor=sales, scope=AccessScope.own(sales.pk), attachment_id=UUID(seeded["deleted_file_id"])
    )
    pipeline = Pipeline.objects.get(pk=seeded["pipeline_id"])
    kept = [
        {"id": f.pk, "name": f.name, "type": f.field_type, "required": f.required}
        for f in CustomField.objects.filter(pipeline=pipeline, is_active=True).exclude(
            pk=seeded["field_id"]
        )
    ]
    configuration.replace_fields(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        pipeline_id=pipeline.pk,
        version=pipeline.version,
        fields=kept,
        delete_removed_values=True,
    )
    staff.pseudonymise(UUID(seeded["former_id"]), operator_id=admin.pk)
    drain_outbox(rounds=20)  # the file purge and the custom-value deletion
    return {
        "operations": [
            "lead_erased",
            "attachment_deleted",
            "custom_values_deleted",
            "user_pseudonymised",
        ]
    }


def _api_status() -> int:
    return Client(HTTP_HOST="localhost").get("/api/v1/auth/csrf").status_code


def gate_only() -> dict[str, Any]:
    GATE.reset()
    return {"gate": GATE.status(), "api_status": _api_status()}


def inspect(raw: str) -> dict[str, Any]:
    seeded = json.loads(raw)
    GATE.reset()
    gate = GATE.status()
    lead = Lead.objects.get(pk=seeded["lead_id"])
    bystander = Lead.objects.get(pk=seeded["bystander_lead_id"])
    deleted = Attachment.objects.get(pk=seeded["deleted_file_id"])
    former = User.objects.get(pk=seeded["former_id"])
    return {
        "gate": gate,
        "api_status": _api_status(),
        "resurrected": lead.first_name == "Drill Customer",
        "lead_erased": lead.first_name == privacy_services.ERASED,
        "chunks": KnowledgeChunk.objects.filter(lead_id=lead.pk).count(),
        "attachment_forgotten": deleted.deleted_at is not None
        and deleted.purged_at is not None
        and deleted.original_name == "removed",
        "custom_values": Opportunity.objects.filter(
            custom_fields__has_key=seeded["field_id"]
        ).count(),
        "staff_pseudonymised": staff.pseudonymised(former),
        "bystander_intact": bystander.first_name == "Drill Bystander",
    }
