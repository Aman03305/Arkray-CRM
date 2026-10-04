"""Authorised semantic retrieval (docs/rag-architecture.md#query-path-defence-in-depth).

1. **Pre-filter in SQL.** Candidates are only ever drawn from the chunks the caller's scope
   could own: `owner_id = ANY(scope owners)` (or one lead's / opportunity's chunks) is part
   of the query, never applied afterwards to an organisation-wide top-K. For one person's
   scope the search is exact (their chunks are read through the owner index and ranked by
   distance in a materialised subquery); organisation-wide it uses the HNSW index.
2. **Live re-verification.** Every candidate's source is re-read through the owning
   module's scoped selector (`knowledge_documents(scope, ids)`): a source that is gone,
   archived, emptied or now someone else's is dropped, whatever the chunk's (possibly
   stale) metadata says.
3. **Version check.** The live document's hash must equal the chunk's `source_hash`; the
   passage is then sliced from the *live* text. A changed source is dropped and
   re-indexed, so an edited note's old wording can't be quoted.

The result carries only text the caller may read now. Bounded: TOP_K passages, at most
AI_CONTEXT_MAX_CHARS characters in total.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction

from arkray.core.access import AccessScope
from arkray.core.knowledge import KnowledgeDocument, SourceType

from .embeddings import EmbeddingUnavailable, get_embedder
from .formatting import user_text_slice
from .indexing import enqueue_source
from .sources import MODULE_OF, live_documents

logger = logging.getLogger(__name__)

CANDIDATE_FACTOR = 3  # candidates fetched per passage wanted (some fail re-verification)
MAX_PASSAGES_PER_SOURCE = 2
ORG_EF_SEARCH = 100


@dataclass(frozen=True, slots=True)
class Passage:
    source_type: SourceType
    source_id: UUID
    lead_id: UUID
    opportunity_id: UUID | None
    owner_id: UUID
    label: str
    text: str  # sliced from the live document
    occurred_at: datetime
    similarity: float
    source_hash: str = ""  # the live document's content hash, verified


@dataclass(frozen=True, slots=True)
class Retrieval:
    passages: list[Passage]
    available: bool  # False: the embedding model is unavailable (note search is down)
    dropped: int  # candidates refused by live re-verification (stale or unauthorised)


@dataclass(frozen=True, slots=True)
class _Candidate:
    source_type: SourceType
    source_id: UUID
    source_hash: str
    char_start: int
    char_end: int
    distance: float


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"


def _candidates(
    scope: AccessScope,
    query_vector: list[float],
    *,
    lead_id: UUID | None,
    opportunity_id: UUID | None,
    source_types: frozenset[SourceType] | None,
    limit: int,
) -> list[_Candidate]:
    where: list[str] = []
    params: list[Any] = []
    if not scope.is_organization_wide:
        where.append("owner_id = ANY(%s::uuid[])")
        params.append([str(owner) for owner in scope.owner_ids])
    if lead_id is not None:
        where.append("lead_id = %s")
        params.append(str(lead_id))
    if opportunity_id is not None:
        where.append("opportunity_id = %s")
        params.append(str(opportunity_id))
    if source_types:
        where.append("source_type = ANY(%s::text[])")
        params.append(sorted(t.value for t in source_types))
    vector = _vector_literal(query_vector)
    columns = "source_type, source_id, source_hash, char_start, char_end"
    narrow = not scope.is_organization_wide or lead_id is not None or opportunity_id is not None
    if narrow:
        # Exact: the scope's (or the lead's) chunks are read through their btree index and
        # ranked here; the HNSW index can't serve a selective filter reliably.
        # Only constant fragments are spliced in; every value is a parameter.
        sql = (
            f"WITH scoped AS MATERIALIZED (SELECT {columns}, embedding FROM ai_knowledge_chunk"  # noqa: S608
            f" WHERE {' AND '.join(where)}) "
            f"SELECT {columns}, embedding <=> %s::vector AS distance FROM scoped"
            " ORDER BY distance LIMIT %s"
        )
    else:
        condition = f"WHERE {' AND '.join(where)} " if where else ""
        sql = (  # constant fragments only, as above
            f"SELECT {columns}, embedding <=> %s::vector AS distance FROM ai_knowledge_chunk "  # noqa: S608
            f"{condition}ORDER BY embedding <=> %s::vector LIMIT %s"
        )
    with transaction.atomic(), connection.cursor() as cursor:
        if narrow:
            cursor.execute(sql, [*params, vector, limit])
        else:
            cursor.execute("SET LOCAL hnsw.ef_search = %s", [ORG_EF_SEARCH])
            cursor.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            cursor.execute(sql, [vector, *params, vector, limit])
        rows = cursor.fetchall()
    # relaxed_order may return near-ties out of order; rank exactly here.
    candidates = [
        _Candidate(SourceType(row[0]), row[1], row[2], row[3], row[4], float(row[5]))
        for row in rows
    ]
    return sorted(candidates, key=lambda c: (c.distance, str(c.source_id), c.char_start))


def retrieve(
    scope: AccessScope,
    query: str,
    *,
    lead_id: UUID | None = None,
    opportunity_id: UUID | None = None,
    source_types: frozenset[SourceType] | None = None,
    top_k: int | None = None,
    max_chars: int | None = None,
) -> Retrieval:
    top_k = min(top_k or settings.AI_RETRIEVAL_TOP_K, settings.AI_RETRIEVAL_TOP_K)
    try:
        query_vector = get_embedder().embed_query(query)
    except EmbeddingUnavailable:
        logger.warning("ai_retrieval_unavailable", extra={"reason": "embedding_unavailable"})
        return Retrieval(passages=[], available=False, dropped=0)

    candidates = _candidates(
        scope,
        query_vector,
        lead_id=lead_id,
        opportunity_id=opportunity_id,
        source_types=source_types,
        limit=top_k * CANDIDATE_FACTOR,
    )
    live: dict[tuple[SourceType, UUID], KnowledgeDocument] = live_documents(
        scope, [(c.source_type, c.source_id) for c in candidates]
    )
    passages: list[Passage] = []
    per_source: dict[tuple[SourceType, UUID], list[tuple[int, int]]] = {}
    dropped = 0
    budget = min(settings.AI_CONTEXT_MAX_CHARS, max_chars or settings.AI_CONTEXT_MAX_CHARS)
    stale: set[tuple[SourceType, UUID]] = set()
    for candidate in candidates:
        ref = (candidate.source_type, candidate.source_id)
        document = live.get(ref)
        if document is None:
            dropped += 1  # not visible to this scope now (or gone): never shown
            continue
        if document.content_hash != candidate.source_hash or candidate.char_end > len(
            document.text
        ):
            dropped += 1
            stale.add(ref)
            continue
        similarity = 1.0 - candidate.distance
        if similarity < settings.AI_RETRIEVAL_MIN_SIMILARITY:
            continue
        ranges = per_source.setdefault(ref, [])
        overlapping = any(
            candidate.char_start < end and start < candidate.char_end for start, end in ranges
        )
        if len(ranges) >= MAX_PASSAGES_PER_SOURCE or overlapping:
            continue
        # Links, emails and phones judged on the whole document (a chunk can cut a URL).
        text = user_text_slice(document.text, candidate.char_start, candidate.char_end).strip()
        if len(text) > budget:
            break
        budget -= len(text)
        ranges.append((candidate.char_start, candidate.char_end))
        passages.append(
            Passage(
                source_type=document.source_type,
                source_id=document.source_id,
                lead_id=document.lead_id,
                opportunity_id=document.opportunity_id,
                owner_id=document.owner_id,
                label=document.label,
                text=text,
                occurred_at=document.occurred_at,
                similarity=round(similarity, 4),
                source_hash=document.content_hash,
            )
        )
        if len(passages) >= top_k:
            break
    for source_type, source_id in stale:
        enqueue_source(MODULE_OF[source_type], source_id)
    return Retrieval(passages=passages, available=True, dropped=dropped)
