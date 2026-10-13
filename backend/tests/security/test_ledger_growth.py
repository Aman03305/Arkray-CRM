"""The erasure ledger grows only with personal data really removed (security review M3;
core.ledger, pipeline.field_values, activities.attachments, privacy.replay;
docs/privacy.md#restore-safe-erasure).

The ledger is write-once, so every entry is permanent. Any user could grow it without limit:
`POST .../fields/{id}/delete-values` repeated for the same removed field queued a purge each
time, and each purge appended an entry whether or not a value was left; removing fields that
held nothing appended one per field; every deleted file appended its own. Now:

- a deletion request with nothing left to delete in the caller's workspace queues nothing,
  and requests queued before the job runs share one job;
- the purge appends its entry in the transaction of its first batch that removes a value,
  last, just before that batch commits (as an erasure does): an entry exists exactly when a
  value was removed, and a purge that removes nothing appends nothing;
- a file purge takes the other deleted files waiting for theirs into the same transaction
  and records them in one `attachment_deleted` entry (their ids, comma-separated); replay
  reads the single-file entries of earlier releases and the batched ones.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.core.files.storage import storages
from django.db import transaction
from django.utils import timezone

from arkray.activities import attachments
from arkray.activities.models import Activity, Attachment
from arkray.core import ledger
from arkray.core.access import AccessScope
from arkray.core.ledger_gate import GATE
from arkray.core.models import HoldSubject, LedgerState, LegalHold, OutboxEvent
from arkray.identity.models import User
from arkray.pipeline import configuration, field_values
from arkray.pipeline.models import CustomField, FieldType, Opportunity, Pipeline
from arkray.privacy import replay
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox, run_concurrently, signed_in

pytestmark = pytest.mark.django_db

KEY = "ledger-test-key-" + "x" * 32
PDF = b"%PDF-1.7\n%%EOF\n"
NAME = "Contact notes"


def _use_ledger(settings: Any, directory: Path) -> Path:
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    GATE.reset()
    return directory


@pytest.fixture
def ledger_dir(settings, tmp_path):
    yield _use_ledger(settings, tmp_path / "ledger")
    GATE.reset()


def entries(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


# --- custom values -----------------------------------------------------------------------------
def _removed_field_with_values(count=3, *, name=NAME, position=50):
    """A removed field of the default pipeline whose values `count` deals still hold."""
    pipeline = Pipeline.objects.get(is_default=True)
    field = CustomField.objects.create(
        pipeline=pipeline, name=name, field_type=FieldType.TEXT, position=position
    )
    for index in range(count):
        deal = OpportunityFactory(lead=LeadFactory(), pipeline=pipeline)
        Opportunity.objects.filter(pk=deal.pk).update(
            custom_fields={str(field.pk): f"Mobile 98111 0000{index}"}
        )
    CustomField.objects.filter(pk=field.pk).update(is_active=False)  # removed, values kept
    field.refresh_from_db()
    return field


@pytest.fixture
def removed_field(db):
    return _removed_field_with_values()


def values_left(field: CustomField) -> int:
    return Opportunity.objects.filter(custom_fields__has_key=str(field.pk)).count()


def delete_values(client: Any, field: CustomField, *, name: str = NAME) -> Any:
    return client.post(
        f"/api/v1/workspaces/all/pipelines/{field.pipeline_id}/fields/{field.pk}/delete-values",
        {"confirm_name": name},
        format="json",
    )


def test_fifty_repeated_requests_add_one_entry(ledger_dir, admin, removed_field):
    client = signed_in(admin)
    answers = set()
    for _ in range(50):
        response = delete_values(client, removed_field)
        answers.add((response.status_code, response.content))
        drain_outbox()  # each request's job has run before the next request
    assert answers == {(202, b"")}  # the same answer every time: nothing to tell them apart
    assert values_left(removed_field) == 0
    found = entries(ledger_dir)
    assert [(e["kind"], e["subject"]) for e in found] == [
        ("custom_values_deleted", str(removed_field.pk))
    ]
    assert ledger.applied() == (1, found[0]["mac"])
    assert GATE.status() == "ok"

    # The one entry is enough for a restore: the values come back, replay deletes them again.
    for index, deal in enumerate(Opportunity.objects.filter(pipeline=removed_field.pipeline_id)):
        Opportunity.objects.filter(pk=deal.pk).update(
            custom_fields={str(removed_field.pk): f"Mobile 98111 0000{index}"}
        )
    LedgerState.objects.filter(pk=1).update(applied_seq=0, applied_mac="")
    report = replay.replay(operator=admin)
    assert report.complete, report.as_dict()
    assert values_left(removed_field) == 0


def test_requests_queued_before_the_job_runs_share_it(ledger_dir, admin, removed_field):
    client = signed_in(admin)
    for _ in range(50):
        assert delete_values(client, removed_field).status_code == 202
    assert OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).count() == 1
    drain_outbox()
    assert len(entries(ledger_dir)) == 1
    assert values_left(removed_field) == 0


def test_a_purge_that_removes_nothing_appends_nothing(ledger_dir, removed_field):
    assert field_values.purge(removed_field.pk) == {"rows": 3, "held": 0}
    assert len(entries(ledger_dir)) == 1
    assert field_values.purge(removed_field.pk) == {"rows": 0, "held": 0}
    assert field_values.purge(removed_field.pk, owner_id=uuid4()) == {"rows": 0, "held": 0}
    assert len(entries(ledger_dir)) == 1
    assert GATE.status() == "ok"


def test_removing_fields_that_hold_nothing_queues_and_appends_nothing(ledger_dir, admin):
    pipeline = Pipeline.objects.get(is_default=True)
    for position in range(60, 70):
        CustomField.objects.create(
            pipeline=pipeline,
            name=f"Empty {position}",
            field_type=FieldType.TEXT,
            position=position,
        )
    pipeline.refresh_from_db()
    configuration.replace_fields(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        pipeline_id=pipeline.pk,
        version=pipeline.version,
        fields=[],
        delete_removed_values=True,
    )
    assert not OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).exists()
    drain_outbox()
    assert entries(ledger_dir) == []


def test_values_a_hold_keeps_add_no_entry_until_they_are_deleted(ledger_dir, admin, removed_field):
    held = list(Opportunity.objects.filter(pipeline=removed_field.pipeline_id))
    holds = [
        LegalHold.objects.create(
            subject_type=HoldSubject.LEAD,
            subject_id=deal.lead_id,
            reference="CASE-9",
            placed_by=admin.pk,
        )
        for deal in held
    ]
    client = signed_in(admin)
    for _ in range(5):
        assert delete_values(client, removed_field).status_code == 202
        drain_outbox()
    assert values_left(removed_field) == 3  # all kept
    assert entries(ledger_dir) == []
    LegalHold.objects.filter(pk__in=[h.pk for h in holds]).update(
        released_at=timezone.now(), released_by=admin.pk
    )
    assert delete_values(client, removed_field).status_code == 202  # "run it again"
    drain_outbox()
    assert values_left(removed_field) == 0
    assert len(entries(ledger_dir)) == 1


def test_a_failed_ledger_write_removes_no_value(ledger_dir, removed_field, monkeypatch):
    """The entry and the removal commit together: no value is gone without its entry."""
    store = type(ledger.store())

    def unwritable(self, seq, body):
        raise ledger.LedgerUnavailable("down")

    monkeypatch.setattr(store, "create", unwritable)
    with pytest.raises(ledger.LedgerUnavailable):
        field_values.purge(removed_field.pk)
    assert values_left(removed_field) == 3
    monkeypatch.undo()
    assert field_values.purge(removed_field.pk)["rows"] == 3  # the job's retry
    assert len(entries(ledger_dir)) == 1


# --- files ---------------------------------------------------------------------------------------
def deleted_file(
    note: Activity, owner: User, *, name: str = "Kavita_Menon_report.pdf"
) -> Attachment:
    """A stored file a person deleted, its object still in storage (its purge not run)."""
    key = f"test/{uuid4().hex}"
    storages["attachments"].save(key, io.BytesIO(PDF))
    row = Attachment.objects.create(
        note=note,
        original_name=name,
        extension="pdf",
        content_type="application/pdf",
        size=len(PDF),
        sha256=hashlib.sha256(PDF).hexdigest(),
        storage_key=key,
        state="stored",
        uploaded_by=owner,
        stored_at=timezone.now(),
    )
    Attachment.objects.filter(pk=row.pk).update(deleted_at=timezone.now(), deleted_by=owner)
    row.refresh_from_db()
    return row


def gone(row: Attachment) -> bool:
    row.refresh_from_db()
    return (
        row.purged_at is not None
        and row.original_name == attachments.REMOVED_NAME
        and not storages["attachments"].exists(row.storage_key)
    )


def batched_ids(entry: dict[str, Any]) -> list[str]:
    return sorted(entry["subject"].split(","))


def test_deleted_files_are_recorded_in_one_entry_per_purge(ledger_dir, user_a):
    lead = LeadFactory(owner=user_a)
    note = NoteFactory(lead=lead, created_by=user_a)
    client = signed_in(user_a)
    ids = []
    for index in range(5):
        response = client.post(
            f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
            PDF,
            content_type="application/octet-stream",
            HTTP_X_FILENAME=f"Quote_{index}.pdf",
        )
        assert response.status_code == 201, response.content
        ids.append(response.json()["id"])
    drain_outbox()
    for attachment_id in ids:
        assert (
            client.delete(f"/api/v1/workspaces/me/attachments/{attachment_id}").status_code == 204
        )
    drain_outbox()
    (entry,) = entries(ledger_dir)
    assert entry["kind"] == "attachment_deleted"
    assert batched_ids(entry) == sorted(ids)
    assert all(gone(row) for row in Attachment.objects.filter(pk__in=ids))
    assert GATE.status() == "ok"


def test_housekeeping_records_its_purges_in_batches(ledger_dir, user_a):
    note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a)
    rows = [deleted_file(note, user_a) for _ in range(5)]
    assert attachments.housekeeping(timezone.now())["purged"] == 5
    (entry,) = entries(ledger_dir)
    assert batched_ids(entry) == sorted(str(row.pk) for row in rows)
    assert all(gone(row) for row in rows)


def test_a_purge_takes_no_held_leads_file_with_it(ledger_dir, admin, user_a):
    free = deleted_file(NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a), user_a)
    held_lead = LeadFactory(owner=user_a)
    held = deleted_file(NoteFactory(lead=held_lead, created_by=user_a), user_a)
    LegalHold.objects.create(
        subject_type=HoldSubject.LEAD,
        subject_id=held_lead.pk,
        reference="CASE-7",
        placed_by=admin.pk,
    )
    assert attachments.purge(free.pk) is True
    assert gone(free)
    held.refresh_from_db()
    assert (held.purged_at, held.original_name) == (None, "Kavita_Menon_report.pdf")
    assert storages["attachments"].exists(held.storage_key)
    assert [e["subject"] for e in entries(ledger_dir)] == [str(free.pk)]


def test_a_failed_ledger_write_purges_none_of_the_batch(ledger_dir, user_a, monkeypatch):
    note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a)
    rows = [deleted_file(note, user_a) for _ in range(3)]

    def unwritable(self, seq, body):
        raise ledger.LedgerUnavailable("down")

    monkeypatch.setattr(type(ledger.store()), "create", unwritable)
    with pytest.raises(ledger.LedgerUnavailable):
        attachments.purge(rows[0].pk)
    for row in rows:
        row.refresh_from_db()
        assert (row.purged_at, row.original_name) == (None, "Kavita_Menon_report.pdf")
    monkeypatch.undo()
    assert attachments.purge(rows[0].pk) is True  # the job's retry: the objects already gone
    assert all(gone(row) for row in rows)
    (entry,) = entries(ledger_dir)
    assert batched_ids(entry) == sorted(str(row.pk) for row in rows)


def _restore_files(rows: list[Attachment], name: str) -> None:
    """A backup from before the deletions: the rows as they were, the objects back in a
    restored bucket, the ledger state 0."""
    for row in rows:
        Attachment.objects.filter(pk=row.pk).update(
            deleted_at=None, deleted_by=None, purged_at=None, original_name=name, sha256="b" * 64
        )
        storages["attachments"].save(row.storage_key, io.BytesIO(PDF))
    LedgerState.objects.filter(pk=1).update(applied_seq=0, applied_mac="")


def test_replay_purges_every_file_of_a_batched_entry(ledger_dir, admin, user_a):
    note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a)
    rows = [deleted_file(note, user_a) for _ in range(3)]
    assert attachments.purge(rows[0].pk) is True
    assert len(entries(ledger_dir)) == 1
    _restore_files(rows, "Restore_Drill_Name.pdf")
    assert GATE.status() == "behind"
    report = replay.replay(operator=admin)
    assert report.complete, report.as_dict()
    assert report.counts() == {"reapplied": 1}
    assert all(gone(row) for row in rows)
    assert GATE.status() == "ok"
    assert replay.replay(operator=admin).counts() == {"already_applied": 1}


def test_replay_still_reads_an_earlier_releases_single_file_entries(ledger_dir, admin, user_a):
    note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a)
    single, batch = deleted_file(note, user_a), [deleted_file(note, user_a) for _ in range(2)]
    with transaction.atomic():
        ledger.append("attachment_deleted", single.pk)  # as earlier releases wrote them
    with transaction.atomic():
        ledger.append("attachment_deleted", ",".join(str(row.pk) for row in batch))
    report = replay.replay(operator=admin)
    assert report.complete, report.as_dict()
    assert report.counts() == {"reapplied": 2}
    assert all(gone(row) for row in [single, *batch])


def test_a_batched_entry_naming_a_file_the_backup_never_had_still_applies_the_rest(
    ledger_dir, admin, user_a
):
    note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a)
    row = deleted_file(note, user_a)
    with transaction.atomic():
        ledger.append("attachment_deleted", f"{uuid4()},{row.pk}")
    report = replay.replay(operator=admin)
    assert report.complete, report.as_dict()
    assert gone(row)


# --- races ---------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
class TestRaces:
    @pytest.fixture(autouse=True)
    def _ledger(self, settings, tmp_path, crm_configuration):
        self.directory = _use_ledger(settings, tmp_path / "ledger")
        yield
        GATE.reset()

    def test_two_value_purges_at_once_append_only_for_what_they_removed(self):
        """Each purge appends an entry only if it removed values itself (they may share
        the rows between them: SKIP LOCKED); one that found them gone appends nothing."""
        field = _removed_field_with_values(4)
        results = run_concurrently(
            lambda: field_values.purge(field.pk), lambda: field_values.purge(field.pk)
        )
        assert all(isinstance(result, dict) for result in results), results
        assert sum(result["rows"] for result in results) == 4
        assert len(entries(self.directory)) == sum(1 for r in results if r["rows"])
        assert values_left(field) == 0
        assert [e.seq for e in ledger.verify()] == list(range(1, len(entries(self.directory)) + 1))
        assert field_values.purge(field.pk) == {"rows": 0, "held": 0}  # a third: nothing
        assert len(entries(self.directory)) == sum(1 for r in results if r["rows"])

    def test_two_appenders_at_once_keep_one_chain(self):
        def append() -> ledger.Entry | None:
            with transaction.atomic():
                return ledger.append("lead_erased", uuid4())

        results = run_concurrently(append, append, append)
        assert all(isinstance(result, ledger.Entry) for result in results), results
        assert sorted(result.seq for result in results) == [1, 2, 3]
        assert [e.seq for e in ledger.verify()] == [1, 2, 3]
        assert ledger.applied()[0] == 3

    def test_two_file_purges_at_once_purge_each_file_once(self):
        from tests.factories import UserFactory

        owner = UserFactory()
        note = NoteFactory(lead=LeadFactory(owner=owner), created_by=owner)
        rows = [deleted_file(note, owner) for _ in range(6)]
        results = run_concurrently(
            lambda: attachments.purge(rows[0].pk), lambda: attachments.purge(rows[-1].pk)
        )
        assert not any(isinstance(result, BaseException) for result in results), results
        assert all(gone(row) for row in rows)
        recorded = [rid for entry in entries(self.directory) for rid in batched_ids(entry)]
        assert sorted(recorded) == sorted(str(row.pk) for row in rows)  # each exactly once
        assert len(entries(self.directory)) <= 2


# --- review round 1 ------------------------------------------------------------------------------
def _fields_with_values(count: int, *, position: int = 100) -> list[CustomField]:
    """`count` active fields of the default pipeline, each with a value on two deals."""
    pipeline = Pipeline.objects.get(is_default=True)
    fields = [
        CustomField.objects.create(
            pipeline=pipeline, name=f"Notes {n}", field_type=FieldType.TEXT, position=position + n
        )
        for n in range(count)
    ]
    for index in range(2):
        deal: Any = OpportunityFactory(lead=LeadFactory(), pipeline=pipeline)
        Opportunity.objects.filter(pk=deal.pk).update(
            custom_fields={str(f.pk): f"Mobile 98111 0{index}{n:03d}" for n, f in enumerate(fields)}
        )
    return fields


def _remove_every_field(client: Any) -> Any:
    pipeline = Pipeline.objects.get(is_default=True)
    return client.put(
        f"/api/v1/workspaces/all/pipelines/{pipeline.pk}/fields",
        {"version": pipeline.version, "custom_fields": [], "delete_removed_values": True},
        format="json",
    )


def test_removing_thirty_fields_with_their_values_adds_one_entry(ledger_dir, admin):
    """LEDGER-2 (a): each removed field queued its own job and appended its own entry, so
    two requests (thirty fields with values, then removed) added thirty entries."""
    fields = _fields_with_values(30)
    response = _remove_every_field(signed_in(admin))
    assert response.status_code == 200, response.content
    assert OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).count() == 1
    drain_outbox()
    (entry,) = entries(ledger_dir)
    assert entry["kind"] == "custom_values_deleted"
    assert sorted(entry["subject"].split(",")) == sorted(str(f.pk) for f in fields)
    assert all(values_left(f) == 0 for f in fields)
    assert GATE.status() == "ok"


def test_replay_deletes_every_field_of_a_batched_entry_and_reads_single_ones(ledger_dir, admin):
    fields = _fields_with_values(3)
    assert _remove_every_field(signed_in(admin)).status_code == 200
    drain_outbox()
    old = _removed_field_with_values(2)
    with transaction.atomic():
        ledger.append("custom_values_deleted", old.pk)  # as earlier releases wrote them
    field_values.purge(old.pk, replay=True)
    # The restore: every value back, the database's ledger state 0.
    for deal in Opportunity.objects.filter(pipeline__is_default=True):
        Opportunity.objects.filter(pk=deal.pk).update(
            custom_fields={str(f.pk): "Mobile 98111 00000" for f in [*fields, old]}
        )
    LedgerState.objects.filter(pk=1).update(applied_seq=0, applied_mac="")
    report = replay.replay(operator=admin)
    assert report.complete, report.as_dict()
    assert report.counts() == {"reapplied": 2}
    assert all(values_left(f) == 0 for f in [*fields, old])


def test_a_job_queued_by_the_previous_release_still_runs(ledger_dir, removed_field):
    from arkray.core import outbox

    with transaction.atomic():  # the payload a previous release queued
        outbox.enqueue(field_values.TOPIC_PURGE_VALUES, {"field_id": str(removed_field.pk)})
    drain_outbox()
    assert values_left(removed_field) == 0
    assert [e["subject"] for e in entries(ledger_dir)] == [str(removed_field.pk)]


def test_value_deletions_past_the_daily_budget_are_refused(ledger_dir, admin, monkeypatch):
    """LEDGER-2 (a): a per-person budget on the requests that write the ledger (owner
    decision; MAX_VALUE_DELETIONS_PER_USER_PER_DAY)."""
    from arkray.audit.models import AuditEvent
    from tests.factories import AdminFactory

    monkeypatch.setattr(field_values, "MAX_VALUE_DELETIONS_PER_USER_PER_DAY", 3)
    removed = [_removed_field_with_values(1, name=f"Budget {n}", position=60 + n) for n in range(4)]
    client = signed_in(admin)
    for n, field in enumerate(removed[:3]):
        assert delete_values(client, field, name=f"Budget {n}").status_code == 202
    queued = OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).count()
    requested = AuditEvent.objects.filter(action=field_values.AUDIT_REQUESTED).count()
    refused = delete_values(client, removed[3], name="Budget 3")
    assert refused.status_code == 429, refused.content
    assert refused.json()["error"]["code"] == "rate_limited"
    assert int(refused["Retry-After"]) > 0
    assert OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).count() == queued
    assert AuditEvent.objects.filter(action=field_values.AUDIT_REQUESTED).count() == requested
    # A field removal asking for deletion is refused as a whole, nothing changed.
    active = _fields_with_values(2, position=200)
    response = _remove_every_field(client)
    assert response.status_code == 429, response.content
    assert all(CustomField.objects.get(pk=f.pk).is_active for f in active)
    # Someone else's budget is their own.
    assert delete_values(signed_in(AdminFactory()), removed[3], name="Budget 3").status_code == 202


def test_a_database_error_partway_through_a_batch_keeps_what_was_done(
    ledger_dir, user_a, monkeypatch
):
    """LEDGER-5: a lock timeout on a later file's hold lock rolled back the rows of the
    files whose objects were already deleted: no entry, their names kept until a retry."""
    from django.db import OperationalError

    from arkray.core import holds

    note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a)
    rows = [deleted_file(note, user_a) for _ in range(5)]
    calls = {"n": 0}
    real = holds.refuse_if_held

    def flaky(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:  # the third file's hold lock: the 5 s lock timeout
            raise OperationalError("canceling statement due to lock timeout")
        real(*args, **kwargs)

    monkeypatch.setattr(holds, "refuse_if_held", flaky)
    assert attachments.purge(rows[0].pk) is True
    assert all(gone(row) for row in rows[:2])
    (entry,) = entries(ledger_dir)
    assert batched_ids(entry) == sorted(str(row.pk) for row in rows[:2])
    for row in rows[2:]:
        row.refresh_from_db()
        assert (row.purged_at, row.original_name) == (None, "Kavita_Menon_report.pdf")
        assert storages["attachments"].exists(row.storage_key)


def _customer_with_files(owner: User, notes: int, per_note: int) -> Any:
    """A customer with `notes` notes of `per_note` stored files each (rows only: their
    objects are not in storage, which a purge counts as removed)."""
    from django.db import connection

    lead: Any = LeadFactory(owner=owner)
    Activity.objects.bulk_create(NoteFactory.build_batch(notes, lead=lead), batch_size=1000)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO activities_attachment (id, note_id, original_name, extension,"
            " content_type, size, sha256, storage_key, state, scan_status, uploaded_by_id,"
            " created_at, stored_at, deleted_at, deleted_by_id, purged_at)"
            " SELECT gen_random_uuid(), a.id, 'Kavita_Menon_report.pdf', 'pdf',"
            " 'application/pdf', 100, repeat('0', 64), 'test/' || gen_random_uuid()::text,"
            " 'stored', 'not_scanned', %s, now(), now(), NULL, NULL, NULL"
            " FROM activities_activity a CROSS JOIN generate_series(1, %s) g"
            " WHERE a.lead_id = %s AND a.type = 'note'",
            [owner.pk, per_note, lead.pk],
        )
    return lead


def _erasure_statements(admin: User, lead: Any) -> tuple[int, int]:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from arkray.privacy import services

    before = OutboxEvent.objects.filter(topic__startswith="activities.purge").count()
    with CaptureQueriesContext(connection) as captured:
        services.erase(lead.pk, operator_id=admin.pk)
    queued = OutboxEvent.objects.filter(topic__startswith="activities.purge").count() - before
    return len(captured.captured_queries), queued


def test_erasing_a_customer_with_many_files_costs_the_same(ledger_dir, admin, user_a, monkeypatch):
    """QUEUES-2: the erasure queued one purge job per file (a savepoint, an insert and a
    release each) inside its transaction: 66.8 s for 500 notes of 10 files, beyond the
    automatic replay's limit at the activity budget. Now one paged job for them all."""
    # Full batches whatever this machine's storage speed (the entry count below counts them).
    monkeypatch.setattr(attachments, "PURGE_BATCH_BUDGET_S", 60.0)
    small = _customer_with_files(user_a, 20, 10)
    large = _customer_with_files(user_a, 200, 10)
    few, queued_few = _erasure_statements(admin, small)
    many, queued_many = _erasure_statements(admin, large)
    assert (queued_few, queued_many) == (1, 0)  # the second shares the waiting job
    assert many - few <= 10, (few, many)
    erased = Attachment.objects.filter(note__lead=large)
    assert erased.count() == 2_000
    assert not erased.exclude(original_name="erased").exists()
    drain_outbox(rounds=50)
    assert not Attachment.objects.filter(
        note__lead_id__in=[small.pk, large.pk], purged_at__isnull=True
    ).exists()
    assert (
        not Attachment.objects.filter(note__lead_id__in=[small.pk, large.pk])
        .exclude(original_name=attachments.REMOVED_NAME)
        .exists()
    )
    recorded = {rid for entry in entries(ledger_dir) for rid in entry["subject"].split(",")}
    assert {
        str(pk) for pk in Attachment.objects.filter(note__lead=large).values_list("pk", flat=True)
    } <= recorded
    assert len(entries(ledger_dir)) <= 2 + 2_200 // ledger.MAX_SUBJECTS + 2


