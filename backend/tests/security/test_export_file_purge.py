"""An ended export's file is deleted for good, durably (final audit PRIV-3, ARCH-4;
docs/privacy.md#access-requests, docs/runbooks.md#export-file-cleanup).

- The row keeps its storage key until the object is confirmed gone; a failed deletion is
  retried by housekeeping with a back-off, recorded on the row and counted by a gauge, and the
  export is never downloadable meanwhile.
- In a versioned bucket every version and delete marker of exactly that key goes, in pages;
  DeleteObjects' `Errors` are checked; a version the store's retention refuses (AccessDenied)
  is "retained": audited once, retried later, never in a loop.
- Claims are leased under SKIP LOCKED: a duplicate task or a crash is harmless.
- Reconciliation deletes objects no live export explains, and nothing else.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.stub import Stubber
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, storages
from django.db import connections, transaction
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core import storage_purge
from arkray.core.errors import BusinessRuleViolation
from arkray.privacy import exports
from arkray.privacy import metrics as privacy_metrics
from arkray.privacy.models import DataExport
from arkray.privacy.tasks import housekeeping
from tests.factories import LeadFactory, NoteFactory, UserFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

EXPORTS = "/api/v1/admin/privacy/exports"
BUCKET = "arkray"


# --- an S3 store backed by a stubbed client -----------------------------------------------------
def s3_client():
    return boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s"
    )


class FakeS3Store:
    """What django-storages' S3Storage offers the purge: bucket, location, client."""

    bucket_name = BUCKET

    def __init__(self, client, location="exports"):
        self.location = location
        self.connection = type("C", (), {"meta": type("M", (), {"client": client})})()
        self.deleted: list[str] = []

    def _normalize_name(self, key):
        return f"{self.location}/{key}"

    def exists(self, key):
        return True

    def delete(self, key):
        self.deleted.append(key)


def versions(key, *ids, markers=(), others=(), truncated=None):
    page = {
        "Versions": [{"Key": key, "VersionId": v} for v in ids]
        + [{"Key": other, "VersionId": "o1"} for other in others],
        "DeleteMarkers": [{"Key": key, "VersionId": m} for m in markers],
        "IsTruncated": truncated is not None,
    }
    if truncated:
        page["NextKeyMarker"], page["NextVersionIdMarker"] = truncated
    return page


def delete_call(key, *ids):
    return {
        "Bucket": BUCKET,
        "Delete": {"Objects": [{"Key": key, "VersionId": v} for v in ids], "Quiet": True},
    }


KEY = "2026/10/x.zip"
FULL = "exports/2026/10/x.zip"


def test_every_version_and_delete_marker_of_exactly_that_key_is_removed():
    client = s3_client()
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions",
            versions(FULL, "v1", "v2", markers=["m1"], others=[FULL + ".bak"]),
            {"Bucket": BUCKET, "Prefix": FULL},
        )
        stub.add_response("delete_objects", {}, delete_call(FULL, "v1", "v2", "m1"))
        result = storage_purge.purge(FakeS3Store(client), KEY)
        stub.assert_no_pending_responses()
    assert result == storage_purge.PurgeResult(removed=3, versioned=True)
    assert result.complete


def test_versions_are_listed_page_by_page():
    client = s3_client()
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions",
            versions(FULL, "v1", truncated=(FULL, "v1")),
            {"Bucket": BUCKET, "Prefix": FULL},
        )
        stub.add_response("delete_objects", {}, delete_call(FULL, "v1"))
        stub.add_response(
            "list_object_versions",
            versions(FULL, "v2", markers=["m1"]),
            {"Bucket": BUCKET, "Prefix": FULL, "KeyMarker": FULL, "VersionIdMarker": "v1"},
        )
        stub.add_response("delete_objects", {}, delete_call(FULL, "v2", "m1"))
        result = storage_purge.purge(FakeS3Store(client), KEY)
        stub.assert_no_pending_responses()
    assert (result.removed, result.complete) == (3, True)


