"""A legal hold freezes the held lead's data in every edit, not only in the correction
(audit PRIV-2; core.holds; docs/privacy.md#legal-holds).

While a hold covers a lead, nothing an erasure would clear may change: the lead's personal
fields, its deals' customer copy, text and custom values, its activities' text, its notes'
files (409 `legal_hold`, nothing written, versions unchanged). What only adds to the record
or moves it on goes on: status, rating, stage moves, new notes, tasks and files, completing
work. Releasing the hold re-allows the edits. A hold placed while an edit is in flight is
serialised with it: the edit commits first and the hold waits, or the edit waits for the
hold and is refused.
"""

from __future__ import annotations

import threading
import time
import uuid
from urllib.parse import quote

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from arkray.activities import services as activity_services
from arkray.activities.models import Attachment
from arkray.audit.models import AuditEvent
from arkray.core import holds
from arkray.core.access import AccessScope
from arkray.core.models import HoldSubject, LegalHold, OutboxEvent
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import Opportunity
from tests.factories import (
    AdminFactory,
    LeadFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
    default_stage,
)
from tests.helpers import signed_in

API = "/api/v1/workspaces/me"
EMAIL = "ravi.kumar@hospital.example"
PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF\n"


def _own(user):
    return AccessScope.own(user.pk)


def hold(lead, admin):
    return LegalHold.objects.create(
        subject_type=HoldSubject.LEAD, subject_id=lead.pk, reference="CASE-42", placed_by=admin.pk
    )


def release(hold_row, admin):
    hold_row.released_at, hold_row.released_by = timezone.now(), admin.pk
    hold_row.save()


def refused(response, *fields):
    assert response.status_code == 409, response.content
    error = response.json()["error"]
    assert error["code"] == "legal_hold"
    if fields:
        assert error["details"] == {"fields": sorted(fields)}


@pytest.fixture
def held(db, admin, user_a):
    lead = LeadFactory(owner=user_a, first_name="Ravi", last_name="Kumar", email=EMAIL)
    deal = OpportunityFactory(
        lead=lead, customer_name="Ravi Kumar", account_name="Apollo Labs", description="Terms"
    )
    note = NoteFactory(lead=lead, opportunity=deal, created_by=user_a, description="Met Ravi.")
    task = TaskFactory(lead=lead, title="Call Ravi about the quote")
    return {"lead": lead, "deal": deal, "note": note, "task": task, "hold": hold(lead, admin)}


def unchanged_since(rows, events_before, outbox_before):
    for row in rows:
        version = row.version
        row.refresh_from_db()
        assert row.version == version
    assert AuditEvent.objects.count() == events_before
    assert OutboxEvent.objects.count() == outbox_before


