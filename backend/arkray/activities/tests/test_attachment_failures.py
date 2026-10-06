"""A file is never "stored" without its whole object, at whatever step an upload fails
(final audit R105): the row reserved and the write failing, the write done and the finish
failing, the process dying between the two, a deletion whose purge fails."""

from __future__ import annotations

import io
from datetime import timedelta
from unittest import mock
from urllib.parse import quote

import pytest
from django.core.files.storage import storages
from django.db import DatabaseError
from django.utils import timezone

from arkray.activities import attachments, reconcile, storage
from arkray.activities.models import Attachment
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fresh_guard():
    storage.reset_guard()
    yield
    storage.reset_guard()


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


def upload(client, note, content=b"terms and prices"):
    return client.post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        content,
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote("terms.txt", safe=""),
    )


def download(client, attachment_id):
    return client.get(f"/api/v1/workspaces/me/attachments/{attachment_id}/download")


def test_the_write_failing_leaves_a_failed_row_and_a_503_with_retry_after(
    user_a_client, note, monkeypatch
):
    def refused(*_args):
        raise storage.StorageUnavailable(error_class="server_error")

    monkeypatch.setattr(storage, "save", refused)
    response = upload(user_a_client, note)
    assert response.status_code == 503
    assert response["Retry-After"] == str(storage.STORAGE_RETRY_AFTER_S)
    row = Attachment.objects.get()
    assert row.state == "failed"
    assert download(user_a_client, row.pk).status_code == 404


@pytest.mark.parametrize("step", ["after_write", "in_finish"])
def test_a_crash_after_the_write_never_leaves_a_stored_row(
    user_a, user_a_client, note, monkeypatch, step
):
    """The process dies once the bytes are written (simulated by raising there), or the
    finishing transaction fails: the row stays "uploading", which nothing lists or serves;
    reconciliation then finishes it (the object is whole), or housekeeping gives up on it."""
    if step == "after_write":
        real = storage.save

        def written_then_killed(key, file):
            real(key, file)
            raise RuntimeError("killed")

        monkeypatch.setattr(storage, "save", written_then_killed)
    else:

        def commit_fails(*_args, **_kwargs):
            raise DatabaseError("could not serialize access")

        monkeypatch.setattr(attachments, "_audit", commit_fails)
    user_a_client.raise_request_exception = False
    response = upload(user_a_client, note)
    assert response.status_code == 500
    row = Attachment.objects.get()
    assert row.state == "uploading"
    assert storages["attachments"].exists(row.storage_key)
    assert download(user_a_client, row.pk).status_code == 404
    notes = user_a_client.get(f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes")
    assert notes.json()["results"][0]["attachments"] == []
    monkeypatch.undo()
    report = reconcile.run(
        reconcile.Options(repair=True, stale_after=timedelta(0), retry_pause_s=0)
    )
    assert (report.pending, report.repaired) >= (1, 1)
    row.refresh_from_db()
    assert row.state == "stored"
    got = download(user_a_client, row.pk)
    assert got.status_code == 200
    assert b"".join(got.streaming_content) == b"terms and prices"


def test_housekeeping_gives_up_on_a_crashed_upload_and_removes_its_bytes(
    user_a_client, note, monkeypatch
):
    real = storage.save

    def written_then_killed(key, file):
        real(key, file)
        raise RuntimeError("killed")

    monkeypatch.setattr(storage, "save", written_then_killed)
    user_a_client.raise_request_exception = False
    upload(user_a_client, note)
    row = Attachment.objects.get()
    later = timezone.now() + attachments.ABANDONED_AFTER + timedelta(minutes=1)
    result = attachments.housekeeping(later)
    assert (result["abandoned"], result["purged"]) == (1, 1)
    row.refresh_from_db()
    assert row.state == "failed"
    assert not storages["attachments"].exists(row.storage_key)


def test_a_deletion_whose_purge_fails_stays_hidden_until_storage_is_back(
    user_a_client, note, monkeypatch
):
    created = upload(user_a_client, note).json()
    row = Attachment.objects.get(pk=created["id"])
    assert user_a_client.delete(f"/api/v1/workspaces/me/attachments/{row.pk}").status_code == 204

    def down(_key):
        raise storage.StorageUnavailable(error_class="connection")

    monkeypatch.setattr(storage, "delete", down)
    drain_outbox(rounds=1)
    row.refresh_from_db()
    assert (row.deleted_at is not None, row.purged_at) == (True, None)
    assert download(user_a_client, row.pk).status_code == 404
    assert storages["attachments"].exists(row.storage_key)
    monkeypatch.undo()
    assert attachments.housekeeping(timezone.now())["purged"] == 1
    assert not storages["attachments"].exists(row.storage_key)


def test_a_failed_or_uploading_row_never_downloads(user_a, user_a_client, note):
    for state in ("failed", "uploading"):
        row = Attachment.objects.create(
            note=note,
            original_name="x.txt",
            extension="txt",
            content_type="text/plain",
            size=1,
            sha256="0" * 64,
            storage_key=f"2026/10/{state}",
            state=state,
            uploaded_by=user_a,
        )
        storages["attachments"].save(row.storage_key, io.BytesIO(b"x"))
        response = download(user_a_client, row.pk)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_one_users_uploads_are_rate_limited(user_a_client, note, settings):
    """Uploads have their own budget (API_THROTTLE_ATTACHMENTS), well below the 600
    requests a minute every user gets: one user can't push gigabytes a minute."""
    from rest_framework.throttling import ScopedRateThrottle

    rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "attachments": "2/min"}
    with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
        codes = [upload(user_a_client, note).status_code for _ in range(3)]
        refused = upload(user_a_client, note)
    assert codes == [201, 201, 429]
    assert refused.status_code == 429
    assert "Retry-After" in refused
    assert Attachment.objects.count() == 2


def test_the_default_upload_budget_is_configured(settings):
    assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["attachments"] == "30/min"
