"""Restore-safe erasure (privacy remediation P2-8; core.ledger, core.ledger_gate,
privacy.replay; docs/privacy.md#restore-safe-erasure).

Every erasure is written to a tamper-evident ledger outside the database. A database that is
behind it (a restored backup), ahead of it (a lost ledger), or a ledger that is tampered
with or unreadable, closes the API until `replay_erasures` has re-applied every entry. A
restore is simulated here by putting rows and the ledger state back as a backup taken
before the erasure would have them; scripts/restore_drill.sh does it with a real
pg_dump/pg_restore.
"""

from __future__ import annotations

import io
import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import Stubber
from django.core.files.storage import storages
from django.core.management import call_command
from django.db import connection

from arkray.activities import attachments
from arkray.activities.models import Attachment
from arkray.ai import indexing
from arkray.ai.models import KnowledgeChunk
from arkray.audit.models import AuditEvent
from arkray.core import ledger
from arkray.core.ledger_gate import GATE
from arkray.core.models import LedgerState
from arkray.identity.models import User
from arkray.leads.models import Lead
from arkray.privacy import replay, services, staff
from arkray.privacy.services import ERASED
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

KEY = "ledger-test-key-" + "x" * 32


@pytest.fixture
def ledger_dir(settings, tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    GATE.reset()
    yield directory
    GATE.reset()


def entries(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


def api_status(user) -> int:
    return signed_in(user).get("/api/v1/auth/me").status_code


def erase(lead, admin):
    return services.erase(lead.pk, operator_id=admin.pk)


def test_an_erasure_is_recorded_chained_and_applied(ledger_dir, admin, user_a):
    first, second = LeadFactory(owner=user_a), LeadFactory(owner=user_a)
    erase(first, admin)
    erase(second, admin)
    found = entries(ledger_dir)
    assert [(e["seq"], e["kind"], e["subject"]) for e in found] == [
        (1, "lead_erased", str(first.pk)),
        (2, "lead_erased", str(second.pk)),
    ]
    assert found[1]["prev"] == found[0]["mac"]
    assert first.first_name not in json.dumps(found)  # ids only
    assert [e.seq for e in ledger.verify()] == [1, 2]
    assert ledger.applied() == (2, found[1]["mac"])
    assert GATE.status() == "ok"
    assert api_status(user_a) == 200


@pytest.mark.django_db(transaction=True)
def test_entries_are_appended_inside_the_erasing_transaction_only(ledger_dir):
    with pytest.raises(RuntimeError):
        ledger.append("lead_erased", "00000000-0000-0000-0000-000000000001")


def test_an_entry_is_never_overwritten(ledger_dir, admin, user_a):
    erase(LeadFactory(owner=user_a), admin)
    store = ledger.store()
    assert store.create(1, b"{}") is False
    assert entries(ledger_dir)[0]["kind"] == "lead_erased"


@pytest.mark.parametrize("damage", ["edit", "remove", "reorder"])
def test_a_tampered_ledger_closes_the_api(ledger_dir, admin, user_a, damage):
    for _ in range(3):
        erase(LeadFactory(owner=user_a), admin)
    files = sorted(ledger_dir.glob("*.json"))
    if damage == "edit":
        data = json.loads(files[1].read_text())
        data["subject"] = "00000000-0000-0000-0000-000000000000"
        files[1].write_text(json.dumps(data))
    elif damage == "remove":
        files[1].unlink()
    else:
        first, last = files[0].read_text(), files[2].read_text()
        files[0].write_text(last)
        files[2].write_text(first)
    with pytest.raises(ledger.LedgerTampered):
        ledger.verify()
    if damage != "remove":
        assert GATE.status() in {"tampered", "diverged"}
        assert api_status(user_a) == 503
    with pytest.raises(ledger.LedgerTampered):
        replay.replay(operator=admin)


def test_an_unwritable_ledger_stops_the_erasure_with_nothing_changed(
    ledger_dir, admin, user_a, settings
):
    lead = LeadFactory(owner=user_a)
    settings.ERASURE_LEDGER_URL = (ledger_dir / "missing").as_uri()
    with pytest.raises(ledger.LedgerUnavailable):
        erase(lead, admin)
    lead.refresh_from_db()
    assert lead.first_name != ERASED
    assert not AuditEvent.objects.filter(action="lead.erased").exists()


def test_an_unreadable_ledger_closes_a_process_that_never_verified_it(ledger_dir, user_a, settings):
    settings.ERASURE_LEDGER_URL = (ledger_dir / "missing").as_uri()
    assert GATE.status() == "unavailable"
    assert api_status(user_a) == 503
    assert signed_in(user_a).get("/health/ready").status_code == 503


def test_a_brief_outage_after_a_verified_state_stays_open(ledger_dir, user_a, settings):
    assert GATE.status(now=1000.0) == "ok"
    settings.ERASURE_LEDGER_URL = (ledger_dir / "missing").as_uri()
    assert GATE.status(now=1001.0) == "ok"  # within ERASURE_LEDGER_MAX_STALE_S
    assert GATE.status(now=1000.0 + settings.ERASURE_LEDGER_MAX_STALE_S + 1) == "unavailable"


def test_a_database_ahead_of_its_ledger_is_closed(ledger_dir, admin, user_a, settings, tmp_path):
    erase(LeadFactory(owner=user_a), admin)
    replacement = tmp_path / "other-ledger"
    replacement.mkdir()
    settings.ERASURE_LEDGER_URL = replacement.as_uri()  # the ledger lost or swapped
    assert GATE.status() == "ahead"
    assert api_status(user_a) == 503


def _restore(lead: Lead, snapshot: dict[str, Any], state: tuple[int, str]) -> None:
    """Put the lead and the ledger state back as a backup from before the erasure has
    them (scripts/restore_drill.sh does this with pg_dump/pg_restore)."""
    Lead.objects.filter(pk=lead.pk).update(**snapshot)
    LedgerState.objects.filter(pk=1).update(applied_seq=state[0], applied_mac=state[1])


def test_a_restored_backup_is_closed_until_its_erasures_are_replayed(
    ledger_dir, admin, user_a, tmp_path, ai_on
):
    lead = LeadFactory(
        owner=user_a,
        first_name="Resurrected",
        last_name="Person",
        description="Resurrected Person asked about the analyser warranty.",
    )
    snapshot = {
        "first_name": "Resurrected",
        "last_name": "Person",
        "email": lead.email,
        "organization_name": lead.organization_name,
        "description": lead.description,
        "archived_at": None,
    }
    before = ledger.applied()
    erase(lead, admin)
    _restore(lead, snapshot, before)
    # The restored database's Ask Arkray index holds the person's text again.
    indexing.enqueue_lead(lead.pk)
    drain_outbox()
    assert KnowledgeChunk.objects.filter(lead_id=lead.pk).exists()
    assert GATE.status() == "behind"
    assert api_status(user_a) == 503

    dry = replay.replay(operator=admin, dry_run=True)
    assert dry.counts() == {"would_reapply": 1}
    assert Lead.objects.get(pk=lead.pk).first_name == "Resurrected"  # unchanged
    assert GATE.status() == "behind"

    report_path = tmp_path / "report.json"
    out = StringIO()
    call_command("replay_erasures", by=admin.email, report=str(report_path), stdout=out)
    assert Lead.objects.get(pk=lead.pk).first_name == ERASED
    # The index chunks the restore brought back are gone (re-indexing reads the redacted text).
    assert not KnowledgeChunk.objects.filter(lead_id=lead.pk).exists()
    report = json.loads(report_path.read_text())
    assert report["complete"] is True
    assert report["counts"] == {"reapplied": 1}
    assert report["applied_after"] == report["ledger_head_seq"] == 1
    assert "Resurrected" not in report_path.read_text()
    assert GATE.status() == "ok"
    assert api_status(user_a) == 200
    assert AuditEvent.objects.filter(action="privacy.erasures_replayed").exists()

    again = replay.replay(operator=admin)  # idempotent
    assert again.counts() == {"already_applied": 1}


def test_replay_re_deletes_files_and_forgets_their_names(ledger_dir, admin, user_a):
    lead = LeadFactory(owner=user_a)
    note = NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)
    response = signed_in(user_a).post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        b"%PDF-1.7\n%%EOF\n",
        content_type="application/octet-stream",
        HTTP_X_FILENAME="Restore_Drill_Name.pdf",
    )
    row = Attachment.objects.get(pk=response.json()["id"])
    signed_in(user_a).delete(f"/api/v1/workspaces/me/attachments/{row.pk}")
    drain_outbox()
    assert entries(ledger_dir)[-1]["kind"] == "attachment_deleted"
    # The restore: the row as it was, its object back in a restored bucket.
    Attachment.objects.filter(pk=row.pk).update(
        deleted_at=None,
        deleted_by=None,
        purged_at=None,
        original_name="Restore_Drill_Name.pdf",
        sha256="b" * 64,
    )
    storages["attachments"].save(row.storage_key, io.BytesIO(b"%PDF"))
    LedgerState.objects.filter(pk=1).update(applied_seq=0, applied_mac="")
    result = replay.replay(operator=admin)
    assert result.counts() == {"reapplied": 1}
    row.refresh_from_db()
    assert row.deleted_at is not None
    assert row.original_name == attachments.REMOVED_NAME
    assert not storages["attachments"].exists(row.storage_key)