# --- the API refuses, atomically ------------------------------------------------------------
@pytest.mark.django_db
class TestRefused:
    def test_the_lead_patch(self, user_a, held):
        lead = held["lead"]
        before = (AuditEvent.objects.count(), OutboxEvent.objects.count())
        response = signed_in(user_a).patch(
            f"{API}/leads/{lead.pk}",
            # one frozen field among allowed ones: the whole request is refused
            {"version": lead.version, "email": "new@x.example", "rating": "hot"},
            format="json",
        )
        refused(response, "email")
        unchanged_since([lead], *before)
        assert (lead.email, lead.rating) == (EMAIL, None)

    def test_clearing_filling_or_rewriting_a_field(self, user_a, held):
        lead = held["lead"]
        client = signed_in(user_a)
        for changes in (
            {"last_name": ""},
            {"description": "rewritten"},
            {"phone": "+91 98111 22333"},  # even an empty field stays as it was
        ):
            response = client.patch(
                f"{API}/leads/{lead.pk}", {"version": lead.version, **changes}, format="json"
            )
            refused(response, *changes)

    def test_the_opportunity_patch(self, user_a, held):
        deal = held["deal"]
        client = signed_in(user_a)
        before = (AuditEvent.objects.count(), OutboxEvent.objects.count())
        for changes in (
            {"customer_name": "Someone Else"},
            {"contact_email": "x@y.example"},
            {"description": ""},
            {"work_load": "40 tests a day"},
        ):
            response = client.patch(
                f"{API}/opportunities/{deal.pk}",
                {"version": deal.version, **changes},
                format="json",
            )
            refused(response, *changes)
        unchanged_since([deal], *before)
        assert (deal.customer_name, deal.title, deal.description) == (
            "Ravi Kumar",
            deal.title,
            "Terms",
        )

    def test_the_activity_patch(self, user_a, held):
        note, task = held["note"], held["task"]
        client = signed_in(user_a)
        before = (AuditEvent.objects.count(), OutboxEvent.objects.count())
        response = client.patch(
            f"{API}/activities/{note.pk}",
            {"version": note.version, "description": "Nothing happened."},
            format="json",
        )
        refused(response, "description")
        response = client.patch(
            f"{API}/activities/{task.pk}",
            {"version": task.version, "title": "Call someone"},
            format="json",
        )
        refused(response, "title")
        unchanged_since([note, task], *before)
        assert note.description == "Met Ravi."

    def test_deleting_a_notes_file(self, user_a, held):
        client = signed_in(user_a)
        note = held["note"]
        LegalHold.objects.filter(pk=held["hold"].pk).update(
            released_at=timezone.now(), released_by=held["hold"].placed_by
        )
        uploaded = client.post(
            f"{API}/activities/{note.pk}/attachments",
            PDF,
            content_type="application/octet-stream",
            HTTP_X_FILENAME=quote("quote.pdf", safe=""),
        )
        assert uploaded.status_code == 201, uploaded.content
        hold(held["lead"], AdminFactory())
        row = Attachment.objects.get(pk=uploaded.json()["id"])
        refused(client.delete(f"{API}/attachments/{row.pk}"))
        row.refresh_from_db()
        assert row.deleted_at is None
        assert not OutboxEvent.objects.filter(topic="activities.purge_attachment").exists()

    def test_the_correction_still_refuses(self, user_a, held):
        lead = held["lead"]
        response = signed_in(user_a).post(
            f"{API}/leads/{lead.pk}/correction",
            {"version": lead.version, "email": "new@x.example"},
            format="json",
        )
        refused(response)


# --- what only adds or moves on goes on ------------------------------------------------------
@pytest.mark.django_db
class TestAllowed:
    def test_status_rating_and_last_contact(self, user_a, held):
        lead = held["lead"]
        response = signed_in(user_a).patch(
            f"{API}/leads/{lead.pk}", {"version": lead.version, "rating": "warm"}, format="json"
        )
        assert response.status_code == 200, response.content
        lead.refresh_from_db()
        assert lead.rating == "warm"
        lead_services.change_status(
            actor=user_a,
            scope=_own(user_a),
            lead_id=lead.pk,
            version=lead.version,
            status="contacted",
        )
        lead.refresh_from_db()
        assert lead.status_id == "contacted"

    def test_deal_value_and_stage_move(self, user_a, held):
        deal = held["deal"]
        client = signed_in(user_a)
        response = client.patch(
            f"{API}/opportunities/{deal.pk}",
            {"version": deal.version, "value": "250000"},
            format="json",
        )
        assert response.status_code == 200, response.content
        deal.refresh_from_db()
        moved = pipeline_services.move_opportunity(
            actor=user_a,
            scope=_own(user_a),
            opportunity_id=deal.pk,
            version=deal.version,
            stage_id=default_stage("qualified").pk,
        )
        assert moved.stage.key == "qualified"

    def test_new_work_and_files_and_completing_a_task(self, user_a, held):
        client = signed_in(user_a)
        lead, task = held["lead"], held["task"]
        note = client.post(
            f"{API}/activities",
            {"type": "note", "lead": str(lead.pk), "description": "A new note."},
            format="json",
        )
        assert note.status_code == 201, note.content
        uploaded = client.post(
            f"{API}/activities/{note.json()['id']}/attachments",
            PDF,
            content_type="application/octet-stream",
            HTTP_X_FILENAME=quote("new.pdf", safe=""),
        )
        assert uploaded.status_code == 201, uploaded.content
        response = client.patch(
            f"{API}/activities/{task.pk}",
            {"version": task.version, "priority": "high"},
            format="json",
        )
        assert response.status_code == 200, response.content
        done = client.post(
            f"{API}/activities/{task.pk}/complete",
            {"version": response.json()["version"]},
            format="json",
        )
        assert done.status_code == 200, done.content

    def test_another_leads_records(self, user_a, held):
        other = LeadFactory(owner=user_a)
        response = signed_in(user_a).patch(
            f"{API}/leads/{other.pk}",
            {"version": other.version, "email": "a@b.example"},
            format="json",
        )
        assert response.status_code == 200, response.content

    def test_releasing_the_hold_re_allows_every_edit(self, admin, user_a, held):
        release(held["hold"], admin)
        client = signed_in(user_a)
        lead, deal, note = held["lead"], held["deal"], held["note"]
        assert (
            client.patch(
                f"{API}/leads/{lead.pk}",
                {"version": lead.version, "email": "new@x.example"},
                format="json",
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"{API}/opportunities/{deal.pk}",
                {"version": deal.version, "customer_name": "Ravi K."},
                format="json",
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"{API}/activities/{note.pk}",
                {"version": note.version, "description": "Met Ravi K."},
                format="json",
            ).status_code
            == 200
        )
        lead.refresh_from_db()
        assert lead.email == "new@x.example"


