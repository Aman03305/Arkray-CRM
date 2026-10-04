"""The indexing pipeline: CRM change -> domain event -> outbox -> worker -> chunks.

Indexing is asynchronous, idempotent, derived (rebuildable from the CRM) and follows every
lifecycle change: create, edit, archive, restore, reassignment, opportunity outcomes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.db import connection

from arkray.activities import services as activity_services
from arkray.ai import indexing
from arkray.ai.embeddings import EmbeddingUnavailable, HashingEmbedder
from arkray.ai.indexing import Outcome
from arkray.ai.models import KnowledgeChunk
from arkray.ai.sources import SourceModule
from arkray.core.access import AccessScope
from arkray.core.knowledge import SourceType
from arkray.core.models import OutboxEvent, OutboxStatus
from arkray.leads import services as lead_services
from arkray.pipeline import services as pipeline_services
from tests.ai_fixtures import described_lead, index, note, own
from tests.factories import LeadFactory, MeetingFactory, TaskFactory, default_stage

pytestmark = pytest.mark.django_db

AI_TOPICS = (indexing.TOPIC_INDEX_SOURCE, indexing.TOPIC_INDEX_LEAD)


class CountingEmbedder(HashingEmbedder):
    model_name = "hashing-v1"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingUnavailable("down")
        self.calls += len(texts)
        return super().embed_documents(texts)


@pytest.fixture
def embedder(monkeypatch) -> CountingEmbedder:
    counting = CountingEmbedder()
    monkeypatch.setattr("arkray.ai.indexing.get_embedder", lambda: counting)
    monkeypatch.setattr("arkray.ai.retrieval.get_embedder", lambda: counting)
    return counting


def chunks_of(source_id: Any) -> list[KnowledgeChunk]:
    return list(KnowledgeChunk.objects.filter(source_id=source_id).order_by("chunk_index"))


def ai_events() -> list[OutboxEvent]:
    return list(OutboxEvent.objects.filter(topic__in=AI_TOPICS).order_by("id"))


class TestFromCrmChangeToChunks:
    def test_a_new_note_is_indexed_through_the_outbox_not_inline(self, user_a, embedder):
        lead = LeadFactory(owner=user_a)
        created = note(user_a, lead, "Discussed the HbA1c analyser pricing with Dr Mehta.")
        assert chunks_of(created.pk) == []  # nothing inline: the CRM write didn't wait
        (event,) = ai_events()
        assert event.payload == {"source": "activity", "id": str(created.pk)}
        assert event.queue == "ai_index"
        index()
        (chunk,) = chunks_of(created.pk)
        assert chunk.source_type == SourceType.NOTE
        assert (chunk.owner_id, chunk.lead_id) == (user_a.pk, lead.pk)
        assert chunk.char_start == 0
        assert chunk.embedding_model == "hashing-v1"

    def test_the_index_stores_no_text(self):
        columns = {
            c.name
            for c in connection.introspection.get_table_description(
                connection.cursor(), "ai_knowledge_chunk"
            )
        }
        assert not columns & {"text", "content", "body", "chunk_text", "description"}

    def test_nothing_is_queued_while_indexing_is_off(self, settings, user_a):
        settings.AI_INDEXING_ENABLED = False
        note(user_a, LeadFactory(owner=user_a), "Some note")
        assert ai_events() == []

    def test_unembedded_fields_do_not_queue_work(self, user_a):
        lead = described_lead(user_a, "Large hospital chain.")
        index()
        lead_services.update_lead(
            actor=user_a,
            scope=own(user_a),
            lead_id=lead.pk,
            version=lead.version,
            changes={"phone": "+91 98765 43210"},
        )
        assert [e for e in ai_events() if e.status == OutboxStatus.PENDING] == []


class TestIdempotency:
    def test_duplicate_delivery_leaves_one_set_and_embeds_once(self, user_a, embedder):
        created = note(user_a, LeadFactory(owner=user_a), "Asked for a demo next month.")
        assert indexing.index_source(SourceModule.ACTIVITY, created.pk) == Outcome.EMBEDDED
        first = [(c.chunk_index, c.source_hash) for c in chunks_of(created.pk)]
        assert indexing.index_source(SourceModule.ACTIVITY, created.pk) == Outcome.UNCHANGED
        assert indexing.index_source(SourceModule.ACTIVITY, created.pk) == Outcome.UNCHANGED
        assert [(c.chunk_index, c.source_hash) for c in chunks_of(created.pk)] == first
        assert embedder.calls == 1

    def test_a_long_note_is_several_chunks_replaced_together_on_edit(self, user_a, embedder):
        lead = LeadFactory(owner=user_a)
        created = note(user_a, lead, "Pricing discussion. " * 200)
        index()
        assert len(chunks_of(created.pk)) > 2
        activity_services.update_activity(
            actor=user_a,
            scope=own(user_a),
            activity_id=created.pk,
            version=created.version,
            changes={"description": "Short now."},
        )
        index()
        (only,) = chunks_of(created.pk)
        assert (only.char_start, only.char_end) == (0, len("Text: Short now."))


class TestLifecycle:
    def test_edit_re_embeds_with_the_new_hash(self, user_a, embedder):
        created = note(user_a, LeadFactory(owner=user_a), "Original wording.")
        index()
        before = chunks_of(created.pk)[0].source_hash
        activity_services.update_activity(
            actor=user_a,
            scope=own(user_a),
            activity_id=created.pk,
            version=created.version,
            changes={"description": "Changed wording about the tender."},
        )
        index()
        assert chunks_of(created.pk)[0].source_hash != before

    def test_archive_removes_and_restore_brings_back(self, user_a, embedder):
        created = note(user_a, LeadFactory(owner=user_a), "Archivable note.")
        index()
        archived = activity_services.archive_activity(
            actor=user_a, scope=own(user_a), activity_id=created.pk, version=created.version
        )
        index()
        assert chunks_of(created.pk) == []
        activity_services.restore_activity(
            actor=user_a, scope=own(user_a), activity_id=created.pk, version=archived.version
        )
        index()
        assert len(chunks_of(created.pk)) == 1

    def test_reassignment_moves_chunks_without_re_embedding(self, admin, user_a, user_b, embedder):
        lead = described_lead(user_a, "Chain of diagnostic labs in Pune.")
        first = note(user_a, lead, "First note.")
        second = note(user_a, lead, "Second note.")
        index()
        embedded = embedder.calls
        lead.refresh_from_db()
        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        pending = [e for e in ai_events() if e.status == OutboxStatus.PENDING]
        assert [e.topic for e in pending] == [indexing.TOPIC_INDEX_LEAD]  # one, not one per row
        index()
        for source in (lead.pk, first.pk, second.pk):
            assert {c.owner_id for c in chunks_of(source)} == {user_b.pk}
        assert embedder.calls == embedded  # metadata only

    def test_completed_meetings_keep_their_owner_after_reassignment(
        self, admin, user_a, user_b, embedder
    ):
        lead = LeadFactory(owner=user_a)
        done = MeetingFactory(lead=lead, status="completed", description="Agreed on a trial.")
        indexing.index_source(SourceModule.ACTIVITY, done.pk)
        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        index()
        assert {c.owner_id for c in chunks_of(done.pk)} == {user_a.pk}

    def test_an_opportunity_is_indexed_with_its_lost_reason(self, user_a, embedder):
        lead = LeadFactory(owner=user_a)
        created = pipeline_services.create_opportunity(
            actor=user_a,
            scope=own(user_a),
            lead_id=lead.pk,
            fields={
                "title": "Analyzer upgrade",
                "value": Decimal("100000"),
                "description": "Replace two old analyzers.",
            },
        ).opportunity
        index()
        assert len(chunks_of(created.pk)) == 1
        hash_open = chunks_of(created.pk)[0].source_hash
        pipeline_services.move_opportunity(
            actor=user_a,
            scope=own(user_a),
            opportunity_id=created.pk,
            version=created.version,
            stage_id=default_stage("lost").pk,
            lost_reason="Budget frozen until April.",
        )
        index()
        assert chunks_of(created.pk)[0].source_hash != hash_open

    def test_what_is_embedded_and_what_never_is(self, user_a):
        from arkray.activities import selectors as activity_selectors
        from arkray.leads import selectors as lead_selectors

        lead = LeadFactory(
            owner=user_a,
            email="secret@example.com",
            phone="+919876543210",
            description="Prefers morning calls.",
        )
        bare_task = TaskFactory(lead=lead, description="")
        described = MeetingFactory(
            lead=lead,
            description="Agenda: pricing.",
            location="12 MG Road",
            meeting_url="https://meet.example/abc?pwd=hunter2",
        )
        (lead_doc,) = lead_selectors.knowledge_documents_for_indexing([lead.pk])
        assert "secret@example.com" not in lead_doc.text
        assert "9876543210" not in lead_doc.text
        assert "Prefers morning calls." in lead_doc.text
        assert activity_selectors.knowledge_documents_for_indexing([bare_task.pk]) == []
        (meeting_doc,) = activity_selectors.knowledge_documents_for_indexing([described.pk])
        assert "hunter2" not in meeting_doc.text
        assert "MG Road" not in meeting_doc.text
        assert "Agenda: pricing." in meeting_doc.text


class TestDerivedData:
    def test_the_whole_index_can_be_deleted_and_rebuilt_from_the_crm(self, user_a, user_b):
        notes = [note(user_a, LeadFactory(owner=user_a), f"Note number {i}") for i in range(3)]
        notes.append(note(user_b, LeadFactory(owner=user_b), "Priya's note"))
        lead = described_lead(user_a, "A lead with context.")
        index()
        before = sorted(
            KnowledgeChunk.objects.values_list(
                "source_id", "chunk_index", "source_hash", "owner_id"
            )
        )
        KnowledgeChunk.objects.all().delete()
        totals = indexing.rebuild()
        after = sorted(
            KnowledgeChunk.objects.values_list(
                "source_id", "chunk_index", "source_hash", "owner_id"
            )
        )
        assert after == before
        assert totals[Outcome.EMBEDDED] == 5
        assert lead.pk in {row[0] for row in after}

    def test_reconciliation_finds_every_kind_of_drift(self, user_a, user_b, embedder):
        lead = LeadFactory(owner=user_a)
        missing, moved, fine = (note(user_a, lead, f"Note {n}") for n in ("a", "b", "c"))
        index()
        KnowledgeChunk.objects.filter(source_id=missing.pk).delete()
        KnowledgeChunk.objects.filter(source_id=moved.pk).update(owner_id=user_b.pk)
        orphan_source = TaskFactory(lead=lead, description="Will be emptied.")
        indexing.index_source(SourceModule.ACTIVITY, orphan_source.pk)
        type(orphan_source).objects.filter(pk=orphan_source.pk).update(description="")
        OutboxEvent.objects.all().delete()
        result = indexing.reconcile_batch(SourceModule.ACTIVITY, None)
        queued = {e.payload["id"] for e in ai_events()}
        assert queued == {str(missing.pk), str(moved.pk), str(orphan_source.pk)}
        assert result.enqueued == 3
        assert str(fine.pk) not in queued
        index()
        assert len(chunks_of(missing.pk)) == 1
        assert {c.owner_id for c in chunks_of(moved.pk)} == {user_a.pk}
        assert chunks_of(orphan_source.pk) == []


class TestFailures:
    def test_the_crm_write_commits_when_embedding_fails_and_indexing_retries(
        self, user_a, embedder
    ):
        embedder.fail = True
        created = note(user_a, LeadFactory(owner=user_a), "Written while the model is down.")
        index()
        (event,) = ai_events()
        assert event.status == OutboxStatus.PENDING
        assert event.attempts == 1
        assert "EmbeddingUnavailable" in event.last_error
        assert type(created).objects.filter(pk=created.pk).exists()  # the note is saved
        embedder.fail = False
        OutboxEvent.objects.filter(pk=event.pk).update(available_at=event.created_at)
        index()
        assert len(chunks_of(created.pk)) == 1

    def test_a_malformed_payload_is_dead_not_retried(self):
        from arkray.core import outbox

        event = outbox.enqueue(indexing.TOPIC_INDEX_SOURCE, {"source": "users", "id": "x"})
        index()
        event.refresh_from_db()
        assert event.status == OutboxStatus.DEAD
