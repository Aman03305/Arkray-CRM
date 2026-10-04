# Observability

## Structured logging (built)

Every line the application writes is a single ASCII-escaped JSON object on stdout (so
U+2028, U+2029 and other line separators can't split an event)
([`core/logging.py`](../backend/arkray/core/logging.py)). Not quite every line in the
containers: gunicorn's own error lines (`WORKER TIMEOUT`, a killed worker), Celery's start
banner and onnxruntime's warnings are plain text (R89; the whole-software audit counted 11
of 73 backend lines). A database error is logged by type, `db_sqlstate` and
`db_constraint`, never its message: PostgreSQL's quotes the failing row (whole-software
audit, R63):

```json
{"ts": "2026-09-30T02:38:20.798+00:00", "level": "INFO", "logger": "arkray.access",
 "msg": "http_request", "method": "GET", "path": "/api/v1/workspaces/me/leads",
 "route": "api/v1/workspaces/<str:workspace>/leads", "status": 200, "duration_ms": 18.2,
 "user_id": "6f0c…", "correlation_id": "0d531bcf…", "client_ip": "203.0.113.9",
 "subject_user_id": "3f2b…"}
```

| Field | Source |
|---|---|
| `correlation_id` | minted by Django per request, or adopted from `X-Request-ID` only when `TRUST_INCOMING_REQUEST_ID` is set (because a trusted edge proxy writes it); echoed in the response header, stored in audit events and outbox rows, and included in every error envelope |
| `client_request_id` | a well-formed client-sent `X-Request-ID` that is not trusted; logged for support, never used in audit |
| `user_id` | authenticated user's UUID (never email or name) |
| `subject_user_id` | set when an admin works in another user's workspace |
| `route`, `method`, `status`, `duration_ms` | one access-log line per request (`arkray.access`) |
| `task_id`, `task_name` | Celery signals bind them for every task |
| `outbox_event_id`, `topic`, `attempt` | outbox processing |
| `exc_type`, `exc` | exceptions (the traceback is one escaped JSON string) |

Conventions:
- Event names are stable snake_case keys (`http_request`, `outbox_event_dead`,
  `readiness_degraded`) so they can be alerted on; details go in fields, not in the message.
- **Never logged:** request or response bodies, headers, cookies, query strings (they carry
  search terms), passwords, tokens, API keys, CRM free text, emails, phone numbers.
  Third-party loggers that would log request bodies at DEBUG (`anthropic`, `httpx`, `httpx2`,
  `httpcore`, `urllib3`) are pinned to WARNING whatever `LOG_LEVEL` says, and production
  refuses `ANTHROPIC_LOG` (Phase 9). Unexpected AI failures are logged by exception type only
  (`ai_answer_failed`, `ai_provider_bad_response`): an exception's text can quote model or
  CRM text.
