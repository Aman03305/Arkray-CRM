# 0023. Ask Arkray as built: local embeddings, answers on their own queue, a deterministic fast path

Status: Accepted (refines [ADR-0008](0008-ask-arkray-hybrid-rag.md); replaces its embeddings-provider choice)
Date: 2026-10-04

## Context
ADR-0008 fixed the shape of Ask Arkray: scope-bound deterministic tools for figures, pgvector
retrieval with an owner pre-filter and live re-verification for text, a bounded tool loop
with Claude, grounding checks. Building it in Phase 8 forced four further decisions:

1. **Who computes embeddings.** ADR-0008 planned Voyage AI. That sends every note's text
   to a second third party at indexing time, costs per record, adds an outage source and
   needs a key. None is available to verify it here.
2. **Where an answer is computed.** A question can take 2–30 s (several model calls).
   Gunicorn's sync workers serve the whole CRM; a slow provider holding them would starve
   the CRM.
3. **What happens without a model** (no key, provider down, breaker open). The brief requires
   the CRM, global search and structured answers to keep working.
4. **What a vector row stores.** Copying chunk text into the index duplicates CRM text in a
   second table that must then be kept authorised and private.

## Decision
- **Local embeddings.** `BAAI/bge-small-en-v1.5` (MIT, 384 dimensions, ONNX on CPU) at a
  pinned revision, SHA-256-verified at image build (`arkray.ai.model_files`), loaded from
  disk only by the ai workers. Note text never leaves the deployment to be indexed. The
  column is `vector(384)`; changing model or dimension is a new column and a backfill.
- **Answers on their own Celery queue.** `POST /ask` stores the question and dispatches it to
  queue `ai` (its own workers); the browser polls `GET /ask/questions/{id}`. A question
  unanswered within `AI_QUESTION_TIMEOUT_S` (90 s) fails as `timeout`. Indexing runs on a
  separate outbox queue, `ai_index`, so a re-indexing backlog never delays an answer, and
  neither touches `default`/`email`. Bulkheads: 2 pending questions per user, 40 in total,
  counted in PostgreSQL.
- **A deterministic router first.** A question made only of an intent's words plus generic
  filler ("What is my pipeline value?", "How many overdue tasks do I have?", "Which deals are
  in Negotiation?") is answered on the web request by the same tools and a fixed template:
  no model, no queue, no cost. Anything more specific goes to the model.
- **Degradation without a model.** With `AI_LLM_PROVIDER=none`, a missing key, an open
  breaker, or a failing provider, the worker answers with the records that best match by
  meaning (quoted, linked), and says that no summary was written.
- **Chunks hold no text.** A chunk row holds where its source is, which character range of
  the source's knowledge document it embeds, the document's hash and the vector. Retrieval
  re-reads the source through the caller's scope and slices the live text, only if the hash
  still matches.
- **Each module owns its knowledge documents.** Leads, pipeline and activities selectors
  decide what of their records may be embedded (`knowledge_documents`), as each decides what
  search matches. Never contact data, meeting links or anything from identity.

## Consequences
- Indexing needs no key and no network; quality is that of a small English model (measured:
  recall@1 8/8 on the evaluation set; scores are compressed, so "nothing relevant" is a
  calibrated threshold, 0.55). Hindi or mixed-language notes are a known weakness.
- An ai worker carries the model (~130 MB on disk, a few hundred MB in memory) and embeds on
  CPU (5-7 chunks/s per 3-thread process for typical notes on the benchmark machine; a
  full rebuild of 486,646 notes, 525,511 chunks, took about 3 h 25 min in 8 processes).
- The web tier never waits on an AI provider; Ask Arkray adds one table write and one broker
  publish per non-routed question, and polling reads.
- With no Anthropic key Ask Arkray is still useful (structured answers, retrieval) and no
  CRM text leaves the deployment at all.

## Alternatives considered
- **Voyage AI embeddings** (ADR-0008's plan): better multilingual quality, but a second data
  processor for every note, per-record cost, a key, and not verifiable here.
- **Answering inside the web request** with a concurrency limiter: simpler, but sync workers
  pinned for up to 30 s each, and nothing to show after a page reload.
- **Storing chunk text** for hybrid lexical+vector ranking: duplicates note text into a table
  that must be secured separately; global search already covers lexical lookup.
- **A dedicated vector database:** a second store to secure, back up and keep consistent;
  PostgreSQL + pgvector meets the measured needs (docs/rag-architecture.md#performance).
