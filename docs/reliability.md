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
        W->>R: core.outbox.process(id, token) on queue default | email | ai_index<br/>(message expires with the dispatch lease)
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
  queue (`default` 200, `email` 50, `ai_index` 100). The backlog lives durably in PostgreSQL, so
  Redis never holds an unbounded queue. The relay refills a queue once per 5 s tick, at most
  100 events (`OUTBOX_RELAY_BATCH_SIZE`), so min(cap, 100) / 5 s is each queue's sustained
  ceiling however fast its workers are: 20 events/s. (Phase 10 measured `ai_index` at 20: a
  4 events/s ceiling while its one worker cleared each batch in under 0.5 s; under the load
  test's 13 writes/s the indexing backlog grew to 616 and drained at exactly 4/s. At 100 it
  peaked at 55 and kept up. One indexing process handled more than 33 of the load test's
  short notes a second; full-length notes embed at 1.5-7 chunks/s per process (R70), where
  the workers, not the relay, are the limit.) Relay ticks expire after 10 s, so beat can't pile
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
token) and `identity.notify_email_changed` (queue `email`). Phase 8: `ai.index_source`
(one source), `ai.index_lead` (everything under a reassigned lead) and `ai.reconcile_batch`
(nightly self-healing, each batch enqueues the next), all on queue `ai_index`, 8 attempts
(reconciliation 4). Planned: `leads.import_batch` (queue `default`).

**Ask Arkray's questions are not outbox work.** A question is interactive, so the web request
publishes it straight to Celery queue `ai` (consumed only by the `worker-ai` deployment), with
`retry=False`, an expiry equal to the question's deadline, and a 30 s in-process fail-fast
breaker on publish failures. A question is claimed once in PostgreSQL (redelivery never
answers twice); one never answered expires after `AI_QUESTION_TIMEOUT_S` (90 s) and reads as
`timeout`. Bulkheads: 2 pending per user, 40 in total (PostgreSQL counts). So an AI provider
that is slow or down occupies only the `ai` workers, never web workers or the workers that
send email and run CRM background work.

Periodic (beat, not outbox): `identity.housekeeping` hourly. It purges throttle evidence
older than 24 h and expired sessions, and blanks the payloads of finished
`identity.password_reset_requested` and `identity.notify_email_changed` events after 24 h,
because they hold email addresses (`core.outbox.redact_finished_payloads`). It is safe to
run late or twice.

## Failure and degradation matrix

