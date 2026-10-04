"""Building and maintaining the semantic index (docs/rag-architecture.md#indexing-pipeline).

    CRM service (its transaction) --domain event--> ai.subscribers --outbox.enqueue-->
    worker (queue ai_index) --> index_source(): load the live document, embed, replace

- **Never inline.** CRM writes only enqueue (an outbox row in their own transaction). If the
  model or a worker is down, the CRM write still commits and the work waits durably.
- **Idempotent.** A source's chunks are identified by (source_type, source_id, chunk_index)
  (unique) and replaced together under a per-source advisory lock, so duplicate or
  concurrent deliveries leave exactly one set. Unchanged text (same hash, same model) is
  never re-embedded: only the authorization metadata (owner, lead, opportunity) is
  refreshed, which is how a reassignment is applied.
- **Derived.** Everything here can be deleted and rebuilt from the CRM (`rebuild`, the
  `ai_reindex` command); the nightly reconciliation re-enqueues drifted sources.
- **Bounded.** One source per event; reconciliation walks bounded batches, each its own
  outbox event, so no job runs long or holds locks across the model call.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib.postgres.aggregates import ArrayAgg
from django.db import connection, transaction
from django.db.models import Count, Max, Min

from arkray.core import outbox
from arkray.core.knowledge import KnowledgeDocument, SourceType
from arkray.leads import selectors as lead_selectors
from arkray.pipeline import selectors as pipeline_selectors

from . import chunking
from .embeddings import get_embedder
from .models import KnowledgeChunk
from .sources import LOADERS, MODULE_OF, SourceModule

logger = logging.getLogger(__name__)

TOPIC_INDEX_SOURCE = "ai.index_source"  # {"source": module, "id": uuid}
TOPIC_INDEX_LEAD = "ai.index_lead"  # {"lead_id": uuid}: everything indexed under a lead
TOPIC_RECONCILE = "ai.reconcile_batch"  # {"source": module, "after": uuid | null}
RECONCILE_BATCH = 500


class Outcome(StrEnum):
    EMBEDDED = "embedded"  # (re)embedded: new or changed text, or a new model
    METADATA = "metadata"  # same text; owner / lead / opportunity refreshed
    UNCHANGED = "unchanged"
    REMOVED = "removed"  # the source no longer has a document (archived, emptied, gone)
    ABSENT = "absent"  # no document and nothing indexed


# --- producing work ------------------------------------------------------------------------
def enqueue_source(module: SourceModule, source_id: UUID) -> None:
    """Re-index one source after its transaction commits (coalesced per source)."""
    if not settings.AI_INDEXING_ENABLED:
        return
    outbox.enqueue(
        TOPIC_INDEX_SOURCE,
        {"source": module.value, "id": str(source_id)},
        dedupe_key=f"{module.value}:{source_id}",
    )


def enqueue_lead(lead_id: UUID) -> None:
    """Re-sync everything indexed under a lead (its reassignment moved the lead, its open
    opportunities and its current work in one transaction): one event, not one per row."""
    if not settings.AI_INDEXING_ENABLED:
        return
    outbox.enqueue(TOPIC_INDEX_LEAD, {"lead_id": str(lead_id)}, dedupe_key=f"lead-tree:{lead_id}")


# --- applying it ---------------------------------------------------------------------------
def _embed_input(document: KnowledgeDocument, piece: chunking.Chunk) -> str:
    text = document.text[piece.start : piece.end]
    # Later chunks lose the document's first line ("Note: ...", "Opportunity: ..."): give
    # the model that context back.
    return text if piece.index == 0 else f"{document.label}\n{text}"


def _lock_source(source_id: UUID) -> None:
    """Serialise writers of one source's chunks for the rest of the transaction."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 8008))", [str(source_id)])


def _hold_references(document: KnowledgeDocument) -> None:
    """Before touching any chunk: key-share the lead and opportunity the chunks will
    reference, as their foreign-key checks would at commit. An erasure locks the lead and
    then deletes these chunks; waiting for the lead while already holding the chunks made
    the two deadlock (whole-software audit). Now the index waits first, holding nothing."""
    lead_selectors.hold_lead_key(document.lead_id)
    if document.opportunity_id is not None:
        pipeline_selectors.hold_opportunity_key(document.opportunity_id)


def _metadata_differs(chunk: KnowledgeChunk, document: KnowledgeDocument) -> bool:
    return (
        chunk.owner_id != document.owner_id
        or chunk.lead_id != document.lead_id
        or chunk.opportunity_id != document.opportunity_id
    )


