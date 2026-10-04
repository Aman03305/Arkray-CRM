"""Authorised retrieval: the owner pre-filter is part of the SQL, every hit is re-read
through the caller's scope, and the live text must still match what was embedded."""

from __future__ import annotations

from typing import Any

import pytest

from arkray.activities import services as activity_services
from arkray.ai import indexing, retrieval
from arkray.ai.embeddings import EmbeddingUnavailable
from arkray.ai.models import KnowledgeChunk
from arkray.ai.sources import SourceModule
from arkray.core.access import AccessScope
from arkray.core.models import OutboxEvent
from tests.ai_fixtures import index, note, own
from tests.factories import LeadFactory

pytestmark = pytest.mark.django_db

QUERY = "analyser pricing concern raised by the hospital"


def texts(found: retrieval.Retrieval) -> list[str]:
    return [p.text for p in found.passages]


@pytest.fixture
def twin_notes(user_a: Any, user_b: Any) -> dict[str, Any]:
    """The same words in Rahul's and Priya's workspaces: only the scope can tell them apart."""
    rahul = note(user_a, LeadFactory(owner=user_a), f"Rahul: {QUERY}.")
    priya = note(user_b, LeadFactory(owner=user_b), f"Priya: {QUERY}.")
    index()
    return {"rahul": rahul, "priya": priya}


class TestPreFilter:
    def test_each_user_retrieves_only_their_own(self, user_a, user_b, twin_notes):
        assert texts(retrieval.retrieve(own(user_a), QUERY)) == [f"Text: Rahul: {QUERY}."]
        assert texts(retrieval.retrieve(own(user_b), QUERY)) == [f"Text: Priya: {QUERY}."]

    def test_the_owner_filter_is_in_the_query_not_applied_afterwards(self, user_a, twin_notes):
        from arkray.ai.embeddings import get_embedder

        vector = get_embedder().embed_query(QUERY)
        candidates = retrieval._candidates(
            own(user_a), vector, lead_id=None, opportunity_id=None, source_types=None, limit=50
        )
        assert {c.source_id for c in candidates} == {twin_notes["rahul"].pk}

    def test_an_admin_in_one_users_workspace_sees_only_that_user(self, admin, user_a, twin_notes):
        found = retrieval.retrieve(AccessScope.for_user(admin.pk, user_a.pk), QUERY)
        assert texts(found) == [f"Text: Rahul: {QUERY}."]

    def test_organisation_wide_sees_both_through_the_ann_index(self, admin, twin_notes):
        found = retrieval.retrieve(AccessScope.organization(admin.pk), QUERY)
        assert sorted(texts(found)) == sorted([f"Text: Rahul: {QUERY}.", f"Text: Priya: {QUERY}."])

    def test_a_lead_filter_narrows_to_that_lead(self, user_a):
        first, second = LeadFactory(owner=user_a), LeadFactory(owner=user_a)
        note(user_a, first, f"First lead: {QUERY}")
        note(user_a, second, f"Second lead: {QUERY}")
        index()
        found = retrieval.retrieve(own(user_a), QUERY, lead_id=second.pk)
        assert texts(found) == [f"Text: Second lead: {QUERY}"]


