"""Phase 10 concurrency: global search while records are written, and Ask Arkray's indexing
racing edits of the text it indexes. Real PostgreSQL, one connection per thread."""

from __future__ import annotations

from typing import Any

import pytest

from arkray.activities import services as activity_services
from arkray.activities.models import Activity
from arkray.ai import indexing
from arkray.ai.models import KnowledgeChunk
from arkray.ai.sources import SourceModule, live_documents
from arkray.core.access import AccessScope
from arkray.core.knowledge import SourceType
from arkray.core.ranking import SearchQuery
from arkray.leads import services as lead_services
from arkray.search.selectors import global_search
from tests.ai_fixtures import note
from tests.factories import LeadFactory
from tests.helpers import drain_outbox, run_concurrently

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration", "ai_on"),
]


def test_searching_while_records_are_written(user_a):
    """Searches run in a read-only snapshot beside creates and edits of the very records
    they find: no errors, no deadlocks, and every search sees a consistent moment."""
    scope = AccessScope.own(user_a.pk)
    lead = LeadFactory(owner=user_a, first_name="Concurrent")
    for i in range(5):
        note(user_a, lead, f"concurrentword note {i}")
    query = SearchQuery.parse("concurrentword")

    def search() -> int:
        return len(global_search(scope, query).notes.items)

    def write(i: int) -> Any:
        def run() -> Any:
            created = note(user_a, lead, f"concurrentword written {i}")
            activity = Activity.objects.get(pk=created.pk)
            return activity_services.update_activity(
                actor=user_a,
                scope=scope,
                activity_id=activity.pk,
                version=activity.version,
                changes={"description": f"concurrentword edited {i}"},
            )

        return run

    def edit_lead() -> Any:
        current = lead.__class__.objects.get(pk=lead.pk)
        return lead_services.update_lead(
            actor=user_a,
            scope=scope,
            lead_id=lead.pk,
            version=current.version,
            changes={"city": "Pune"},
        )

    results = run_concurrently(*([search] * 6), *(write(i) for i in range(6)), edit_lead)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], errors
    assert all(1 <= r <= 5 for r in results[:6])  # five shown per group, whatever the moment
    assert search() == 5


def test_indexing_racing_edits_ends_with_the_final_text_indexed(user_a):
    """Indexing jobs for a note run while the note is edited again and again (and twice at
    once for the same note): when the outbox drains, its chunks describe its final text,
    once, and nothing stale is left."""
    scope = AccessScope.own(user_a.pk)
    lead = LeadFactory(owner=user_a)
    written = note(user_a, lead, "racing edits version zero")
    drain_outbox(rounds=20)

    def edit(i: int) -> Any:
        def run() -> Any:
            activity = Activity.objects.get(pk=written.pk)
            try:
                return activity_services.update_activity(
                    actor=user_a,
                    scope=scope,
                    activity_id=written.pk,
                    version=activity.version,
                    changes={"description": f"racing edits version {i} " + "word " * i},
                )
            except Exception as exc:  # a lost optimistic race is a 409, not a failure here
                return exc

        return run

    def reindex() -> Any:
        return indexing.index_source(SourceModule.ACTIVITY, written.pk)

    for _ in range(3):
        results = run_concurrently(edit(1), reindex, edit(2), reindex, reindex)
        unexpected = [
            r
            for r in results
            if isinstance(r, BaseException) and type(r).__name__ != "ConflictError"
        ]
        assert unexpected == [], unexpected
    drain_outbox(rounds=20)

    live = live_documents(scope, [(SourceType.NOTE, written.pk)])[(SourceType.NOTE, written.pk)]
    chunks = list(KnowledgeChunk.objects.filter(source_type="note", source_id=written.pk))
    assert chunks, "the note is indexed"
    assert {c.source_hash for c in chunks} == {live.content_hash}
    assert sorted(c.chunk_index for c in chunks) == list(range(len(chunks)))