# --- review round 2 ------------------------------------------------------------------------------
def test_a_file_that_cannot_be_removed_holds_up_no_other(ledger_dir, admin, user_a, monkeypatch):
    """LEDGER2-5: one permanently failing delete (Object Lock on a version: access denied)
    stopped the paged job and the housekeeping at the first failure, every time: the erased
    customer's objects stayed in storage."""
    from arkray.activities import storage
    from arkray.privacy import services

    stuck = deleted_file(NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a), user_a)
    lead = LeadFactory(owner=user_a)
    note = NoteFactory(lead=lead, created_by=user_a)
    customer = [deleted_file(note, user_a) for _ in range(3)]
    Attachment.objects.filter(pk__in=[r.pk for r in customer]).update(
        deleted_at=None, deleted_by=None
    )
    real = storage.delete

    def denied(key):
        if key == stuck.storage_key:
            raise storage.StorageUnavailable(error_class="access_denied")
        return real(key)

    monkeypatch.setattr(storage, "delete", denied)
    services.erase(lead.pk, operator_id=admin.pk)
    drain_outbox()
    assert all(gone(row) for row in customer)  # the erasure's files: gone
    stuck.refresh_from_db()
    assert stuck.purged_at is None  # kept, retried later
    assert storages["attachments"].exists(stuck.storage_key)
    # The housekeeping leaves it alone while it backs off...
    other = deleted_file(NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a), user_a)
    report = attachments.housekeeping(timezone.now())
    assert gone(other)
    assert (report["purged"], report["storage_failures"]) == (1, 0)
    # ...and once it is due again, tries it and goes on past it.
    stuck.refresh_from_db()
    assert stuck.purge_attempts >= 1
    assert stuck.purge_next_at is not None
    assert stuck.purge_next_at > timezone.now()
    Attachment.objects.filter(pk=stuck.pk).update(purge_next_at=timezone.now())
    another = deleted_file(NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a), user_a)
    report = attachments.housekeeping(timezone.now())
    assert gone(another)
    assert (report["purged"], report["storage_failures"]) == (1, 1)