def test_a_partial_delete_objects_failure_is_not_success():
    client = s3_client()
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions",
            versions(FULL, "v1", "v2", "v3"),
            {"Bucket": BUCKET, "Prefix": FULL},
        )
        stub.add_response(
            "delete_objects",
            {
                "Errors": [
                    {"Key": FULL, "VersionId": "v1", "Code": "InternalError", "Message": "x"},
                    {"Key": FULL, "VersionId": "v2", "Code": "AccessDenied", "Message": "x"},
                    {"Key": FULL, "VersionId": "v3", "Code": "NoSuchVersion", "Message": "x"},
                ]
            },
            delete_call(FULL, "v1", "v2", "v3"),
        )
        result = storage_purge.purge(FakeS3Store(client), KEY)
    # v3 was already gone (removed); v2 is under retention; v1 failed.
    assert (result.removed, result.retained, result.failed) == (1, 1, 1)
    assert not result.complete


def test_a_store_refusing_the_listing_raises_purge_failed():
    client = s3_client()
    with Stubber(client) as stub:
        stub.add_client_error(
            "list_object_versions", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(storage_purge.PurgeFailed) as raised:
            storage_purge.purge(FakeS3Store(client), KEY)
    assert raised.value.error_class == "access_denied"


def test_a_store_without_versions_deletes_the_object():
    client = s3_client()
    store = FakeS3Store(client)
    with Stubber(client) as stub:
        stub.add_client_error(
            "list_object_versions", service_error_code="NotImplemented", http_status_code=501
        )
        result = storage_purge.purge(store, KEY)
    assert store.deleted == [KEY]
    assert result == storage_purge.PurgeResult(removed=1)


def test_nothing_under_the_key_purges_to_zero():
    client = s3_client()
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", {}, {"Bucket": BUCKET, "Prefix": FULL})
        result = storage_purge.purge(FakeS3Store(client), KEY)
    assert (result.removed, result.complete) == (0, True)


def test_the_filesystem_store_deletes_the_file(tmp_path):
    store = FileSystemStorage(location=str(tmp_path))
    store.save(KEY, ContentFile(b"zip"))
    assert storage_purge.purge(store, KEY) == storage_purge.PurgeResult(removed=1)
    assert not store.exists(KEY)
    assert storage_purge.purge(store, KEY) == storage_purge.PurgeResult()  # idempotent


def test_s3_keys_are_listed_with_their_versions_and_delete_markers():
    client = s3_client()
    old = datetime(2026, 10, 1, tzinfo=UTC)
    new = datetime(2026, 10, 2, tzinfo=UTC)
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions",
            {
                "Versions": [{"Key": "exports/a.zip", "VersionId": "v1", "LastModified": old}],
                "DeleteMarkers": [
                    {"Key": "exports/a.zip", "VersionId": "m1", "LastModified": new},
                    {"Key": "exports/b.zip", "VersionId": "m2", "LastModified": old},
                ],
                "IsTruncated": True,
                "NextKeyMarker": "exports/b.zip",
                "NextVersionIdMarker": "m2",
            },
            {"Bucket": BUCKET, "Prefix": "exports/", "MaxKeys": 1000, "KeyMarker": "exports/0"},
        )
        stub.add_response(
            "list_object_versions",
            {"Versions": [{"Key": "exports/c.zip", "VersionId": "v1", "LastModified": old}]},
            {
                "Bucket": BUCKET,
                "Prefix": "exports/",
                "MaxKeys": 1000,
                "KeyMarker": "exports/b.zip",
                "VersionIdMarker": "m2",
            },
        )
        found, done = storage_purge.objects(FakeS3Store(client), after="0", limit=10)
    assert done
    assert [(f.key, f.modified) for f in found] == [
        ("a.zip", new),  # the newest of its versions and markers
        ("b.zip", old),  # a deleted object whose earlier versions may survive
        ("c.zip", old),
    ]


