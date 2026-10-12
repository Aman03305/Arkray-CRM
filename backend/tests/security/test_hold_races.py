"""Placing a legal hold is serialised with everything that reads the holds and then deletes,
purges, erases or overwrites data on that basis (core.holds; risk register R134;
docs/privacy.md#legal-holds).

A hold placed at the same moment as one of those readers has either committed before the
reader read the holds (the reader keeps the data), or waits until the reader's transaction
has committed: it is never committed between the reader's check and its deletion. Each test
runs the reader for real, in its own thread and database connection, stops it between its
check and its deletion and places the hold meanwhile (or stops a placement between its
insert and its commit and runs the reader meanwhile). Before the hold barrier the hold was
committed at once and the reader then deleted what it covered: every test here failed
(mutation-checked, one reader at a time). `legal_hold place`, kept waiting past the lock
timeout by a reader in flight, stops with nothing placed and says to run it again.

Lock order (core.holds): the barrier is the first lock of its transaction. A reader holding a
subject's lock therefore never waits for the barrier while a placement holds it and waits
for that subject's lock: `TestLockOrder` runs that interleaving, which deadlocked with the
barrier taken after the subject's lock.
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest import mock
from uuid import UUID, uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, connections, transaction
from django.utils import timezone

from arkray.activities import attachments, storage
from arkray.activities.models import Activity, Attachment
from arkray.ai import service, subjects
from arkray.ai.models import Conversation, Question, QuestionStatus, WorkspaceKind
from arkray.audit import retention
from arkray.audit import services as audit
from arkray.audit.models import AuditDetail, AuditEvent
from arkray.core import holds, ledger
from arkray.core.access import AccessScope
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.models import HoldSubject, LegalHold
from arkray.identity import services as identity_services
from arkray.identity import support
from arkray.identity.models import SupportSession, User
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import field_values
from arkray.pipeline.models import CustomField, Opportunity
from arkray.privacy import replay, services, staff
from tests.factories import AdminFactory, LeadFactory, NoteFactory, OpportunityFactory, UserFactory
from tests.helpers import run_concurrently

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration", "ai_on"),
]

WAIT_S = 20.0  # the longest a stopped thread or a check waits: a test fails, never hangs
PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF\n"


# --- threads ---------------------------------------------------------------------------------
class Running:
    """`target` running in its own thread and database connection: its result (or the
    exception it raised) once joined, and its server process, to see what it waits for."""

    def __init__(self, target: Callable[[], Any]) -> None:
        self.result: Any = None
        self.pid = 0
        self._connected = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
        self._thread.start()
        assert self._connected.wait(WAIT_S), "the thread never connected"

    def _run(self, target: Callable[[], Any]) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                self.pid = cursor.fetchone()[0]
            self._connected.set()
            self.result = target()
        except BaseException as exc:  # handed to the test
            self.result = exc
        finally:
            self._connected.set()
            connections.close_all()

    def join(self) -> Any:
        self._thread.join(WAIT_S * 2)
        assert not self._thread.is_alive(), "the thread is still running"
        return self.result

    def ended_within(self, seconds: float) -> bool:
        self._thread.join(seconds)
        return not self._thread.is_alive()

    def waits_for_a_lock(self) -> bool:
        """True once its session waits for an advisory lock (the hold barrier or a subject's
        hold lock: pg_stat_activity); False if the thread ends first, so it never had to
        wait, or after WAIT_S."""
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline and self._thread.is_alive():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                    [self.pid],
                )
                row = cursor.fetchone()
            if row is not None and tuple(row) == ("Lock", "advisory"):
                return True
            time.sleep(0.01)
        return False


class Stop:
    """Where a thread stops until the test lets it go on; `reached` once it is there."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self._go = threading.Event()

    def __call__(self) -> None:
        self.reached.set()
        assert self._go.wait(WAIT_S), "never let go"

    def go(self) -> None:
        self._go.set()


def stopping_after(function: Callable[..., Any], stop: Stop) -> Callable[..., Any]:
    def stopped(*args: Any, **kwargs: Any) -> Any:
        result = function(*args, **kwargs)
        stop()
        return result

    return stopped


def stopping_before(function: Callable[..., Any], stop: Stop) -> Callable[..., Any]:
    def stopped(*args: Any, **kwargs: Any) -> Any:
        stop()
        return function(*args, **kwargs)

    return stopped


