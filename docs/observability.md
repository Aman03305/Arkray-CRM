# Observability

## Structured logging (built)

Every log line is a single ASCII-escaped JSON object on stdout (so U+2028, U+2029 and
other line separators can't split an event) ([`core/logging.py`](../backend/arkray/core/logging.py)):

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
- Django's 4xx duplicates are suppressed (the access log has them); 5xx are logged with
  tracebacks and correlation ids.
- `LOG_FORMAT=console` gives a human-readable variant for local development.

## Correlation end to end

```
browser → X-Request-ID → access log → audit_event.request_id
                                     → core_outbox_event.correlation_id → worker logs
```

A background failure can be traced to the request that caused it. This was verified in
Phase 0: an outbox event inserted with `correlation_id=phase0-smoke-test` produced worker
log lines carrying that id together with the Celery task id.

## Health checks (built)

| Endpoint | Checks | Status codes | Use |
|---|---|---|---|
| `GET /health/live` | the process serves requests | 200 | liveness probe (restart if failing) |
| `GET /health/ready` | PostgreSQL `SELECT 1` (required); cache round trip (optional) | 200 `ok` · 200 `degraded` (cache down) · 503 `unavailable` (DB down) | readiness probe (drain if failing) |

Bodies contain only a status word: no versions, hostnames or error text. Probes are exempt
from the HTTPS redirect. The probe's `Host` must be in `DJANGO_ALLOWED_HOSTS`.

## Metrics (Phase 10)

OpenTelemetry metrics exported to Prometheus-compatible storage:

| Area | Metrics |
|---|---|
| HTTP | request rate, p50/p95/p99 latency and error rate by `route`, status class |
| Database | query duration, pool/connection usage, statement-timeout count |
| Outbox | pending and in-flight count per queue, **age of oldest pending event**, dead count, processing duration and outcome by topic |
| Celery | task duration and failures by task, queue depth |
| Auth | login success and failure rate, lockouts |
| Ask Arkray | questions, latency, tool rounds, tokens and cost, grounding-check failures, breaker state, refusals |
| Cache | breaker trips, hit ratio |

Tests already guard query counts per endpoint (`django_assert_max_num_queries`), so N+1
regressions fail CI before they reach production.

## Tracing (Phase 10)

OpenTelemetry instrumentation for Django, psycopg, Celery, Redis and the Anthropic client's
HTTP layer, with W3C `traceparent` propagation into outbox payloads and Celery headers. The
correlation id is recorded as a span attribute so logs and traces join.

## Error tracking (Phase 10)

Optional Sentry-compatible DSN (`SENTRY_DSN`), with PII scrubbing (no request bodies,
cookies or headers) and release tagging.

## Alerts (Phase 10)

| Alert | Condition |
|---|---|
| Dead outbox events | any new `dead` event |
| Background work stalled | oldest pending outbox event older than 10 min |
| API errors | 5xx rate > 1 % for 5 min |
| Latency | p95 > 1 s for 10 min on CRM routes |
| Readiness | any instance not ready for 2 min; `degraded` for 10 min |
| AI unavailable | breaker open > 15 min |
| Security | login failure spike; lockout spike; `workspace.accessed` volume anomaly |
