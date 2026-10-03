# Deployment

The system ships as two container images (backend, frontend) plus managed PostgreSQL and
Redis. It runs on any container platform (Kubernetes, ECS, Cloud Run + workers, or a VM
with Compose). Phase 11 adds concrete manifests; this document fixes the topology and the
rules those manifests must follow.

## Production topology

```mermaid
flowchart TB
    U([Users]) --> LB[Load balancer / reverse proxy<br/>TLS termination, HSTS, body-size limit]
    LB -->|/api/*, /health/*| API[api: gunicorn<br/>N replicas, stateless]
    LB -->|everything else| WEB[web: Next.js standalone<br/>N replicas, stateless]
    API --> PGB[PgBouncer<br/>transaction pooling]
    WK0[worker-outbox<br/>queue: outbox, concurrency 1] --> PGB
    WK1[worker-default-email<br/>queues: default, email] --> PGB
    WK2[worker-ai<br/>queue: ai] --> PGB
    BEAT[beat<br/>exactly 1 replica] --> BR
    PGB --> PG[(PostgreSQL 16 + pgvector<br/>managed, PITR backups)]
    API --> RC[(Redis cache<br/>allkeys-lru)]
    WK1 & WK2 --> BR[(Redis broker<br/>AOF, noeviction)]
    WK1 --> SMTP[SMTP provider]
    WK2 --> AIP[Anthropic + embeddings APIs]
    API --> AIP
```

| Component | Image / command | Scaling | Probes |
|---|---|---|---|
| api | backend image, default `gunicorn` CMD | horizontal; stateless | live `/health/live`, ready `/health/ready` |
| web | frontend image, `node server.js` | horizontal; stateless | HTTP GET `/` |
| worker-outbox | `celery -A config worker -Q outbox --concurrency 1` | 1-2 replicas; tiny | process liveness |
| worker-default-email | `celery -A config worker -Q default,email` | horizontal | process liveness |
| worker-ai | `celery -A config worker -Q ai --concurrency 2` | horizontal, sized to provider rate limits | process liveness |
| beat | `celery -A config beat` | **exactly one** replica (duplicates cause duplicate relay ticks, harmless but wasteful) | process liveness |
| migrate | `python manage.py migrate --noinput` | one-off job per release | exit code |

The WSGI entry point defaults to production settings, so a deployment that forgets
`DJANGO_SETTINGS_MODULE` never serves with `DEBUG=True`.

- Route `/api` and `/health` to the api service **at the proxy**, so browser traffic never
  hops through Next.js in production. The Next.js `/api` rewrite is for development and the
  local Compose stack only. Next.js passes a client's `X-Forwarded-For` through unchanged
  and doesn't append the real client address, so behind it either every user shares one
  source address for throttling (`TRUSTED_PROXY_COUNT=0`, the Compose setting) or clients
  could choose their own (`TRUSTED_PROXY_COUNT=1`). Never run production that way.
- Django must be reachable **only through the proxy** (network policy). Otherwise a
  client could send its own `X-Forwarded-For` straight to Django.
- **Two Redis instances:** the broker must never evict (`noeviction`, AOF); the cache
  should evict (`allkeys-lru`). Don't share them in production.
- `TRUSTED_PROXY_COUNT` is **required** in production, and startup fails without it. Set it
  to the number of proxies that append `X-Forwarded-For`, and set `FORWARDED_ALLOW_IPS` for
  gunicorn. Client IPs drive rate limits and audit records, so they must be correct and
  unspoofable.
- The edge proxy must **overwrite** (never pass through) `X-Forwarded-Proto` and
  `X-Forwarded-For`. If it also sets `X-Request-ID`, enable `TRUST_INCOMING_REQUEST_ID`.
  Otherwise Django mints its own correlation ids, and client-sent ids are logged only as
  `client_request_id`.
- Production settings refuse to disable secure cookies, the HTTPS redirect or HSTS, or to
  use a non-HTTPS `APP_BASE_URL`, unless `DJANGO_ALLOW_INSECURE_LOCAL_HTTP=true` is set.
  Only the local Compose stack sets it.
- **Don't log the paths `/activate/*` and `/reset-password/*`** at the proxy (or redact
  their last segment). They carry one-time secrets until used. The pages themselves send
  `Referrer-Policy: no-referrer` and `Cache-Control: no-store`, and Django never sees
  them.

## Release process

1. CI builds both images once per commit, runs all gates, and pushes images tagged with the
   commit SHA.
