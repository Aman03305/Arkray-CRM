"""Malware scanning fails closed (R94; docs/activities.md#attachments).

With a scanner configured, a file is quarantined until clamd says exactly "clean": no route
(download, preview, a data export) serves it while it is pending, and every way a scan can
fail without a verdict (nothing listening, a timeout, a dropped connection, an error reply
such as a stream over clamd's StreamMaxLength, a reply of any other shape, bytes that aren't
the uploaded file) keeps it pending: the job retries with backoff, then goes dead (an
alert), and reconciliation reports the overdue scan. Infected files are blocked, their
object removed and their name forgotten; the owner sees "Blocked". The scan runs on a
worker, never in the upload request, and what it logs and audits names no file.

The peer here is a scripted clamd (the protocol, not a scanner); tests/drills/clamav_drill.py
runs the same client against a real ClamAV daemon.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import struct
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from datetime import timedelta
from urllib.parse import quote

import pytest
from django.core.files.storage import storages
from django.utils import timezone

from arkray.activities import attachments, reconcile, storage
from arkray.activities.handlers import scan_attachment
from arkray.activities.models import Attachment
from arkray.audit.models import AuditEvent
from arkray.core import outbox
from arkray.core.models import OutboxEvent
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

NAME = "Zorvana_lab_results.txt"
CLEAN = b"stream: OK\0"
FOUND = b"stream: Win.Test.EICAR_HDB-1 FOUND\0"
SIZE_LIMIT = b"INSTREAM size limit exceeded. ERROR\0"

Reply = Callable[[socket.socket, bytes], None]


class ScriptedClamd:
    """A clamd peer on loopback that reads the zINSTREAM command and its chunks, then does
    whatever `reply` says (answer, stall, close). Counts connections and bytes received."""

    def __init__(self, reply: Reply, *, read_stream: bool = True) -> None:
        self.reply = reply
        self.read_stream = read_stream
        self.connections = 0
        self.received: list[bytes] = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.url = f"clamd://127.0.0.1:{self.port}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                command = b""
                while not command.endswith(b"\0"):
                    part = conn.recv(1)
                    if not part:
                        return
                    command += part
                data = b""
                if self.read_stream:
                    while True:
                        (size,) = struct.unpack("!L", _exactly(conn, 4))
                        if size == 0:
                            break
                        data += _exactly(conn, size)
                self.received.append(data)
                self.reply(conn, data)
            except OSError:
                return

    def close(self) -> None:
        self.server.close()


def _exactly(conn: socket.socket, size: int) -> bytes:
    out = b""
    while len(out) < size:
        part = conn.recv(size - len(out))
        if not part:
            raise ConnectionError("closed")
        out += part
    return out


def answer(reply: bytes) -> Reply:
    return lambda conn, _data: conn.sendall(reply)


def by_content(conn: socket.socket, data: bytes) -> None:
    conn.sendall(FOUND if b"MALWARE-MARKER" in data else CLEAN)


@pytest.fixture
def clamd(settings) -> Iterator[Callable[..., ScriptedClamd]]:
    peers: list[ScriptedClamd] = []

    def start(reply: Reply = by_content, **kwargs: bool) -> ScriptedClamd:
        peer = ScriptedClamd(reply, **kwargs)
        peers.append(peer)
        settings.ATTACHMENT_SCANNER = peer.url
        return peer

    yield start
    for peer in peers:
        peer.close()


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


def upload(client, note, content=b"lab results", name=NAME):
    response = client.post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        content,
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote(name, safe=""),
    )
    assert response.status_code == 201, response.content
    return response.json()


def fetch(client, attachment_id, kind: str = "download"):
    return client.get(f"/api/v1/workspaces/me/attachments/{attachment_id}/{kind}")


def scan_event(attachment_id) -> OutboxEvent:
    return OutboxEvent.objects.get(
        topic=attachments.TOPIC_SCAN, payload__attachment_id=str(attachment_id)
    )


# --- the clamd client ------------------------------------------------------------------------
class TestVerdicts:
    def scan(self, content: bytes = b"hello", **kwargs: str) -> storage.Verdict:
        return storage.scan(io.BytesIO(content), **kwargs)

    def test_only_clamds_exact_clean_reply_is_clean(self, clamd):
        peer = clamd(answer(CLEAN))
        assert self.scan(b"hello") == storage.Verdict(clean=True)
        assert peer.received == [b"hello"]

    def test_found_names_the_signature(self, clamd):
        clamd(answer(FOUND))
        assert self.scan() == storage.Verdict(clean=False, signature="Win.Test.EICAR_HDB-1")

    def test_a_signature_of_another_shape_is_still_malware(self, clamd):
        clamd(answer(b"stream: <script>alert(1)</script> FOUND\0"))
        assert self.scan() == storage.Verdict(clean=False, signature="unknown")

    @pytest.mark.parametrize(
        ("reply", "reason"),
        [
            (SIZE_LIMIT, "size_limit"),
            # AlertExceedsMax: over a scan limit, so not scanned whole; not malware either.
            (b"stream: Heuristics.Limits.Exceeded.MaxFileSize FOUND\0", "limits_exceeded"),
            (b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND\0", "limits_exceeded"),
            (b"stream: Can't allocate memory ERROR\0", "error_reply"),
            (b"UNKNOWN COMMAND\0", "protocol"),
            (b"PONG\0", "protocol"),
            # The old parser took anything ending in " OK" as clean.
            (b"stream: not quite OK\0", "protocol"),
            (b"1: stream: OK\0", "protocol"),
            # Cut off before its terminator, or followed by more than one reply.
            (b"stream: OK", "protocol"),
            (b"stream: OK\0stream: OK\0", "protocol"),
            (b"stream: OK" + b" " * 5000, "protocol"),
            (b"", "connection"),
        ],
    )
    def test_any_other_reply_is_no_verdict(self, clamd, reply, reason):
        clamd(answer(reply))
        with pytest.raises(storage.ScannerUnavailable) as raised:
            self.scan()
        assert raised.value.reason == reason

    def test_nothing_listening(self, settings):
        with socket.create_server(("127.0.0.1", 0)) as probe:
            port = probe.getsockname()[1]
        settings.ATTACHMENT_SCANNER = f"clamd://127.0.0.1:{port}"
        with pytest.raises(storage.ScannerUnavailable) as raised:
            self.scan()
        assert raised.value.reason == "connection"

    def test_a_silent_scanner_times_out_within_the_deadline(self, clamd, monkeypatch):
        release = threading.Event()
        clamd(lambda conn, _data: release.wait(10))
        monkeypatch.setattr(storage, "SCAN_DEADLINE_S", 0.5)
        started = time.monotonic()
        try:
            with pytest.raises(storage.ScannerUnavailable) as raised:
                self.scan()
        finally:
            release.set()
        assert raised.value.reason == "timeout"
        assert time.monotonic() - started < 3

    def test_a_scanner_that_stops_reading_times_out_too(self, clamd, monkeypatch):
        """A peer that accepts the command and then reads nothing: the send blocks once the
        socket buffers fill, and the one deadline still ends the conversation."""
        release = threading.Event()
        clamd(lambda conn, _data: release.wait(10), read_stream=False)
        monkeypatch.setattr(storage, "SCAN_DEADLINE_S", 1.0)
        started = time.monotonic()
        try:
            with pytest.raises(storage.ScannerUnavailable) as raised:
                self.scan(b"x" * (32 * 1024 * 1024))
        finally:
            release.set()
        assert raised.value.reason == "timeout"
        assert time.monotonic() - started < 5

    def test_a_connection_dropped_mid_stream(self, clamd):
        def drop(conn: socket.socket, _data: bytes) -> None:
            conn.shutdown(socket.SHUT_RDWR)

        clamd(drop, read_stream=False)
        with pytest.raises(storage.ScannerUnavailable) as raised:
            self.scan(b"x" * (8 * 1024 * 1024))
        assert raised.value.reason in {"connection", "protocol"}

    def test_a_stream_over_the_scanners_limit_is_never_clean(self, clamd):
        """clamd answers the size-limit error and closes while the file is still arriving:
        the reply is read when it can be (size_limit), else it is a dropped connection."""

        def refuse(conn: socket.socket, _data: bytes) -> None:
            _exactly(conn, 4 + 64 * 1024)  # the first chunk, then the limit
            conn.sendall(SIZE_LIMIT)

        clamd(refuse, read_stream=False)
        with pytest.raises(storage.ScannerUnavailable) as raised:
            self.scan(b"x" * (8 * 1024 * 1024))
        assert raised.value.reason in {"size_limit", "connection"}

    def test_the_bytes_scanned_must_be_the_uploaded_file(self, clamd):
        clamd(answer(CLEAN))
        with pytest.raises(storage.ScannerUnavailable) as raised:
            self.scan(b"hello", expected_sha256="0" * 64)
        assert raised.value.reason == "integrity"

    def test_malware_is_malware_whatever_its_hash(self, clamd):
        clamd(answer(FOUND))
        assert not self.scan(b"hello", expected_sha256="0" * 64).clean

    def test_no_scanner_configured(self, settings):
        settings.ATTACHMENT_SCANNER = ""
        with pytest.raises(storage.ScannerUnavailable) as raised:
            self.scan()
        assert raised.value.reason == "unconfigured"


# --- quarantine: every route ---------------------------------------------------------------------
class TestQuarantine:
    def test_the_scan_runs_on_a_worker_never_in_the_upload_request(
        self, user_a_client, note, clamd
    ):
        peer = clamd()
        created = upload(user_a_client, note)
        assert peer.connections == 0  # the request only queued it
        event = scan_event(created["id"])
        assert (event.queue, event.max_attempts) == ("default", 12)
        assert outbox._HANDLERS[attachments.TOPIC_SCAN].func is scan_attachment
        drain_outbox()
        assert peer.connections == 1
        assert Attachment.objects.get(pk=created["id"]).scan_status == "clean"

    def test_pending_is_served_by_no_route(self, admin, user_a_client, note, clamd):
        clamd(answer(b""))  # no verdict, ever: the file stays pending throughout
        created = upload(user_a_client, note, b"\x89PNG\r\n\x1a\n" + b"\x01" * 64, "scan.png")
        assert (created["scan_status"], created["downloadable"], created["previewable"]) == (
            "pending",
            False,
            False,
        )
        for kind in ("download", "preview"):
            response = fetch(user_a_client, created["id"], kind)
            assert response.status_code == 422
            assert "being checked" in response.json()["error"]["message"]
        assert files_exported(admin, note) == []
        listed = user_a_client.get(
            f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes"
        ).json()["results"][0]["attachments"]
        assert [(f["scan_status"], f["downloadable"]) for f in listed] == [("pending", False)]

    def test_clean_is_served_everywhere(self, admin, user_a_client, note, clamd):
        clamd()
        created = upload(user_a_client, note, b"\x89PNG\r\n\x1a\n" + b"\x01" * 64, "scan.png")
        drain_outbox()
        assert fetch(user_a_client, created["id"]).status_code == 200
        assert fetch(user_a_client, created["id"], "preview").status_code == 200
        assert files_exported(admin, note) == [f"files/{created['id']}-scan.png"]

    def test_files_stored_before_the_scanner_wait_for_it(
        self, user_a_client, note, clamd, settings
    ):
        """Enabling a scanner later: the earlier, unscanned files stop downloading at once
        and stay so until the backlog scan finds them clean."""
        settings.ATTACHMENT_SCANNER = ""
        created = upload(user_a_client, note)
        assert fetch(user_a_client, created["id"]).status_code == 200
        peer = clamd(answer(SIZE_LIMIT))  # configured, but every scan fails
        assert fetch(user_a_client, created["id"]).status_code == 422  # not yet queued
        assert attachments.housekeeping(timezone.now())["queued_for_scanning"] == 1
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "pending"
        assert fetch(user_a_client, created["id"]).status_code == 422
        assert peer.connections == 1


def files_exported(admin, note):
    client = signed_in(admin)
    response = client.post(
        "/api/v1/admin/privacy/exports",
        {
            "subject_type": "lead",
            "subject_id": str(note.lead_id),
            "reference": "DSAR-R94",
            "identity_verified": True,
        },
        format="json",
    )
    assert response.status_code == 202, response.content
    export_id = response.json()["id"]
    drain_outbox()  # the export's job (and any scan that is due)
    download = client.get(f"/api/v1/admin/privacy/exports/{export_id}/download")
    assert download.status_code == 200, client.get(f"/api/v1/admin/privacy/exports/{export_id}")
    archive = zipfile.ZipFile(io.BytesIO(b"".join(download.streaming_content)))
    return sorted(n for n in archive.namelist() if n.startswith("files/"))


# --- failures fail closed ----------------------------------------------------------------------
class TestFailClosed:
    @pytest.mark.parametrize(
        ("reply", "reason"),
        [
            (answer(SIZE_LIMIT), "size_limit"),
            (answer(b"stream: not quite OK\0"), "protocol"),
            (answer(b""), "connection"),
            (answer(b"stream: Can't allocate memory ERROR\0"), "error_reply"),
            (answer(b"stream: Heuristics.Limits.Exceeded.MaxFileSize FOUND\0"), "limits_exceeded"),
        ],
    )
    def test_no_verdict_keeps_the_file_pending_and_retries_with_backoff(
        self, user_a_client, note, clamd, caplog, reply, reason
    ):
        clamd(reply)
        created = upload(user_a_client, note)
        with caplog.at_level(logging.WARNING, logger="arkray.activities.attachments"):
            drain_outbox()
        row = Attachment.objects.get(pk=created["id"])
        assert row.scan_status == "pending"
        assert fetch(user_a_client, created["id"]).status_code == 422
        event = scan_event(created["id"])
        assert (event.status, event.attempts) == ("pending", 1)
        assert event.available_at > timezone.now()  # backed off
        assert reason in event.last_error
        failed = [r for r in caplog.records if r.getMessage() == "attachment_scan_failed"]
        assert [(r.reason, r.attachment_id) for r in failed] == [(reason, created["id"])]
        assert NAME not in caplog.text
        assert NAME not in event.last_error

    def test_bytes_that_arent_the_upload_are_never_marked_clean(self, user_a_client, note, clamd):
        clamd(answer(CLEAN))
        created = upload(user_a_client, note)
        row = Attachment.objects.get(pk=created["id"])
        with storages["attachments"].open(row.storage_key, "wb") as file:
            file.write(b"something else")  # replaced in storage behind the application
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "pending"
        assert "integrity" in scan_event(created["id"]).last_error

    def test_after_the_last_retry_the_job_is_dead_and_the_file_still_quarantined(
        self, user_a_client, note, clamd
    ):
        clamd(answer(SIZE_LIMIT))
        created = upload(user_a_client, note)
        event = scan_event(created["id"])
        OutboxEvent.objects.filter(pk=event.pk).update(attempts=event.max_attempts - 1)
        drain_outbox()
        assert scan_event(created["id"]).status == "dead"  # the dead-events alert
        assert Attachment.objects.get(pk=created["id"]).scan_status == "pending"
        assert fetch(user_a_client, created["id"]).status_code == 422
        # An overdue scan is what the daily reconciliation reports (an alert too).
        Attachment.objects.filter(pk=created["id"]).update(
            stored_at=timezone.now() - timedelta(hours=2)
        )
        report = reconcile.run(reconcile.Options())
        assert (report.pending_scan, report.samples["pending_scan"]) == (1, [created["id"]])
        # Requeued once the scanner is fixed (docs/runbooks.md#dead-events): clean.
        clamd(answer(CLEAN))
        outbox.requeue_dead([scan_event(created["id"]).pk])
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "clean"
        assert fetch(user_a_client, created["id"]).status_code == 200

    def test_the_backoff_spreads_twelve_attempts_over_hours(self):
        ceilings = [outbox.backoff_seconds(n, rng=lambda: 1.0) for n in range(1, 12)]
        floors = [outbox.backoff_seconds(n, rng=lambda: 0.0) for n in range(1, 12)]
        assert sum(floors) > 1.5 * 3600
        assert sum(ceilings) < 3.5 * 3600


# --- infected files ------------------------------------------------------------------------------
class TestInfected:
    def test_blocked_removed_forgotten_and_audited_without_the_name(
        self, admin, user_a_client, note, clamd, caplog
    ):
        clamd()
        created = upload(user_a_client, note, b"MALWARE-MARKER in a text file")
        key = Attachment.objects.get(pk=created["id"]).storage_key
        with caplog.at_level(logging.INFO, logger="arkray.activities.attachments"):
            drain_outbox()
        row = Attachment.objects.get(pk=created["id"])
        assert row.scan_status == "rejected"
        assert row.purged_at is not None
        assert not storages["attachments"].exists(key)
        assert row.original_name == attachments.REMOVED_NAME
        for kind in ("download", "preview"):
            response = fetch(user_a_client, created["id"], kind)
            assert response.status_code == 422
        assert "blocked" in fetch(user_a_client, created["id"]).json()["error"]["message"]
        # The owner sees it blocked ("Blocked" in the UI), not downloadable.
        listed = user_a_client.get(
            f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes"
        ).json()["results"][0]["attachments"]
        assert [(f["scan_status"], f["downloadable"]) for f in listed] == [("rejected", False)]
        assert files_exported(admin, note) == []
        event = AuditEvent.objects.get(action=attachments.AUDIT_REJECTED)
        assert event.metadata["detected"] == "Win.Test.EICAR_HDB-1"
        assert NAME not in json.dumps(event.metadata)
        blocked = [r for r in caplog.records if r.getMessage() == "attachment_malware_blocked"]
        assert [(r.attachment_id, r.outcome, r.security) for r in blocked] == [
            (created["id"], "rejected", True)
        ]
        assert NAME not in caplog.text
        assert "MALWARE-MARKER" not in caplog.text

    def test_a_clean_scan_is_logged_by_id_and_outcome(self, user_a_client, note, clamd, caplog):
        clamd()
        created = upload(user_a_client, note)
        with caplog.at_level(logging.INFO, logger="arkray.activities.attachments"):
            drain_outbox()
        scanned = [r for r in caplog.records if r.getMessage() == "attachment_scanned"]
        assert [(r.attachment_id, r.outcome) for r in scanned] == [(created["id"], "clean")]
        assert NAME not in caplog.text
