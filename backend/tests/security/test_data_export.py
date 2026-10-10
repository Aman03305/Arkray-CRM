"""Exporting a person's data on a verified access request (privacy remediation P2-5;
privacy.exports; docs/privacy.md#access-requests).

Content (everything about the subject, in full, archived included; JSON and CSV), isolation
(nothing about anyone else: other records and staff appear by role), authorisation (the
requesting administrator only, never in a support session), abuse limits, size caps, expiry
and auditing.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import timedelta
from urllib.parse import quote

import pytest
from django.core.files.storage import storages
from django.utils import timezone

from arkray.audit import services as audit
from arkray.audit.models import AuditEvent
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.privacy import exports
from arkray.privacy.models import DataExport
from arkray.privacy.tasks import housekeeping
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory, TaskFactory, UserFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

EXPORTS = "/api/v1/admin/privacy/exports"
LONG_NOTE = "Visit notes. " + "The lab manager discussed reagent supply in detail. " * 20
FORMULA = '=HYPERLINK("http://evil.example","click")'


@pytest.fixture
def customer(user_a):
    lead = LeadFactory(
        owner=user_a, first_name="Sunita", last_name="Rao", email="sunita@lab.example"
    )
    deal = OpportunityFactory(
        lead=lead, customer_name="Sunita Rao", contact_email="sunita@lab.example"
    )
    note = NoteFactory(lead=lead, opportunity=deal, created_by=user_a, description=LONG_NOTE)
    TaskFactory(lead=lead, opportunity=deal, title=FORMULA, archived_at=timezone.now())
    other = LeadFactory(
        owner=user_a, first_name="Other", last_name="Person", email="other@x.example"
    )
    return {"lead": lead, "deal": deal, "note": note, "other": other}


def request_export(client, subject_type, subject_id, **extra):
    body = {
        "subject_type": subject_type,
        "subject_id": str(subject_id),
        "reference": "DSAR-2026-17",
        "identity_verified": True,
        **extra,
    }
    return client.post(EXPORTS, body, format="json")


def built(client, response) -> zipfile.ZipFile:
    assert response.status_code == 202, response.content
    export_id = response.json()["id"]
    drain_outbox()
    detail = client.get(f"{EXPORTS}/{export_id}").json()
    assert detail["status"] == "ready", detail
    download = client.get(f"{EXPORTS}/{export_id}/download")
    assert download.status_code == 200
    assert "no-store" in download["Cache-Control"]
    return zipfile.ZipFile(io.BytesIO(b"".join(download.streaming_content)))


def test_a_customer_export_holds_everything_in_full(admin, user_a, customer):
    client = signed_in(admin)
    note = customer["note"]
    upload = signed_in(user_a).post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        b"%PDF-1.7\n%%EOF\n",
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote("lab-report.pdf", safe=""),
    )
    assert upload.status_code == 201
    archive = built(client, request_export(client, "lead", customer["lead"].pk))
    names = archive.namelist()
    assert {"data.json", "README.txt", "csv/lead.csv", "csv/opportunities.csv"} <= set(names)
    assert any(n.startswith("files/") and n.endswith("lab-report.pdf") for n in names)
    data = json.loads(archive.read("data.json"))
    assert data["lead"]["email"] == "sunita@lab.example"
    assert data["opportunities"][0]["contact_email"] == "sunita@lab.example"
    texts = [a["description"] for a in data["activities"]]
    assert LONG_NOTE in texts  # full text, not the screens' 240-character preview
    assert any(a["archived_at"] for a in data["activities"])  # archived included
    everything = archive.read("data.json").decode() + "".join(
        archive.read(n).decode("utf-8-sig") for n in names if n.startswith("csv/")
    )
    assert "Other Person" not in everything  # nobody else's record
    assert "other@x.example" not in everything
    assert user_a.email not in everything  # staff by role only
    assert "Rahul" not in everything


def test_csv_cells_never_run_as_formulas(admin, customer):
    client = signed_in(admin)
    archive = built(client, request_export(client, "lead", customer["lead"].pk))
    rows = list(csv.DictReader(io.StringIO(archive.read("csv/activities.csv").decode("utf-8-sig"))))
    titles = [row["title"] for row in rows]
    assert "'" + FORMULA in titles
    assert FORMULA not in titles


def test_only_the_requesting_administrator_can_download(admin, user_a, customer):
    client = signed_in(admin)
    response = request_export(client, "lead", customer["lead"].pk)
    drain_outbox()
    export_id = response.json()["id"]
    other_admin = signed_in(UserFactory(role="admin"))
    assert other_admin.get(f"{EXPORTS}/{export_id}").status_code == 404
    assert other_admin.get(f"{EXPORTS}/{export_id}/download").status_code == 404
    sales = signed_in(user_a)
    assert sales.get(EXPORTS).status_code == 403
    assert request_export(sales, "lead", customer["lead"].pk).status_code == 403
    assert sales.get(f"{EXPORTS}/{export_id}/download").status_code == 403


def test_refused_inside_a_support_session(admin, user_a, customer):
    client = signed_in(admin)
    client.post("/api/v1/admin/support-sessions", {"user": str(user_a.pk)}, format="json")
    assert request_export(client, "lead", customer["lead"].pk).status_code in (403, 409)


@pytest.mark.parametrize(
    "extra",
    [{"identity_verified": False}, {"reference": "John Smith asked by phone"}, {"reference": ""}],
)
def test_identity_verification_and_a_reference_are_required(admin, customer, extra):
    assert request_export(signed_in(admin), "lead", customer["lead"].pk, **extra).status_code == 400


def test_an_unknown_or_erased_subject_has_nothing_to_export(admin, customer):
    from uuid import uuid4

    from arkray.privacy import services

    client = signed_in(admin)
    assert request_export(client, "lead", uuid4()).status_code == 404
    services.erase(customer["lead"].pk, operator_id=admin.pk)
    assert request_export(client, "lead", customer["lead"].pk).status_code == 422


def test_abuse_limits(admin, customer, settings):
    settings.EXPORT_MAX_PENDING_PER_ADMIN = 2
    client = signed_in(admin)
    for _ in range(2):
        assert request_export(client, "lead", customer["lead"].pk).status_code == 202
    assert request_export(client, "lead", customer["lead"].pk).status_code == 429


def test_files_over_the_size_cap_are_listed_not_included(admin, user_a, customer, settings):
    settings.EXPORT_MAX_BYTES = 30_000  # the JSON fits, the 40 kB file doesn't
    note = customer["note"]
    signed_in(user_a).post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        b"name,tests\n" + b"Lab,300\n" * 5000,
        content_type="application/octet-stream",
        HTTP_X_FILENAME="big.csv",
    )
    client = signed_in(admin)
    archive = built(client, request_export(client, "lead", customer["lead"].pk))
    assert not any(n.startswith("files/") for n in archive.namelist())
    assert "over the export's size limit" in archive.read("README.txt").decode()
    assert DataExport.objects.get().files_omitted == 1


def test_an_export_too_large_even_without_files_fails_cleanly(admin, customer, settings):
    settings.EXPORT_MAX_BYTES = 100
    client = signed_in(admin)
    response = request_export(client, "lead", customer["lead"].pk)
    drain_outbox()
    assert client.get(f"{EXPORTS}/{response.json()['id']}").json()["error_code"] == "too_large"


def test_expired_exports_are_deleted_and_audited(admin, customer):
    client = signed_in(admin)
    response = request_export(client, "lead", customer["lead"].pk)
    drain_outbox()
    export = DataExport.objects.get(pk=response.json()["id"])
    key = export.storage_key
    assert storages["exports"].exists(key)
    DataExport.objects.filter(pk=export.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert client.get(f"{EXPORTS}/{export.pk}/download").status_code == 422
    assert housekeeping() == {"expired_exports": 1}
    assert not storages["exports"].exists(key)
    assert housekeeping() == {"expired_exports": 0}  # idempotent
    trail = AuditEvent.objects.filter(action__startswith="privacy.export")
    assert {e.action for e in trail} == {"privacy.export_requested", "privacy.export_expired"}
    assert "Sunita" not in json.dumps(list(trail.values("metadata")))


def test_a_staff_export_holds_their_account_and_activity_only(admin, user_a, user_b):
    token = bind_context(ExecutionContext(correlation_id="r", client_ip="203.0.113.50"))
    try:
        audit.record("auth.login", actor_id=user_a.pk, target_type="user", target_id=user_a.pk)
        audit.record("auth.login", actor_id=user_b.pk, target_type="user", target_id=user_b.pk)
    finally:
        reset_context(token)
    client = signed_in(admin)
    archive = built(client, request_export(client, "user", user_a.pk))
    data = json.loads(archive.read("data.json"))
    assert data["user"]["email"] == user_a.email
    logins = [e for e in data["audit_events"] if e["action"] == "auth.login"]
    assert logins
    assert logins[0]["client_address"] == "203.0.113.50"
    assert user_b.email not in archive.read("data.json").decode()
    assert set(data["records_owned"]) == {"leads", "opportunities", "activities"}


def test_requests_and_downloads_are_audited(admin, customer):
    client = signed_in(admin)
    built(client, request_export(client, "lead", customer["lead"].pk))
    requested = AuditEvent.objects.get(action=exports.AUDIT_REQUESTED)
    assert requested.metadata["reference"] == "DSAR-2026-17"
    assert requested.metadata["identity_verified"] is True
    assert AuditEvent.objects.filter(action=exports.AUDIT_DOWNLOADED).count() == 1


# --- backend review findings (P2-5, P2-6, P2-7) -------------------------------------------------
def test_erasing_the_customer_withdraws_their_ready_export(
    admin, customer, django_capture_on_commit_callbacks
):
    from arkray.privacy import services

    client = signed_in(admin)
    response = request_export(client, "lead", customer["lead"].pk)
    drain_outbox()
    export = DataExport.objects.get(pk=response.json()["id"])
    assert export.status == "ready"
    key = export.storage_key
    with django_capture_on_commit_callbacks(execute=True):
        services.erase(customer["lead"].pk, operator_id=admin.pk)
    export.refresh_from_db()
    assert (export.status, export.storage_key, export.error_code) == (
        "expired",
        "",
        "subject_erased",
    )
    assert not storages["exports"].exists(key)
    assert client.get(f"{EXPORTS}/{export.pk}/download").status_code in (404, 422)
    erased = AuditEvent.objects.get(action="lead.erased")
    assert erased.metadata["exports_withdrawn"] == 1


def test_pseudonymising_a_user_withdraws_their_export(
    admin, user_b, django_capture_on_commit_callbacks
):
    from arkray.identity import services as identity_services
    from arkray.privacy import staff

    client = signed_in(admin)
    response = request_export(client, "user", user_b.pk)  # queued, not yet built
    identity_services.deactivate_user(actor_id=admin.pk, user_id=user_b.pk)
    with django_capture_on_commit_callbacks(execute=True):
        staff.pseudonymise(user_b.pk, operator_id=admin.pk)
    drain_outbox()  # the build job finds it withdrawn and writes nothing
    export = DataExport.objects.get(pk=response.json()["id"])
    assert export.status == "expired"
    assert not storages["exports"].exists(exports.storage_key(export))


def test_a_build_that_finishes_after_its_export_ended_deletes_its_file(
    admin, customer, monkeypatch
):
    client = signed_in(admin)
    response = request_export(client, "lead", customer["lead"].pk)
    export_id = response.json()["id"]
    real_build = exports.build

    def build_then_lose_the_race(export):
        built_file = real_build(export)
        DataExport.objects.filter(pk=export.pk).update(status="failed", error_code="timed_out")
        return built_file

    monkeypatch.setattr(exports, "build", build_then_lose_the_race)
    drain_outbox()
    export = DataExport.objects.get(pk=export_id)
    assert export.status == "failed"
    assert not storages["exports"].exists(exports.storage_key(export))


def test_a_retried_build_reuses_its_key_and_a_dead_ones_file_is_deleted(admin, customer):
    client = signed_in(admin)
    export = DataExport.objects.get(
        pk=request_export(client, "lead", customer["lead"].pk).json()["id"]
    )
    first = exports.build(export)[0]  # a job that died after saving
    second = exports.build(export)[0]  # its retry
    assert first == second == exports.storage_key(export)
    DataExport.objects.filter(pk=export.pk).update(created_at=timezone.now() - timedelta(hours=48))
    export.refresh_from_db()
    housekeeping()
    export.refresh_from_db()
    assert export.status == "failed"
    assert not storages["exports"].exists(exports.storage_key(export))


def test_only_questions_about_the_customer_are_exported(admin, user_a, customer):
    from arkray.ai.models import Conversation, Question, QuestionStatus, WorkspaceKind

    conversation = Conversation.objects.create(
        actor=user_a, workspace_kind=WorkspaceKind.SELF, subject=user_a
    )
    now = timezone.now()

    def ask(text, answer):
        return Question.objects.create(
            conversation=conversation,
            actor=user_a,
            text=text,
            status=QuestionStatus.ANSWERED,
            mode="retrieval",
            answer=answer,
            finished_at=now,
            expires_at=now + timedelta(minutes=5),
        )

    about = ask(
        "What did Sunita Rao ask?",
        {"sources": [{"ref": f"note:{customer['note'].pk}", "label": "Visit"}], "blocks": []},
    )
    ask("And what about Other Person's lab?", {"sources": [], "blocks": []})
    client = signed_in(admin)
    data = json.loads(
        built(client, request_export(client, "lead", customer["lead"].pk)).read("data.json")
    )
    assert [q["question"] for q in data["ask_arkray"]] == [about.text]
    assert "Other Person" not in json.dumps(data)