def placing(subject_type: HoldSubject, subject_id: UUID, by: User) -> Callable[[], str]:
    """Place a hold in a transaction of its own."""

    def place() -> str:
        with transaction.atomic():
            with connection.cursor() as cursor:  # waits a little longer than the default 5 s
                cursor.execute("SET LOCAL lock_timeout = '15s'")
            holds.place(subject_type, subject_id, reference="CASE-11", placed_by=by.pk)
        return "placed"

    return place


def hold_placed_while_stopped(
    reader: Callable[[], Any], stop: Stop, place: Callable[[], str]
) -> Any:
    """Run `reader` until it stops between its check and its deletion, then place the hold:
    the placement must wait for a lock the stopped reader holds, so nothing is committed
    until the reader has (it holds that lock until it commits). Returns the reader's
    result."""
    before = LegalHold.objects.count()
    reading = Running(reader)
    placer: Running | None = None
    try:
        assert stop.reached.wait(WAIT_S), f"the reader never got there: {reading.join()!r}"
        placer = Running(place)
        assert placer.waits_for_a_lock(), (
            "the hold was placed between the reader's check and its deletion"
        )
        assert LegalHold.objects.count() == before  # and nothing is committed meanwhile
    finally:
        stop.go()
        result = reading.join()
        placed = placer.join() if placer is not None else None
    assert not isinstance(result, BaseException), result
    assert placed == "placed", placed
    return result


def reader_arriving_while_a_hold_is_placed(
    reader: Callable[[], Any], subject_type: HoldSubject, subject_id: UUID, by: User
) -> Any:
    """Stop a placement between its insert and its commit, then run `reader`: it must wait
    for the hold, and then see it. Returns the reader's result (or exception)."""
    inserted, commit = threading.Event(), threading.Event()

    def place() -> str:
        with transaction.atomic():
            holds.place(subject_type, subject_id, reference="CASE-12", placed_by=by.pk)
            inserted.set()
            assert commit.wait(WAIT_S)
        return "placed"

    placer = Running(place)
    reading: Running | None = None
    try:
        assert inserted.wait(WAIT_S), placer.join()
        reading = Running(reader)
        assert reading.waits_for_a_lock(), "the reader didn't wait for the hold being placed"
    finally:
        commit.set()
        assert placer.join() == "placed"
        result = reading.join() if reading is not None else None
    return result


# --- records ---------------------------------------------------------------------------------
def stored(
    actor: User, text: str, answer: dict[str, Any] | None = None, *, days_ago: int = 0
) -> Question:
    """An answered question in a new conversation of the actor's own workspace."""
    when = timezone.now() - timedelta(days=days_ago)
    conversation = Conversation.objects.create(
        actor=actor,
        workspace_kind=WorkspaceKind.SELF,
        subject=actor,
        created_at=when,
        updated_at=when,
    )
    return Question.objects.create(
        conversation=conversation,
        actor=actor,
        text=text,
        status=QuestionStatus.ANSWERED,
        mode="llm",
        answer=answer or {"blocks": [], "sources": []},
        created_at=when,
        finished_at=when,
        expires_at=when + timedelta(minutes=2),
    )


def cites(*record_ids: UUID) -> dict[str, Any]:
    return {"citations": [{"ref": f"opportunity:{pk}", "snippet": "a deal"} for pk in record_ids]}


def kept(question: Question) -> bool:
    return Conversation.objects.filter(pk=question.conversation_id).exists()


def stored_file(note: Activity, owner: User, *, deleted: bool = True) -> Attachment:
    key = f"test/{uuid4().hex}"
    storage.save(key, io.BytesIO(PDF))
    row = Attachment.objects.create(
        note=note,
        original_name="Kavita_Menon_report.pdf",
        extension="pdf",
        content_type="application/pdf",
        size=len(PDF),
        sha256=hashlib.sha256(PDF).hexdigest(),
        storage_key=key,
        state="stored",
        uploaded_by=owner,
        stored_at=timezone.now(),
    )
    if deleted:
        Attachment.objects.filter(pk=row.pk).update(deleted_at=timezone.now(), deleted_by=owner)
    return row


def object_exists(row: Attachment) -> bool:
    from django.core.files.storage import storages

    return bool(storages["attachments"].exists(row.storage_key))


