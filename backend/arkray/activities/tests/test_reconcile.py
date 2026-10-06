"""reconcile_attachments (final audit ARCH-3, SRE-9, R105; docs/runbooks.md#attachment-
reconciliation): rows against objects, a read-only check by default, only safe repairs, an
outage never reported as lost files."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from datetime import timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management import call_command
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities import attachments, reconcile, storage, telemetry
from arkray.activities.models import Attachment
from arkray.audit.models import AuditEvent
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory

pytestmark = pytest.mark.django_db

CHECK = reconcile.Options(retry_pause_s=0)
REPAIR = reconcile.Options(repair=True, retry_pause_s=0)


@pytest.fixture(autouse=True)
def own_root(settings, tmp_path):
    """A store of this test's own (the test settings share one directory per process)."""
    settings.STORAGES = {
        **settings.STORAGES,
        "attachments": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "files"), "base_url": None},
        },
    }
    storage.reset_guard()
    yield
    storage.reset_guard()


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


@pytest.fixture
def make(note, user_a):
    counter = iter(range(10_000))

    def make_row(
        *,
        content: bytes = b"price list",
        state: str = "stored",
        scan: str = "not_scanned",
        age: timedelta = timedelta(hours=2),
        deleted: bool = False,
        purged: bool = False,
        write: bool = True,
        stored_size: int | None = None,
        key: str | None = None,
    ) -> Attachment:
        key = key or f"2026/10/{next(counter):04d}{'0' * 28}"
        if write:
            storages["attachments"].save(key, ContentFile(content))
        created = timezone.now() - age
        return Attachment.objects.create(
            note=note,
            original_name="price-list.txt",
            extension="txt",
            content_type="text/plain",
            size=stored_size or len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            storage_key=key,
            state=state,
            scan_status=scan,
            uploaded_by=user_a,
            created_at=created,
            stored_at=created if state == "stored" else None,
            deleted_at=created if deleted else None,
            purged_at=created if purged else None,
        )

    return make_row


def object_exists(row: Attachment) -> bool:
    return storages["attachments"].exists(row.storage_key)


class TestCheck:
    def test_a_healthy_store_is_exit_0_and_a_check_writes_nothing(self, make):
        make()
        make(deleted=True, purged=True, write=False)  # removed, as it should be
        make(state="uploading", age=timedelta(minutes=1), write=False)  # being written
        make(state="failed", age=timedelta(minutes=1))  # its purge job hasn't run yet
        with CaptureQueriesContext(connection) as queries:
            report = reconcile.run(CHECK)
        assert (report.exit_code(), report.checked, report.healthy) == (0, 4, 4)
        assert report.complete
        statements = {q["sql"].split()[0].upper() for q in queries.captured_queries}
        assert statements == {"SELECT"}

    def test_a_check_repairs_nothing(self, make):
        stale_upload = make(state="uploading")
        leftover = make(state="failed")
        lost = make()
        storages["attachments"].delete(lost.storage_key)
        report = reconcile.run(CHECK)
        assert (report.pending, report.failed, report.missing, report.repaired) == (1, 1, 1, 0)
        states = dict(Attachment.objects.values_list("pk", "state"))
        assert states == {stale_upload.pk: "uploading", leftover.pk: "failed", lost.pk: "stored"}
        assert object_exists(leftover)
        assert Attachment.objects.get(pk=leftover.pk).purged_at is None
        assert not AuditEvent.objects.exists()

    def test_a_stored_file_without_its_object_is_missing(self, make):
        lost = make()
        storages["attachments"].delete(lost.storage_key)
        report = reconcile.run(CHECK)
        assert (report.missing, report.exit_code()) == (1, 1)
        assert report.samples["missing"] == [str(lost.pk)]
        lost.refresh_from_db()
        assert lost.state == "stored"  # a check changes nothing

    def test_an_object_no_row_names_is_an_orphan_reported_by_count_only(self, make):
        make()
        storages["attachments"].save("2026/10/stray", io.BytesIO(b"who wrote this"))
        report = reconcile.run(CHECK)
        assert report.orphaned == 1
        assert "2026/10/stray" not in json.dumps(report.as_dict())
        shown = reconcile.run(reconcile.Options(show_keys=True, retry_pause_s=0))
        assert shown.orphan_keys == ["2026/10/stray"]
        repaired = reconcile.run(REPAIR)
        assert repaired.orphaned == 1
        assert storages["attachments"].exists("2026/10/stray")  # never deleted

    def test_a_size_or_content_mismatch_is_reported_never_repaired(self, make):
        short = make(content=b"abc", stored_size=10)
        altered = make(content=b"original")
        storages["attachments"].delete(altered.storage_key)
        storages["attachments"].save(altered.storage_key, ContentFile(b"tampered"))
        report = reconcile.run(
            reconcile.Options(repair=True, verify_hash=True, hash_sample=1.0, retry_pause_s=0)
        )
        assert (report.size_mismatch, report.hash_mismatch, report.repaired) == (1, 1, 0)
        assert report.samples["size_mismatch"] == [str(short.pk)]
        assert report.samples["hash_mismatch"] == [str(altered.pk)]

    def test_an_overdue_malware_scan_is_reported(self, make):
        make(scan="pending")
        make(scan="pending", age=timedelta(minutes=5))
        assert reconcile.run(CHECK).pending_scan == 1