def test_a_single_field_deletion_keeps_the_previous_release_s_job_format(
    ledger_dir, admin, removed_field
):
    """LEDGER2-6: a previous release's worker reads `field_id` only: a delete-values job,
    always one field, keeps that format, so a rolling deploy never loses it."""
    assert delete_values(signed_in(admin), removed_field).status_code == 202
    (event,) = OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES)
    assert event.payload == {"field_id": str(removed_field.pk)}
    drain_outbox()
    assert values_left(removed_field) == 0


# --- review round 3 ------------------------------------------------------------------------------
class _DownCache:
    """A cache that is down (django-redis with IGNORE_EXCEPTIONS: misses, writes dropped)."""

    def get(self, *args, **kwargs):
        return None

    def get_many(self, *args, **kwargs):
        return {}

    def set(self, *args, **kwargs):
        return None


@pytest.mark.parametrize("cache_down", [False, True], ids=["cache-up", "cache-down"])
def test_files_that_cannot_be_removed_never_hold_up_an_erasure(
    ledger_dir, admin, user_a, monkeypatch, caplog, cache_down
):
    """LEDGER3-3: five permanently failing oldest files kept the paged job from ever
    reaching an erased customer's files (0, 0, 0 purged); with the cache down every run
    failed and the attempt count never grew."""
    import logging

    from arkray.activities import storage
    from arkray.privacy import services

    if cache_down:
        monkeypatch.setattr(attachments, "cache", _DownCache(), raising=False)
    note = NoteFactory(lead=LeadFactory(owner=user_a), created_by=user_a)
    stuck = [deleted_file(note, user_a) for _ in range(5)]
    keys = {row.storage_key for row in stuck}
    real = storage.delete

    def denied(key):
        if key in keys:
            raise storage.StorageUnavailable(error_class="access_denied")
        return real(key)

    monkeypatch.setattr(storage, "delete", denied)
    lead = LeadFactory(owner=user_a)
    customer = [deleted_file(NoteFactory(lead=lead, created_by=user_a), user_a) for _ in range(3)]
    Attachment.objects.filter(pk__in=[r.pk for r in customer]).update(
        deleted_at=None, deleted_by=None
    )
    services.erase(lead.pk, operator_id=admin.pk)
    for _ in range(3):  # the job's runs (each may stop at its failures and be retried)
        with contextlib.suppress(storage.StorageUnavailable):
            attachments.purge_deleted()
    assert all(gone(row) for row in customer)
    # The count of failed attempts lives with the file: it grows, and escalates, whatever
    # the cache does.
    Attachment.objects.filter(pk=stuck[0].pk).update(
        purge_attempts=attachments.PURGE_ALERT_AFTER - 1, purge_next_at=None
    )
    with (
        caplog.at_level(logging.WARNING, logger="arkray.activities.attachments"),
        contextlib.suppress(storage.StorageUnavailable),
    ):
        attachments.purge_deleted()
    escalated = [
        r
        for r in caplog.records
        if r.getMessage() == "attachment_purge_failed" and r.levelno == logging.ERROR
    ]
    assert escalated