# --- Ask Arkray ------------------------------------------------------------------------------
class TestAskArkray:
    def test_a_deletion_stopped_after_its_check_holds_the_placement_off(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner, first_name="Sunita", last_name="Rao")
        question = stored(owner, "What did Sunita Rao say about reagents?")
        stop = Stop()
        with mock.patch.object(
            subjects, "held_conversations", stopping_after(subjects.held_conversations, stop)
        ):
            hold_placed_while_stopped(
                lambda: service.forget_conversation(
                    owner, AccessScope.own(owner.pk), question.conversation_id
                ),
                stop,
                placing(HoldSubject.LEAD, lead.pk, admin),
            )
        assert not kept(question)  # deleted before the hold, which came after
        assert holds.is_held(HoldSubject.LEAD, lead.pk)

    def test_a_deletion_arriving_while_a_hold_is_placed_waits_and_is_refused(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner, first_name="Sunita", last_name="Rao")
        question = stored(owner, "What did Sunita Rao say about reagents?")
        result = reader_arriving_while_a_hold_is_placed(
            lambda: service.forget_conversation(
                owner, AccessScope.own(owner.pk), question.conversation_id
            ),
            HoldSubject.LEAD,
            lead.pk,
            admin,
        )
        assert isinstance(result, service.ConversationHeld), result
        assert kept(question)
        assert not AuditEvent.objects.filter(action=service.CONVERSATION_DELETED).exists()

    @pytest.mark.parametrize("subject", [HoldSubject.LEAD, HoldSubject.USER])
    def test_housekeeping_honours_a_hold_placed_after_it_read_them(self, subject):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner, first_name="Sunita", last_name="Rao")
        about = stored(owner, "What did Sunita Rao order?", days_ago=40)
        other = stored(UserFactory(), "How many overdue tasks?", days_ago=40)
        held_id = lead.pk if subject == HoldSubject.LEAD else owner.pk
        original = service._held_memo_key

        def placed_then(customers):
            # Right after housekeeping read the holds, before its first batch.
            placing(subject, held_id, admin)()
            return original(customers)

        with mock.patch.object(service, "_held_memo_key", placed_then):
            done = service.housekeeping()
        assert kept(about)
        assert not kept(other)
        assert done["deleted_conversations"] == 1

    def test_a_housekeeping_batch_holds_the_placement_off(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner, first_name="Sunita", last_name="Rao")
        about = stored(owner, "What did Sunita Rao order?", days_ago=40)
        stop = Stop()
        touching = subjects.HeldCustomers.touching
        with mock.patch.object(subjects.HeldCustomers, "touching", stopping_after(touching, stop)):
            done = hold_placed_while_stopped(
                service.housekeeping, stop, placing(HoldSubject.LEAD, lead.pk, admin)
            )
        assert done["deleted_conversations"] == 1
        assert not kept(about)  # deleted before the hold, which came after


# --- erasure and pseudonymisation ------------------------------------------------------------
@pytest.fixture
def customers():
    """Kavita (to be erased) and Meera, a customer about to be held, and a conversation
    citing a deal of each: a hold on Meera keeps it."""
    owner, admin = UserFactory(), AdminFactory()
    person = LeadFactory(owner=owner, first_name="Kavita", last_name="Menon")
    other = LeadFactory(owner=owner, first_name="Meera", last_name="Iyer")
    deal, other_deal = OpportunityFactory(lead=person), OpportunityFactory(lead=other)
    both = stored(owner, "Compare the two quotes", cites(deal.pk, other_deal.pk))
    return {"owner": owner, "admin": admin, "person": person, "other": other, "both": both}


class TestErasure:
    def test_an_erasure_holds_off_a_hold_on_another_customer(self, customers):
        admin, other = customers["admin"], customers["other"]
        stop = Stop()
        erasable = services._erasable_conversations
        with mock.patch.object(services, "_erasable_conversations", stopping_after(erasable, stop)):
            done = hold_placed_while_stopped(
                lambda: services.erase(customers["person"].pk, operator_id=admin.pk),
                stop,
                placing(HoldSubject.LEAD, other.pk, admin),
            )
        assert done.conversations == 1  # matched before the hold, deleted before it committed
        assert not kept(customers["both"])
        assert holds.is_held(HoldSubject.LEAD, other.pk)

    def test_an_erasure_arriving_while_a_hold_is_placed_keeps_what_it_covers(self, customers):
        admin = customers["admin"]
        done = reader_arriving_while_a_hold_is_placed(
            lambda: services.erase(customers["person"].pk, operator_id=admin.pk),
            HoldSubject.LEAD,
            customers["other"].pk,
            admin,
        )
        assert isinstance(done, services.Erasure), done
        assert done.conversations == 0
        assert kept(customers["both"])
        event = AuditEvent.objects.get(action="lead.erased")
        assert event.metadata["conversations_held"] == 1
        assert Lead.objects.get(pk=customers["person"].pk).erased_at is not None