def test_filesystem_keys_are_listed_in_order_and_resumably(tmp_path):
    store = FileSystemStorage(location=str(tmp_path))
    for key in ("2026/10/b.zip", "2026/09/a.zip", "2026/10/a.zip"):
        store.save(key, ContentFile(b"zip"))
    first, done = storage_purge.objects(store, after="", limit=2)
    assert ([f.key for f in first], done) == (["2026/09/a.zip", "2026/10/a.zip"], False)
    rest, done = storage_purge.objects(store, after=first[-1].key, limit=2)
    assert ([f.key for f in rest], done) == (["2026/10/b.zip"], True)


def test_overlapping_stores_are_recognised(tmp_path):
    client = s3_client()
    exports_store = FakeS3Store(client, "exports")
    assert not storage_purge.overlaps(exports_store, FakeS3Store(client, "attachments"))
    assert storage_purge.overlaps(exports_store, FakeS3Store(client, "exports/inner"))
    assert storage_purge.overlaps(exports_store, FakeS3Store(client, ""))
    assert not storage_purge.overlaps(exports_store, FakeS3Store(client, "exportsx"))
    inner = tmp_path / "a" / "b"
    inner.mkdir(parents=True)
    a = FileSystemStorage(location=str(tmp_path / "a"))
    assert storage_purge.overlaps(a, FileSystemStorage(location=str(inner)))
    assert not storage_purge.overlaps(a, FileSystemStorage(location=str(tmp_path / "c")))
    assert not storage_purge.overlaps(a, exports_store)


# --- exports -------------------------------------------------------------------------------------
def ended_export(admin, *, key=None, **fields):
    now = timezone.now()
    export = DataExport.objects.create(
        subject_type="lead",
        subject_id=uuid4(),
        requested_by=admin,
        reference="DSAR-1",
        status="expired",
        purge_requested_at=now,
        purge_next_at=now,
        **fields,
    )
    key = key if key is not None else exports.storage_key(export)
    DataExport.objects.filter(pk=export.pk).update(storage_key=key)
    storages["exports"].save(key, ContentFile(b"zip"))
    export.refresh_from_db()
    return export


def failing(error_class="connection"):
    def purge(store, key):
        raise storage_purge.PurgeFailed(error_class)

    return purge


@pytest.fixture
def ready(admin, user_a):
    lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
    NoteFactory(lead=lead, created_by=user_a, description="A visit")
    client = signed_in(admin)
    response = client.post(
        EXPORTS,
        {
            "subject_type": "lead",
            "subject_id": str(lead.pk),
            "reference": "DSAR-1",
            "identity_verified": True,
        },
        format="json",
    )
    drain_outbox()
    export = DataExport.objects.get(pk=response.json()["id"])
    assert export.status == "ready"
    return {"client": client, "export": export, "lead": lead}


def test_a_failed_deletion_keeps_the_key_and_housekeeping_finishes_it(
    admin, ready, monkeypatch, django_capture_on_commit_callbacks
):
    from arkray.privacy import services

    export, client = ready["export"], ready["client"]
    key = export.storage_key
    with monkeypatch.context() as patched:
        patched.setattr(storage_purge, "purge", failing())
        with django_capture_on_commit_callbacks(execute=True):
            services.erase(ready["lead"].pk, operator_id=admin.pk)
    export.refresh_from_db()
    assert (export.status, export.storage_key, export.purged_at) == ("expired", key, None)
    assert (export.purge_attempts, export.purge_error) == (1, "connection")
    assert storages["exports"].exists(key)
    assert client.get(f"{EXPORTS}/{export.pk}/download").status_code in (404, 422)
    gauges = {g.name: g for g in privacy_metrics.gauges()}
    pending = {
        labels["state"]: value for labels, value in gauges["arkray_export_file_purges"].samples
    }
    assert pending == {"pending": 1, "retained": 0, "stuck": 0}

    assert housekeeping() == {"expired_exports": 0}  # not due yet: backing off
    assert storages["exports"].exists(key)
    DataExport.objects.filter(pk=export.pk).update(purge_next_at=timezone.now())
    housekeeping()
    export.refresh_from_db()
    assert export.purged_at is not None
    assert (export.storage_key, export.purge_error) == ("", "")
    assert not storages["exports"].exists(key)
    gauges = {g.name: g for g in privacy_metrics.gauges()}
    assert gauges["arkray_export_file_purges"].samples[0][1] == 0


