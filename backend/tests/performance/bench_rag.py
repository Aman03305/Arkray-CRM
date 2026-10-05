# ruff: noqa: T201, E402
# A diagnostics command (prints its report), not application code; its SQL splices only
# constants and placeholders.
"""Ask Arkray retrieval benchmark (Phase 8; docs/rag-architecture.md#performance).

Run against a copy of the Phase 7 search benchmark database (1M leads, ~2M activities of
which ~487k notes with text), with the real embedding model:

    CREATE DATABASE arkray_bench_rag TEMPLATE arkray_bench_search;
    DATABASE_URL=.../arkray_bench_rag manage.py migrate ai
    bench_rag.py --prepare                         empty the table, drop the HNSW index
    bench_rag.py --seed --shard 0/8 (... 7/8)      in parallel processes (ONNX scales by process)
    bench_rag.py --seed --shard 0/8 --split 1/4 --resume   split a slow shard further, skipping
                                                   notes already loaded (after stopping it)
    bench_rag.py --build-index                     build the HNSW index once, after loading
    bench_rag.py --measure

--seed embeds every note's real knowledge document (identical texts embedded once per
process) and loads the chunks with COPY; the index is built afterwards, once.
--measure times retrieval per
scope shape (own workspace for a heavy, median and small owner; an administrator's selected
user; the organisation; one lead), for a typical question, one matching many similar notes
and one matching nothing, separating query embedding, the candidate SQL, and live
re-verification. It also times the structured path (router + tools + answer assembly, no
model) in each scope. Database/retrieval latency only: a language model's latency is not
part of this (and is not measurable without a key).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
os.environ.setdefault("AI_EMBEDDING_PROVIDER", "local")

import django

django.setup()

from django.conf import settings
from django.db import connection
from django.utils import timezone

from arkray.activities import selectors as activity_selectors
from arkray.activities.models import Activity, ActivityType
from arkray.ai import chunking, retrieval, router, service, tools
from arkray.ai.embeddings import LocalEmbedder
from arkray.core.access import AccessScope

BATCH = 1000


def _embedder() -> LocalEmbedder:
    threads = int(os.environ.get("BENCH_THREADS", "8"))
    return LocalEmbedder(Path(settings.AI_EMBEDDING_MODEL_DIR), threads=threads)


def prepare() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
        cursor.execute("DROP INDEX IF EXISTS ai_chunk_embedding_hnsw")
        cursor.execute("TRUNCATE ai_knowledge_chunk")


def build_index() -> None:
    started = time.perf_counter()
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
        cursor.execute("SET max_parallel_maintenance_workers = 0")  # 64 MB /dev/shm in Docker
        cursor.execute("SET maintenance_work_mem = '1GB'")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ai_chunk_embedding_hnsw ON ai_knowledge_chunk USING hnsw"
            " (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )
        cursor.execute("VACUUM ANALYZE ai_knowledge_chunk")
    print(f"HNSW built in {time.perf_counter() - started:.0f}s", flush=True)


def seed(shard: int, shards: int, *, split: int = 0, splits: int = 1, resume: bool = False) -> None:
    settings.AI_EMBEDDING_PROVIDER = "local"
    embedder = _embedder()
    cache: dict[str, list[float]] = {}
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
    after = None
    loaded = embedded = 0
    started = time.perf_counter()
    while True:
        queryset = (
            Activity.objects.filter(type=ActivityType.NOTE)
            .exclude(description="")
            .extra(where=["abs(hashtext(id::text)) %% %s = %s"], params=[shards, shard])
            .extra(where=["abs(hashtext(id::text || 'split')) %% %s = %s"], params=[splits, split])
        )
        if resume:  # notes a stopped run already loaded (each batch is one COPY)
            queryset = queryset.extra(
                where=[
                    "NOT EXISTS (SELECT 1 FROM ai_knowledge_chunk c WHERE c.source_type = 'note'"
                    " AND c.source_id = activities_activity.id)"
                ]
            )
        if after is not None:
            queryset = queryset.filter(pk__gt=after)
        ids = list(queryset.order_by("pk").values_list("pk", flat=True)[:BATCH])
        if not ids:
            break
        after = ids[-1]
        documents = activity_selectors.knowledge_documents_for_indexing(ids)
        pieces = [(d, p) for d in documents for p in chunking.chunk(d.text)]
        texts = [
            d.text[p.start : p.end] if p.index == 0 else f"{d.label}\n{d.text[p.start : p.end]}"
            for d, p in pieces
        ]
        missing = sorted({t for t in texts if t not in cache})
        if missing:
            for text, vector in zip(missing, embedder.embed_documents(missing), strict=True):
                cache[text] = vector
            embedded += len(missing)
        rows = []
        for (document, piece), text in zip(pieces, texts, strict=True):
            literal = "[" + ",".join(f"{x:.6f}" for x in cache[text]) + "]"
            rows.append(
                "\t".join(
                    [
                        document.source_type.value,
                        str(document.source_id),
                        str(piece.index),
                        str(document.owner_id),
                        str(document.lead_id),
                        str(document.opportunity_id) if document.opportunity_id else "\\N",
                        document.content_hash,
                        str(piece.start),
                        str(piece.end),
                        literal,
                        embedder.model_name,
                        document.updated_at.isoformat(),
                        document.updated_at.isoformat(),
                    ]
                )
            )
        with (
            connection.cursor() as cursor,
            cursor.copy(
                "COPY ai_knowledge_chunk (source_type, source_id, chunk_index, owner_id, lead_id,"
                " opportunity_id, source_hash, char_start, char_end, embedding, embedding_model,"
                " source_updated_at, indexed_at) FROM STDIN"
            ) as copy,
        ):
            copy.write("\n".join(rows) + "\n")
        loaded += len(rows)
        if len(cache) > 200_000:
            cache.clear()
        rate = loaded / (time.perf_counter() - started)
        print(f"[{shard}/{shards}] {loaded} chunks, {embedded} embedded, {rate:.0f}/s", flush=True)


def _time(fn: Any, runs: int) -> tuple[float, float, float]:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return statistics.median(samples), p95, samples[-1]


def measure(runs: int) -> None:
    settings.AI_EMBEDDING_PROVIDER = "local"
    # Timings below use no similarity floor (every candidate re-verified: the worst case);
    # the configured floor is applied once at the end, to the question matching nothing.
    configured_floor = settings.AI_RETRIEVAL_MIN_SIMILARITY
    settings.AI_RETRIEVAL_MIN_SIMILARITY = 0.0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT owner_id, count(*) FROM ai_knowledge_chunk GROUP BY owner_id ORDER BY 2 DESC"
        )
        owners = cursor.fetchall()
        cursor.execute(
            "SELECT count(*), pg_size_pretty(pg_total_relation_size('ai_knowledge_chunk')),"
            " pg_size_pretty(pg_relation_size('ai_chunk_embedding_hnsw')) FROM ai_knowledge_chunk"
        )
        total, table_size, hnsw_size = cursor.fetchone()
        cursor.execute(
            "SELECT lead_id, count(*) FROM ai_knowledge_chunk"
            " GROUP BY lead_id ORDER BY 2 DESC LIMIT 1"
        )
        busiest_lead = cursor.fetchone()
    heavy, median_owner, small = owners[0], owners[len(owners) // 2], owners[-1]
    admin_id = heavy[0]  # any id: an organisation scope ignores owners
    print(f"chunks={total} table+indexes={table_size} hnsw={hnsw_size} owners={len(owners)}")
    print(
        f"heavy owner {heavy[1]} chunks, median {median_owner[1]}, small {small[1]};"
        f" busiest lead {busiest_lead[1]}"
    )
    from arkray.ai.embeddings import get_embedder

    embedder = get_embedder()
    queries = {
        "typical": "What concerns did the customer raise about pricing?",
        "many_similar": "Call back on Monday morning",
        "no_match": "quantum zebra telescope",
    }
    embed = _time(lambda: embedder.embed_query(queries["typical"]), runs)
    print(f"query embedding: p50 {embed[0]:.1f} ms  p95 {embed[1]:.1f} ms")
    scopes = {
        "own_heavy": AccessScope.own(heavy[0]),
        "own_median": AccessScope.own(median_owner[0]),
        "own_small": AccessScope.own(small[0]),
        # An administrator in a salesperson's workspace (the actor is another user).
        "selected_user": AccessScope.for_user(heavy[0], median_owner[0]),
        "organization": AccessScope.organization(admin_id),
    }
    structured(scopes, runs)
    for name, scope in scopes.items():
        for label, text in queries.items():
            vector = embedder.embed_query(text)
            sql = _time(
                lambda scope=scope, vector=vector: retrieval._candidates(
                    scope, vector, lead_id=None, opportunity_id=None, source_types=None, limit=24
                ),
                runs,
            )
            whole = _time(lambda scope=scope, text=text: retrieval.retrieve(scope, text), runs)
            found = retrieval.retrieve(scope, text)
            print(
                f"{name:13} {label:12} candidates SQL p50 {sql[0]:6.1f} p95 {sql[1]:6.1f} | "
                f"retrieve (embed+SQL+verify) p50 {whole[0]:6.1f} p95 {whole[1]:6.1f} ms | "
                f"{len(found.passages)} passages, dropped {found.dropped}"
            )
    lead_scope = AccessScope.organization(admin_id)
    lead_time = _time(
        lambda: retrieval.retrieve(lead_scope, queries["typical"], lead_id=busiest_lead[0]), runs
    )
    print(
        f"one lead (busiest, {busiest_lead[1]} chunks): retrieve p50 {lead_time[0]:.1f}"
        f" p95 {lead_time[1]:.1f} ms"
    )
    settings.AI_RETRIEVAL_MIN_SIMILARITY = configured_floor
    for name in ("own_heavy", "organization"):
        floor_scope = scopes[name]
        floored = _time(lambda s=floor_scope: retrieval.retrieve(s, queries["no_match"]), runs)
        found = retrieval.retrieve(floor_scope, queries["no_match"])
        print(
            f"{name} no_match at the configured floor {configured_floor}: p50 {floored[0]:.1f}"
            f" p95 {floored[1]:.1f} ms, {len(found.passages)} passages"
        )
    vector = embedder.embed_query(queries["typical"])
    literal = retrieval._vector_literal(vector)
    with connection.cursor() as cursor:
        for label, scope in (
            ("own_heavy", scopes["own_heavy"]),
            ("organization", scopes["organization"]),
        ):
            if scope.is_organization_wide:
                cursor.execute("BEGIN")
                cursor.execute("SET LOCAL hnsw.ef_search = 100")
                cursor.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                cursor.execute(
                    "EXPLAIN (ANALYZE, BUFFERS) SELECT source_id, embedding <=> %s::vector AS d"
                    " FROM ai_knowledge_chunk ORDER BY embedding <=> %s::vector LIMIT 24",
                    [literal, literal],
                )
            else:
                cursor.execute("BEGIN")
                cursor.execute(
                    "EXPLAIN (ANALYZE, BUFFERS) WITH scoped AS MATERIALIZED"
                    " (SELECT source_id, embedding"
                    " FROM ai_knowledge_chunk WHERE owner_id = ANY(%s::uuid[])) SELECT source_id,"
                    " embedding <=> %s::vector AS d FROM scoped ORDER BY d LIMIT 24",
                    [[str(heavy[0])], literal],
                )
            plan = "\n".join(row[0] for row in cursor.fetchall())
            cursor.execute("ROLLBACK")
            print(f"--- plan: {label}\n{plan}")


ROUTED = (
    "What is my pipeline value?",
    "What is my weighted pipeline?",
    "Pipeline by stage",
    "How many leads do I have?",
    "New leads today",
    "What are my overdue tasks?",
    "Meetings tomorrow",
)


def structured(scopes: dict[str, AccessScope], runs: int) -> None:
    """The structured path: router, tools and answer assembly as the service runs them (no
    model, no retrieval)."""
    words = service.WorkspaceWords("You", "have", "Your", "benchmark")
    # The routed questions name no stage, so any scope's stage names route them alike.
    stage_names = router.active_stage_names(next(iter(scopes.values())))
    routes: list[router.Route] = []
    for question in ROUTED:
        found = router.route(question, stage_names=lambda: stage_names)
        if found is None:
            raise SystemExit(f"Not routed: {question}")
        routes.append(found)
    for name, scope in scopes.items():
        cells = []
        for routed in routes:

            def run(scope: AccessScope = scope, routed: router.Route = routed) -> None:
                ctx = tools.ToolContext(scope=scope, now=timezone.now())
                service.answer_routed(ctx, routed, workspace=words)

            p50, p95, _ = _time(run, runs)
            cells.append(f"{routed.intent} {p50:.0f}/{p95:.0f}")
        print(f"structured {name:13} (p50/p95 ms): " + "; ".join(cells), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--shard", default="0/1")
    parser.add_argument("--split", default="0/1")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--runs", type=int, default=20)
    options = parser.parse_args()
    if options.prepare:
        prepare()
    if options.seed:
        index, total = (int(part) for part in options.shard.split("/"))
        part, parts = (int(x) for x in options.split.split("/"))
        seed(index, total, split=part, splits=parts, resume=options.resume)
    if options.build_index:
        build_index()
    if options.measure:
        measure(options.runs)