def _live(module: SourceModule, source_id: UUID) -> KnowledgeDocument | None:
    found = LOADERS[module].for_indexing([source_id])
    return found[0] if found else None


def sync(module: SourceModule, source_id: UUID, document: KnowledgeDocument | None) -> Outcome:
    """Make the index agree with the source's knowledge document. `document` (loaded by the
    caller, possibly a while ago) only decides whether to embed; every write re-reads the
    live document under the per-source lock and acts on *that*, so a slow or out-of-order
    handler can't undo a newer change (an old "archived" can't delete a restored source's
    chunks; an old owner can't overwrite a newer reassignment). The model runs outside any
    transaction."""
    types = LOADERS[module].source_types
    indexed = KnowledgeChunk.objects.filter(source_type__in=types, source_id=source_id)
    if document is None:
        # Decided under the lock: a first indexing still embedding when the source was
        # archived commits its chunks before this looks (audit P3: an orphan until the
        # nightly reconciliation).
        with transaction.atomic():
            _lock_source(source_id)
            if not indexed.exists():
                return Outcome.ABSENT
            if _live(module, source_id) is not None:
                return Outcome.UNCHANGED  # it has a document again (restored): keep it
            indexed.delete()
        return Outcome.REMOVED

    current = list(
        indexed.only(
            "id",
            "source_type",
            "chunk_index",
            "owner_id",
            "lead_id",
            "opportunity_id",
            "source_hash",
            "embedding_model",
        )
    )
    embedder = get_embedder()
    pieces = chunking.chunk(document.text)
    same_text = (
        current
        and len(current) == len(pieces)
        and all(
            c.source_hash == document.content_hash
            and c.embedding_model == embedder.model_name
            and c.source_type == document.source_type
            for c in current
        )
    )
    if same_text:
        if not any(_metadata_differs(c, document) for c in current):
            return Outcome.UNCHANGED
        with transaction.atomic():
            _lock_source(source_id)
            live = _live(module, source_id)
            if live is None:
                indexed.delete()
                return Outcome.REMOVED
            if live.content_hash != document.content_hash:
                return Outcome.UNCHANGED  # changed meanwhile; its own event re-indexes it
            _hold_references(live)
            indexed.update(
                owner_id=live.owner_id,
                lead_id=live.lead_id,
                opportunity_id=live.opportunity_id,
                source_updated_at=live.updated_at,
            )
        return Outcome.METADATA

    vectors = embedder.embed_documents([_embed_input(document, piece) for piece in pieces])
    with transaction.atomic():
        _lock_source(source_id)
        latest = _live(module, source_id)
        if latest is None:
            indexed.delete()
            return Outcome.REMOVED
        if latest.content_hash != document.content_hash:
            return Outcome.UNCHANGED  # changed while we embedded; its own event follows
        _hold_references(latest)
        indexed.delete()
        KnowledgeChunk.objects.bulk_create(
            KnowledgeChunk(
                source_type=latest.source_type,
                source_id=source_id,
                chunk_index=piece.index,
                owner_id=latest.owner_id,
                lead_id=latest.lead_id,
                opportunity_id=latest.opportunity_id,
                source_hash=latest.content_hash,
                char_start=piece.start,
                char_end=piece.end,
                embedding=vector,
                embedding_model=embedder.model_name,
                source_updated_at=latest.updated_at,
            )
            for piece, vector in zip(pieces, vectors, strict=True)
        )
    return Outcome.EMBEDDED


def index_sources(module: SourceModule, source_ids: Iterable[UUID]) -> dict[Outcome, int]:
    """Index several sources of one module (their documents loaded in one query)."""
    ids = list(dict.fromkeys(source_ids))
    documents = {d.source_id: d for d in LOADERS[module].for_indexing(ids)}
    outcomes: dict[Outcome, int] = {}
    for source_id in ids:
        outcome = sync(module, source_id, documents.get(source_id))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


def index_source(module: SourceModule, source_id: UUID) -> Outcome:
    return next(iter(index_sources(module, [source_id])))


def index_lead(lead_id: UUID) -> dict[Outcome, int]:
    """Re-sync the lead and every source indexed under it (after a reassignment: the moved
    records' chunks get their new owner; text is not re-embedded)."""
    totals: dict[Outcome, int] = {}
    indexed = (
        KnowledgeChunk.objects.filter(lead_id=lead_id)
        .values_list("source_type", "source_id")
        .distinct()
    )
    by_module: dict[SourceModule, set[UUID]] = {SourceModule.LEAD: {lead_id}}
    for source_type, source_id in indexed:
        by_module.setdefault(MODULE_OF[SourceType(source_type)], set()).add(source_id)
    for module, ids in by_module.items():
        for outcome, count in index_sources(module, ids).items():
            totals[outcome] = totals.get(outcome, 0) + count
    return totals


