"""Uploads racing for the last of someone's storage quota (deep security audit L-7;
docs/activities.md#attachments): checked under a per-person advisory lock, so uploads to
different leads at once (which the lead and note locks don't serialise) never take more than the
quota between them."""

from __future__ import annotations

import time
from urllib.parse import quote

import pytest
from django.test import override_settings

from arkray.activities import attachments
from arkray.activities.models import Attachment
from tests.factories import LeadFactory, NoteFactory, UserFactory
from tests.helpers import run_concurrently, signed_in

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]


@override_settings(ATTACHMENT_MAX_BYTES_PER_USER=30)
def test_uploads_to_different_notes_at_once_stay_within_the_quota(monkeypatch):
    owner = UserFactory()
    # A lead each: the lead and note locks then serialise nothing; only the quota lock does.
    notes = [NoteFactory(lead=LeadFactory(owner=owner), created_by=owner) for _ in range(6)]
    clients = [signed_in(owner) for _ in notes]
    measured = attachments.stored_bytes

    def slow_measure(user_id):
        # Widen the window between measuring and writing the row: without the lock, every
        # upload would measure an empty quota and all six would be stored.
        used = measured(user_id)
        time.sleep(0.2)
        return used

    monkeypatch.setattr(attachments, "stored_bytes", slow_measure)

    def upload(client, note, n):
        return client.post(
            f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
            b"0123456789",
            content_type="application/octet-stream",
            HTTP_X_FILENAME=quote(f"f{n}.txt"),
        )

    responses = run_concurrently(
        *(
            lambda c=c, note=note, n=n: upload(c, note, n)
            for n, (c, note) in enumerate(zip(clients, notes, strict=True))
        )
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 201, 201, 422, 422, 422], statuses
    refused = [r.json()["error"]["code"] for r in responses if r.status_code == 422]
    assert refused == ["attachment_quota_exceeded"] * 3
    assert Attachment.objects.filter(uploaded_by=owner, state="stored").count() == 3
    assert measured(owner.pk) == 30