class TestRepair:
    def test_a_lost_object_marks_the_file_unavailable_once_and_keeps_the_row(self, make, caplog):
        lost = make()
        storages["attachments"].delete(lost.storage_key)
        with caplog.at_level("WARNING"):
            report = reconcile.run(REPAIR)
        assert (report.missing, report.repaired) == (1, 1)
        lost.refresh_from_db()
        assert lost.state == "failed"
        assert lost.purged_at is not None  # nothing left to remove
        assert lost.note_id is not None
        event = AuditEvent.objects.get(action="attachment.marked_unavailable")
        assert event.target_id == str(lost.pk)
        assert lost.storage_key not in caplog.text
        assert "price-list" not in caplog.text
        assert AuditEvent.objects.filter(action="attachment.reconciled").count() == 1
        again = reconcile.run(REPAIR)
        assert (again.missing, again.repaired, again.exit_code()) == (0, 0, 0)

    def test_stale_uploads_finish_only_when_their_object_is_whole(self, make, settings):
        settings.ATTACHMENT_SCANNER = "clamd://127.0.0.1:1"
        whole = make(state="uploading")
        partial = make(state="uploading", content=b"abc", stored_size=50)
        never = make(state="uploading", write=False)
        recent = make(state="uploading", age=timedelta(minutes=2), write=False)
        report = reconcile.run(REPAIR)
        assert (report.pending, report.repaired) == (3, 3)
        states = dict(Attachment.objects.values_list("pk", "state"))
        assert states[whole.pk] == "stored"
        assert states[partial.pk] == "failed"
        assert states[never.pk] == "failed"
        assert states[recent.pk] == "uploading"  # in progress: left alone
        whole.refresh_from_db()
        assert whole.scan_status == "pending"  # a scanner is configured: checked first
        assert AuditEvent.objects.get(action="attachment.uploaded").metadata["repaired"] is True

    def test_leftover_objects_of_gone_files_are_purged(self, make):
        failed = make(state="failed")
        deleted_late_write = make(deleted=True, purged=True)  # written after its purge
        young = make(state="failed", age=timedelta(minutes=5))
        report = reconcile.run(REPAIR)
        assert (report.failed, report.repaired) == (2, 2)
        assert not object_exists(failed)
        assert not object_exists(deleted_late_write)
        assert object_exists(young)  # its purge job hasn't run yet
        failed.refresh_from_db()
        assert failed.purged_at is not None

    def test_a_file_marked_unavailable_whose_object_comes_back_is_left_for_an_admin(self, make):
        """A bucket restored from a backup brings it back: reported, never purged (an
        erased file is purged again, whatever happened before)."""
        lost = make()
        erased = make()
        for row in (lost, erased):
            storages["attachments"].delete(row.storage_key)
        reconcile.run(REPAIR)
        attachments.erase_for_notes([erased.note_id], timezone.now())  # both notes' files
        Attachment.objects.filter(pk=lost.pk).update(
            original_name="price-list.txt", sha256=hashlib.sha256(b"price list").hexdigest()
        )
        for row in (lost, erased):
            storages["attachments"].save(row.storage_key, ContentFile(b"price list"))
        report = reconcile.run(REPAIR)
        assert (report.restorable, report.failed) == (1, 1)
        assert report.samples["restorable"] == [str(lost.pk)]
        assert object_exists(lost)
        assert not object_exists(erased)

    def test_many_missing_objects_are_reported_but_not_all_marked(self, make):
        rows = [make() for _ in range(3)]
        for row in rows:
            storages["attachments"].delete(row.storage_key)
        report = reconcile.run(reconcile.Options(repair=True, max_repair_missing=1))
        assert (report.missing, report.repaired) == (3, 1)
        assert "bucket or volume" in report.notes[0]
        assert Attachment.objects.filter(state="stored").count() == 2

    def test_a_repair_applies_only_to_the_row_as_it_was_seen(self, make):
        row = make()
        seen = attachments.Seen("stored", None, None)
        Attachment.objects.filter(pk=row.pk).update(deleted_at=timezone.now())
        assert attachments.mark_unavailable(row.pk, seen) is False
        assert Attachment.objects.get(pk=row.pk).state == "stored"