class TestStaff:
    def test_a_pseudonymisation_holds_off_a_hold_on_a_customer(self):
        admin, colleague = AdminFactory(), UserFactory()
        former = UserFactory(first_name="Vikram", last_name="Joshi")
        lead = LeadFactory(owner=colleague, first_name="Sunita", last_name="Rao")
        question = stored(colleague, "Did Vikram Joshi meet Sunita Rao?")
        identity_services.deactivate_user(actor_id=admin.pk, user_id=former.pk)
        stop = Stop()
        with mock.patch.object(staff, "_conversations", stopping_after(staff._conversations, stop)):
            done = hold_placed_while_stopped(
                lambda: staff.pseudonymise(former.pk, operator_id=admin.pk),
                stop,
                placing(HoldSubject.LEAD, lead.pk, admin),
            )
        assert done.conversations == 1
        assert not kept(question)

    def test_a_pseudonymisation_arriving_while_a_hold_is_placed_keeps_what_it_covers(self):
        admin, colleague = AdminFactory(), UserFactory()
        former = UserFactory(first_name="Vikram", last_name="Joshi")
        lead = LeadFactory(owner=colleague, first_name="Sunita", last_name="Rao")
        question = stored(colleague, "Did Vikram Joshi meet Sunita Rao?")
        identity_services.deactivate_user(actor_id=admin.pk, user_id=former.pk)
        done = reader_arriving_while_a_hold_is_placed(
            lambda: staff.pseudonymise(former.pk, operator_id=admin.pk),
            HoldSubject.LEAD,
            lead.pk,
            admin,
        )
        assert isinstance(done, staff.StaffErasure), done
        assert done.conversations == 0
        assert kept(question)


