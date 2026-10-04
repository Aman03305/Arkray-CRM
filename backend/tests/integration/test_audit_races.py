"""Races the whole-software audit found, reproduced with real threads and their own
PostgreSQL connections (each was forced with a hook that holds one side at the worst moment).

- An erasure and Ask Arkray: a routed answer composed during the erasure kept the erased
  person's name and task titles (P2).
- An erasure and the indexer deadlocked; whichever lost was rolled back (P3).
- A question added to a conversation the erasure was deleting aborted the whole erasure at
  commit (P3).
- An archive while a note's first indexing was still in flight left its chunks behind (P3).
- A lead created in one's own workspace while being deactivated committed afterwards,
  owned by a deactivated user (P3).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from django.db import connections

from arkray.activities import services as activity_services
from arkray.ai import indexing
from arkray.ai import service as ai_service
from arkray.ai.models import KnowledgeChunk, Question, QuestionStatus
from arkray.ai.sources import SourceModule
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.core.models import OutboxEvent
from arkray.identity import services as identity_services
from arkray.leads import services as lead_services
from arkray.leads.events import LeadCreated
from arkray.leads.models import Lead
from arkray.privacy import services as privacy
from tests.ai_fixtures import index, note
from tests.factories import AdminFactory, LeadFactory, TaskFactory, UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration", "ai_on"),
]


def in_thread(target: Callable[[], Any], results: dict[str, Any], key: str) -> threading.Thread:
    def run() -> None:
        try:
            results[key] = target()
        except BaseException as exc:
            results[key] = exc
        finally:
            connections.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_a_routed_question_during_an_erasure_waits_and_quotes_nothing_erased(monkeypatch):
    owner, admin = UserFactory(), AdminFactory()
    lead = LeadFactory(owner=owner, first_name="Periwinkle", last_name="Rao")
    TaskFactory(lead=lead, owner=owner, title="Call Periwinkle Rao about the Zeta quote")
    paused, asked = threading.Event(), threading.Event()
    enqueue_lead = privacy.indexing.enqueue_lead

    def held_open(lead_id: Any) -> None:  # inside the erasure's transaction, near its end
        paused.set()
        asked.wait(10)
        enqueue_lead(lead_id)

    monkeypatch.setattr(privacy.indexing, "enqueue_lead", held_open)
    results: dict[str, Any] = {}

    def ask() -> Any:
        paused.wait(10)
        try:
            return ai_service.submit(
                owner, AccessScope.own(owner.pk), "What are my open tasks?", None
            )
        finally:
            asked.set()

    threads = [
        in_thread(lambda: privacy.erase(lead.pk, operator_id=admin.pk), results, "erase"),
        in_thread(ask, results, "ask"),
    ]
    for thread in threads:
        thread.join(60)
    assert not isinstance(results["erase"], BaseException), results["erase"]
    question = Question.objects.get(pk=results["ask"].pk)
    # Not answered from the pre-erasure data: queued while the erasure held its lock...
    assert question.status == QuestionStatus.PENDING
    # ...and answered by the worker afterwards, the router's answer from what is left.
    ai_service.answer(question.pk)
    question.refresh_from_db()
    assert (question.status, question.mode) == (QuestionStatus.ANSWERED, "router")
    stored = json.dumps(question.answer)
    assert "Periwinkle" not in stored
    assert "Zeta" not in stored


@pytest.mark.parametrize("scan_seconds", [0.2, 2.0])
def test_the_erasure_and_the_indexer_do_not_deadlock(monkeypatch, scan_seconds):
    """The indexer replaced a note's chunks (locking the old rows) and at commit needed the
    lead (key share) that the erasure held; the erasure then wanted those chunks."""
    owner, admin = UserFactory(), AdminFactory()
    lead = LeadFactory(owner=owner, first_name="Periwinkle", last_name="Rao")
    written = note(owner, lead, "Periwinkle wants a quote for the analyser")
    index()
    activity_services.update_activity(
        actor=owner,
        scope=AccessScope.own(owner.pk),
        activity_id=written.pk,
        version=1,
        changes={"description": "Periwinkle asked again: call after 5pm"},
    )
    lead_locked, chunks_written = threading.Event(), threading.Event()
    scan = privacy._touching_conversations

    def slow_scan(records: Any, terms: Any) -> Any:  # the erasure holds the lead now
        lead_locked.set()
        chunks_written.wait(3)
        time.sleep(scan_seconds)
        return scan(records, terms)

    monkeypatch.setattr(privacy, "_touching_conversations", slow_scan)
    manager = KnowledgeChunk.objects
    bulk_create = manager.bulk_create

    def writing(*args: Any, **kwargs: Any) -> Any:
        created = bulk_create(*args, **kwargs)
        chunks_written.set()
        return created

    monkeypatch.setattr(manager, "bulk_create", writing)
    results: dict[str, Any] = {}

    def reindex() -> Any:
        lead_locked.wait(10)
        return indexing.index_source(SourceModule.ACTIVITY, written.pk)

    threads = [
        in_thread(lambda: privacy.erase(lead.pk, operator_id=admin.pk), results, "erase"),
        in_thread(reindex, results, "index"),
    ]
    for thread in threads:
        thread.join(60)
    # The erasure always completes. The indexer waited for the lead before touching any
    # chunk, so it either finished after the erasure or (past its lock timeout) failed
    # cleanly to be retried by the outbox: never a deadlock.
    assert not isinstance(results["erase"], BaseException), results["erase"]
    assert "deadlock" not in str(results["index"]).lower(), results["index"]
    lead.refresh_from_db()
    assert lead.first_name == privacy.ERASED


def test_continuing_a_conversation_during_the_erasure_does_not_abort_it(monkeypatch):
    owner, admin = UserFactory(), AdminFactory()
    lead = LeadFactory(owner=owner, first_name="Periwinkle", last_name="Rao")
    TaskFactory(lead=lead, owner=owner, title="Call Periwinkle Rao")
    first = ai_service.submit(owner, AccessScope.own(owner.pk), "What are my open tasks?", None)
    assert "Periwinkle" in json.dumps(first.answer)
    scanned, conversation_locked = threading.Event(), threading.Event()
    scan = privacy._touching_conversations

    def after_scan(records: Any, terms: Any) -> Any:
        found = scan(records, terms)
        scanned.set()
        conversation_locked.wait(10)
        time.sleep(0.3)
        return found

    monkeypatch.setattr(privacy, "_touching_conversations", after_scan)
    route = ai_service.router.route

    def slow_route(text: str, **kwargs: Any) -> Any:  # submit holds the conversation now
        conversation_locked.set()
        time.sleep(1.0)
        return route(text, **kwargs)

    monkeypatch.setattr(ai_service.router, "route", slow_route)
    results: dict[str, Any] = {}

    def follow_up() -> Any:
        scanned.wait(10)
        return ai_service.submit(
            owner, AccessScope.own(owner.pk), "Any others?", first.conversation_id
        )

    threads = [
        in_thread(lambda: privacy.erase(lead.pk, operator_id=admin.pk), results, "erase"),
        in_thread(follow_up, results, "ask"),
    ]
    for thread in threads:
        thread.join(60)
    assert not isinstance(results["erase"], BaseException), results["erase"]
    lead.refresh_from_db()
    assert lead.first_name == privacy.ERASED
    assert not Question.objects.filter(conversation_id=first.conversation_id).exists()


def test_an_archive_during_a_first_indexing_leaves_no_chunks(monkeypatch):
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    written = note(owner, lead, "Customer asked about the periwinkle analyser")
    OutboxEvent.objects.all().delete()  # the first indexing job has been claimed
    wrote, release = threading.Event(), threading.Event()
    manager = KnowledgeChunk.objects
    bulk_create = manager.bulk_create

    def held(*args: Any, **kwargs: Any) -> Any:  # still inside the first job's transaction
        created = bulk_create(*args, **kwargs)
        wrote.set()
        release.wait(10)
        return created

    monkeypatch.setattr(manager, "bulk_create", held)
    results: dict[str, Any] = {}
    first = in_thread(
        lambda: indexing.index_source(SourceModule.ACTIVITY, written.pk), results, "first"
    )
    assert wrote.wait(10)
    monkeypatch.setattr(manager, "bulk_create", bulk_create)
    activity_services.archive_activity(
        actor=owner, scope=AccessScope.own(owner.pk), activity_id=written.pk, version=1
    )
    # The archive's own job: it waits for the first job's lock, then sees its chunks.
    archived = in_thread(
        lambda: indexing.index_source(SourceModule.ACTIVITY, written.pk), results, "archive"
    )
    time.sleep(0.5)
    release.set()
    first.join(30)
    archived.join(30)
    assert not isinstance(results["archive"], BaseException), results["archive"]
    assert results["archive"] == indexing.Outcome.REMOVED
    assert not KnowledgeChunk.objects.filter(source_id=written.pk).exists()


def test_a_lead_created_while_its_owner_is_deactivated_is_refused_or_waited_for():
    user, admin = UserFactory(), AdminFactory()
    inside, deactivating = threading.Event(), threading.Event()

    def hold(_event: Any) -> None:  # the lead is written, its transaction still open
        inside.set()
        deactivating.wait(1)
        time.sleep(0.2)

    results: dict[str, Any] = {}

    def create() -> Any:
        with subscribed(LeadCreated, hold):
            return lead_services.create_lead(
                actor=user, scope=AccessScope.own(user.pk), fields={"first_name": "Late"}
            ).lead.pk

    def deactivate() -> Any:
        inside.wait(10)
        deactivating.set()
        started = time.monotonic()
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
        return time.monotonic() - started

    threads = [in_thread(create, results, "create"), in_thread(deactivate, results, "deactivate")]
    for thread in threads:
        thread.join(30)
    assert not isinstance(results["deactivate"], BaseException), results["deactivate"]
    # The lead's share lock on its owner made the deactivation wait for it to commit.
    assert results["deactivate"] >= 0.2
    assert Lead.objects.filter(pk=results["create"], owner=user).exists()
