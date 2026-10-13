"""Re-syncing a large lead in bounded jobs (review of 73ff1e3, M6; docs/rag-architecture.md).

`ai.index_lead` (after a reassignment or an erasure) re-synced every source indexed under the
lead in one job: on a customer with thousands of notes, one job held the single indexing worker
for minutes while everyone else's new notes waited. Now each job handles at most
`indexing.LEAD_BATCH` sources and queues a continuation from where it stopped, which
re-enters the fair queue (core.outbox) like any other work: someone else's note is indexed
between two batches. A continuation is dropped when a fresh re-sync of the same lead is
already waiting (it would redo the whole lead anyway), and a job queued by the previous
release (no cursor) starts from the beginning.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.utils import timezone

from arkray.ai import indexing
from arkray.ai.models import KnowledgeChunk
from arkray.ai.sources import SourceModule
from arkray.core import outbox
from arkray.core.access import AccessScope
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.models import OutboxEvent, OutboxStatus
from arkray.leads import services as lead_services
from tests.ai_fixtures import index, note
from tests.factories import LeadFactory, NoteFactory

pytestmark = pytest.mark.django_db

NOTES = 25
BATCH = 10


@pytest.fixture(autouse=True)
def small_batches(monkeypatch):
    monkeypatch.setattr(indexing, "LEAD_BATCH", BATCH, raising=False)


@contextmanager
def acting_as(user: Any) -> Iterator[None]:
    token = bind_context(ExecutionContext(correlation_id="req-1", user_id=str(user.pk)))
    try:
        yield
    finally:
        reset_context(token)


def large_lead(owner: Any) -> tuple[Any, list[UUID]]:
    """A lead with NOTES notes, all indexed (written directly: no outbox work left over)."""
    lead: Any = LeadFactory(owner=owner, description="Chain of diagnostic labs in Pune.")
    notes: list[Any] = [
        NoteFactory(lead=lead, description=f"Visit {n}: reagent supply.") for n in range(NOTES)
    ]
    indexing.index_source(SourceModule.LEAD, lead.pk)
    indexing.index_sources(SourceModule.ACTIVITY, [n.pk for n in notes])
    return lead, [lead.pk, *(n.pk for n in notes)]


def reassign(admin: Any, lead: Any, to: Any) -> None:
    lead.refresh_from_db()
    with acting_as(admin):
        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=to.pk,
        )


def owners(sources: list[UUID]) -> dict[UUID, set[UUID]]:
    found: dict[UUID, set[UUID]] = {}
    for source_id, owner_id in KnowledgeChunk.objects.filter(source_id__in=sources).values_list(
        "source_id", "owner_id"
    ):
        found.setdefault(source_id, set()).add(owner_id)
    return found


def lead_jobs(status: str = OutboxStatus.PENDING) -> list[OutboxEvent]:
    return list(
        OutboxEvent.objects.filter(topic=indexing.TOPIC_INDEX_LEAD, status=status).order_by("id")
    )


def run_one(event: OutboxEvent) -> None:
    """Claim and run exactly this event, as a worker would, while the rest keep waiting."""
    token = uuid4()
    OutboxEvent.objects.filter(pk=event.pk, status=OutboxStatus.PENDING).update(
        status=OutboxStatus.IN_FLIGHT,
        claim_token=token,
        locked_until=timezone.now() + timedelta(minutes=5),
    )
    outbox.process_event(event.pk, token)


class TestBoundedJobs:
    def test_one_job_handles_one_batch_and_queues_the_rest(self, admin, user_a, user_b):
        lead, sources = large_lead(user_a)
        reassign(admin, lead, user_b)
        (first,) = lead_jobs()
        run_one(first)
        moved = [s for s, found in owners(sources).items() if found == {user_b.pk}]
        assert 0 < len(moved) <= BATCH  # was all 26 in one job
        (continuation,) = lead_jobs()
        assert continuation.payload["lead_id"] == str(lead.pk)
        assert continuation.payload["after"]  # the cursor: where the next batch starts
        assert continuation.fairness_key == str(admin.pk)  # still the admin's work
        index()
        assert all(found == {user_b.pk} for found in owners(sources).values())
        done = lead_jobs(OutboxStatus.DONE)
        assert len(done) == -(-len(sources) // BATCH)  # ceil(26 / 10) = 3 jobs, no more

    def test_a_small_lead_is_one_job(self, admin, user_a, user_b):
        lead = LeadFactory(owner=user_a, description="One lab.")
        indexing.index_source(SourceModule.LEAD, lead.pk)
        reassign(admin, lead, user_b)
        index()
        assert len(lead_jobs(OutboxStatus.DONE)) == 1

    def test_a_job_queued_by_the_previous_release_starts_from_the_beginning(
        self, admin, user_a, user_b
    ):
        lead, sources = large_lead(user_a)
        reassign(admin, lead, user_b)
        (queued,) = lead_jobs()
        OutboxEvent.objects.filter(pk=queued.pk).update(payload={"lead_id": str(lead.pk)})
        index()
        assert all(found == {user_b.pk} for found in owners(sources).values())


class TestFairness:
    def test_another_users_note_is_indexed_between_two_batches(
        self, monkeypatch, admin, user_a, user_b
    ):
        lead, sources = large_lead(user_a)
        reassign(admin, lead, user_b)
        other_lead = LeadFactory(owner=user_a)
        with acting_as(user_a):
            mine = note(user_a, other_lead, "Dr Mehta wants the HbA1c analyser demo.")
        synced: list[UUID] = []
        real_sync = indexing.sync

        def recording(module, source_id, document):
            synced.append(source_id)
            return real_sync(module, source_id, document)

        monkeypatch.setattr(indexing, "sync", recording)
        index()
        # First-come, first-served ran all 26 of the lead's sources first; now at most one
        # batch of them goes before the note.
        assert synced.index(mine.pk) <= BATCH
        assert set(sources) <= set(synced)


class TestCoalescing:
    def test_a_waiting_fresh_resync_absorbs_the_chain_in_progress(self, admin, user_a, user_b):
        lead, sources = large_lead(user_a)
        reassign(admin, lead, user_b)
        (first,) = lead_jobs()
        run_one(first)
        (continuation,) = lead_jobs()
        reassign(admin, lead, user_a)  # back again: a fresh re-sync of the whole lead
        fresh = [job for job in lead_jobs() if job.pk != continuation.pk]
        assert len(fresh) == 1
        assert "after" not in fresh[0].payload
        run_one(continuation)
        # Its next batch is left to the fresh re-sync, which redoes the lead from the start.
        assert [job.pk for job in lead_jobs()] == [fresh[0].pk]
        index()
        assert all(found == {user_a.pk} for found in owners(sources).values())
        assert len(lead_jobs(OutboxStatus.DONE)) == 2 + -(-len(sources) // BATCH)

    def test_a_duplicate_delivery_queues_one_continuation(self, admin, user_a, user_b):
        lead, _ = large_lead(user_a)
        reassign(admin, lead, user_b)
        (first,) = lead_jobs()
        run_one(first)  # queues the continuation
        indexing.continue_lead(lead.pk, None)  # the same first batch again (at-least-once)
        assert len(lead_jobs()) == 1  # coalesced into the waiting continuation