@pytest.mark.django_db(transaction=True)
def test_a_row_locked_by_an_upload_is_skipped_not_waited_for(crm_configuration, user_a):
    lead = LeadFactory(owner=user_a)
    note = NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)
    row = Attachment.objects.create(
        note=note,
        original_name="x.txt",
        extension="txt",
        content_type="text/plain",
        size=1,
        sha256="0" * 64,
        storage_key="2026/10/locked",
        uploaded_by=user_a,
        created_at=timezone.now() - timedelta(hours=2),
    )
    locked, release = threading.Event(), threading.Event()

    def hold() -> None:
        try:
            with transaction.atomic():
                Attachment.objects.select_for_update().get(pk=row.pk)
                locked.set()
                release.wait(10)
        finally:
            connection.close()

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert locked.wait(10)
        seen = attachments.Seen("uploading", None, None)
        assert attachments.fail_stale_upload(row.pk, seen) is False  # at once, not blocked
    finally:
        release.set()
        holder.join(10)
    assert attachments.fail_stale_upload(row.pk, seen) is True


class TestOutages:
    def test_storage_that_cannot_be_listed_is_an_error_never_missing_files(self, make, monkeypatch):
        make()
        make()

        def down(self):
            raise storage.StorageUnavailable(error_class="timeout")

        monkeypatch.setattr(storage.ObjectPages, "next_page", down)
        report = reconcile.run(reconcile.Options(repair=True, retry_pause_s=0, max_errors=3))
        assert report.exit_code() == 2
        assert "timeout" in report.aborted
        assert (report.missing, report.repaired, report.errors) == (0, 0, 3)
        assert Attachment.objects.filter(state="stored").count() == 2

    def test_a_store_that_fails_each_lookup_is_an_error_too(self, make, monkeypatch):
        rows = [make() for _ in range(3)]
        for row in rows:  # the listing shows nothing (an unmounted volume, say)...
            storages["attachments"].delete(row.storage_key)

        def failing(_key):  # ...and the store can't answer for any one file
            raise storage.StorageUnavailable(error_class="connection")

        monkeypatch.setattr(storage, "size", failing)
        report = reconcile.run(reconcile.Options(repair=True, retry_pause_s=0, max_errors=2))
        assert (report.exit_code(), report.missing, report.repaired) == (2, 0, 0)
        assert report.aborted

    def test_the_breaker_opening_mid_pass_stops_it(self, make, settings, monkeypatch):
        settings.ATTACHMENT_STORAGE_BREAKER_FAILURES = 2
        storage.reset_guard()
        for _ in range(4):
            make(write=False)

        def broken(self, name):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(type(storages["attachments"]), "size", broken)
        report = reconcile.run(reconcile.Options(retry_pause_s=0, max_errors=3))
        assert report.exit_code() == 2
        assert report.missing == 0
        assert storage.breaker_open()


