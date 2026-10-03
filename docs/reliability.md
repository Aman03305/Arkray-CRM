# Reliability: async processing, failure handling and degradation

The CRM must degrade gracefully instead of freezing or crashing. PostgreSQL is the only
hard dependency; everything else (Redis, Celery, email, AI) may fail without breaking CRM
reads and writes.

## Asynchronous processing: transactional outbox (built)

[ADR-0006](adr/0006-transactional-outbox.md) · code: [`core/outbox.py`](../backend/arkray/core/outbox.py)

```mermaid
sequenceDiagram
    participant S as Service (web request)
    participant DB as PostgreSQL
    participant Beat as Celery beat
    participant R as Redis broker
    participant W as Worker
    S->>DB: BEGIN; business write; INSERT outbox_event; COMMIT
    Note over S,DB: If the transaction rolls back, the event never existed
    loop every 5 s
        Beat->>R: core.outbox.relay on queue "outbox" (expires 10 s)
        R->>W: relay
        W->>DB: recover expired leases;<br/>claim due events FOR UPDATE SKIP LOCKED<br/>(up to the queue's free in-flight capacity),<br/>new claim token + 30 min dispatch lease
        W->>R: core.outbox.process(id, token) on queue default | email | ai<br/>(message expires with the dispatch lease)
    end
    R->>W: process(id, token)
    W->>DB: if token still current: attempts += 1, 5 min running lease
    W->>W: run handler (idempotent)
    W->>DB: done | pending (backoff) | dead, only if token still current
```

Rules:
- **Atomic with the business change.** A lead can't be created without its re-index event,
  and an event can't exist for a lead that wasn't created.
- **One retry layer.** The outbox retries; Celery tasks do not. Layered retries multiply
  into retry storms.
- **Bounded attempts.** Default 8, with exponential backoff and equal jitter (10 s, 20 s,
  40 s … capped at 1 h). Then `dead`, logged at ERROR and alerted. Nothing retries forever.
  `PermanentFailure` goes straight to `dead`.
- **Claim tokens.** Every claim gets a fresh token; the task carries it, and every state
  transition (start, done, retry, dead) must present the current token. Stale or duplicated
  messages are ignored instead of running the handler twice or double-counting attempts. Such
  messages come from a requeue after a worker crash, a broker redelivery, a publish that raised
  but went through, or a message that outlived its lease.
- **Two leases.** A *dispatch* lease (30 min) covers time waiting in the broker, and the
  message expires with it. Once a worker starts, a *running* lease (5 min, longer than the
  120 s Celery hard limit) applies. An expired lease returns the event to `pending`, and the
  next relay re-claims it under a new token.
- **Crash-safe.** Attempts are counted *before* the handler runs, so a handler that kills its
  worker still exhausts its attempts.
- **Bounded broker queues.** The relay dispatches only up to `OUTBOX_MAX_IN_FLIGHT` per
  queue (`default` 200, `email` 50, `ai` 50). The backlog lives durably in PostgreSQL, so
  Redis never holds an unbounded queue. Relay ticks expire after 10 s, so beat can't pile
  them up while workers are down.
- **The relay has its own queue** (`outbox`, with a dedicated consumer in production), so a
  backlog of real work can never delay claiming, and so can never stall the other queues.
- **Isolation.** Separate queues (and, in production, separate worker deployments) mean an
  AI backlog cannot delay invitation or password-reset emails.