def test_replay_pseudonymises_a_restored_former_user_again(ledger_dir, admin, user_b):
    from arkray.identity import services as identity_services

    identity_services.deactivate_user(actor_id=admin.pk, user_id=user_b.pk)
    before = ledger.applied()
    staff.pseudonymise(user_b.pk, operator_id=admin.pk)
    with connection.cursor() as cursor:  # the backup had the account active, named
        cursor.execute(
            "UPDATE identity_user SET first_name = 'Priya', last_name = 'Patel',"
            " email = 'priya.restored@example.test', status = 'active', is_active = true,"
            " deactivated_at = NULL, password = 'md5$restored$x' WHERE id = %s",
            [user_b.pk],
        )
    LedgerState.objects.filter(pk=1).update(applied_seq=before[0], applied_mac=before[1])
    assert replay.replay(operator=admin).counts() == {"reapplied": 1}
    restored = User.objects.get(pk=user_b.pk)
    assert staff.pseudonymised(restored)
    assert (restored.status, restored.has_usable_password()) == ("deactivated", False)


def test_s3_entries_are_written_once_with_a_conditional_put(settings):
    import boto3  # type: ignore[import-untyped]

    settings.ERASURE_LEDGER_KEY = KEY
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s"
    )
    store = ledger.S3Store("s3://arkray-ledger/prod", client=client)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "arkray-ledger",
                "Key": "prod/000000000001.json",
                "Body": b"{}",
                "IfNoneMatch": "*",
                "ContentType": "application/json",
            },
        )
        stub.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        assert store.create(1, b"{}") is True
        assert store.create(1, b"{}") is False