class TestLiveReverification:
    def test_a_planted_stale_chunk_never_surfaces_another_users_text(
        self, user_a, user_b, twin_notes
    ):
        """The worst case: Priya's note's chunk carries Rahul's owner id (stale metadata after
        a reassignment, or corruption). The pre-filter lets it through; live
        re-verification through Rahul's scope drops it."""
        KnowledgeChunk.objects.filter(source_id=twin_notes["priya"].pk).update(owner_id=user_a.pk)
        found = retrieval.retrieve(own(user_a), QUERY)
        assert texts(found) == [f"Text: Rahul: {QUERY}."]
        assert found.dropped == 1

    def test_an_edited_source_is_not_quoted_from_its_old_vector(self, user_a, twin_notes):
        rahul = twin_notes["rahul"]
        type(rahul).objects.filter(pk=rahul.pk).update(description="Completely different now.")
        OutboxEvent.objects.all().delete()
        found = retrieval.retrieve(own(user_a), QUERY)
        assert found.passages == []
        assert found.dropped == 1
        (event,) = OutboxEvent.objects.filter(topic=indexing.TOPIC_INDEX_SOURCE)
        assert event.payload == {"source": "activity", "id": str(rahul.pk)}

    def test_an_archived_source_is_dropped_before_its_chunks_are(self, user_a, twin_notes):
        rahul = twin_notes["rahul"]
        activity_services.archive_activity(
            actor=user_a, scope=own(user_a), activity_id=rahul.pk, version=rahul.version
        )
        # Not indexed yet: the chunks are still there.
        assert KnowledgeChunk.objects.filter(source_id=rahul.pk).exists()
        found = retrieval.retrieve(own(user_a), QUERY)
        assert found.passages == []
        assert found.dropped == 1

    def test_a_reassigned_source_leaves_the_old_owner_before_re_indexing(
        self, admin, user_a, user_b
    ):
        from arkray.leads import services as lead_services

        lead = LeadFactory(owner=user_a)
        moved = note(user_a, lead, f"Moving note: {QUERY}")
        index()
        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        # Before the worker runs: Rahul's stale chunks are refused, Priya can't find it yet.
        assert retrieval.retrieve(own(user_a), QUERY).passages == []
        assert retrieval.retrieve(own(user_b), QUERY).passages == []
        index()
        assert texts(retrieval.retrieve(own(user_b), QUERY)) == [f"Text: Moving note: {QUERY}"]
        assert retrieval.retrieve(own(user_a), QUERY).passages == []
        del moved


class TestBounds:
    def test_at_most_two_passages_per_source_and_top_k_overall(self, settings, user_a):
        settings.AI_RETRIEVAL_TOP_K = 3
        lead = LeadFactory(owner=user_a)
        note(user_a, lead, (f"{QUERY}. " * 120))  # several chunks
        for i in range(4):
            note(user_a, lead, f"Another {i}: {QUERY}")
        index()
        found = retrieval.retrieve(own(user_a), QUERY)
        per_source: dict[Any, int] = {}
        for passage in found.passages:
            per_source[passage.source_id] = per_source.get(passage.source_id, 0) + 1
        assert len(found.passages) == 3
        assert max(per_source.values()) <= 2

    def test_the_context_budget_bounds_the_text(self, settings, user_a):
        settings.AI_CONTEXT_MAX_CHARS = 1500
        lead = LeadFactory(owner=user_a)
        for i in range(5):
            note(user_a, lead, f"{i} {QUERY}. " * 15)
        index()
        found = retrieval.retrieve(own(user_a), QUERY)
        assert sum(len(t) for t in texts(found)) <= 1500

    def test_unrelated_text_below_the_threshold_is_not_returned(self, settings, user_a):
        settings.AI_RETRIEVAL_MIN_SIMILARITY = 0.5
        note(user_a, LeadFactory(owner=user_a), "Lunch moved to Friday.")
        index()
        assert retrieval.retrieve(own(user_a), QUERY).passages == []

    def test_an_unavailable_model_reports_note_search_down(self, monkeypatch, user_a):
        def broken() -> Any:
            raise EmbeddingUnavailable("missing")

        monkeypatch.setattr("arkray.ai.retrieval.get_embedder", broken)
        found = retrieval.retrieve(own(user_a), QUERY)
        assert (found.available, found.passages) == (False, [])

    def test_the_stored_hash_must_match_and_slices_come_from_live_text(self, user_a):
        created = note(user_a, LeadFactory(owner=user_a), f"Exact live text: {QUERY}")
        indexing.index_source(SourceModule.ACTIVITY, created.pk)
        (passage,) = retrieval.retrieve(own(user_a), QUERY).passages
        assert passage.text == f"Text: Exact live text: {QUERY}"
