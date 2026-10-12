"""The restore drills' steps inside Django (tests/drills/restore_drill.py and pitr_drill.py
run each in a fresh interpreter against the database the drill points it at). Synthetic data
only."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from django.conf import settings
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.activities import services as activity_services
from arkray.activities import storage
from arkray.activities.models import Activity, Attachment
from arkray.ai.models import KnowledgeChunk
from arkray.core.access import AccessScope
from arkray.core.ledger_gate import GATE
from arkray.core.models import OutboxEvent
from arkray.identity import services as identity_services
from arkray.identity.models import Role, User
from arkray.leads.models import Lead
from arkray.pipeline import configuration
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import CustomField, FieldType, Opportunity, Pipeline
from arkray.privacy import services as privacy_services
from arkray.privacy import staff
from tests.helpers import drain_outbox, signed_in

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


# Point-in-time recovery (tests/drills/pitr_drill.py). The recovery target T falls between
# pitr_before_target and pitr_after_target: everything the first writes must come back, nothing
# the second writes may, and the erasure the second makes must be re-applied from the ledger.
EARLY = {"customer_name": "Drill Early Erasure", "account_name": "Early Labs"}
LATE = {"customer_name": "Drill Late Customer", "account_name": "Late Labs"}
PITR_DEALS = 20


def _note(actor: User, deal: Opportunity, text: str) -> Activity:
    return activity_services.create_activity(
        actor=actor,
        scope=AccessScope.own(actor.pk),
        activity_type="note",
        fields={"description": text},
        lead_id=deal.lead_id,
        opportunity_id=deal.pk,
    ).activity


def pitr_before_target(raw: str) -> dict[str, Any]:
    """Before T: more synthetic deals with a note each, and one lead erased (the ledger's
    first entry: the recovered database has applied it, so the replay must leave it)."""
    seeded = json.loads(raw)
    admin = User.objects.get(pk=seeded["admin_id"])
    sales = User.objects.get(pk=seeded["sales_id"])
    for number in range(PITR_DEALS):
        deal = _deal(
            sales,
            {
                "customer_name": f"Drill Customer {number:02d}",
                "account_name": f"Drill Account {number:02d}",
            },
        )
        _note(sales, deal, f"Drill note {number:02d}: asked for a demonstration.")
    early = _deal(sales, EARLY)
    _note(sales, early, "Drill Early Erasure asked to be forgotten.")
    drain_outbox(rounds=20)
    privacy_services.erase(early.lead_id, operator_id=admin.pk)
    drain_outbox(rounds=20)
    # A browser signed in before T (and signed out after it, below).
    browser = signed_in(sales, APIClient(HTTP_HOST="localhost"))
    return {
        "early_lead_id": str(early.lead_id),
        "session_key": browser.cookies[settings.SESSION_COOKIE_NAME].value,
    }


def _browser(session_key: str) -> Client:
    browser = Client(HTTP_HOST="localhost")
    browser.cookies[settings.SESSION_COOKIE_NAME] = session_key
    return browser


def pitr_after_target(raw: str) -> dict[str, Any]:
    """After T: the browser signed in before T signs out, the customer is erased (with their
    file: ledgered), and rows a recovery to T must not have are added: a user, a deal with its
    lead, a note and a stored file (whose object stays in storage without a row: the orphan
    the attachment reconciliation must report)."""
    seeded = json.loads(raw)
    admin = User.objects.get(pk=seeded["admin_id"])
    sales = User.objects.get(pk=seeded["sales_id"])
    browser = _browser(seeded["session_key"])
    signed_in = browser.get("/api/v1/auth/me").status_code
    browser.logout()
    privacy_services.erase(UUID(seeded["lead_id"]), operator_id=admin.pk)
    late_user = User.objects.create_user(
        "drill-late@example.invalid",
        "Drill",
        "Late",
        role=Role.SALES_USER,
        password=secrets.token_urlsafe(24),
    )
    late = _deal(sales, LATE)
    note = _note(sales, late, "Drill Late Customer called after the target time.")
    late_file = _file(note.pk, sales, b"%PDF-1.7 drill late customer\n%%EOF\n")
    drain_outbox(rounds=20)
    return {
        "session_before_sign_out": signed_in,
        "session_after_sign_out": _browser(seeded["session_key"])
        .get("/api/v1/auth/me")
        .status_code,
        "late_user_id": str(late_user.pk),
        "late_lead_id": str(late.lead_id),
        "late_deal_id": str(late.pk),
        "late_note_id": str(note.pk),
        "late_file_id": str(late_file.pk),
    }


def pitr_session(session_key: str) -> dict[str, Any]:
    """Whether the browser that signed out after T is signed in again on the recovered
    database (its session row is as it was at T)."""
    GATE.reset()
    return {"status": _browser(session_key).get("/api/v1/auth/me").status_code}


def pitr_inspect(raw: str) -> dict[str, Any]:
    """The recovered database, as the application sees it (ids in, outcomes and counts out)."""
    ids = json.loads(raw)
    GATE.reset()
    gate = GATE.status()
    response = Client(HTTP_HOST="localhost").get("/api/v1/auth/csrf")
    code = response.json()["error"]["code"] if response.status_code >= 400 else None
    customer = Lead.objects.get(pk=ids["lead_id"])
    outbox = dict.fromkeys(("pending", "in_flight", "dead"), 0)
    for status in OutboxEvent.objects.filter(status__in=list(outbox)).values_list(
        "status", flat=True
    ):
        outbox[status] += 1
    return {
        "gate": gate,
        "api_status": response.status_code,
        "api_code": code,
        "customer_back": customer.first_name == CUSTOMER["customer_name"],
        "customer_erased": customer.first_name == privacy_services.ERASED,
        "customer_chunks": KnowledgeChunk.objects.filter(lead_id=customer.pk).count(),
        "early_erased": Lead.objects.get(pk=ids["early_lead_id"]).first_name
        == privacy_services.ERASED,
        "bystander_intact": Lead.objects.get(pk=ids["bystander_lead_id"]).first_name
        == BYSTANDER["customer_name"],
        "late_rows": {
            "user": User.objects.filter(pk=ids["late_user_id"]).exists(),
            "lead": Lead.objects.filter(pk=ids["late_lead_id"]).exists(),
            "deal": Opportunity.objects.filter(pk=ids["late_deal_id"]).exists(),
            "note": Activity.objects.filter(pk=ids["late_note_id"]).exists(),
            "file": Attachment.objects.filter(pk=ids["late_file_id"]).exists(),
        },
        "counts": {
            "users": User.objects.count(),
            "leads": Lead.objects.count(),
            "opportunities": Opportunity.objects.count(),
            "activities": Activity.objects.count(),
            "attachments": Attachment.objects.count(),
            "chunks": KnowledgeChunk.objects.count(),
        },
        "outbox": outbox,
    }