2. Run the **migrate job** with the new image. Migrations follow expand → migrate →
   contract ([database.md](database.md#migrations)), so the running old version keeps
   working.
3. Roll out api, workers and web (rolling update; readiness gates traffic).
4. Beat restarts last.
5. Rollback = redeploy the previous image. Because migrations are backward compatible, no
   down-migration is needed during the release window.

## Configuration

All configuration is environment variables; secrets come from the platform's secret
manager ([security.md](security.md#secrets-management)).

| Variable | Required | Notes |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | yes | `config.settings.production` (image default) |
| `DJANGO_SECRET_KEY` | yes | ≥ 50 chars, production-only; startup fails otherwise |
| `DJANGO_ALLOWED_HOSTS` | yes | public host(s) **and** the probe host |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | yes | `https://crm.example.com` |
| `APP_BASE_URL` | **yes** | public web URL used in emailed links, for example `https://crm.example.com`; startup fails if unset or not `https://` |
| `SESSION_COOKIE_AGE_S`, `SESSION_IDLE_TIMEOUT_S` | no | 12 h absolute, 2 h idle |
| `ACCOUNT_INVITATION_TTL_S`, `PASSWORD_RESET_TTL_S` | no | 72 h, 1 h |
| `DATABASE_URL` | yes | points at PgBouncer in production |
| `DB_STATEMENT_TIMEOUT_MS` | no | 10000 web; set 60000 for workers |
| `DB_CONN_MAX_AGE_S` / `DB_POOL_*` | no | see [reliability.md](reliability.md#connection-budget) |
| `CELERY_BROKER_URL` | yes | broker Redis (`rediss://` for TLS) |
| `REDIS_CACHE_URL` | yes | cache Redis |
| `EMAIL_URL`, `DEFAULT_FROM_EMAIL` | **yes** | for example `smtp+tls://user:pass@smtp.example.com:587`; startup fails if `EMAIL_URL` is unset or names a console, file, in-memory or dummy backend, because those would put one-time account links in logs or on disk, or drop them |
| `TRUSTED_PROXY_COUNT` | **yes** | proxy hops appending `X-Forwarded-For`; startup fails if unset |
| `FORWARDED_ALLOW_IPS` | yes | gunicorn's trusted proxy addresses |
| `TRUST_INCOMING_REQUEST_ID` | no | `true` only if the edge proxy sets or overwrites `X-Request-ID` |
| `DJANGO_ALLOW_INSECURE_LOCAL_HTTP` | no | never in real deployments; local Compose only |
| `CRM_TIME_ZONE`, `CRM_CURRENCY` | no | `Asia/Kolkata`, `INR` |
| `DJANGO_HSTS_SECONDS`, `DJANGO_SECURE_SSL_REDIRECT` | no | secure defaults |
| `LOG_LEVEL`, `LOG_FORMAT` | no | `INFO`, `json` |
| `AI_ENABLED`, `ANTHROPIC_API_KEY`, `AI_CHAT_MODEL`, `EMBEDDINGS_API_KEY` | Phase 8 | AI off unless set |
| `API_ORIGIN` (frontend build arg) | yes for images | where Next.js proxies `/api` |

## Database

- Managed PostgreSQL 16 with the `vector` extension available, point-in-time recovery, and
  daily snapshots retained for 30 days.
- Two roles: `arkray_owner` for migrations and `arkray_app` for runtime, with SELECT and
  INSERT only on append-only tables
  ([security.md](security.md#database-privileges)).
- PgBouncer in transaction mode once replicas × workers approaches the connection budget;
  set `DISABLE_SERVER_SIDE_CURSORS=True` when it is in place.
- Targets (to confirm with the business in Phase 11): **RPO ≤ 5 min** (PITR), **RTO ≤ 1 h**.
  A restore drill is part of Phase 11.

## Operations

- **Dead outbox events** are investigated from logs (`outbox_event_dead`, with id, topic,
  reason) and re-queued with a management command (Phase 10) once the cause is fixed.
- **Scheduled jobs** (beat): outbox relay (5 s); `identity.housekeeping` (hourly: expired
  sessions and throttle evidence older than 24 h). Later phases add outbox housekeeping
  (daily) and AI index reconciliation (nightly).
- **First administrator:** `manage.py createsuperuser` (asks for email, first and last name,
  password). Everyone else is invited from the Users page.
- **Invitation emails that went dead** (for example, SMTP down for over an hour) are
  re-sent by an admin with *Resend invitation*.
- **Capacity:** start with 2 api replicas × `WEB_CONCURRENCY` 4, 1 default/email worker, 1 ai
  worker, 1 beat. Scale api on p95 latency and CPU, and workers on oldest-pending age.