def test_retries_back_off_and_repeated_failures_are_loud(admin, monkeypatch, caplog):
    export = ended_export(admin)
    monkeypatch.setattr(storage_purge, "purge", failing("server_error"))
    now = timezone.now()
    delays = []
    for attempt in range(1, exports.PURGE_ALERT_AFTER + 1):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="arkray.privacy.exports"):
            assert exports.purge_file(export.pk, now) == "failed"
        export.refresh_from_db()
        delays.append(export.purge_next_at - now)
        level = logging.ERROR if attempt >= exports.PURGE_ALERT_AFTER else logging.WARNING
        assert [r.levelno for r in caplog.records if r.msg == "export_file_purge_failed"] == [level]
        assert exports.purge_file(export.pk, now) == "skipped"  # not due again yet
        now = export.purge_next_at
    assert delays[:3] == [timedelta(minutes=5), timedelta(minutes=10), timedelta(minutes=20)]
    assert export.purge_attempts == exports.PURGE_ALERT_AFTER
    assert storages["exports"].exists(export.storage_key)
    stuck = [s for s in privacy_metrics.gauges()[0].samples if s[0]["state"] == "stuck"]
    assert stuck[0][1] == 1
    big = exports._retry_delay(400)
    assert big == exports.PURGE_RETRY_MAX


def test_a_version_under_retention_is_retained_audited_once_and_retried(admin, monkeypatch):
    export = ended_export(admin)
    monkeypatch.setattr(
        storage_purge,
        "purge",
        lambda store, key: storage_purge.PurgeResult(removed=1, retained=1, versioned=True),
    )
    now = timezone.now()
    assert exports.purge_file(export.pk, now) == "retained"
    export.refresh_from_db()
    assert export.purge_next_at is not None
    assert exports.purge_file(export.pk, export.purge_next_at) == "retained"
    export.refresh_from_db()
    assert (export.purge_error, export.purge_attempts, export.purged_at) == ("retained", 2, None)
    assert export.storage_key  # kept until it really goes
    events = AuditEvent.objects.filter(action=exports.AUDIT_RETAINED)
    assert events.count() == 1
    assert events.get().metadata == {"export_id": str(export.pk), "count": 1}
    retained = [s for s in privacy_metrics.gauges()[0].samples if s[0]["state"] == "retained"]
    assert retained[0][1] == 1


def test_a_versioned_bucket_purges_every_version_of_the_export(admin, monkeypatch):
    export = ended_export(admin)
    key = export.storage_key
    full = f"exports/{key}"
    client = s3_client()
    monkeypatch.setitem(storages._storages, "exports", FakeS3Store(client))
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions",
            versions(full, "v1", "v2", markers=["m1"]),
            {"Bucket": BUCKET, "Prefix": full},
        )
        stub.add_response("delete_objects", {}, delete_call(full, "v1", "v2", "m1"))
        assert exports.purge_file(export.pk) == "purged"
        stub.assert_no_pending_responses()
    export.refresh_from_db()
    assert export.purged_at is not None
    assert exports.purge_file(export.pk) == "skipped"  # done: nothing listed again


def test_a_partial_failure_in_a_versioned_bucket_keeps_the_export_pending(admin, monkeypatch):
    export = ended_export(admin)
    full = f"exports/{export.storage_key}"
    client = s3_client()
    monkeypatch.setitem(storages._storages, "exports", FakeS3Store(client))
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions", versions(full, "v1", "v2"), {"Bucket": BUCKET, "Prefix": full}
        )
        stub.add_response(
            "delete_objects",
            {"Errors": [{"Key": full, "VersionId": "v2", "Code": "InternalError", "Message": "x"}]},
            delete_call(full, "v1", "v2"),
        )
        assert exports.purge_file(export.pk) == "failed"
    export.refresh_from_db()
    assert (export.purge_error, export.purged_at) == ("partial", None)
    assert export.storage_key