def test_the_operator_commands_need_an_administrator(ledger_dir, user_a, tmp_path):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("replay_erasures", by=user_a.email, report=str(tmp_path / "r.json"))


# --- backend review findings (P1-2, P2-3, P2-8, P2-9, P2-10, P2-11) ---------------------------
def _two_removable_fields():
    from arkray.pipeline.models import CustomField, FieldType, Opportunity, Pipeline

    pipeline = Pipeline.objects.get(is_default=True)
    first = CustomField.objects.create(
        pipeline=pipeline, name="Notes one", field_type=FieldType.TEXT, position=60
    )
    second = CustomField.objects.create(
        pipeline=pipeline, name="Notes two", field_type=FieldType.TEXT, position=61
    )
    deal = OpportunityFactory(lead=LeadFactory(), pipeline=pipeline)
    Opportunity.objects.filter(pk=deal.pk).update(
        custom_fields={str(first.pk): "Call 98111 00001", str(second.pk): "Call 98111 00002"}
    )
    pipeline.refresh_from_db()
    return pipeline, first, second, deal


def _remove_all_custom_fields(admin, pipeline):
    from arkray.core.access import AccessScope
    from arkray.pipeline import configuration

    return configuration.replace_fields(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        pipeline_id=pipeline.pk,
        version=pipeline.version,
        fields=[],
        delete_removed_values=True,
    )


