"""A file removed for good leaves nothing that identifies it (privacy remediation P2-9;
docs/privacy.md#attachments).

When a deleted, blocked or never-finished file's object is purged, its name and content hash
go too; the row keeps type, size and who/when as audit evidence. A legal hold on the lead
defers the purge entirely. Storage failures forget nothing early. A stored file that went
missing keeps its hash (how a bucket restore proves the object it brings back).
"""

from __future__ import annotations

import io
from io import StringIO
from urllib.parse import quote

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.stub import Stubber
from django.core.files.storage import storages
from django.core.management import call_command
from django.utils import timezone

from arkray.activities import attachments, storage
from arkray.activities.models import Attachment
from arkray.audit.models import AuditEvent
from arkray.core.models import HoldSubject, LegalHold
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

NAME = "Ravi_Kumar_Aadhaar.pdf"
PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF\n"


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


def upload(client, note, name=NAME, content=PDF):
    response = client.post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        content,
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote(name, safe=""),
    )
    assert response.status_code == 201, response.content
    return Attachment.objects.get(pk=response.json()["id"])


def delete(client, row):
    assert client.delete(f"/api/v1/workspaces/me/attachments/{row.pk}").status_code == 204


def test_a_deleted_file_loses_its_name_and_hash_with_its_object(user_a, user_b, note):
    client = signed_in(user_a)
    row = upload(client, note)
    delete(client, row)
    drain_outbox()
    row.refresh_from_db()
    assert row.purged_at is not None
    assert not storages["attachments"].exists(row.storage_key)
    assert (row.original_name, row.sha256) == (attachments.REMOVED_NAME, attachments.ERASED_SHA256)
    assert (row.extension, row.size, row.uploaded_by_id) == ("pdf", len(PDF), user_a.pk)
    assert row.deleted_by_id == user_a.pk
    for viewer in (user_a, user_b):  # nobody can reach it, nor learn it existed elsewhere
        response = signed_in(viewer).get(f"/api/v1/workspaces/me/attachments/{row.pk}/download")
        assert response.status_code == 404
    assert NAME not in str(list(AuditEvent.objects.values_list("metadata", flat=True)))


def test_failed_and_blocked_files_are_forgotten_too(user_a, note):
    client = signed_in(user_a)
    failed = upload(client, note, "a.pdf")
    blocked = upload(client, note, "b.pdf")
    Attachment.objects.filter(pk=failed.pk).update(state="failed", stored_at=None)
    Attachment.objects.filter(pk=blocked.pk).update(scan_status="rejected")
    for row in (failed, blocked):
        assert attachments.purge(row.pk)
        row.refresh_from_db()
        assert row.original_name == attachments.REMOVED_NAME


def test_a_stored_file_found_missing_keeps_its_hash(user_a, note):
    row = upload(signed_in(user_a), note)
    storages["attachments"].delete(row.storage_key)
    seen = attachments.Seen(row.state, row.deleted_at, row.purged_at)
    assert attachments.mark_unavailable(row.pk, seen)
    row.refresh_from_db()
    assert row.sha256 != attachments.ERASED_SHA256  # a restored object can still be proven
    assert row.original_name == NAME


def test_a_legal_hold_keeps_the_file_until_released(admin, user_a, note):
    client = signed_in(user_a)
    row = upload(client, note)
    hold = LegalHold.objects.create(
        subject_type=HoldSubject.LEAD,
        subject_id=note.lead_id,
        reference="CASE-7",
        placed_by=admin.pk,
    )
    delete(client, row)
    drain_outbox()
    attachments.housekeeping(timezone.now())
    row.refresh_from_db()
    assert row.purged_at is None
    assert storages["attachments"].exists(row.storage_key)
    assert row.original_name == NAME
    hold.released_at, hold.released_by = timezone.now(), admin.pk
    hold.save()
    assert attachments.housekeeping(timezone.now())["purged"] == 1
    row.refresh_from_db()
    assert row.original_name == attachments.REMOVED_NAME
    assert not storages["attachments"].exists(row.storage_key)