def test_a_claimed_purge_is_not_run_twice(admin, monkeypatch):
    export = ended_export(admin)
    calls = []

    def purge_and_meanwhile_run_a_duplicate(store, key):
        calls.append(key)
        assert exports.purge_file(export.pk) == "skipped"  # the lease holds it
        return storage_purge.PurgeResult(removed=1)

    monkeypatch.setattr(storage_purge, "purge", purge_and_meanwhile_run_a_duplicate)
    assert exports.purge_file(export.pk) == "purged"
    assert calls == [export.storage_key]


def test_a_crashed_attempts_lease_runs_out(admin, monkeypatch):
    export = ended_export(admin)

    def crash(store, key):
        raise SystemExit  # the worker died mid-purge

    monkeypatch.setattr(storage_purge, "purge", crash)
    with pytest.raises(SystemExit):
        exports.purge_file(export.pk)
    export.refresh_from_db()
    assert export.purged_at is None
    monkeypatch.undo()
    assert exports.purge_file(export.pk) == "skipped"
    later = timezone.now() + exports.PURGE_LEASE + timedelta(seconds=1)
    assert exports.purge_file(export.pk, later) == "purged"
    assert not storages["exports"].exists(exports.storage_key(export))


@pytest.mark.django_db(transaction=True)
def test_a_row_another_worker_holds_is_skipped_not_waited_for():
    admin = UserFactory()
    export = ended_export(admin)
    held, release = threading.Event(), threading.Event()

    def other_worker():
        try:
            with transaction.atomic():
                list(DataExport.objects.select_for_update().filter(pk=export.pk))
                held.set()
                release.wait(10)
        finally:
            connections.close_all()

    thread = threading.Thread(target=other_worker)
    thread.start()
    try:
        assert held.wait(10)
        started = time.monotonic()
        assert exports.purge_file(export.pk) == "skipped"
        assert time.monotonic() - started < 5
    finally:
        release.set()
        thread.join()
    assert exports.purge_file(export.pk) == "purged"


def test_a_download_racing_the_end_of_its_export_is_refused(admin, ready, monkeypatch):
    export = ready["export"]
    real_open = storages["exports"].open

    def open_then_lose_the_race(name, mode="rb"):
        file = real_open(name, mode)
        DataExport.objects.filter(pk=export.pk).update(
            status="expired", purge_requested_at=timezone.now(), purge_next_at=timezone.now()
        )
        return file

    monkeypatch.setattr(storages["exports"], "open", open_then_lose_the_race)
    with pytest.raises(BusinessRuleViolation):
        exports.for_download(actor=admin, export_id=export.pk)
    export.refresh_from_db()
    assert export.downloads == 0
    assert not AuditEvent.objects.filter(action=exports.AUDIT_DOWNLOADED).exists()


def test_expiry_ends_the_export_first_and_deletes_its_file_later_if_it_must(
    admin, ready, monkeypatch
):
    export = ready["export"]
    DataExport.objects.filter(pk=export.pk).update(expires_at=timezone.now())
    monkeypatch.setattr(storage_purge, "purge", failing())
    assert exports.expire() == 1
    export.refresh_from_db()
    assert (export.status, export.purge_error) == ("expired", "connection")
    assert export.storage_key
    assert storages["exports"].exists(export.storage_key)
    with pytest.raises(BusinessRuleViolation):
        exports.for_download(actor=admin, export_id=export.pk)
    monkeypatch.undo()
    assert exports.expire(export.purge_next_at) == 0  # nothing more expired; the file goes
    export.refresh_from_db()
    assert export.purged_at is not None
    assert AuditEvent.objects.filter(action=exports.AUDIT_EXPIRED).count() == 1


def test_a_build_that_lost_its_export_has_its_file_deleted_durably(admin, ready, monkeypatch):
    """The build saved after the withdrawal's deletion had already run: due again."""
    export = ready["export"]
    DataExport.objects.filter(pk=export.pk).update(status="queued", storage_key="", expires_at=None)
    real_build = exports.build

    def build_then_lose_the_race(row):
        built = real_build(row)
        DataExport.objects.filter(pk=row.pk).update(
            status="expired",
            purge_requested_at=timezone.now(),
            purge_next_at=None,
            purged_at=timezone.now(),  # its deletion ran before the file existed
        )
        return built

    monkeypatch.setattr(exports, "build", build_then_lose_the_race)
    monkeypatch.setattr(storage_purge, "purge", failing())
    exports.run(export.pk)
    export.refresh_from_db()
    key = exports.storage_key(export)
    assert (export.storage_key, export.purged_at, export.purge_error) == (key, None, "connection")
    assert storages["exports"].exists(key)
    monkeypatch.undo()
    exports.purge_due(export.purge_next_at)
    assert not storages["exports"].exists(key)