# --- reconciliation and rebuild ------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BatchResult:
    checked: int
    enqueued: int
    next_after: UUID | None


def reconcile_batch(
    module: SourceModule, after: UUID | None, *, limit: int = RECONCILE_BATCH
) -> BatchResult:
    """Compare one batch of a module's sources with their chunks and enqueue the drifted
    ones (missing, changed text, other model, other owner). Also sweeps the chunks of
    sources in the same id range that no longer have a document. Constant queries."""
    ids = LOADERS[module].source_ids(after=after, limit=limit)
    upper = ids[-1] if len(ids) == limit else None
    documents = {d.source_id: d for d in LOADERS[module].for_indexing(ids)}
    model = get_embedder().model_name
    types = LOADERS[module].source_types
    in_range = KnowledgeChunk.objects.filter(source_type__in=types)
    if after is not None:
        in_range = in_range.filter(source_id__gt=after)
    if upper is not None:
        in_range = in_range.filter(source_id__lte=upper)
    summaries: dict[UUID, dict[str, Any]] = {
        row["source_id"]: row
        for row in in_range.values("source_id").annotate(
            chunks=Count("id"),
            hash_min=Min("source_hash"),
            hash_max=Max("source_hash"),
            model_min=Min("embedding_model"),
            model_max=Max("embedding_model"),
            owners=ArrayAgg("owner_id", distinct=True),
        )
    }
    drifted: list[UUID] = []
    for source_id in set(summaries) | set(documents):
        document = documents.get(source_id)
        summary = summaries.get(source_id)
        if document is None or summary is None:
            drifted.append(source_id)  # orphaned chunks, or never indexed
            continue
        expected = len(chunking.chunk(document.text))
        if (
            summary["chunks"] != expected
            or summary["hash_min"] != summary["hash_max"]
            or summary["hash_max"] != document.content_hash
            or summary["model_min"] != model
            or summary["model_max"] != model
            or summary["owners"] != [document.owner_id]
        ):
            drifted.append(source_id)
    for source_id in drifted:
        enqueue_source(module, source_id)
    return BatchResult(checked=len(ids), enqueued=len(drifted), next_after=upper)


def start_reconciliation() -> None:
    """Enqueue the first batch of every module; each batch enqueues the next."""
    if not settings.AI_INDEXING_ENABLED:
        return
    for module in SourceModule:
        outbox.enqueue(
            TOPIC_RECONCILE,
            {"source": module.value, "after": None},
            dedupe_key=f"reconcile:{module}",
        )


def continue_reconciliation(module: SourceModule, after: UUID | None) -> BatchResult:
    result = reconcile_batch(module, after)
    if result.next_after is not None:
        outbox.enqueue(TOPIC_RECONCILE, {"source": module.value, "after": str(result.next_after)})
    else:
        logger.info("ai_reconciliation_finished", extra={"source": module.value})
    return result


def rebuild(*, batch: int = 200, progress: Any = None) -> dict[Outcome, int]:
    """Index every source now, synchronously (operators: `manage.py ai_reindex`). With an
    empty chunk table this rebuilds the whole index from the CRM. Then removes chunks whose
    source no longer has a document."""
    totals: dict[Outcome, int] = {}
    for module in SourceModule:
        after: UUID | None = None
        while True:
            ids = LOADERS[module].source_ids(after=after, limit=batch)
            if not ids:
                break
            for outcome, count in index_sources(module, ids).items():
                totals[outcome] = totals.get(outcome, 0) + count
            if progress is not None:
                progress(module, len(ids), totals)
            after = ids[-1]
        # Orphans: indexed sources that no longer have a document.
        types = LOADERS[module].source_types
        after_orphan: UUID | None = None
        while True:
            indexed = KnowledgeChunk.objects.filter(source_type__in=types)
            if after_orphan is not None:
                indexed = indexed.filter(source_id__gt=after_orphan)
            chunk_ids = list(
                indexed.order_by("source_id").values_list("source_id", flat=True).distinct()[:batch]
            )
            if not chunk_ids:
                break
            have = {d.source_id for d in LOADERS[module].for_indexing(chunk_ids)}
            for source_id in chunk_ids:
                if source_id not in have:
                    outcome = sync(module, source_id, None)
                    totals[outcome] = totals.get(outcome, 0) + 1
            after_orphan = chunk_ids[-1]
    return totals
