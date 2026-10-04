"""Where knowledge documents come from: the owning modules' selectors, nothing else.

Three source modules: leads, opportunities and activities (tasks, meetings and notes share
the activities table). Each exposes `knowledge_documents(scope, ids)` (what a scope may
see now), `knowledge_documents_for_indexing(ids)` (the indexer, as the system) and
`knowledge_source_ids(after, limit)` (bounded batches for rebuilds and reconciliation).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from arkray.activities import selectors as activity_selectors
from arkray.core.access import AccessScope
from arkray.core.knowledge import KnowledgeDocument, SourceType
from arkray.leads import selectors as lead_selectors
from arkray.pipeline import selectors as pipeline_selectors


class SourceModule(StrEnum):
    LEAD = "lead"
    OPPORTUNITY = "opportunity"
    ACTIVITY = "activity"


@dataclass(frozen=True, slots=True)
class _Loaders:
    scoped: Callable[[AccessScope, Collection[UUID]], list[KnowledgeDocument]]
    for_indexing: Callable[[Collection[UUID]], list[KnowledgeDocument]]
    source_ids: Callable[..., list[UUID]]
    source_types: frozenset[SourceType]


LOADERS: dict[SourceModule, _Loaders] = {
    SourceModule.LEAD: _Loaders(
        lead_selectors.knowledge_documents,
        lead_selectors.knowledge_documents_for_indexing,
        lead_selectors.knowledge_source_ids,
        frozenset({SourceType.LEAD}),
    ),
    SourceModule.OPPORTUNITY: _Loaders(
        pipeline_selectors.knowledge_documents,
        pipeline_selectors.knowledge_documents_for_indexing,
        pipeline_selectors.knowledge_source_ids,
        frozenset({SourceType.OPPORTUNITY}),
    ),
    SourceModule.ACTIVITY: _Loaders(
        activity_selectors.knowledge_documents,
        activity_selectors.knowledge_documents_for_indexing,
        activity_selectors.knowledge_source_ids,
        frozenset({SourceType.TASK, SourceType.MEETING, SourceType.NOTE}),
    ),
}

MODULE_OF: dict[SourceType, SourceModule] = {
    source_type: module
    for module, loaders in LOADERS.items()
    for source_type in loaders.source_types
}


def live_documents(
    scope: AccessScope, refs: Iterable[tuple[SourceType, UUID]]
) -> dict[tuple[SourceType, UUID], KnowledgeDocument]:
    """The documents behind `refs` as `scope` may see them *now* (one query per source
    module). A ref absent from the result is not visible: deleted, archived, reassigned
    away, or no longer has text."""
    wanted = set(refs)
    by_module: dict[SourceModule, set[UUID]] = {}
    for source_type, source_id in wanted:
        by_module.setdefault(MODULE_OF[source_type], set()).add(source_id)
    found: dict[tuple[SourceType, UUID], KnowledgeDocument] = {}
    for module, ids in by_module.items():
        for document in LOADERS[module].scoped(scope, ids):
            found[(document.source_type, document.source_id)] = document
    # Only exactly what was asked for: an activity id asked for as a note is not returned
    # as a task.
    return {ref: document for ref, document in found.items() if ref in wanted}
