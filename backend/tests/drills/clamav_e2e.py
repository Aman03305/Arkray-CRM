"""Upload, scan and download through the API against a real ClamAV daemon (R94). Part of
tests/drills/clamav_drill.py --e2e, which runs it with ARKRAY_CLAMAV_URL set; never collected
by the normal suite (its name doesn't match `test_*.py`), so it is never a silent skip.

    ARKRAY_CLAMAV_URL=clamd://127.0.0.1:53310 pytest tests/drills/clamav_e2e.py --no-cov
"""

from __future__ import annotations

import os
from urllib.parse import quote

import pytest
from django.core.files.storage import storages
from django.utils import timezone

from arkray.activities import attachments
from arkray.activities.models import Attachment
from arkray.audit.models import AuditEvent
from tests.drills.clamav_drill import eicar
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF\n"
# The drill passes the marker of the signature it installed: a host whose own antivirus
# blocks reading a stored EICAR file (Windows Defender on a developer machine: the scan job
# then can't open the object, and the file stays pending) still runs the infected path.
MARKER = os.environ.get("ARKRAY_CLAMAV_MARKER", "")


def malware() -> tuple[bytes, str]:
    """Content clamd reports FOUND, and a word its signature name contains."""
    if MARKER:
        return f"lab notes {MARKER} end\n".encode(), "Arkray.Drill.Marker"
    return eicar(), "Eicar"


@pytest.fixture(autouse=True)
def real_clamd(settings):
    url = os.environ.get("ARKRAY_CLAMAV_URL", "")
    if not url:
        pytest.fail("Set ARKRAY_CLAMAV_URL=clamd://host:port (see tests/drills/clamav_drill.py).")
    settings.ATTACHMENT_SCANNER = url


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


def upload(client, note, name, content):
    response = client.post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        content,
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote(name, safe=""),
    )
    assert response.status_code == 201, response.content
    return response.json()


def download(client, attachment_id):
    return client.get(f"/api/v1/workspaces/me/attachments/{attachment_id}/download")


def test_a_clean_file_downloads_once_clamd_says_so(user_a_client, note):
    created = upload(user_a_client, note, "report.pdf", PDF)
    assert (created["scan_status"], created["downloadable"]) == ("pending", False)
    assert download(user_a_client, created["id"]).status_code == 422
    drain_outbox()
    assert Attachment.objects.get(pk=created["id"]).scan_status == "clean"
    response = download(user_a_client, created["id"])
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == PDF


def test_an_infected_file_is_blocked_and_removed(user_a_client, note):
    content, signature = malware()
    created = upload(user_a_client, note, "eicar.txt", content)
    key = Attachment.objects.get(pk=created["id"]).storage_key
    drain_outbox()
    row = Attachment.objects.get(pk=created["id"])
    assert row.scan_status == "rejected"
    assert row.purged_at is not None
    assert not storages["attachments"].exists(key)
    response = download(user_a_client, created["id"])
    assert response.status_code == 422
    assert "blocked" in response.json()["error"]["message"]
    event = AuditEvent.objects.get(action=attachments.AUDIT_REJECTED)
    assert signature in event.metadata["detected"]
    assert "eicar.txt" not in str(event.metadata)


def test_files_stored_before_the_scanner_are_scanned_by_the_backlog(user_a_client, note, settings):
    url = settings.ATTACHMENT_SCANNER
    settings.ATTACHMENT_SCANNER = ""
    clean = upload(user_a_client, note, "old.pdf", PDF)
    infected = upload(user_a_client, note, "old.txt", malware()[0])
    settings.ATTACHMENT_SCANNER = url
    assert download(user_a_client, clean["id"]).status_code == 422  # waits for its scan
    assert attachments.housekeeping(timezone.now())["queued_for_scanning"] == 2
    drain_outbox()
    assert download(user_a_client, clean["id"]).status_code == 200
    assert Attachment.objects.get(pk=infected["id"]).scan_status == "rejected"