def test_a_failed_field_removal_leaves_nothing_in_the_ledger(ledger_dir, admin):
    """Backend review P1: the request appended its entries before it could still fail, so a
    rolled-back removal closed the API and replay then deleted values nobody deleted."""
    from django.db import transaction

    from arkray.pipeline.models import CustomField

    pipeline, first, second, _deal = _two_removable_fields()

    class Boom(Exception):
        pass

    def remove_then_fail():
        with transaction.atomic():
            _remove_all_custom_fields(admin, pipeline)
            raise Boom  # anything failing after the request, in the same transaction

    with pytest.raises(Boom):
        remove_then_fail()
    assert entries(ledger_dir) == []
    assert CustomField.objects.get(pk=first.pk).is_active
    assert GATE.status() == "ok"

    _remove_all_custom_fields(admin, pipeline)
    assert entries(ledger_dir) == []  # the request itself writes none
    drain_outbox()
    found = entries(ledger_dir)
    assert sorted(e["subject"] for e in found) == sorted([str(first.pk), str(second.pk)])
    assert {e["kind"] for e in found} == {"custom_values_deleted"}
    assert ledger.applied()[0] == len(found)
    assert GATE.status() == "ok"


def test_a_failed_write_leaves_no_partial_entry(ledger_dir, admin, user_a, monkeypatch):
    """Backend review P2: a full disk left an empty entry that read as tampered for good."""
    import os as os_module

    erase(LeadFactory(owner=user_a), admin)
    real_fsync = os_module.fsync

    def full_disk(descriptor):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ledger.os, "fsync", full_disk)
    lead = LeadFactory(owner=user_a)
    with pytest.raises(ledger.LedgerUnavailable):
        erase(lead, admin)
    monkeypatch.setattr(ledger.os, "fsync", real_fsync)
    assert [p.name for p in sorted(ledger_dir.iterdir())] == ["000000000001.json"]
    assert [e.seq for e in ledger.verify()] == [1]
    assert GATE.status() == "ok"
    erase(lead, admin)  # retried once the disk has room
    assert [e.seq for e in ledger.verify()] == [1, 2]


def test_an_s3_ledger_without_a_prefix_lists_the_bucket_root(settings):
    """Backend review P2: s3://bucket listed Prefix="/" and never found an entry."""
    import boto3

    settings.ERASURE_LEDGER_KEY = KEY
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s"
    )
    store = ledger.S3Store("s3://arkray-ledger", client=client)
    with Stubber(client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": False,
                "Contents": [
                    {"Key": "000000000001.json"},
                    {"Key": "000000000002.json"},
                    {"Key": "notes/000000000003.json"},
                    {"Key": "README.txt"},
                ],
            },
            {"Bucket": "arkray-ledger", "Prefix": ""},
        )
        stub.add_response(
            "get_object",
            {"Body": io.BytesIO(b'{"seq": 2}')},
            {"Bucket": "arkray-ledger", "Key": "000000000002.json"},
        )
        assert store.last() == {"seq": 2}
        stub.assert_no_pending_responses()


def test_the_gate_verifies_only_new_entries_after_its_first_check(ledger_dir, admin, user_a):
    """Backend review P2: every new entry re-read the whole ledger inside a request."""
    erase(LeadFactory(owner=user_a), admin)
    assert GATE.status() == "ok"
    erase(LeadFactory(owner=user_a), admin)
    store = ledger.store()
    reads = []
    original = type(store).read

    def counting(self, after=0):
        reads.append(after)
        return original(self, after)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(store), "read", counting)
        assert GATE.status() == "ok"
    assert reads == [1]  # only entry 2 was read
    # An entry that doesn't continue the verified chain still closes it.
    files = sorted(ledger_dir.glob("*.json"))
    data = json.loads(files[1].read_text())
    data["prev"] = "0" * 64
    data["mac"] = ledger._mac({k: v for k, v in data.items() if k != "mac"})
    erase(LeadFactory(owner=user_a), admin)  # entry 3, linked to the real entry 2
    files[1].write_text(json.dumps(data))  # entry 2 replaced after the gate verified it...
    GATE.reset()
    assert GATE.status() == "tampered"  # ...a fresh process checks the whole chain