# --- files and custom values -----------------------------------------------------------------
class TestFiles:
    def test_a_purge_holds_off_a_hold_on_the_notes_lead(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner)
        row = stored_file(NoteFactory(lead=lead, created_by=owner), owner)
        stop = Stop()
        with mock.patch.object(storage, "delete", stopping_before(storage.delete, stop)):
            purged = hold_placed_while_stopped(
                lambda: attachments.purge(row.pk), stop, placing(HoldSubject.LEAD, lead.pk, admin)
            )
        assert purged is True
        row.refresh_from_db()
        assert row.purged_at is not None
        assert not object_exists(row)

    def test_a_purge_arriving_while_a_hold_is_placed_keeps_the_file(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner)
        row = stored_file(NoteFactory(lead=lead, created_by=owner), owner)
        purged = reader_arriving_while_a_hold_is_placed(
            lambda: attachments.purge(row.pk), HoldSubject.LEAD, lead.pk, admin
        )
        assert purged is False
        row.refresh_from_db()
        assert (row.purged_at, row.original_name) == (None, "Kavita_Menon_report.pdf")
        assert object_exists(row)

    def test_forgetting_purged_names_holds_off_a_hold(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner)
        row = stored_file(NoteFactory(lead=lead, created_by=owner), owner)
        Attachment.objects.filter(pk=row.pk).update(purged_at=timezone.now())  # names kept
        stop = Stop()
        with mock.patch.object(
            attachments, "_held_notes", stopping_after(attachments._held_notes, stop)
        ):
            count = hold_placed_while_stopped(
                lambda: attachments.forget_purged(actor_id=admin.pk, dry_run=False),
                stop,
                placing(HoldSubject.LEAD, lead.pk, admin),
            )
        assert count == 1
        row.refresh_from_db()
        assert row.original_name == attachments.REMOVED_NAME

    def test_an_erasure_never_waits_for_a_file_being_purged(self):
        """Review P3-4: the purge held its file's row across the storage calls, so erasing
        the lead (which rewrites every unpurged file of it) waited past the 5 s lock timeout
        and failed. It now goes through while the purge waits on storage; the purge then
        finishes, and the file is gone, nameless."""
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner, first_name="Kavita", last_name="Menon")
        row = stored_file(NoteFactory(lead=lead, created_by=owner), owner)
        stop = Stop()
        with mock.patch.object(storage, "delete", stopping_before(storage.delete, stop)):
            purging = Running(lambda: attachments.purge(row.pk))
            try:
                assert stop.reached.wait(WAIT_S), purging.join()
                erased = Running(lambda: services.erase(lead.pk, operator_id=admin.pk)).join()
                purge_still_waiting = not purging.ended_within(0)
            finally:
                stop.go()
                purged = purging.join()
        assert isinstance(erased, services.Erasure), erased
        assert purge_still_waiting  # the erasure went through while it waited on storage
        assert purged is True, purged
        row.refresh_from_db()
        assert row.purged_at is not None
        assert (row.original_name, row.sha256) == (
            attachments.REMOVED_NAME,
            attachments.ERASED_SHA256,
        )
        assert not object_exists(row)

    def test_a_second_purge_of_a_file_being_purged_skips_it_at_once(self):
        """Review P3-4: an outbox retry or the housekeeping meeting a purge in progress waited
        for the file's row past the lock timeout and failed; it now skips the file at once
        (the purge in progress finishes it)."""
        owner = UserFactory()
        row = stored_file(NoteFactory(lead=LeadFactory(owner=owner), created_by=owner), owner)
        stop = Stop()
        with mock.patch.object(storage, "delete", stopping_before(storage.delete, stop)):
            first = Running(lambda: attachments.purge(row.pk))
            second: Running | None = None
            try:
                assert stop.reached.wait(WAIT_S), first.join()
                second = Running(lambda: attachments.purge(row.pk))
                assert second.ended_within(3.0), "the second purge waited for the first"
            finally:
                stop.go()
                done = first.join()
                skipped = second.join() if second is not None else None
        assert skipped is False, skipped
        assert done is True, done
        row.refresh_from_db()
        assert row.purged_at is not None


class TestCustomValues:
    def test_a_deletion_batch_holds_off_a_hold(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner)
        deal = OpportunityFactory(lead=lead)
        field = CustomField.objects.create(
            pipeline=deal.pipeline, name="Contact notes", field_type="text", position=0
        )
        Opportunity.objects.filter(pk=deal.pk).update(custom_fields={str(field.pk): "Call Ravi"})
        CustomField.objects.filter(pk=field.pk).update(is_active=False)
        stop = Stop()
        with mock.patch.object(holds, "held_leads", stopping_after(holds.held_leads, stop)):
            done = hold_placed_while_stopped(
                lambda: field_values.purge(field.pk),
                stop,
                placing(HoldSubject.LEAD, lead.pk, admin),
            )
        assert done == {"rows": 1, "held": 0}  # removed before the hold committed
        assert Opportunity.objects.get(pk=deal.pk).custom_fields == {}
        assert holds.is_held(HoldSubject.LEAD, lead.pk)