- **At-least-once.** Handlers must be idempotent (content hashes for indexing, "already
  sent" checks for emails). Payloads carry identifiers only; handlers reload current state.
- **Coalescing (best effort).** A `dedupe_key` merges new work into an identical event that is
  pending and has **never been attempted** (five quick edits to a lead produce one re-index).
  The merged-into event is row-locked until the caller's transaction commits, so it can't run
  on pre-commit state. This is deliberately *not* a unique constraint. A constraint would block
  rows returning to `pending` on retry or lease recovery (a relay-jamming bug found in the
  Phase 0 review), and duplicate events are harmless because handlers are idempotent. New work
  never coalesces into an event that is backing off after a failure.
- **Traceable.** Events store the originating request's correlation id; worker logs for the
  event carry it (verified end to end in Phase 0).

Topics in use (Phase 1): `identity.deliver_account_token` (queue `email`: mints the
one-time secret, stores its digest, sends the invitation or reset email),
`identity.password_reset_requested` (queue `default`: decides eligibility, issues a reset
token) and `identity.notify_email_changed` (queue `email`). Planned: `ai.index_source`,
`ai.remove_source` (queue `ai`); `leads.import_batch` (queue `default`).

Periodic (beat, not outbox): `identity.housekeeping` hourly. It purges throttle evidence
older than 24 h and expired sessions, and blanks the payloads of finished
`identity.password_reset_requested` and `identity.notify_email_changed` events after 24 h,
because they hold email addresses (`core.outbox.redact_finished_payloads`). It is safe to
run late or twice.

## Failure and degradation matrix

| Failure | Impact | Behaviour | Detection |
|---|---|---|---|
| **PostgreSQL down** | CRM unavailable (the only hard dependency) | `/health/ready` 503, so the load balancer drains; `/health/live` stays 200 (no restart loops); API returns a JSON 500/503 with request id | readiness, error rate |
| **PostgreSQL slow / lock contention** | slow requests | `statement_timeout` 10 s (web), `lock_timeout` 5 s, `idle_in_transaction_session_timeout` 60 s: a stuck query fails fast instead of pinning workers and connections; `jit` off (short OLTP statements: compilation only added latency, Phase 5) | latency, timeout errors |
| **Redis cache down** | none functionally | cache ops become misses via django-redis `IGNORE_EXCEPTIONS`; **fail-fast connections**: no redis-py retries plus a per-process circuit breaker (15 s cool-down), so an outage costs one failed attempt per process per cool-down instead of seconds per request; readiness reports `degraded` (still 200). Measured: 8 s per request before the breaker, 0.2 s after | readiness `degraded`, logs |
| **Redis cache down: side effects** | DRF request-rate throttling fails open; admin-workspace audit de-duplication fails toward *more* auditing; **login and reset throttling are unaffected** (PostgreSQL-backed; tested) | by design | — |
| **Redis broker down** | background work pauses | CRM writes still succeed (outbox rows accumulate in PostgreSQL); beat and workers reconnect automatically; the backlog drains within the in-flight caps | oldest-pending age alert |
| **Worker crash mid-task** | one event delayed | `acks_late` + `reject_on_worker_lost` redeliver; lease recovery as backstop; attempts counted | dead events, logs |
| **Duplicate / stale broker messages** | none | claim tokens make every non-current message a no-op (`outbox_stale_message_ignored`) | logs |
| **Default-queue backlog** (for example a large import) | only that queue slows | relay runs on its own `outbox` queue; other queues keep draining | queue age per queue |
| **Beat down** | nothing dispatched | same as broker down; run exactly one beat (see deployment) | oldest-pending age alert |
| **Poison event** | one event fails repeatedly | bounded attempts → `dead`; other events unaffected | dead-event alert |
| **AI provider slow / down / rate-limited** | Ask Arkray only | per-call timeout, one SDK retry, circuit breaker (60 s) → immediate 503 `ai_unavailable`; indexing events back off in the outbox | breaker-open alert |
| **Embeddings provider down** | note search only | indexing backs off; Ask Arkray structured tools keep working | outbox backlog on queue `ai` |
| **SMTP down** | emails delayed | user creation and reset requests succeed; email events retry with backoff, each retry minting a fresh link secret; the admin table shows "Sending invitation…" until delivery; resend is available to admins (tested) | email-queue backlog |
| **Recovery thundering herd** | spike after an outage | jittered backoff; in-flight caps meter the drain rate | — |
| **Bad deploy / failed migration** | release blocked | migrations run as a separate one-off job before rollout; the app never migrates on boot; expand/contract migrations keep old and new code compatible | job failure |

## Timeouts

Every network call and every wait is bounded:

| Operation | Timeout |
|---|---|
| Browser → API (frontend `apiFetch`) | 15 s (AbortController) |
| Gunicorn worker per request | 30 s |
| DB connect / statement (web) / statement (workers) | 5 s / 10 s / 60 s |
| DB lock wait / idle in transaction | 5 s / 60 s |
| Redis cache connect / socket | 1 s / 1 s, no retries, circuit breaker |
| Redis broker socket | 5 s |
| SMTP | 10 s |
| Celery task soft / hard limit | 100 s / 120 s |
| Outbox dispatch lease / running lease | 30 min / 300 s |
| LLM call / whole Ask Arkray question | 20 s / 30 s |
| Embeddings call | 10 s |

## Retries

| Where | Policy |
|---|---|
| Outbox handlers | 8 attempts, exponential + jitter, then dead |
| Anthropic SDK | `max_retries=1` (429/5xx/connection) inside the 30 s question budget; the circuit breaker is the outer layer |
| Web requests | never retried server-side |
| Frontend | GET queries retried twice with backoff (TanStack Query, Phase 1); **mutations never auto-retried**. Create endpoints accept an `Idempotency-Key` where duplicates would be harmful (see [api-conventions.md](api-conventions.md#idempotency)). |
| Redis cache | none (fail fast) |

## Concurrency and consistency

- **Row locks for state transitions:** stage moves and reassignment use
  `SELECT … FOR UPDATE` inside the transaction, so stage history always records the true
  `from_stage`, and two concurrent drags produce two consistent history rows.
- **Optimistic concurrency for edits:** `version` column; a stale PATCH gets 409
  (no lost updates).
- **Constraints as the final arbiter:** uniqueness and state invariants are DB constraints,
  so a race that slips past validation still can't store invalid data.
- **Queue claims:** `FOR UPDATE SKIP LOCKED` lets relays and workers scale horizontally
  without double-claiming.

## Connection budget

PostgreSQL `max_connections` is finite; exhaustion cascades into total outage. Budget:

```
budget = max_connections − reserved (superuser 3 + migrations/ops 5)
usage  = web_pods × gunicorn_workers × 1        (sync workers: one connection each)
       + worker_pods × celery_concurrency × 1
       + beat (1)
usage ≤ 0.8 × budget
```

Example: `max_connections=100` gives a budget of 92, so the target is ≤ 73; for instance
3 web pods × 8 workers (24) + 2 worker pods × 8 (16) + 1 = 41. Beyond that, put
**PgBouncer** (transaction pooling) in front: Django uses client-side parameter binding
with psycopg 3 by default (safe with transaction pooling), and
`DISABLE_SERVER_SIDE_CURSORS=True` must be set when PgBouncer is used. The optional
per-process psycopg pool (`DB_POOL_ENABLED`) bounds connections per process when threaded
servers are used.

## Backpressure and bounded work

- Page sizes ≤ 100, cursor pagination, no unbounded list endpoints.
- Scoped throttles on expensive endpoints (login, reset, search, ask, import).
- Imports and exports run in workers in batches, never inside a web request.
- Statement timeouts cap the cost of any single query.

## Graceful shutdown

Gunicorn: 30 s graceful timeout, and workers recycle after about 2000 requests to contain
memory growth. Celery: warm shutdown finishes the current task; unacknowledged tasks are
redelivered (`acks_late`).