class TestOrdering:
    def test_rows_and_objects_meet_in_key_order_without_extra_lookups(self, make, monkeypatch):
        """The merge pairs both streams; a lookup per row would be a sign the database
        orders keys differently from the store (correct, but slow)."""
        for _ in range(7):
            make()
        make(key="2026/09/" + "f" * 32)
        make(key="2027/01/" + "0" * 32)
        lookups = []
        real = storage.size
        monkeypatch.setattr(storage, "size", lambda key: lookups.append(key) or real(key))
        report = reconcile.run(reconcile.Options(batch_size=2, retry_pause_s=0))
        assert (report.checked, report.healthy, report.listed) == (9, 9, 9)
        assert lookups == []

    def test_a_row_created_during_the_pass_is_not_an_orphan(self, make, note, user_a, monkeypatch):
        make()
        # Listed before the row stream reaches it... but its row commits only after the
        # rows around it were read: the candidate is confirmed by a lookup, and isn't one.
        storages["attachments"].save("2026/09/new", io.BytesIO(b"new"))
        real = storage.ObjectPages.next_page

        def listed_then_committed(self):
            page = real(self)
            if not Attachment.objects.filter(storage_key="2026/09/new").exists():
                make(key="2026/09/new", content=b"new", write=False, age=timedelta(0))
            return page

        monkeypatch.setattr(storage.ObjectPages, "next_page", listed_then_committed)
        report = reconcile.run(reconcile.Options(batch_size=1, retry_pause_s=0))
        assert report.orphaned == 0

    def test_each_batch_of_rows_is_an_index_range_scan(self, make):
        """Keyset pages on the unique storage_key index: no sort, no table scan, at any
        size (20,000 rows here, planner statistics fresh)."""
        row = make()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO activities_attachment (id, note_id, original_name, extension,"
                " content_type, size, sha256, storage_key, state, scan_status, uploaded_by_id,"
                " created_at) SELECT gen_random_uuid(), %s, 'x.txt', 'txt', 'text/plain', 1,"
                " repeat('0', 64), '2025/' || lpad(n::text, 8, '0'), 'stored', 'not_scanned',"
                " %s, now() FROM generate_series(1, 20000) AS n",
                [row.note_id, row.uploaded_by_id],
            )
            cursor.execute("ANALYZE activities_attachment")
            query = (
                Attachment.objects.order_by("storage_key")
                .values(*reconcile.FIELDS)
                .filter(storage_key__gt="2025/00010000")[:500]
            )
            sql, params = query.query.sql_with_params()
            cursor.execute(f"EXPLAIN {sql}", params)
            plan = " | ".join(line[0] for line in cursor.fetchall())
        assert "Index Scan using activities_attachment_storage_key" in plan, plan
        assert "Sort" not in plan, plan
        assert "Seq Scan" not in plan, plan


class TestPublishing:
    def test_a_whole_pass_is_published_a_partial_one_is_not(self, make):
        make()
        lost = make()
        storages["attachments"].delete(lost.storage_key)
        reconcile.run(reconcile.Options(limit=1, retry_pause_s=0))
        assert telemetry.last_reconcile() is None
        reconcile.run(CHECK)
        last = telemetry.last_reconcile()
        assert (last["outcome"], last["missing"], last["mode"]) == (1, 1, "check")


class TestCommand:
    def test_json_report_and_exit_codes(self, make):
        make()
        call_command("reconcile_attachments", stdout=io.StringIO(), stderr=io.StringIO())
        lost = make()
        storages["attachments"].delete(lost.storage_key)
        out, progress = io.StringIO(), io.StringIO()
        with pytest.raises(SystemExit) as exited:
            call_command("reconcile_attachments", "--json", stdout=out, stderr=progress)
        assert exited.value.code == 1
        found = json.loads(out.getvalue())
        assert (found["missing"], found["exit_code"], found["complete"]) == (1, 1, True)
        assert found["samples"] == {"missing": [str(lost.pk)]}
        assert "checked 2 rows" in progress.getvalue()

    def test_the_human_report_names_ids_never_keys_or_names(self, make):
        lost = make()
        storages["attachments"].delete(lost.storage_key)
        output = io.StringIO()
        with pytest.raises(SystemExit):
            call_command("reconcile_attachments", stdout=output, stderr=io.StringIO())
        out = output.getvalue()
        assert str(lost.pk) in out
        assert lost.storage_key not in out
        assert "price-list" not in out
        assert "exit 1" in out

    def test_a_repair_is_explicit(self, make):
        lost = make()
        storages["attachments"].delete(lost.storage_key)
        with pytest.raises(SystemExit):
            call_command(
                "reconcile_attachments",
                "--repair",
                "--stale-after",
                "30m",
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        assert Attachment.objects.get(pk=lost.pk).state == "failed"


class TestDailyCheck:
    def test_the_beat_task_checks_and_publishes_never_repairs(self, make, settings):
        from arkray.activities import tasks

        lost = make()
        storages["attachments"].delete(lost.storage_key)
        counts = tasks.reconcile_check()
        assert (counts["missing"], counts["repaired"]) == (1, 0)
        assert Attachment.objects.get(pk=lost.pk).state == "stored"
        assert telemetry.last_reconcile()["outcome"] == 1
        entry = settings.CELERY_BEAT_SCHEDULE["activities-reconcile-check"]
        assert entry["task"] == "activities.reconcile_check"

    def test_it_can_be_turned_off(self, make, settings):
        from arkray.activities import tasks

        settings.ATTACHMENT_RECONCILE_DAILY = False
        assert tasks.reconcile_check() is None
        assert telemetry.last_reconcile() is None