# --- audit details and support reasons -------------------------------------------------------
class TestAuditTrail:
    def test_an_expiry_batch_holds_off_a_hold_on_the_events_user(self):
        user, admin = UserFactory(), AdminFactory()
        token = bind_context(ExecutionContext(correlation_id="req-1", client_ip="203.0.113.7"))
        try:
            event = audit.record(
                "auth.login", actor_id=user.pk, target_type="user", target_id=user.pk
            )
        finally:
            reset_context(token)
        AuditDetail.objects.filter(pk=event.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        stop = Stop()
        with mock.patch.object(retention, "_held", stopping_after(retention._held, stop)):
            done = hold_placed_while_stopped(
                retention.expire_details, stop, placing(HoldSubject.USER, user.pk, admin)
            )
        assert done == {"details_expired": 1, "held": 0}  # expired before the hold committed
        assert not AuditDetail.objects.filter(pk=event.pk).exists()
        assert holds.is_held(HoldSubject.USER, user.pk)

    def test_blanking_old_support_reasons_holds_off_a_hold_on_the_user(self):
        user, admin = UserFactory(), AdminFactory()
        long_ago = timezone.now() - timedelta(days=91)
        SupportSession.objects.create(
            admin=admin,
            target=user,
            reason="Case 9",
            session_digest="0" * 64,
            started_at=long_ago,
            expires_at=long_ago + timedelta(hours=1),
            ended_at=long_ago + timedelta(hours=1),
            end_reason="exited",
        )
        stop = Stop()
        with mock.patch.object(holds, "held_users", stopping_after(holds.held_users, stop)):
            blanked = hold_placed_while_stopped(
                lambda: support.forget_reasons(timezone.now()),
                stop,
                placing(HoldSubject.USER, user.pk, admin),
            )
        assert blanked == 1  # blanked before the hold committed
        assert SupportSession.objects.get().reason == ""
        assert holds.is_held(HoldSubject.USER, user.pk)


# --- the legal_hold command -------------------------------------------------------------------
class TestPlacingCommand:
    def test_a_placement_kept_waiting_too_long_stops_cleanly_with_nothing_placed(self):
        """`legal_hold place` waits for the readers in flight (here one holding the barrier);
        past the lock timeout it says to run it again, and places nothing."""
        admin, lead = AdminFactory(), LeadFactory()
        stop = Stop()

        def reading() -> str:
            with transaction.atomic():
                holds.barrier()
                stop()
            return "read"

        reader = Running(reading)
        try:
            assert stop.reached.wait(WAIT_S)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '300ms'")
            try:
                with pytest.raises(CommandError, match="Run the command again"):
                    call_command(
                        "legal_hold",
                        "place",
                        "--lead",
                        str(lead.pk),
                        "--reference",
                        "CASE-13",
                        "--by",
                        admin.email,
                        stdout=io.StringIO(),
                    )
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("RESET lock_timeout")
        finally:
            stop.go()
            assert reader.join() == "read"
        assert not LegalHold.objects.exists()
        assert not AuditEvent.objects.filter(action="privacy.legal_hold_placed").exists()
        # Once nothing holds it up, the same command places it.
        call_command(
            "legal_hold",
            "place",
            "--lead",
            str(lead.pk),
            "--reference",
            "CASE-13",
            "--by",
            admin.email,
            stdout=io.StringIO(),
        )
        assert holds.is_held(HoldSubject.LEAD, lead.pk)

    def test_any_other_database_error_is_not_mistaken_for_waiting(self):
        admin, lead = AdminFactory(), LeadFactory()
        with (
            mock.patch.object(holds, "place", side_effect=OperationalError("connection lost")),
            pytest.raises(OperationalError),
        ):
            call_command(
                "legal_hold",
                "place",
                "--lead",
                str(lead.pk),
                "--reference",
                "CASE-14",
                "--by",
                admin.email,
                stdout=io.StringIO(),
            )


# --- the automatic reconciliation (privacy.reconcile -> privacy.replay) ----------------------
def entry(kind: str, subject: UUID) -> ledger.Entry:
    return ledger.Entry(seq=1, kind=kind, subject=str(subject), at="", prev="", mac="")


class TestAutomaticReplay:
    def test_a_hold_placed_since_the_reconciliation_looked_stops_it(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner, first_name="Kavita", last_name="Menon")
        stored_file(NoteFactory(lead=lead, created_by=owner), owner, deleted=False)
        former = UserFactory(first_name="Vikram", last_name="Joshi")
        identity_services.deactivate_user(actor_id=admin.pk, user_id=former.pk)
        for subject_type, subject_id in (
            (HoldSubject.LEAD, lead.pk),
            (HoldSubject.USER, former.pk),
        ):
            placing(subject_type, subject_id, admin)()
        # Without an operator, a held subject is left to one: refused in the entry's own
        # transaction, nothing changed.
        with pytest.raises(holds.UnderLegalHold):
            replay._lead(entry("lead_erased", lead.pk), None, False)
        with pytest.raises(holds.UnderLegalHold):
            replay._user(entry("user_pseudonymised", former.pk), None, False)
        assert Lead.objects.get(pk=lead.pk).first_name == "Kavita"
        assert not staff.pseudonymised(User.objects.get(pk=former.pk))
        # An operator's replay applies them whatever holds say (authorised when asked for).
        assert replay._lead(entry("lead_erased", lead.pk), admin, False) == replay.REAPPLIED
        assert (
            replay._user(entry("user_pseudonymised", former.pk), admin, False) == replay.REAPPLIED
        )
        # Still erased, with its file's object not purged yet (as a restored bucket leaves
        # it): the clean-up follows the same rule.
        with pytest.raises(holds.UnderLegalHold):
            replay._lead(entry("lead_erased", lead.pk), None, False)
        assert replay._lead(entry("lead_erased", lead.pk), admin, False) == replay.CLEANED

    def test_a_held_leads_deleted_file_is_left_to_an_operator(self):
        """Review P3-5: replaying `attachment_deleted` purged a held lead's file (its name
        and hash) without an operator. A file deleted, its entry written, its purge's commit
        lost: the reconciliation leaves it while the lead is held."""
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner)
        note = NoteFactory(lead=lead, created_by=owner)
        row = stored_file(note, owner)
        # And one purged by a release that kept names: the replay would mark it unpurged
        # again before purging it, so it is checked before anything changes.
        old = stored_file(note, owner)
        purged_at = timezone.now() - timedelta(days=3)
        Attachment.objects.filter(pk=old.pk).update(purged_at=purged_at)
        placing(HoldSubject.LEAD, lead.pk, admin)()
        for held in (row, old):
            with pytest.raises(holds.UnderLegalHold):
                replay._attachment(entry("attachment_deleted", held.pk), None, False)
        row.refresh_from_db()
        assert (row.purged_at, row.original_name) == (None, "Kavita_Menon_report.pdf")
        assert object_exists(row)
        old.refresh_from_db()
        assert (old.purged_at, old.original_name) == (purged_at, "Kavita_Menon_report.pdf")
        # An operator's replay applies it (authorised when the file was deleted).
        assert replay._attachment(entry("attachment_deleted", row.pk), admin, False) == (
            replay.REAPPLIED
        )
        row.refresh_from_db()
        assert row.original_name == attachments.REMOVED_NAME
        assert not object_exists(row)

    def test_a_hold_placed_before_the_replays_purge_still_stops_it(self):
        """Placed after the replay's first check, before its purge: the purge checks again
        under the lead's hold lock, so the file and its name stay."""
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner)
        row = stored_file(NoteFactory(lead=lead, created_by=owner), owner)
        purge = attachments.purge

        def placed_then_purged(*args, **kwargs):
            placing(HoldSubject.LEAD, lead.pk, admin)()
            return purge(*args, **kwargs)

        with (
            mock.patch.object(attachments, "purge", placed_then_purged),
            pytest.raises(holds.UnderLegalHold),
        ):
            replay._attachment(entry("attachment_deleted", row.pk), None, False)
        row.refresh_from_db()
        assert (row.purged_at, row.original_name) == (None, "Kavita_Menon_report.pdf")
        assert object_exists(row)

    def test_the_replays_purge_holds_off_a_hold_on_the_files_lead(self):
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner)
        row = stored_file(NoteFactory(lead=lead, created_by=owner), owner)
        stop = Stop()
        with mock.patch.object(storage, "delete", stopping_before(storage.delete, stop)):
            outcome = hold_placed_while_stopped(
                lambda: replay._attachment(entry("attachment_deleted", row.pk), None, False),
                stop,
                placing(HoldSubject.LEAD, lead.pk, admin),
            )
        assert outcome == replay.REAPPLIED  # purged before the hold, which came after
        row.refresh_from_db()
        assert row.original_name == attachments.REMOVED_NAME