def test_storage_failure_forgets_nothing_early(user_a, note, monkeypatch):
    client = signed_in(user_a)
    row = upload(client, note)
    delete(client, row)

    def down(key):
        raise storage.StorageUnavailable("down", error_class="connection")

    monkeypatch.setattr(storage, "delete", down)
    with pytest.raises(storage.StorageUnavailable):
        attachments.purge(row.pk)
    row.refresh_from_db()
    assert (row.purged_at, row.original_name) == (None, NAME)
    monkeypatch.undo()
    assert attachments.purge(row.pk)


def test_an_already_missing_object_is_still_forgotten(user_a, note):
    client = signed_in(user_a)
    row = upload(client, note)
    delete(client, row)
    storages["attachments"].delete(row.storage_key)
    assert attachments.purge(row.pk)
    row.refresh_from_db()
    assert row.original_name == attachments.REMOVED_NAME
    assert not attachments.purge(row.pk)  # idempotent


def test_a_deleted_files_object_brought_back_by_a_bucket_restore_is_purged_again(user_a, note):
    from arkray.activities import reconcile

    client = signed_in(user_a)
    row = upload(client, note)
    delete(client, row)
    drain_outbox()
    storages["attachments"].save(row.storage_key, io.BytesIO(PDF))  # the restore
    report = reconcile.run(reconcile.Options(repair=True))
    assert report.restorable == 0
    assert not storages["attachments"].exists(row.storage_key)


def test_files_removed_before_this_release_are_forgotten_on_request(admin, user_a, note):
    client = signed_in(user_a)
    row = upload(client, note)
    delete(client, row)
    drain_outbox()
    Attachment.objects.filter(pk=row.pk).update(original_name=NAME, sha256="a" * 64)  # as before
    out = StringIO()
    call_command("forget_attachment_names", by=admin.email, stdout=out)
    assert "1 removed files" in out.getvalue()
    assert Attachment.objects.get(pk=row.pk).original_name == NAME  # a dry run
    call_command("forget_attachment_names", by=admin.email, yes=True, stdout=StringIO())
    assert Attachment.objects.get(pk=row.pk).original_name == attachments.REMOVED_NAME
    assert AuditEvent.objects.get(action="attachment.metadata_removed").metadata == {"count": 1}
    out = StringIO()
    call_command("forget_attachment_names", by=admin.email, yes=True, stdout=out)
    assert "of 0 removed files" in out.getvalue()


def test_every_version_is_removed_when_asked(settings, monkeypatch):
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="k",
        aws_secret_access_key="s",
    )

    class Store:
        bucket_name = "arkray"
        connection = type("C", (), {"meta": type("M", (), {"client": client})})()

        def _normalize_name(self, key):
            return f"attachments/{key}"

    monkeypatch.setattr(storage, "_store", lambda: Store())
    monkeypatch.setattr(storage, "backend_name", lambda store=None: "s3")
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions",
            {
                "Versions": [
                    {"Key": "attachments/2026/10/k", "VersionId": "v1"},
                    {"Key": "attachments/2026/10/k", "VersionId": "v2"},
                    {"Key": "attachments/2026/10/k2", "VersionId": "other"},
                ],
                "DeleteMarkers": [{"Key": "attachments/2026/10/k", "VersionId": "m1"}],
            },
            {"Bucket": "arkray", "Prefix": "attachments/2026/10/k"},
        )
        stub.add_response(
            "delete_objects",
            {},
            {
                "Bucket": "arkray",
                "Delete": {
                    "Objects": [
                        {"Key": "attachments/2026/10/k", "VersionId": "v1"},
                        {"Key": "attachments/2026/10/k", "VersionId": "v2"},
                        {"Key": "attachments/2026/10/k", "VersionId": "m1"},
                    ],
                    "Quiet": True,
                },
            },
        )
        assert storage.delete_versions("2026/10/k") == 3