# --- the services refuse whatever the caller -------------------------------------------------
@pytest.mark.django_db
def test_the_services_refuse_directly(user_a, held):
    scope = _own(user_a)
    lead, deal, note = held["lead"], held["deal"], held["note"]
    with pytest.raises(holds.UnderLegalHold):
        lead_services.update_lead(
            actor=user_a,
            scope=scope,
            lead_id=lead.pk,
            version=lead.version,
            changes={"city": "Pune"},
        )
    with pytest.raises(holds.UnderLegalHold):
        pipeline_services.update_opportunity(
            actor=user_a,
            scope=scope,
            opportunity_id=deal.pk,
            version=deal.version,
            changes={"address": "12 New Road"},
        )
    with pytest.raises(holds.UnderLegalHold):
        activity_services.update_activity(
            actor=user_a,
            scope=scope,
            activity_id=note.pk,
            version=note.version,
            changes={"description": "x"},
        )


@pytest.mark.django_db(transaction=True)
def test_checks_outside_a_transaction_are_refused():
    # A transaction-scoped lock taken in autocommit would end at once: a programming error.
    with pytest.raises(RuntimeError):
        holds.refuse_if_held(HoldSubject.LEAD, uuid.uuid4())
    with pytest.raises(RuntimeError):
        holds.place(HoldSubject.LEAD, uuid.uuid4(), reference="CASE-1", placed_by=uuid.uuid4())
    assert not LegalHold.objects.exists()


