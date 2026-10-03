# 0008. Ask Arkray: deterministic tools + authorized semantic retrieval

Status: Accepted
Date: 2026-09-30

## Context
Users ask both structured questions (counts, totals, dates) and unstructured ones (summaries of
notes and histories). Answers must respect CRM authorization exactly and must not fabricate
figures, and the CRM must keep working when AI is down.

## Decision
- **Structured questions → server-side tools** that run scoped aggregates and selects. Vector
  similarity is never used for counts or totals.
- **Unstructured questions → pgvector retrieval** with a mandatory owner pre-filter, followed
  by re-verification of every hit through the caller's scope. Context text comes from live
  records, not index copies.
- The LLM (Claude via the Anthropic Python SDK; default `claude-opus-5-5`, configurable via
  `AI_CHAT_MODEL`) runs a bounded manual tool loop (≤ 4 rounds, 30 s budget) with strict tool
  schemas. Tools are bound to the caller's `AccessScope`; organisation-wide tools exist only for
  org-scoped admins.
- Responses separate facts (rendered from tool results), citations and model narrative. Record
  links resolve only for records present in tool results, and a numeric grounding check drops
  narratives containing unsupported numbers.
- Embeddings go through a provider interface (Anthropic has no embeddings API; Voyage AI is the
  planned default) and are built by outbox handlers. A circuit breaker and a kill switch isolate
  failures.

## Consequences
- Authorization and correctness don't depend on model behaviour or prompt wording.
- More engineering than "put rows in a prompt", but each piece can be tested deterministically
  with a fake model.
- The assistant is read-only in v1.

## Alternatives considered
- Text-to-SQL: the model writes queries, which is an authorization bypass waiting to happen.
- Pure vector RAG: wrong for totals and counts; similarity search is not arithmetic.
- Embedding structured fields: cost with no benefit for deterministic questions.