- Health-probe access lines are DEBUG (probes run every few seconds).
- Identity events (Phase 1): `login_failed`, `login_throttled` (fields `login` = 12-char
  prefix of the identifier HMAC, never the email; `reason`), `password_reset_suppressed`
  (per-account cap), `account_link_sent` / `account_link_delivery_skipped`
  (`account_token_id`, `purpose`; never the secret), `identity_housekeeping`, and
  `csrf_rejected` (logger `django.security.csrf`, with Django's reason). The log context's
  `user_id` is set only after DRF authentication succeeds, so a revoked session never
  labels log lines. Security
  events are also written to the append-only audit trail: `auth.login`, `auth.logout`,
  `auth.login_throttled`, `auth.password_changed`, `auth.password_reset_issued`,
  `auth.password_reset_completed`, `auth.password_reset_suppressed` (per-account cap;
  reset events carry `requested_from`), `user.created`, `user.invitation_created`,
  `user.invitation_resent`, `user.activated`, `user.deactivated`, `user.reactivated`,
  `user.role_changed`, `user.profile_updated`, `user.email_changed`, `workspace.accessed`.
- Ask Arkray events (Phase 8; ids, kinds, counts and timings only, never question,
  answer or CRM text, tested with planted secrets): `ai_question_answered`
  (`question_id`, `mode`, `tools`, `records`, `outcome`, `workspace_kind`, `latency_ms`),
  `ai_question_skipped`, `ai_question_refused`, `ai_dispatch_failed`, `ai_dispatch_skipped`
  (the hand-off breaker is open), `ai_provider_failed` (`kind`), `ai_provider_rejected`
  (`status`: 4xx other than 429), `ai_provider_configured` (once per process: the provider's
  host and the model, never the key), `ai_breaker_opened` (ERROR),
  `ai_grounding_failed` (`unsupported_numbers`: a count), `ai_tool_failed` (`tool`),
  `ai_retrieval_unavailable`, `ai_reconciliation_finished`, `embedding_model_loaded`. Audit:
  `ai.question` per answered question (actor, workspace, subject when delegated; metadata:
  question id, mode, tool names, record count, outcome).
- Django's 4xx duplicates are suppressed (the access log has them); 5xx are logged with
  tracebacks and correlation ids. A database outage is answered 503 `service_unavailable`
  (Phase 10), so an outage and a bug are different status codes in the access log.
- Dependency breakers (Phase 10): `cache_circuit_opened` and `ai_dispatch_circuit_opened`
  (WARNING, `cooldown_s`), one line per process each time the breaker opens; the cool-down
  doubles while the outage lasts (cache 15 s to 120 s, Ask Arkray's hand-off 30 s to 120 s).
  `metrics_broker_unavailable` when a scrape can't reach the broker,
  `metrics_ai_breaker_unknown` when it can't read the provider breaker, `metrics_gauge_failed`
  (`gauge`, `exc_type`) when one gauge's query fails without the database being down,
  `metrics_token_rejected` when a request offers a wrong token (never its value). Every
  model call logs `ai_model_call`: `outcome` `ok` (latency, stop reason, tokens, the
  provider's request id) or `failed` (`kind`, latency); a call's latency includes its retry.
- Operations (Phase 10): `outbox_events_requeued` (counts and ids) from
  `manage.py outbox_requeue`.
- Startup (Phase 11): `database_role_refused` (CRITICAL, a worker on a privileged database
  role, then exit), `database_role_privileged` (the same, warned where the check is off),
  `database_role_unchecked` (the database was unreachable at start),
  `database_logs_failed_statements` (R63: PostgreSQL would log statements' values);
  `ai_provider_configured` (the provider's host). Audit: `lead.erased` (the operator, the
  lead's id, counts).
- `LOG_FORMAT=console` gives a human-readable variant for local development.

## Correlation end to end (built)

```
browser → X-Request-ID → access log → audit_event.request_id
                                     → core_outbox_event.correlation_id → worker logs
                       → Ask Arkray question → ai worker (task kwarg) → ai_model_call lines
                                                                       (+ the provider's request id)
```

A background failure can be traced to the request that caused it: the outbox stores the
request's id with each event and the worker binds it for every line it logs (verified in
Phase 0). Since Phase 10 an Ask Arkray question carries the asking request's id into the ai
worker too (it is published directly, not through the outbox), and every model call logs
`ai_model_call` with its latency, stop reason, token usage and the provider's own request id,
the handle to give the provider's support; never any content
(`tests/integration/test_tracing.py`). Ids only: no payload, question, note or answer text is
ever copied into a log line or a trace.

## Health checks (built)

| Endpoint | Checks | Status codes | Use |
|---|---|---|---|
| `GET /health/live` | the process serves requests | 200 | liveness probe (restart if failing) |
| `GET /health/ready` | PostgreSQL `SELECT 1` (required); cache round trip (optional) | 200 `ok` · 200 `degraded` (cache down) · 503 `unavailable` (DB down) | readiness probe (drain if failing) |

Bodies contain only a status word: no versions, hostnames or error text. Probes are exempt
from the HTTPS redirect. The probe's `Host` must be in `DJANGO_ALLOWED_HOSTS`.

## Metrics (built, Phase 10)

Two sources, both low-cardinality (routes, statuses, queues, never a user, record or text):

**From the access log** (one structured `http_request` line per request: `route`, `status`,
`duration_ms`, `user_id`, `subject_user_id`, `correlation_id`): request rate, latency percentiles
and the 5xx rate per route, as log-based metrics in the log platform. Gunicorn runs several
processes, so in-process counters would each hold a fraction; the log line is the single
source. Worker lines (`outbox_event_retry_scheduled`, `outbox_event_dead`, `outbox_dispatch_failed`,
`ai_question_answered`, `ai_model_call`) give failures, AI latency and token usage the same way.

**From `GET /health/metrics`** (Prometheus text format; off unless `METRICS_TOKEN` is set,
then only for `Authorization: Bearer <token>`; 404 otherwise, like an unknown URL; computed
at scrape time in a bounded number of queries, `config/metrics.py`):

| Metric | Labels | Meaning |
|---|---|---|
| `arkray_outbox_events` | `queue`, `status` (pending, in_flight, dead) | background work waiting, running, failed for good |
| `arkray_outbox_oldest_due_seconds` | `queue` | age of the oldest due pending event (backed-off events don't count) |
| `arkray_outbox_oldest_undelivered_seconds` | `queue` | how long the oldest event handed to the broker has waited without a worker starting it: a queue whose workers are down (whole-software audit: such events sat in flight, invisible to the other figures) |
| `arkray_db_up` | — | the database answered this scrape (0: the database-backed gauges are left out, the rest still reported) |
| `arkray_db_connections` | `state` | this database's connections (pg_stat_activity) |
| `arkray_db_server_connections` | — | client connections to the whole server, every database and state (idle ones hold slots too) |
| `arkray_db_max_connections` | — | the server's limit |
| `arkray_broker_up`, `arkray_broker_queue_length` | `queue` | the broker answered; messages waiting per Celery queue |
| `arkray_cache_up` | — | the cache answered |
| `arkray_ai_questions_last_hour` | `status`, `mode`, `error` | Ask Arkray outcomes (router, llm, retrieval; timeout, ai_unavailable, ...) |
| `arkray_ai_questions_pending` | — | questions waiting for an ai worker and not yet expired |
| `arkray_ai_breaker_open` | — | the model provider's circuit breaker (its state is shared through the cache: no sample while the cache can't answer) |

The embedding backlog is `arkray_outbox_events{queue="ai_index",status="pending"}`. Scrape
it from inside the deployment (the proxy needn't route `/health/*`); the token keeps it off
the public internet even if it does. The scrape keeps answering during the outages it reports
(Phase 10 drills): with PostgreSQL down it reports `arkray_db_up 0` instead of failing, and
its Redis probes have a 2 s deadline (a stopped Redis stalled each connection attempt about
4 s in DNS) after which the broker or cache is reported down.

Not built (deployment options): OpenTelemetry metrics and traces (the correlation ids above
already join logs across HTTP, outbox, workers and the AI provider), and a Sentry-compatible
error tracker. Unhandled errors are logged as `django.request` ERROR lines with the traceback
and request id; the response to the client is a generic JSON 500 with the request id.

## Alerts (Phase 10)

The metric-based rows are shipped as Prometheus rules in `infrastructure/alerts/arkray.rules.yml` (whole-software audit; each names exported metrics and links its runbook, tested); the log-based ones belong in the log platform.

| Alert | Condition (source) | First response |
|---|---|---|
| API errors | 5xx > 1 % of requests for 5 min (access log) | [runbooks.md](runbooks.md): errors spike |
| Latency | p95 > 1 s for 10 min on CRM routes; search p95 > 1 s (R59) (access log) | runbooks: slow requests, search |
| Readiness | an instance not ready for 2 min; `degraded` for 10 min (probe) | runbooks: database down, Redis down |
| Database unreachable | `arkray_db_up` = 0, or 503 `service_unavailable` responses, for 1 min | runbooks: database down |
| Dependency breakers | `cache_circuit_opened` or `ai_dispatch_circuit_opened` repeating for 10 min; `arkray_cache_up` or `arkray_broker_up` = 0 | runbooks: Redis down |
| Database saturation | `arkray_db_server_connections` > 80 % of `arkray_db_max_connections` for 5 min (idle connections included: persistent connections hold their slots) | runbooks: connection budget |
| Background work stalled | `arkray_outbox_oldest_due_seconds` > 600 on any queue | runbooks: outbox backlog |
| Work taken by no worker | `arkray_outbox_oldest_undelivered_seconds` > 300 on any queue (whole-software audit) | runbooks: workers |
| Dead events | any increase of `arkray_outbox_events{status="dead"}` | runbooks: dead events |
| Queue backlog | `arkray_broker_queue_length` growing for 15 min | runbooks: workers |
| Worker failures | `outbox_event_retry_scheduled` / `outbox_event_dead` rate above baseline; a worker absent from the broker (log platform, process supervisor) | runbooks: workers |
| AI failure rate | `arkray_ai_breaker_open` = 1 for 15 min, or `ai_unavailable` + `timeout` > 20 % of questions in an hour | runbooks: Ask Arkray degraded |
| RAG indexing backlog | `arkray_outbox_events{queue="ai_index",status="pending"}` > 1,000 for 30 min, or its oldest due > 1 h | runbooks: re-indexing |
| Index health (R64) | a trigram index > 1.5 × its size after the last rebuild (weekly check) | runbooks: index bloat |
| Security | login failure spike; lockout spike; `workspace.accessed` volume anomaly (audit) | runbooks: security events |