| Failure | Impact | Behaviour | Detection |
|---|---|---|---|
| **PostgreSQL down** | CRM unavailable (the only hard dependency) | `/health/ready` 503, so the load balancer drains; `/health/live` stays 200 (no restart loops); the API answers **503 `service_unavailable` with `Retry-After: 30`** and the request id (Phase 10: a connection-level failure anywhere in the request, including the session middleware; a statement timeout or deadlock stays a 500, a fault to fix); the metrics scrape reports `arkray_db_up 0` and keeps its broker and cache gauges. Drill: ready 503, live 200 in 3 ms, healthy again within 5-13 s of PostgreSQL returning, no application restart. One connection attempt per request: the access-log line no longer resolves the user a second time (whole-software audit: 7.7-12.8 s to the 503 before, about half after) | readiness, `arkray_db_up`, 5xx rate |
| **PostgreSQL stalled** (paused, failing over, a network partition: connections neither answer nor close) | requests hang, then fail at the proxy | the server-side timeouts can't fire on a server that is frozen. Web workers wait in their query; gunicorn kills them at 30 s (`WORKER TIMEOUT`) and the proxy answers 502, then 504 (35 s); everything resumes the moment the database does. Bounded since the whole-software audit: readiness answers 503 within its 2 s probe deadline while a worker is free; TCP keepalives and `tcp_user_timeout` turn a partition into an error after 30-60 s. Measured (a 45 s pause, 2 requests a second): 502 at 30-32 s, 504 at 35 s, immediate recovery. An HTTP liveness probe shares the stuck workers: probe liveness at the TCP level (`tcpSocket`), or give an HTTP one more than 35 s of tolerance, or a failover restarts every pod | readiness, `arkray_db_up` 0, gunicorn `WORKER TIMEOUT`, proxy 502/504 (`upstream_status` in the access log) |
| **PostgreSQL slow / lock contention** | slow requests | `statement_timeout` 10 s (web), `lock_timeout` 5 s, `idle_in_transaction_session_timeout` 60 s: a stuck query fails fast instead of pinning workers and connections; `jit` off (short OLTP statements: compilation only added latency, Phase 5) | latency, timeout errors |
| **Redis cache down** | none functionally | cache ops become misses via django-redis `IGNORE_EXCEPTIONS`; **fail-fast connections**: no redis-py retries plus a per-process circuit breaker, so an outage costs one failed attempt per process per cool-down instead of seconds per request; the cool-down starts at 15 s and doubles while the outage lasts, up to 120 s, and the first successful connection resets it (Phase 10); when a cool-down runs out, one background thread per process checks whether Redis is back while requests keep failing fast, so no request waits on the check (2026-10-06); readiness reports `degraded` (still 200). Measured: 8 s per request before the breaker, 0.2 s after. Phase 10 drill (Redis's container stopped under load): each probe stalls its request about 3.9 s in DNS, which socket timeouts don't bound; with a fixed 15 s cool-down that was 50 stalls in 45 s across 8 processes, with the back-off 24 in 90 s; 0 errors, 103 req/s against 127 healthy (8 users). 2026-10-06: the dev Redis crash-looped on a corrupt AOF tail and pages felt randomly slow (requests of 3.9-4.4 s every 2 minutes per process); the probe now runs off the request | readiness `degraded`, `cache_circuit_opened`, `arkray_cache_up` |
| **Redis cache down: side effects** | DRF request-rate throttling fails open; **login and reset throttling, Ask Arkray's bulkheads and the admin-workspace audit window are unaffected** (PostgreSQL-backed since Phases 1, 8 and 9; tested) | by design | — |
| **Redis broker down** | background work pauses | CRM writes still succeed (outbox rows accumulate in PostgreSQL); beat and workers reconnect automatically; the backlog drains within the in-flight caps. Drill: 406 indexing events accumulated during the outage and were done 50 s after Redis returned, none dead | oldest-pending age alert, `arkray_broker_up` |
| **Worker crash mid-task** | one event delayed | `acks_late` + `reject_on_worker_lost` redeliver; lease recovery as backstop; attempts counted. Drill (`kill -9` of the indexing worker under load): 31 events waited in flight, all done within 20 s of its restart; every one of the 1,917 notes written during the drills indexed exactly once (the chunk table's unique key), none dead. The event a killed *container* was running waits out its running lease instead (5 minutes; 4 min 51 s in the whole-software audit's drill): its broker message stays unacknowledged, and the lease recovery runs it again | dead events, logs |
| **A queue's workers down** | that queue's work waits | its events are handed to the broker and sit in flight, taken by nobody, until the workers return (then done within seconds: 6.2 s for the index, 5 s for email in the audit's drills); the CRM is unaffected (0 errors in 712 probes) | `arkray_outbox_oldest_undelivered_seconds` (alert after 5 minutes; before the audit nothing showed it) |
| **PostgreSQL down with background work in flight** (final audit SRE-5) | invitations, emails and index jobs are delayed, up to the 30-minute dispatch lease | a worker whose task fails before it claims its event (the database is down) acknowledges the message with no retry: the event stays in flight until its lease ends, then is dispatched again; nothing is lost or dead-lettered | `arkray_outbox_oldest_undelivered_seconds` (> 300 s alerts) |
| **Duplicate / stale broker messages** | none | claim tokens make every non-current message a no-op (`outbox_stale_message_ignored`) | logs |
| **Default-queue backlog** (for example a large import) | only that queue slows | relay runs on its own `outbox` queue; other queues keep draining | queue age per queue |
| **Beat down** | nothing dispatched | same as broker down; run exactly one beat (see deployment) | oldest-pending age alert |
| **Poison event** | one event fails repeatedly | bounded attempts → `dead`; other events unaffected | dead-event alert |
| **AI provider slow / down / rate-limited / malformed** | Ask Arkray summaries only | per-call 20 s timeout, one retry after a jittered 0.5-1 s (or after the `Retry-After` a 429, 503 or 529 asks for if it fits the question's remaining time; otherwise, or when it's unusable, the question falls back at once, Phase 10), a 30 s budget per question; a rejected key (401/403) counts toward the breaker without a retry; a response that isn't a Messages response (a proxy's HTML page, invalid JSON) counts as a failure, and any unexpected error while answering falls back too (Phase 9); 3 failures in a row open the breaker for 60 s; meanwhile (and for the failing question) the answer is the best-matching records; router questions never use the model | `ai_breaker_opened` (ERROR), `ai_provider_failed`, `ai_provider_bad_response`, `ai_answer_failed`; `arkray_ai_breaker_open` |
| **Embedding model unavailable** (files missing, load or run failure) | note search only | indexing events back off in the outbox (`EmbeddingUnavailable`); `search_notes` says note search is unavailable; router and other tools keep working | outbox backlog / dead events on queue `ai_index` |
| **ai workers down / backlog** | Ask Arkray's non-routed questions | pending questions expire after 90 s (`timeout`); router questions are answered on the web request | `ai_question_skipped`, `arkray_ai_questions_last_hour{error="timeout"}`, `arkray_ai_questions_pending` |
| **Broker down for a question** | that question | fails at once with `ai_unavailable`; a fail-fast breaker (30 s, doubling to 2 min while the broker stays down; Phase 10) spares later questions the connect timeout; router questions unaffected. A failed publish holds its web worker about 10 s (DNS and connect): once per process per cool-down, 16 times in the 90 s drill across 8 processes | `ai_dispatch_failed`, `ai_dispatch_circuit_opened` |
| **SMTP down** | emails delayed | user creation and reset requests succeed; email events retry with backoff, each retry minting a fresh link secret; the admin table shows "Sending invitation…" until delivery; resend is available to admins (tested). An email waits for its next retry after SMTP returns (98-108 s after a 2.6-minute outage, exactly one each). An outage longer than about 10-20 minutes exhausts the 8 attempts: those emails go dead, and an admin resends the invitations (or `outbox_requeue --queue email`) | `outbox_event_retry_scheduled`, dead events on queue `email` |
| **Attachment storage down or slow** (bucket unreachable, black-holed, throttling, a full volume) | file uploads and downloads only | each storage call has a 12 s deadline (botocore's own: 3 s connect, 10 s read, 2 attempts), at most 4 in flight per process, and a per-process breaker: after 3 outage-like failures in a row (timeouts, connection errors, 5xx, a full disk) calls answer 503 `storage_unavailable` + `Retry-After: 30` at once, without a network call, for 15 s doubling to 2 minutes; a background thread checks whether storage is back (`head_bucket`, or a sentinel written and read back), never a request. A failed upload leaves a `failed` row, never a stored one; readiness stays `ok`. Verified against a local fault-injecting S3 endpoint with the real client (below) | `arkray_attachment_storage_up`, `arkray_attachment_storage_errors_last_hour`, `attachment_storage_failed`, `attachment_storage_circuit_opened` |
| **Attachment object lost** (deleted outside the application, a bucket restored to an older point) | that file | its download answers 410 `attachment_unavailable` ("This file is no longer available. Ask an administrator."), never the outage's 503; a 404 or 403 from the store never trips the breaker; the download path writes nothing: the daily reconciliation reports the file and `reconcile_attachments --repair` marks it unavailable | `arkray_attachment_objects_missing_last_hour`, `attachment_object_missing` (WARNING), `arkray_attachment_reconcile_mismatches` |
| **Web tier down** | pages unavailable, the API unaffected | the proxy answers 504, then 502, until a web instance is back (8 s after it started in the audit's drill) | the web probe (`GET /login`, a healthcheck in both Compose files), proxy 502/504 with `upstream_status` |
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
| Redis cache connect / socket | 1 s / 1 s, no retries, circuit breaker (15 s doubling to 120 s) |
| Metrics scrape's Redis probes (broker, cache) | 2 s each, then reported down |
| Redis broker socket | 5 s |
| Attachment storage call (any backend) | 12 s deadline per call; S3 client 3 s connect, 10 s read, 2 attempts |
| Attachment storage liveness probe (metrics) | 2 s, cached 30 s per process |
| SMTP | 10 s |
| Celery task soft / hard limit | 100 s / 120 s |
| Outbox dispatch lease / running lease | 30 min / 300 s |
| LLM call / whole Ask Arkray question (model loop) | 20 s / 30 s |
| Ask Arkray question unanswered (worker down, backlog) | 90 s, then `timeout` |
| `ai.answer_question` task soft / hard limit | 60 s / 75 s |
| Embeddings | no network call (a local ONNX model); bounded by the task limits above |

## Retries

| Where | Policy |
|---|---|
| Outbox handlers | 8 attempts, exponential + jitter, then dead; handlers idempotent (content hashes, "already sent" checks, claim tokens) |
| Model calls (Ask Arkray) | the SDK's own retries off (`max_retries=0`); ours: `AI_LLM_MAX_RETRIES` = 1 for provider-health failures (429, 5xx including 503 and 529, timeout, connection, malformed) after a jittered 0.5-1 s, or after the `Retry-After` the provider sent when it fits the question's remaining time, else none: an unusable value (negative, a day or more, unparseable) also means none (Phase 10: a 429 asking for 30 s used to be retried at once); a rejected key (401/403) is never retried but counts toward the breaker; never past the 30 s budget; the circuit breaker (3 failed questions, 60 s) is the outer layer |
| Ask Arkray hand-off to the broker | none (`retry=False`); a breaker with back-off (30 s to 2 min) |
| Web requests | never retried server-side |
| Frontend | GET queries retried twice with backoff (TanStack Query, Phase 1); **mutations never auto-retried**. Create endpoints accept an `Idempotency-Key` where duplicates would be harmful (see [api-conventions.md](api-conventions.md#idempotency)). |
| Redis cache | none (fail fast); breaker with back-off (15 s to 120 s) |
| Attachment storage | botocore standard mode, 2 attempts in total, inside the 12 s deadline; then a per-process breaker (3 failures, 15 s doubling to 120 s); uploads and downloads are never retried server-side; purge and scan jobs retry through the outbox |

Phase 10 audit: every retry above is bounded (attempt counts, budgets, cool-down caps),
backs off (exponential with jitter, `Retry-After`, or breaker cool-downs), is safe to repeat
(idempotent handlers, `Idempotency-Key`, mutations never auto-retried) and observable
(`outbox_event_retry_scheduled`, `outbox_event_dead`, `ai_provider_failed`,
`ai_breaker_opened`, `cache_circuit_opened`, `ai_dispatch_circuit_opened`, the metrics).

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
3 web pods × 8 workers (24) + 2 worker pods × 8 (16) + 1 = 41. Count every Celery worker
pool: the Compose topology runs the CRM worker (2), the indexing worker (1) and the
questions' worker (2) besides 8 gunicorn processes and beat: 14 connections at most, measured
under the Phase 10 load test (below). `arkray_db_server_connections` /
`arkray_db_max_connections` watch it in production (every state counts: with persistent
connections, idle ones hold their slots). Beyond that, put
**PgBouncer** (transaction pooling) in front: Django uses client-side parameter binding
with psycopg 3 by default (safe with transaction pooling), and
`DISABLE_SERVER_SIDE_CURSORS=True` must be set when PgBouncer is used. The optional
per-process psycopg pool (`DB_POOL_ENABLED`) bounds connections per process when threaded
servers are used. With it on, count **two** per web process (the worker and the readiness
probe's own thread: 16 for 8 workers, measured in the final audit), and `/health/ready`
needs one spare connection beyond the workers: with the role's connection limit equal to
the worker count it answers 503 while the API still serves.

## Backpressure and bounded work

- Page sizes ≤ 100, cursor pagination, no unbounded list endpoints.
- Scoped throttles on expensive endpoints (login, reset, search, ask, import), and 600
  requests a minute per user overall (`API_THROTTLE_USER`). The load test met all of them:
  a user with no pause between requests is answered 429 with `Retry-After`.
- Imports and exports run in workers in batches, never inside a web request.
- Statement timeouts cap the cost of any single query.

## Attachment storage

File storage is a degraded dependency, like the cache: uploads and downloads fail cleanly
while it is down and nothing else does (final audit SRE-4, R105). `activities.storage`
guards every call, whatever the backend:

- **Deadline:** the call runs on a small per-process pool and the request stops waiting
  after `ATTACHMENT_STORAGE_DEADLINE_S`. botocore's timeouts bound each socket operation,
  not the whole call (a stalled body, a slow drip of bytes, two attempts with back-off
  could take 20 s and more); the deadline bounds the request. The abandoned call is told to
  stop at its next chunk and ends within botocore's own timeouts. A download from S3 is
  fetched whole (into a spool file) before the response starts, so a failure is a 503, not
  a 200 cut off mid-file.
- **Bulkhead:** at most `ATTACHMENT_STORAGE_MAX_IN_FLIGHT` calls per process, abandoned
  ones included; beyond that a call answers 503 at once. With gunicorn's sync workers a
  process serves one request at a time, so this bounds threaded servers and the abandoned
  calls; what keeps storage trouble from taking every web worker is the deadline (no
  request waits more than 12 s) and the breaker (after 3 failures a process stops waiting
  at all). Worst case at the start of an outage: about 3 x 12 s of one worker per process
  before its breaker opens. Uploads also have their own rate limit per user
  (`API_THROTTLE_ATTACHMENTS`, 30 a minute, besides the 600 a minute for everything).
- **Breaker:** after `ATTACHMENT_STORAGE_BREAKER_FAILURES` outage-like failures in a row
  (timeout, connection, 5xx, a full volume, an incomplete write), calls fail at once for
  `ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S`, doubling up to 8 times that while the outage
  lasts; when a cool-down ends, one background thread per process checks the store
  (`head_bucket`, or a sentinel written, read back and deleted) and its success closes the
  breaker. A missing object (404) and a refusal (403: a configuration fault, answered at
  once) are answers, not outages: they never trip it.
- **Integrity:** a row becomes `stored` only after `storage.save()` returned, which reads
  the object's size back from the store (a HEAD on S3, a stat on disk); a short object is a
  failed upload. Tested at every step: the write failing, the write done and the finish
  failing, the process dying between the two, a deletion whose purge fails
  (`activities/tests/test_attachment_failures.py`).

| Variable | Default | Notes |
|---|---|---|
| `ATTACHMENT_STORAGE_DEADLINE_S` | `12` | per call; keep it under gunicorn's 30 s with room for the rest of the request |
| `ATTACHMENT_STORAGE_MAX_IN_FLIGHT` | `4` | per process (web and worker) |
| `ATTACHMENT_STORAGE_BREAKER_FAILURES` | `3` | outage-like failures in a row before the breaker opens |
| `ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S` | `15` | first cool-down; doubles to 8 x while the outage lasts |
| `ATTACHMENT_S3_CONNECT_TIMEOUT_S`, `ATTACHMENT_S3_READ_TIMEOUT_S`, `ATTACHMENT_S3_MAX_ATTEMPTS` | `3`, `10`, `2` | botocore's, per socket operation and attempt |
| `ATTACHMENT_RECONCILE_DAILY` | `true` | the daily read-only reconciliation ([runbooks.md](runbooks.md#attachment-reconciliation)) |
| `API_THROTTLE_ATTACHMENTS` | `30/min` | uploads per user |

**Verified** (`tests/integration/test_attachment_storage_faults.py`, [testing.md](testing.md#attachment-storage-faults)):
the real django-storages `S3Storage` and botocore client, built with the production
options, against a local fault-injecting S3-compatible endpoint: connection refused, a
black-holed address, a store that accepts and never answers, 403, 404, 500, 503 SlowDown, a
write the store keeps only half of, slow answers, a download that stalls or drops
mid-body; twelve uploads at once to a dead store (all back within the deadline, the rest
failing in under 100 ms once the breaker is open); the CRM's other endpoints answering
while storage hangs; recovery. The filesystem backend: an unwritable root, a full disk, a
short write, a slow disk. **Not verified here:** a real S3 or S3-compatible bucket (its
consistency, multipart behaviour above 8 MB, server-side encryption, IAM policy errors,
real network latency): R99 stays open until the deployment's own bucket is tested
([runbooks.md](runbooks.md#attachment-storage-degraded)).

## Graceful shutdown

Gunicorn: 30 s graceful timeout, and workers recycle after about 2000 requests to contain
memory growth. Celery: warm shutdown finishes the current task; unacknowledged tasks are
redelivered (`acks_late`, `reject_on_worker_lost`), and outbox events are leased, so an
abandoned one returns to pending when its lease ends. The orchestrator must wait longer
than these before killing (Phase 10: Docker's default is 10 s): Compose sets
`stop_grace_period` 35 s for the web container and 130 s for the Celery workers (task hard
limit 120 s); on Kubernetes set `terminationGracePeriodSeconds` to the same. Drill: SIGTERM
to the web container under 8 users' load: gunicorn's 8 workers finished their requests and
exited within 1 s, exit code 0, no 5xx; each Celery worker's warm shutdown took 2-3 s, exit
code 0, and the outbox was complete after the restart.

## Performance baseline (Phase 10)

Every major flow through the whole Django stack (middleware, authentication, CSRF,
permissions, serializers, the real SQL), in process, on the 1M-lead benchmark database
(`arkray_bench_rag`: 501 users, 1,000,000 leads, 300,000 opportunities, 1,998,067
activities, 733,042 timeline entries, 525,511 Ask Arkray chunks), 40 runs each after
warm-up: `tests/performance/bench_api.py`. Machine: i7-13700HX (16 cores / 24 threads),
15.7 GB, PostgreSQL 16.15 in Docker Desktop with default settings (`shared_buffers` 128 MB).
p50 / p95 in ms; queries per request; the slowest statement.

| Flow | Typical owner | Heaviest owner (61,246 leads) | Admin in heaviest's workspace | Organisation |
|---|---|---|---|---|
| Dashboard | 25 / 40 · 10 q | 50 / 90 · 10 q (15 ms) | 53 / 93 · 12 q | 130 / 156 · 11 q (58 ms) |
| Lead list (25) | 11 / 14 · 3 q | 9 / 11 · 3 q | 12 / 15 · 5 q | 12 / 18 · 4 q |
| Lead list page 2 (sealed cursor) | 11 / 13 | 9 / 15 | 13 / 24 | 14 / 18 |
| Lead search (`q=Rahul`) | 10 / 14 | 11 / 16 | 14 / 23 | 12 / 15 |
| Lead detail | 7 / 9 · 3 q | 11 / 17 | 13 / 24 | 8 / 10 |
| Lead timeline | 10 / 11 · 4 q | 12 / 20 | 14 / 20 | 13 / 17 |
| Pipeline board | 41 / 78 · 8 q | 62 / 79 · 8 q | 66 / 92 · 10 q | 118 / 166 · 9 q (45 ms) |
| Opportunity list (25) | 10 / 13 | 9 / 12 | 19 / 25 | 11 / 22 |
| Activities list (25) | 12 / 15 | 12 / 17 | 16 / 28 | 13 / 17 |
| Global search (`follow quotation`) | 105 / 139 · 8 q (30 ms) | 95 / 109 | 106 / 121 | 75 / 92 |
| Ask Arkray, routed | 29 / 51 · 11 q | 71 / 90 | 54 / 82 | 105 / 139 · 12 q (48 ms; R72) |

Also: sign-in (Argon2) 79 / 117 (14 queries: session, throttle bookkeeping, audit); an
opportunity stage transition (a write: lock, history, timeline, audit, outbox) 29 / 35, 13
queries; the user table 10 / 14, 3 queries; RAG retrieval (embedding, candidate SQL,
re-verification) 34 / 40. Responses are 0.2-84 KB (the organisation board the largest).
Query counts are constant per flow (the N+1 guards in every module's `test_query_counts.py`).

**A just-restored database is slower until it is vacuumed.** The first run found the
heaviest owner's dashboard at 153 / 172 ms: its lead count read 31,630 heap pages (86 ms)
because `leads_lead` and `pipeline_opportunity` had an empty visibility map
(`relallvisible = 0`). The benchmark database had been built by bulk loads ending in
`VACUUM FULL` (which rewrites a table without setting the visibility map) and copied with
`CREATE DATABASE … TEMPLATE` (which resets the statistics that would have triggered
autovacuum). One plain `VACUUM (ANALYZE)` (1.5 s for the leads) restored the index-only
counts: 50 / 90 ms. A restore from backup, a `VACUUM FULL` or a bulk import ends in the same
state: the runbooks run `VACUUM (ANALYZE)` on the large tables afterwards (R55).

**Search's older pass (R59), re-measured on the final implementation:** a word in 190,000
old notes and in none of the 5,000 newest takes 503 ms organisation-wide (434 ms of it the
notes group), 50,000 old matches 195 ms; in a user's workspace 96-147 ms; ordinary searches
23-115 ms. About 2.3 µs per old match: the 10 s statement timeout would be reached around
4 million old matches, far beyond this scale. Operational threshold: search p95 above 1 s or
any search statement timeout (alert) → the documented mitigation, a time-bounded older pass
with an "older records not searched" flag ([search.md](search.md)).

**Trigram indexes under edits (R64), re-measured:** 50,000 note edits took 61.6 s; none was a
HOT update (0 of 50,000: `description` is inside the trigram index), they wrote 3,176 MB of
WAL (66 KB per edit) and grew the note index from 122 MB to 195 MB (+60 %); `VACUUM` doesn't
shrink a GIN index; `REINDEX INDEX CONCURRENTLY activities_note_search_trgm` took 35.8 s
without blocking writes and restored 124 MB. Watch each trigram index's size against its
size after the last rebuild (alert at 1.5 ×) and rebuild concurrently off-peak
([runbooks.md](runbooks.md)).

**Docker shared memory.** PostgreSQL's parallel maintenance and large parallel queries use
dynamic shared memory; Docker's default 64 MB `/dev/shm` made a manual `VACUUM` of the
activities table fail ("could not resize shared memory segment … No space left on device").
Compose now gives the database container `shm_size: 256mb`; any self-run container needs the
same (managed PostgreSQL is unaffected). Autovacuum is never parallel and was not affected.

## Load test (Phase 10)

`tests/performance/load_test.py`: closed-loop virtual users, each signed in through the
real API (CSRF, Argon2, session), looping without pause over a weighted mix: dashboard 15,
lead list 14, lead detail 10, board 9, activities 9, global search 8, lead page two 5, lead
search 5, timelines 5, opportunity list 5, a note 5, routed Ask 3, a stage move 3, semantic
Ask 2, a lead edit 2. One user in six is an admin working organisation-wide. Stack: the
Compose topology (8 gunicorn sync workers, the three Celery workers, beat, PostgreSQL 16
with default settings, Redis) on the 1M-lead benchmark database, on the machine above
(Docker Desktop VM: 24 CPUs, 7.6 GB). These numbers describe this machine and this data, not
a universal capacity.

The per-user limits (600 requests, 120 searches, 20 questions a minute) answer a user with no
pause 429 on every route within seconds: the first run measured the limits, not the server.
The capacity runs raised them through their environment variables (a Compose override, not
committed); a separate check confirmed the defaults answer 429 with `Retry-After`.

| Run (120 s after 20 s warm-up) | Throughput | Errors | p50 / p95, ms (selected) | Indexing backlog |
|---|---|---|---|---|
| 8 users, `ai_index` cap 20 | 131.6 req/s | 0 | lead list 29 / 49, dashboard 55 / 256, board 91 / 228, search 200 / 269 | grew to 616, drained at 4/s |
| 8 users, cap 100 | 127.1 req/s | 0 | lead list 29 / 50, dashboard 55 / 257, board 93 / 235, search 204 / 270 | at most 55 |
| 24 users, cap 100 | 119.8 req/s | 0 | lead list 151 / 253, dashboard 190 / 451, board 230 / 431, search 351 / 476 | at most 51 |
| 24 users, the release candidate (after the whole-software audit) | 119.8 req/s | 0 | lead list 147 / 244, dashboard 181 / 422, board 216 / 396, search 339 / 1,281 (R59: organisation-wide "price" in the older pass) | at most 46 |

Throughput is flat from 8 users on: 8 sync workers serve 8 requests at once and the rest
queue, so latency grows with users while throughput doesn't (an earlier 24-user run, 20
signed in, did 136.7 req/s; with indexing keeping up its ONNX inference shares the same
CPUs). Database connections peaked at 14 of 100. The slowest requests were organisation-wide
(board, dashboard, routed Ask, search); one organisation-wide search for "Mumbai" took 2.6 s
under contention, 0.52 s alone (R59). Scaling beyond: more web processes or pods within the
connection budget above.

Client-side pitfalls found on the way: on Windows, `localhost` first tries IPv6, which Docker
Desktop doesn't forward, and gunicorn's sync workers close every connection, so each request
paid about 2 s in the client (use 127.0.0.1); every virtual user signs in from one address,
which the sign-in limit (20 a minute per address) answers 429, so the script waits as told.

## Failure drills (Phase 10)

Run against the containerised stack on the benchmark database, with a session and the load
test where stated. Ask Arkray's provider was a local stand-in for the Messages API
(pointed to by the SDK's `ANTHROPIC_BASE_URL`, which the review then closed: the base URL is now the `AI_LLM_BASE_URL` setting alone, https in production) switched between failure modes.

| Drill | Result |
|---|---|
| Redis stopped (cache and broker) under 8 users | 0 errors, 103 req/s; readiness `degraded`; CRM writes into the outbox; the found faults are fixed (breaker back-off, bounded metrics probes); recovery: ready 10 s after Redis returned, the 406-event backlog done 50 s later, none dead |
| PostgreSQL stopped | ready 503, live 200; API 503 `service_unavailable` (it was a generic 500: fixed); metrics `arkray_db_up 0` (it was a 500: fixed); recovery within 5-13 s without a restart |
| `kill -9` of the indexing worker under load | 31 events in flight completed within 20 s of the restart; all 1,917 notes indexed exactly once; 0 dead |
| Model provider: 5xx | each question retried once, then answered from retrieval (0.6-2.1 s); the breaker opened after the third; the fourth made no call; the model answered again after the cool-down |
| Model provider: 429 (`Retry-After: 30`) | answered from retrieval in 0.6 s; was retried at once (fixed: now 1 call, no retry); breaker after three |
| Model provider: malformed (an HTML 200) | answered from retrieval in 0.6 s; breaker after three |
| Model provider: no answer (timeout) | answered from retrieval at the 20 s call timeout |
| Model provider: connection reset / refused | answered from retrieval in 0.6 s; breaker after three |
| SIGTERM to the web container under load | requests in flight finished, exit 0 within 1 s, no 5xx |
| Celery workers stopped | warm shutdown 2-3 s, exit 0; outbox complete after restart |

Every Ask Arkray failure ended as an answer from the best-matching records, never an error;
the worker's log lines carried ids, kinds, counts and timings only (no question word
appeared in them). Not drilled live: SMTP (Mailpit, covered by the outage tests) and a full
disk (operating-system level).

## Database review (Phase 10)

- **Sequential scans.** After the load tests, `leads_lead` and `pipeline_opportunity`
  showed thousands of sequential scans. Attributed by resetting their counters and running
  the load with salespeople only: 0 sequential scans on either (or on sessions); with one
  organisation-wide user they returned. They are the organisation-wide dashboard and board
  aggregates over every lead and opportunity, a parallel full scan by design (59 ms and 34
  ms; documented since Phase 5); every per-user and per-workspace path is an index scan.
- **Visibility map.** `leads_lead` has had 1 % autovacuum thresholds since Phase 5. The
  dashboard's activity figures are index-only ranges too, over the newest activities, which
  stay off the visibility map until a vacuum: after 5,947 inserts the organisation's
  "meetings from today" range needed 4,309 heap fetches for 31,263 rows (14 %), while the
  default insert-vacuum waits for 400,000 at 2M activities. Migration `activities.0009` sets
  2 % for `activities_activity`. Opportunities, timeline entries and knowledge chunks keep
  the defaults: nothing reads them index-only.
- **Dead tuples and autovacuum.** Under the load tests autovacuum kept up on the hot small
  tables (outbox events 21 runs, questions 12, sessions 3); dead tuples under 11 % of live
  rows on every table holding rows.
- **Connections.** At most 14 of 100 under 24 users (8 web, 5 worker processes, beat).
- **Slow statements.** The slowest are the organisation-wide aggregates above and search's
  older pass (R59). In production the access log's per-route `duration_ms` and
  `pg_stat_statements` (normalised: literals become placeholders) watch them; not
  `log_min_duration_statement`, which would write statements with their values, personal
  data included, to PostgreSQL's log (R63) ([runbooks.md](runbooks.md)).