# --- lock order ------------------------------------------------------------------------------
def in_one_transaction(*steps: Callable[[], Any]) -> None:
    with transaction.atomic():
        for step in steps:
            step()


class TestLockOrder:
    def test_the_barrier_is_the_first_lock_of_its_transaction(self):
        admin, lead = AdminFactory(), LeadFactory()

        def place() -> None:
            holds.place(HoldSubject.LEAD, lead.pk, reference="CASE-1", placed_by=admin.pk)

        refused = [
            # a subject's lock first
            (lambda: holds.refuse_if_held(HoldSubject.LEAD, lead.pk), holds.barrier),
            # a row lock first
            (lambda: Lead.objects.select_for_update().get(pk=lead.pk), holds.barrier),
            # a write first
            (lambda: Lead.objects.filter(pk=lead.pk).update(updated_at=timezone.now()), place),
            # a reader of the holds can't place one (no upgrade)
            (holds.barrier, place),
        ]
        for steps in refused:
            with pytest.raises(holds.LockOrderError):
                in_one_transaction(*steps)
        assert not LegalHold.objects.exists()
        with transaction.atomic():
            with connection.cursor() as cursor:  # a setting is no lock
                cursor.execute("SET LOCAL statement_timeout = '20s'")
            holds.barrier()
            holds.barrier()  # taken again: already held
            holds.refuse_if_held(HoldSubject.LEAD, lead.pk)  # a subject's lock after it
        with transaction.atomic():
            holds.place(HoldSubject.LEAD, lead.pk, reference="CASE-1", placed_by=admin.pk)
            holds.barrier()  # the placement's exclusive barrier covers a read
        with transaction.atomic():  # inside a caller's transaction, the order is the caller's
            Lead.objects.select_for_update().get(pk=lead.pk)
            with transaction.atomic():
                holds.barrier()
        with pytest.raises(RuntimeError):  # outside a transaction it would end at once
            holds.barrier()

    def test_an_erasure_holding_the_leads_lock_never_deadlocks_with_a_placement_on_it(self):
        """The erasure stops while it holds the barrier and then its lead's lock (both
        SHARED); a placement on that lead waits for the barrier, holding nothing, and goes on
        once the erasure has committed. With the barrier taken after the lead's lock, the
        placement would hold the barrier and wait for the lead's lock while the erasure held
        that and waited for the barrier: a deadlock."""
        owner, admin = UserFactory(), AdminFactory()
        lead = LeadFactory(owner=owner)
        stop = Stop()
        with mock.patch.object(holds, "refuse_if_held", stopping_after(holds.refuse_if_held, stop)):
            erased = hold_placed_while_stopped(
                lambda: services.erase(lead.pk, operator_id=admin.pk),
                stop,
                placing(HoldSubject.LEAD, lead.pk, admin),
            )
        assert isinstance(erased, services.Erasure)
        assert Lead.objects.get(pk=lead.pk).erased_at is not None
        assert holds.is_held(HoldSubject.LEAD, lead.pk)  # placed after it, on what is left

    @pytest.mark.parametrize("round_", range(3))
    def test_readers_edits_and_placements_at_once_never_deadlock(self, round_):
        owner, admin = UserFactory(), AdminFactory()
        a = LeadFactory(owner=owner, first_name="Sunita", last_name="Rao")
        b = LeadFactory(owner=owner, first_name="Meera", last_name="Iyer")
        c = LeadFactory(owner=owner, first_name="Kavita", last_name="Menon")
        deal_a, deal_c = OpportunityFactory(lead=a), OpportunityFactory(lead=c)
        stored(owner, "Compare the quotes", cites(deal_a.pk, deal_c.pk))
        about_b = stored(owner, "What did Meera Iyer order?")
        stored(owner, "What did Sunita Rao order?", days_ago=40)
        field = CustomField.objects.create(
            pipeline=deal_a.pipeline, name="Contact notes", field_type="text", position=0
        )
        Opportunity.objects.filter(pk__in=[deal_a.pk, deal_c.pk]).update(
            custom_fields={str(field.pk): "Call back"}
        )
        CustomField.objects.filter(pk=field.pk).update(is_active=False)
        row = stored_file(NoteFactory(lead=b, created_by=owner), owner)
        own = AccessScope.own(owner.pk)
        results = run_concurrently(
            placing(HoldSubject.LEAD, a.pk, admin),
            placing(HoldSubject.LEAD, b.pk, admin),
            placing(HoldSubject.USER, owner.pk, admin),
            lambda: services.erase(c.pk, operator_id=admin.pk),
            lambda: service.forget_conversation(owner, own, about_b.conversation_id),
            lambda: field_values.purge(field.pk),
            service.housekeeping,
            lambda: attachments.purge(row.pk),
            lambda: lead_services.update_lead(
                actor=owner, scope=own, lead_id=a.pk, version=1, changes={"email": "s@x.example"}
            ),
        )
        for result in results:
            assert not isinstance(result, OperationalError), result  # deadlock, lock timeout
            assert "deadlock" not in str(result).lower(), result
        assert results[:3] == ["placed"] * 3, results[:3]
        allowed = (holds.UnderLegalHold, service.ConversationHeld)
        for result in results[3:]:
            assert not isinstance(result, BaseException) or isinstance(result, allowed), result