# --- concurrent hold placement ---------------------------------------------------------------
def _waiting_for_hold_lock(timeout=10.0):
    """Wait until some session waits for a legal-hold advisory lock ("ARKH")."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                " AND classid = %s",
                [holds._LOCK_NAMESPACE],
            )
            if cursor.fetchone()[0]:
                return True
        time.sleep(0.05)
    return False


def _in_thread(target, results, key):
    def run():
        try:
            results[key] = target()
        except BaseException as exc:
            results[key] = exc
        finally:
            connections.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
class TestConcurrentPlacement:
    def setup_people(self):
        admin, owner = AdminFactory(), UserFactory()
        lead = LeadFactory(owner=owner, first_name="Ravi", last_name="Kumar", email=EMAIL)
        return admin, owner, lead

    def edit(self, owner, lead):
        return lead_services.update_lead(
            actor=owner,
            scope=_own(owner),
            lead_id=lead.pk,
            version=lead.version,
            changes={"email": "new@x.example"},
        )

    def test_an_edit_arriving_while_a_hold_is_placed_waits_and_is_refused(self):
        admin, owner, lead = self.setup_people()
        placed, commit = threading.Event(), threading.Event()
        results: dict[str, object] = {}

        def place():
            with transaction.atomic():
                holds.place(HoldSubject.LEAD, lead.pk, reference="CASE-9", placed_by=admin.pk)
                placed.set()
                assert commit.wait(15)
            return "placed"

        placer = _in_thread(place, results, "place")
        assert placed.wait(10)
        editor = _in_thread(lambda: self.edit(owner, lead), results, "edit")
        try:
            # Without the lock the edit wouldn't wait: it would see no committed hold.
            assert _waiting_for_hold_lock(), "the edit didn't wait for the hold"
        finally:
            commit.set()
            placer.join(20)
            editor.join(20)
        assert results["place"] == "placed"
        assert isinstance(results["edit"], holds.UnderLegalHold), results["edit"]
        fresh = Lead.objects.get(pk=lead.pk)
        assert (fresh.email, fresh.version) == (EMAIL, lead.version)

    def test_a_hold_placed_during_an_edit_waits_for_it_to_commit(self):
        admin, owner, lead = self.setup_people()
        edited, commit = threading.Event(), threading.Event()
        results: dict[str, object] = {}

        def edit():
            with transaction.atomic():
                self.edit(owner, lead)
                edited.set()
                assert commit.wait(15)
            return "edited"

        def place():
            with transaction.atomic():
                holds.place(HoldSubject.LEAD, lead.pk, reference="CASE-9", placed_by=admin.pk)
            return time.monotonic()

        editor = _in_thread(edit, results, "edit")
        assert edited.wait(10)
        placer = _in_thread(place, results, "place")
        try:
            assert _waiting_for_hold_lock(), "the hold didn't wait for the edit"
            committed_at = time.monotonic()
        finally:
            commit.set()
            editor.join(20)
            placer.join(20)
        assert results["edit"] == "edited"
        placed_at = results["place"]
        assert isinstance(placed_at, float), placed_at
        assert placed_at >= committed_at
        fresh = Lead.objects.get(pk=lead.pk)
        assert (fresh.email, fresh.version) == ("new@x.example", lead.version + 1)
        assert holds.is_held(HoldSubject.LEAD, lead.pk)
        with pytest.raises(holds.UnderLegalHold):
            lead_services.update_lead(
                actor=owner,
                scope=_own(owner),
                lead_id=lead.pk,
                version=fresh.version,
                changes={"email": "third@x.example"},
            )

    def test_a_deal_edit_and_a_hold_placed_at_once_never_both_win(self):
        admin, owner, lead = self.setup_people()
        deal = OpportunityFactory(lead=lead, customer_name="Ravi Kumar", account_name="Apollo")
        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def edit():
            barrier.wait(10)
            return pipeline_services.update_opportunity(
                actor=owner,
                scope=_own(owner),
                opportunity_id=deal.pk,
                version=deal.version,
                changes={"customer_name": "Someone Else"},
            )

        def place():
            barrier.wait(10)
            with transaction.atomic():
                holds.place(HoldSubject.LEAD, lead.pk, reference="CASE-9", placed_by=admin.pk)
            return "placed"

        threads = [_in_thread(edit, results, "edit"), _in_thread(place, results, "place")]
        for thread in threads:
            thread.join(20)
        assert results["place"] == "placed"
        fresh = Opportunity.objects.get(pk=deal.pk)
        if isinstance(results["edit"], holds.UnderLegalHold):  # the hold went first
            assert (fresh.customer_name, fresh.version) == ("Ravi Kumar", deal.version)
        else:  # the edit went first and committed before the hold
            assert isinstance(results["edit"], Opportunity), results["edit"]
            assert fresh.customer_name == "Someone Else"