# --- reconciliation ------------------------------------------------------------------------------
def aged(key, hours=3):
    path = storages["exports"].path(key)
    then = time.time() - hours * 3600
    os.utime(path, (then, then))


def put(key, hours=3):
    storages["exports"].delete(key)
    storages["exports"].save(key, io.BytesIO(b"zip"))
    aged(key, hours)


@pytest.fixture
def own_store(tmp_path, monkeypatch):
    """An exports store of this test's own: other tests' files are not in the counts."""
    monkeypatch.setitem(storages._storages, "exports", FileSystemStorage(location=str(tmp_path)))


def test_reconciliation_deletes_only_what_no_live_export_explains(admin, own_store, ready):
    live = ready["export"]
    aged(live.storage_key)
    stray = f"2026/01/{uuid4().hex}.zip"  # no export has this id (a restored database)
    young = f"2026/01/{uuid4().hex}.zip"  # may still be being written
    unknown = "2026/01/notes.txt"  # never an export's name: never deleted
    put(stray)
    put(young, hours=1)
    put(unknown)
    purged = ended_export(admin)
    exports.purge_file(purged.pk)
    put(exports.storage_key(purged))  # a restored bucket brought it back
    queued = DataExport.objects.create(
        subject_type="lead", subject_id=uuid4(), requested_by=admin, reference="DSAR-2"
    )
    put(exports.storage_key(queued))  # being built
    report = exports.reconcile_orphans()
    assert storages["exports"].exists(live.storage_key)
    assert storages["exports"].exists(young)
    assert storages["exports"].exists(unknown)
    assert storages["exports"].exists(exports.storage_key(queued))
    assert not storages["exports"].exists(stray)
    assert not storages["exports"].exists(exports.storage_key(purged))
    purged.refresh_from_db()
    assert purged.purged_at is not None  # deleted again through its row
    assert report == {
        "listed": 6,
        "orphans": 2,
        "removed": 2,
        "failed": 0,
        "unknown": 1,
        "refused": 0,
    }


def test_reconciliation_leaves_a_pending_deletion_to_its_row(admin, own_store, monkeypatch):
    export = ended_export(admin)
    aged(export.storage_key)
    monkeypatch.setattr(exports, "purge_file", lambda *a, **k: pytest.fail("not its job"))
    report = exports.reconcile_orphans()
    assert report["orphans"] == 0
    assert storages["exports"].exists(export.storage_key)


def test_reconciliation_refuses_a_store_that_overlaps_the_attachments(
    admin, own_store, monkeypatch
):
    stray = f"2026/01/{uuid4().hex}.zip"
    put(stray)
    with monkeypatch.context() as patched:
        patched.setattr(storage_purge, "overlaps", lambda a, b: True)
        assert exports.reconcile_orphans()["refused"] == 1
    assert storages["exports"].exists(stray)
    exports.reconcile_orphans()
    assert not storages["exports"].exists(stray)


def test_reconciliation_is_bounded_and_resumes(admin, own_store, monkeypatch):
    monkeypatch.setattr(exports, "RECONCILE_KEYS", 1)
    strays = sorted(f"2026/01/{uuid4().hex}.zip" for _ in range(2))
    for key in strays:
        put(key)
    assert exports.reconcile_orphans()["removed"] == 1
    assert [storages["exports"].exists(key) for key in strays] == [False, True]
    assert exports.reconcile_orphans()["removed"] == 1  # resumed after the first
    assert not storages["exports"].exists(strays[1])
    assert exports.reconcile_orphans()["listed"] == 0  # the next pass starts over: empty
