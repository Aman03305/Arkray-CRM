# Operations

Day-to-day procedures for operators. Runbooks for alerts, incidents, restores, secret
rotation, erasure requests and the rest are in [runbooks.md](runbooks.md).

## Rebuild the RAG index

Ask Arkray's semantic index (`ai_knowledge_chunk`) is **derived data**: every row is rebuilt
from the CRM records, so it can be deleted at any time without losing anything. While it is
being rebuilt, Ask Arkray's note search finds fewer passages; structured answers, the CRM and
global search are unaffected.

```bash
# Re-index everything now (unchanged text is skipped; safe to run any time, any number of times)
docker compose run --rm backend python manage.py ai_reindex

# Start from an empty index (after a model change, or to prove rebuildability)
docker compose run --rm backend python manage.py ai_reindex --rebuild

# Let the ai_index workers do it in the background (bounded batches through the outbox)
docker compose run --rm backend python manage.py ai_reindex --enqueue
```

- The command runs in bounded batches of 200 sources and prints progress; it lifts the
  statement timeout for its own session only.
- Throughput is CPU-bound (local embedding model): 5-7 chunks per second per 3-thread
  process for typical notes, 1.5-2 for long ones, on the benchmark machine; 486,646 notes
  took about 3 h 25 min in 8 processes. Run several workers on queue `ai_index` (with
  `--enqueue`) for a large backfill (see [rag-architecture.md](rag-architecture.md#performance)).
- Verify: `SELECT count(*), count(DISTINCT source_id) FROM ai_knowledge_chunk;` against the
  number of notes with text, then ask a question whose answer is in a known note.

## Check the embedding model files

```bash
docker compose run --rm worker-ai python manage.py ai_fetch_model --verify
```

The image fetches and verifies the pinned files at build time
(`python -m arkray.ai.model_files`); a mismatch fails the build.