@pytest.mark.django_db(transaction=True)
def test_a_check_during_an_erasures_commit_never_closes_the_api(ledger_dir, admin, user_a):
    """Backend review P2: the entry is written just before its transaction commits; a check
    in between saw "behind" and closed the API for a whole interval."""
    import psycopg
    from django.db import connection as django_connection

    assert GATE.status() == "ok"
    entry = ledger.Entry(seq=1, kind="lead_erased", subject=str(user_a.pk), at="t", prev="", mac="")
    entry = ledger.Entry(**{**entry.content(), "mac": ledger._mac(entry.content())})
    params = django_connection.get_connection_params()
    other = psycopg.connect(
        **{k: v for k, v in params.items() if k in {"dbname", "user", "password", "host", "port"}}
    )
    try:
        other.execute("BEGIN")
        other.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(ledger._LOCK))
        assert ledger.store().create(1, entry.as_json())  # written, not yet committed
        assert GATE.status() == "settling"
        assert api_status(user_a) == 200
        other.rollback()  # the erasure failed after its entry: the ledger is ahead
    finally:
        other.close()
    assert GATE.status() == "behind"
    assert api_status(user_a) == 503


def test_replay_applies_entries_added_while_it_runs(ledger_dir, admin, user_a, monkeypatch):
    """Backend review P2: replay marked the head it started with, although re-erasing had
    appended more (its files' purges), so the API stayed closed after "complete"."""
    from django.db import transaction

    lead = LeadFactory(owner=user_a, first_name="Resurrected")
    before = ledger.applied()
    erase(lead, admin)
    _restore(lead, {"first_name": "Resurrected"}, before)
    other = LeadFactory(owner=user_a)
    original = replay._APPLY["lead_erased"]
    appended = []

    def lead_and_a_concurrent_erasure(entry, operator, dry_run):
        outcome = original(entry, operator, dry_run)
        if not appended:  # an entry appended while replaying (as a purge job would)
            with transaction.atomic():
                appended.append(ledger.append("lead_erased", other.pk))
        return outcome

    monkeypatch.setitem(replay._APPLY, "lead_erased", lead_and_a_concurrent_erasure)
    report = replay.replay(operator=admin)
    assert report.complete is True
    assert [o["seq"] for o in report.outcomes] == [1, 2]
    assert report.applied_after == report.ledger_head_seq == 2
    assert GATE.status() == "ok"
    assert api_status(user_a) == 200


def test_background_work_waits_while_the_gate_is_closed(ledger_dir, admin, user_a):
    """Backend review P2: workers kept running a restored backup's queued jobs (an export of
    someone erased since, mail to a pseudonymised address) before the replay."""
    from arkray.core.models import OutboxEvent, OutboxStatus
    from arkray.core.tasks import process_outbox_event, relay_outbox
    from arkray.privacy.tasks import housekeeping

    lead = LeadFactory(owner=user_a)
    before = ledger.applied()
    erase(lead, admin)
    drain_outbox()
    LedgerState.objects.filter(pk=1).update(applied_seq=before[0], applied_mac=before[1])
    from django.db import transaction

    from arkray.core import outbox

    with transaction.atomic():  # the restored database's queued work
        outbox.enqueue("pipeline.purge_custom_values", {"field_id": str(lead.pk)})
    assert GATE.status() == "behind"
    pending = OutboxEvent.objects.filter(status=OutboxStatus.PENDING)
    count = pending.count()
    assert count
    assert relay_outbox.apply().get() is None  # deferred: nothing claimed
    assert housekeeping.apply().get() is None
    event = pending.first()
    assert (
        process_outbox_event.apply(args=(event.pk, "00000000-0000-0000-0000-000000000000")).get()
        is None
    )
    assert OutboxEvent.objects.filter(status=OutboxStatus.PENDING).count() == count
    assert OutboxEvent.objects.get(pk=event.pk).attempts == 0

    replay.replay(operator=admin)
    assert GATE.status() == "ok"
    assert relay_outbox.apply().get() is not None  # runs again
