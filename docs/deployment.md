# Deployment

The system ships as two container images (backend, frontend) plus managed PostgreSQL and
Redis. It runs on any container platform (Kubernetes, ECS, Cloud Run + workers, or a VM
with Compose). This document fixes the topology and the rules any manifests must follow;
The go-live and per-release list is [production-checklist.md](production-checklist.md).
`infrastructure/` holds the reference pieces, verified together as a production-shaped stack
(Phase 11): the edge proxy (`infrastructure/nginx/arkray.conf`), the database and roles
(`infrastructure/postgres/roles.sql`) and a Compose overlay that runs the whole release the way
production must (`infrastructure/compose.production.yml`, [below](#verifying-a-release)).

## Production topology

```mermaid
flowchart TB
    U([Users]) --> LB[Load balancer / reverse proxy<br/>TLS termination, HSTS, body-size limit]
    LB -->|/api/*, /health/*| API[api: gunicorn<br/>N replicas, stateless]
    LB -->|everything else| WEB[web: Next.js standalone<br/>N replicas, stateless]
    API --> PGB[PgBouncer<br/>transaction pooling]
    WK0[worker-outbox<br/>queue: outbox, concurrency 1] --> PGB
    WK1[worker-default-email<br/>queues: default, email] --> PGB
    WK3[worker-index<br/>queue: ai_index] --> PGB
    WK2[worker-ai<br/>queue: ai] --> PGB
    BEAT[beat<br/>exactly 1 replica] --> BR
    PGB --> PG[(PostgreSQL 16 + pgvector<br/>managed, PITR backups)]
    API --> RC[(Redis cache<br/>allkeys-lru)]
    WK1 & WK2 & WK3 --> BR[(Redis broker<br/>AOF, noeviction)]
    WK1 --> SMTP[SMTP provider]
    WK2 -->|optional| AIP[Anthropic API]
```

| Component | Image / command | Scaling | Probes |
|---|---|---|---|
| api | backend image, default `gunicorn` CMD | horizontal; stateless | live: TCP on the port (an HTTP probe shares the request workers, which a stalled database pins: [reliability.md](reliability.md#failure-and-degradation-matrix)), or `/health/live` with more than 35 s of tolerance; ready `/health/ready` |
| web | frontend image, `node server.js` | horizontal; stateless | HTTP GET `/login` |
| worker-outbox | `celery -A config worker -Q outbox --concurrency 1` | 1-2 replicas; tiny | process liveness |
| worker-default-email | `celery -A config worker -Q default,email` | horizontal | process liveness |
| worker-index | `celery -A config worker -Q ai_index --concurrency 1` (Ask Arkray's indexing: CPU-bound embedding, loads the model) | 1 replica normally; scale out temporarily for a large backfill | process liveness |
| worker-ai | `celery -A config worker -Q ai --concurrency 2` (Ask Arkray's questions only; loads the embedding model, ~0.5 GB RAM per process) | horizontal, sized to question volume and the provider's rate limits | process liveness |
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
  should evict (`allkeys-lru`). Don't share them in production. Both need a password
  (AUTH or an ACL user) and, across hosts, TLS (`rediss://`): production refuses Redis and
  broker URLs without a password (Phase 9). The cache never unpickles values, and nothing
  security-relevant lives only in it.
- **One origin for the site and the API.** Over HTTPS the session and CSRF cookies are
  `__Host-` cookies: host-only, `Path=/`, no `Domain`. Serve `/api` and the pages from the
  same host (the edge proxy routes `/api` to Django); moving the API to a sibling host
  (`api.crm.example.com`) would break sign-in by design. Renaming the cookies signs
  everyone out once, at the first deploy that has them.
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

## Reverse proxy

`infrastructure/nginx/arkray.conf` is the reference edge (nginx 1.27.3 or newer). What the
application depends on, whatever proxy or load balancer is used:

- **TLS terminates here**; plain HTTP only redirects; a request for any other host name
  ends at the TLS handshake. HSTS is sent once, for pages and API, without
  `includeSubDomains` (add it, and preload, only when every subdomain is HTTPS).
- **The error log keeps critical events only**: nginx writes the whole request line, query
  string and one-time link included, into every error it logs (Phase 11 review: an
  oversized request to an invitation link put its token there). Upstream failures still
  show in the access log as 502s.
- **`X-Forwarded-For` and `X-Forwarded-Proto` are overwritten**, never passed through: the
  client address drives rate limits and the audit trail. Set `TRUSTED_PROXY_COUNT` to the
  number of proxies that append it (1 here) and gunicorn's `FORWARDED_ALLOW_IPS` to their
  addresses. Verified: a client's `X-Forwarded-For: 6.6.6.6` never reaches a log.
- **`X-Request-ID` is minted here** (set `TRUST_INCOMING_REQUEST_ID=true`): the same id is
  in the proxy's log line and in every application line for the request.
- **No query strings, one-time links or Referer in the access log** (R61): searches travel
  in query strings, invitation and reset links carry secrets until used. The reference log
  format writes the path only and redacts `/activate/*` and `/reset-password/*`. Verified:
  a search term and a reset token appear in no log.
- **`/api/` goes to Django directly** (matched case-insensitively), everything else to
  Next.js; one origin, so the
  `__Host-` cookies work ([below](#production-topology)). **`/health/*` is not routed**:
  probes and the metrics scraper reach the pods directly.
- **Re-resolve upstream names** while running (the `resolve` parameter, Docker's resolver
  here): a replaced container gets a new address, and an address resolved once at start
  turned every API request into a 502 after a redeploy (Phase 11). With a 10 s resolver
  validity, a backend that moves is followed within seconds.
- Request bodies are JSON only: 1 MB at the proxy (Django refuses more than 2.5 MB anyway);
  the proxy's read timeout (35 s) is longer than gunicorn's (30 s).

## Release process

1. CI builds both images once per commit, runs all gates, and pushes images tagged with the
   commit SHA.
2. **Once per environment**, a database administrator runs `infrastructure/postgres/roles.sql`
   (the database, `arkray_owner`, `arkray_app`, the extensions, R63's logging settings).
3. Run the **migrate job** with the new image, as `arkray_owner`:
   `manage.py migrate --noinput && manage.py grant_app_privileges arkray_app`. Migrations
   follow expand → migrate → contract ([database.md](database.md#migrations)), so the
   running old version keeps working; the grant gives new tables to the application role.
4. **Check the release** with the application's credentials:
   `manage.py check --deploy --database default`. It fails on a runtime role that could
   rewrite the audit trail (R74) and warns when PostgreSQL would log failing statements or rows (R63).
   Django's HSTS notes (`security.W005`, `security.W021`) are policy choices: include
   subdomains and preload only when every subdomain is HTTPS.
5. Roll out api, workers and web (rolling update; readiness gates traffic). The web server
   and the workers check their database role at start and refuse to run as a superuser or a
   role that owns the append-only tables (`DB_REQUIRE_RESTRICTED_ROLE`, on in production).
6. Beat restarts last.
7. Rollback = redeploy the previous image. Because migrations are backward compatible, no
   down-migration is needed during the release window.

## Verifying a release

`infrastructure/compose.production.yml` runs the images exactly as production must, on one machine:
HTTPS at the reference proxy (a self-signed certificate), secure cookies and HSTS, both
database roles (migrations as the owner, everything else as `arkray_app`), Redis with a
password, read-only containers, no insecure opt-in, only the proxy published:

```
export DJANGO_SECRET_KEY=... POSTGRES_PASSWORD=... ARKRAY_OWNER_PASSWORD=... \
       ARKRAY_APP_PASSWORD=... REDIS_PASSWORD=... METRICS_TOKEN=...
docker compose -p arkray-prod -f docker-compose.yml -f infrastructure/compose.production.yml \
    --profile app up -d --build
```

What it doesn't reproduce (whole-software audit): one Redis serves as both broker and
cache (production: two instances, the cache evicting), and one worker consumes `outbox`,
`default` and `email` (production may split them). Drills of those topologies need the
production manifests. Its settings are pinned in the overlay, never taken from the
repository's `.env` (tested: a developer's `AI_ENABLED=false` there once switched Ask
Arkray off on a backend recreated alone).

Phase 11 ran the release on it ([testing.md](testing.md#what-exists-after-phase-11)): sign-in
and every page over HTTPS at desktop, tablet and phone widths (nonce CSP, HSTS, `__Host-`
cookies, no horizontal overflow), a lead created through the UI, a note indexed and
answered by Ask Arkray in 5.4 s, the database role refused for the owner (web server and
worker), the proxy's logging and forwarding rules, and zero error lines in every container.

## Configuration

All configuration is environment variables; secrets come from the platform's secret
manager ([security.md](security.md#secrets-management)). Every variable the backend reads is
listed here by name (`tests/architecture/test_configuration_documented.py` fails otherwise).
Production settings refuse to start on the unsafe values named below, so a misconfiguration
is a failed deploy, not a weakened service.

**Core and HTTP**

| Variable | Required | Default and notes |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | yes | `config.settings.production` (the image's default) |
| `DJANGO_SECRET_KEY` | yes | at least 50 characters, production-only; startup fails otherwise. Rotation: [runbooks.md](runbooks.md#rotate-a-secret) |
| `DJANGO_SECRET_KEY_FALLBACKS` | no | comma-separated earlier keys, during a rotation only: sessions, sealed page links and trusted-browser cookies signed with them keep working until they're removed; each must be as strong as the key |
| `DJANGO_ALLOWED_HOSTS` | yes | the public host(s) **and** the probes' host |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | yes | `https://crm.example.com` |
| `APP_BASE_URL` | **yes** | the public web URL for emailed links, `https://crm.example.com`; startup fails if unset or not `https://` |
| `DJANGO_SECURE_COOKIES`, `DJANGO_SECURE_SSL_REDIRECT` | no | `true`; turning either off needs `DJANGO_ALLOW_INSECURE_LOCAL_HTTP` |
| `DJANGO_HSTS_SECONDS`, `DJANGO_HSTS_INCLUDE_SUBDOMAINS` | no | one year, `false` (the edge proxy also sends HSTS, [below](#reverse-proxy)) |
| `DJANGO_ALLOW_INSECURE_LOCAL_HTTP` | no | **never in a real deployment**: the local development stack's opt-in for plain HTTP, Redis without a password and the like |
| `TRUSTED_PROXY_COUNT` | **yes** | proxies appending `X-Forwarded-For` (1 behind [the reference proxy](#reverse-proxy)); startup fails if unset |
| `FORWARDED_ALLOW_IPS` | yes | gunicorn's trusted proxy addresses |
| `TRUST_INCOMING_REQUEST_ID` | no | `false`; `true` only when the edge proxy sets or overwrites `X-Request-ID` |
| `PORT`, `WEB_CONCURRENCY`, `GUNICORN_TIMEOUT_S` | no | 8000; 2 × CPUs + 1 (at most 8); 30 s |
| `SESSION_COOKIE_AGE_S`, `SESSION_IDLE_TIMEOUT_S` | no | 12 h absolute, 2 h idle |
| `ACCOUNT_INVITATION_TTL_S`, `PASSWORD_RESET_TTL_S` | no | 72 h, 1 h |
| `WORKSPACE_ACCESS_AUDIT_WINDOW_S` | no | 15 min: one `workspace.accessed` audit row per administrator and workspace per window |
| `SUPPORT_SESSION_TTL_S` | no | 30 min: how long an administrator's support session lasts (never extended; [admin-user-workspace.md](admin-user-workspace.md#support-sessions)) |
| `TEMPORARY_PASSWORD_TTL_S` | no | 72 h: how long an administrator-chosen password (a new user's initial one, or a reset) signs in before the user must have changed it ([authorization.md](authorization.md#admin-set-passwords)) |

The business time zone (Asia/Kolkata: "today", due dates, every displayed time) and the
currency (INR) are fixed, not configuration: the web app formats in them, so a different
value on the server alone made the API and the screens disagree (whole-software audit;
`tests/architecture/test_business_constants.py` keeps both sides in step).

**Database**

| Variable | Required | Default and notes |
|---|---|---|
| `DATABASE_URL` | yes | the **application role** (`arkray_app`) for web and workers, the owner (`arkray_owner`) for the migrate job ([Database](#database)); PgBouncer's address when it's in place |
| `DB_REQUIRE_RESTRICTED_ROLE` | no | `true` in production: the web server and workers refuse to start as a superuser or as a role that could rewrite the audit trail (R74) |
| `DB_CONNECT_TIMEOUT_S` | no | 5 |
| `DB_STATEMENT_TIMEOUT_MS` | no | 10,000 (web); set 60,000 for workers |
| `DB_LOCK_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | no | 5,000; 60,000 |
| `DB_CONN_MAX_AGE_S` | no | 60 (persistent connections; ignored with the pool) |
| `DB_POOL_ENABLED`, `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_TIMEOUT_S` | no | `false`, 1, 4, 5: a per-process psycopg pool, for threaded servers ([reliability.md](reliability.md#connection-budget)) |

**Redis, background work and email**

| Variable | Required | Default and notes |
|---|---|---|
| `CELERY_BROKER_URL` | yes | the broker Redis, with a password (`rediss://:password@host:6380/0`); startup fails without one |
| `REDIS_CACHE_URL` | yes | the cache Redis (a separate instance in production), with a password |
| `OUTBOX_DONE_RETENTION_DAYS` | no | 7: finished background work kept for investigation, then purged hourly |
| `EMAIL_URL` | **yes** | `smtp+tls://user:pass@smtp.example.com:587`; startup fails if unset or a console, file, in-memory or dummy backend (they would put one-time account links in logs or on disk, or drop them) |
| `DEFAULT_FROM_EMAIL` | **yes** | the sender, on a domain with SPF, DKIM and DMARC ([Email](#email)) |
| `EMAIL_TIMEOUT_S` | no | 10 |

### Attachments

Files on notes ([activities.md](activities.md#attachments)). The bytes are **never** in
PostgreSQL: a database backup is not a backup of the files ([below](#attachments-storage)).

| Variable | Required | Default and notes |
|---|---|---|
| `ATTACHMENT_STORAGE` | **yes** in production | `filesystem` (a private directory, development) or `s3` (private S3-compatible object storage) |
| `ATTACHMENT_ROOT` | with `filesystem` | `backend/var/attachments`; a private volume never served by a web server |
| `ATTACHMENT_S3_BUCKET` | with `s3` | a **private** bucket (block all public access); objects are written `private`, server-side encrypted, never overwritten |
| `ATTACHMENT_S3_ENDPOINT_URL`, `ATTACHMENT_S3_REGION` | no | for S3-compatible stores (MinIO, R2, ...) |
| `ATTACHMENT_S3_ACCESS_KEY_ID`, `ATTACHMENT_S3_SECRET_ACCESS_KEY` | no | prefer an instance/workload role; keys, if used, may only read, write and delete in the bucket's prefix |
| `ATTACHMENT_S3_PREFIX` | no | `attachments` |
| `ATTACHMENT_MAX_BYTES` | no | 10 MB per file; the proxy's body limit on the upload route must be a little above it ([below](#reverse-proxy)) |
| `ATTACHMENT_MAX_PER_NOTE` | no | 10 |
| `ATTACHMENT_ALLOWED_EXTENSIONS` | no | `pdf,png,jpg,jpeg,webp,docx,xlsx,csv,txt`; only from the catalog the server can recognise by content (adds: `gif`, `pptx`); anything else fails startup |
| `ATTACHMENT_SCANNER` | **recommended** | empty (no scanning: files are "not scanned" and downloadable) or `clamd://host:3310` (a ClamAV daemon: files stay "being checked" until clean; infected ones are blocked and deleted) |

**Rate limits** (per user, or per client address for anonymous and sign-in requests)

| Variable | Default | Notes |
|---|---|---|
| `API_THROTTLE_USER` | `600/min` | every signed-in request |
| `API_THROTTLE_ANON` | `60/min` | requests without a session |
| `API_THROTTLE_AUTH` | `20/min` | sign-in, password change and reset requests, **per client address**. Everyone in an office behind one NAT shares it (R77): size it to the number of people who sign in within a minute from one address (60/min for an office of 50 is reasonable). Guessing stays bounded whatever the value: 5 failures per account and browser, and 50 per address, in 15 minutes ([authorization.md](authorization.md#sign-in-throttling)) |
| `API_THROTTLE_SEARCH` | `120/min` | global and list search |
| `API_THROTTLE_ASK` | `20/min` | Ask Arkray questions |

The format is `<count>/<period>` with a period of `s`, `min`, `h` or `day`; production
refuses anything else at startup (DRF would otherwise fail on the first request).

**Observability**

| Variable | Default | Notes |
|---|---|---|
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `json` | `console` only for development |
| `METRICS_TOKEN` | empty | enables `GET /health/metrics` for `Authorization: Bearer <token>`; at least 32 characters (production refuses shorter); empty: no endpoint ([observability.md](observability.md#metrics-built-phase-10)) |

**Ask Arkray** ([rag-architecture.md](rag-architecture.md), [AI configuration](#ai-configuration))

| Variable | Default | Notes |
|---|---|---|
| `AI_ENABLED` | `false` | `false`: Ask Arkray hidden and nothing indexed; also the kill switch |
| `AI_INDEXING_ENABLED` | `AI_ENABLED` | index notes without offering questions yet (a backfill before launch) |
| `AI_LLM_PROVIDER` | `none` | `none`: routed answers and matching records, no CRM text leaves the deployment; `anthropic`: a model writes the answers (R68); anything else fails startup |
| `ANTHROPIC_API_KEY` | empty | **the ai worker only** (`AI_LLM_KEY_HOLDER=true`); a key holder without a key fails startup |
| `AI_LLM_KEY_HOLDER` | `true` | `false` for the web tier and the other workers, which get no key |
| `AI_LLM_BASE_URL` | `https://api.anthropic.com` | where model calls go: an https gateway if one is used; production refuses plain http; logged once per process (host only) |
| `ANTHROPIC_LOG`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS` | — | **never**: production refuses to start with any of them (whole requests in logs; calls and the key sent elsewhere) |
| `AI_CHAT_MODEL`, `AI_CHAT_EFFORT`, `AI_CHAT_MAX_TOKENS` | `claude-opus-5-5`, `low`, 8,000 | the model and how it answers |
| `AI_LLM_TIMEOUT_S`, `AI_LLM_MAX_RETRIES`, `AI_QUESTION_BUDGET_S` | 20 s, 1, 30 s | per call, retries, per question ([reliability.md](reliability.md#retries)) |
| `AI_MAX_TOOL_ROUNDS`, `AI_MAX_TOOL_CALLS_PER_ROUND` | 4, 6 | the model's tool use |
| `AI_BREAKER_FAILURES`, `AI_BREAKER_COOLDOWN_S` | 3, 60 s | the provider's circuit breaker |
| `AI_CONTEXT_MAX_CHARS`, `AI_TOOL_RESULTS_MAX_CHARS`, `AI_HISTORY_TURNS` | 12,000, 40,000, 4 | what a question may send (data minimisation, R68) |
| `AI_RETRIEVAL_TOP_K`, `AI_RETRIEVAL_MIN_SIMILARITY` | 8, 0.55 | note search |
| `AI_QUESTION_TIMEOUT_S` | 90 s | a question nobody answers fails `timeout` |
| `AI_MAX_PENDING_PER_USER`, `AI_MAX_PENDING_TOTAL` | 2, 40 | bulkheads |
| `AI_CONVERSATION_RETENTION_DAYS` | 30 | conversations idle longer are deleted ([privacy.md](privacy.md)) |
| `AI_EMBEDDING_PROVIDER`, `AI_EMBEDDING_MODEL_DIR`, `AI_EMBEDDING_THREADS` | `local`, the image's `/opt/models/bge-small-en-v1.5`, 2 | production accepts only `local` |

**Frontend image (build argument)**

| Variable | Notes |
|---|---|
| `API_ORIGIN` | where Next.js proxies `/api` when the edge proxy doesn't route it (development and the local stack); in production the proxy routes `/api` to Django directly |

## Database

- Managed PostgreSQL 16 with the `vector`, `pg_trgm` and `btree_gin` extensions available,
  point-in-time recovery, and daily snapshots retained for 30 days.
- **Two roles** (`infrastructure/postgres/roles.sql`; [security.md](security.md#database-privileges)):
  `arkray_owner` owns the schema and runs migrations; `arkray_app` runs everything else
  with read and write on ordinary tables, SELECT and INSERT only on the append-only tables,
  no DDL, TRUNCATE or trigger rights, and owns nothing. `manage.py grant_app_privileges
  arkray_app` applies its privileges after every migrate (append-only tables are found by
  their trigger). Verified live: as `arkray_app`, UPDATE, DELETE, TRUNCATE, disabling the
  trigger, `session_replication_role` and DDL are all refused; started as the owner, the
  web server and a worker refuse to run.
- PgBouncer in transaction mode once replicas × workers approaches the connection budget;
  set `DISABLE_SERVER_SIDE_CURSORS=True` when it is in place.

**PostgreSQL settings** (parameter group or `postgresql.conf`):

| Setting | Value | Why |
|---|---|---|
| `log_min_error_statement` | `panic` | R63: otherwise a failing statement's text, values included, goes to PostgreSQL's log (`roles.sql` sets it for the database) |
| `log_error_verbosity` | `terse` | R63: otherwise a constraint violation's DETAIL line quotes the failing row (`Failing row contains (...)`, `Key (email)=(...)`) in the log (`roles.sql` sets it; the release check warns without it) |
| `log_min_duration_statement` | `-1` (off) | the same reason; watch slow statements with `pg_stat_statements` (literals normalised) and the access log's `duration_ms` |
| `shared_preload_libraries` | `pg_stat_statements` | slow-statement review without values |
| `shared_buffers` | 25 % of RAM | the organisation dashboard's working set exceeds 128 MB at 1M leads (R53) |
| `effective_cache_size` | 50-75 % of RAM | planner |
| `work_mem` | 16-32 MB | sorts of the organisation-wide lists and aggregates |
| `maintenance_work_mem` | ≥ 512 MB | vacuum and index builds (the vector index above all) |
| `random_page_cost` | 1.1 | SSD storage |
| `max_connections` | from the [connection budget](reliability.md#connection-budget) | — |
| autovacuum | defaults, plus the per-table settings the migrations set (`leads_lead` 1 %, `activities_activity` 2 %) | index-only dashboard figures (R55) |
| `/dev/shm` (self-run containers) | ≥ 256 MB, and ≥ `maintenance_work_mem` for parallel index builds | Docker's 64 MB default breaks parallel maintenance (Phase 10) |

`jit`, the statement, lock and idle-in-transaction timeouts are set per connection by the
application.

## Backup, restore and disaster recovery

- **Primary backup: point-in-time recovery** on the managed service (RPO: minutes).
  **Secondary: a logical dump** (`scripts/backup.sh`: `pg_dump` custom format, checked to
  read back, with a SHA-256 next to it; sessions left out; created owner-only, the password
  passed through the environment rather than the command line), daily to an encrypted bucket
  in another region, kept 30 days ([privacy.md](privacy.md#retention)).
- **Restore** (`scripts/restore.sh`): into an **empty** database only (it refuses one with
  tables), as the owner; verifies the checksum, restores in parallel, gives the index
  builds more memory, then runs `VACUUM (ANALYZE)`, because a restored database has an empty
  visibility map and no statistics, and the dashboards' index-only figures run about three
  times slower until it is vacuumed (R55). Then `grant_app_privileges` and the release
  check. The Ask Arkray index is part of the dump; `manage.py ai_reindex` rebuilds it if
  needed ([operations.md](operations.md#rebuild-the-rag-index)).
- **Measured** (Phase 11 drill, the 1M-lead / 2M-activity / 531k-chunk copy, 5.6 GB, on the
  benchmark machine): backup 124 s, a 1.0 GB archive; restore 683 s with PostgreSQL's default memory for index
  builds, **392 s** with the script's 1 GB per build (one process each: a parallel 1 GB build
  failed against the container's 256 MB `/dev/shm`); vacuum 13-21 s; every table's row count
  identical to the source; `migrate --check` clean; the heaviest owner's dashboard 47 ms and
  the organisation's 122 ms on the restored copy (the vacuumed baseline). A small database restores in seconds.
- **Targets: RPO ≤ 5 minutes** (point-in-time recovery), **RTO ≤ 1 hour**: a restore at this
  size takes about 7 minutes of database time, leaving the rest for provisioning, DNS and
  the release check. Confirm both with the business, and drill quarterly
  ([runbooks.md](runbooks.md#restore-from-backup)).
- **Redis** holds no data that can't be lost: the broker's queued messages are re-created
  from the outbox (the durable queue is in PostgreSQL), and the cache is a cache.
  Rate-limit counters reset. The questions in flight at a broker loss fail `ai_unavailable`
  and can be asked again.
- **Region loss:** restore the latest backup (or a cross-region PITR replica) in another
  region, deploy the same images, move DNS. Secrets come from the secret manager, which must
  be replicated too.

## Email

`EMAIL_URL` must be a real SMTP service (production refuses console, file, in-memory and
dummy backends). Account emails (invitations, password resets, email-change notices) are
the only mail the system sends. Send from `DEFAULT_FROM_EMAIL` on a domain with **SPF,
DKIM and DMARC** configured, through an authenticated submission port with TLS
(`smtp+tls://...:587`). Delivery runs in the email worker through the outbox: SMTP outages
delay mail, never a request; each retry mints a fresh link; an admin can resend an
invitation. Bounces aren't processed: an undeliverable invitation shows as not activated.

## AI configuration

Ask Arkray is off unless `AI_ENABLED=true`. With `AI_LLM_PROVIDER=none` (the default) it
answers routed questions and shows matching records, and no CRM text leaves the deployment.
With `anthropic`, before enabling it in production:

- a **data-processing agreement** and the provider's retention settings for API data (R68);
- the key in the secret manager, given **to the ai worker only** (`AI_LLM_KEY_HOLDER=true`
  there, `false` everywhere else);
- `AI_LLM_BASE_URL` left at `https://api.anthropic.com` or set to an https gateway: it is
  the only setting that decides where model calls go (production refuses plain http and the
  SDK's own `ANTHROPIC_BASE_URL`/`ANTHROPIC_CUSTOM_HEADERS`);
- the model and its limits in settings (`AI_CHAT_MODEL`, budgets, bulkheads,
  [Configuration](#configuration));
- an evaluation with the real model on the organisation's data (R69: verified here with a
  scripted model only).

The kill switch is `AI_ENABLED=false` (it also stops queued questions).

## Operations

- **Dead outbox events** are investigated from logs (`outbox_event_dead`, with id, topic,
  reason) and re-queued once the cause is fixed: `manage.py outbox_requeue --topic … |
  --queue … | --id … [--since …]` lists them (a dry run), and `--yes` re-queues them with a
  fresh attempt budget (Phase 10). Work already pending again and payloads blanked after
  their retention are skipped and reported; logged as `outbox_events_requeued`.
- **Scheduled jobs** (beat): the outbox relay every 5 s; at wall-clock times (crontab, UTC,
  so a restart never pushes them back): `core.housekeeping` (hourly at :05: expired
  idempotency records, finished outbox events older than 7 days), `identity.housekeeping`
  (:15: expired sessions, throttle evidence older than 24 h, account-email payloads),
  `ai.housekeeping` (:25: expire stuck questions, delete conversations idle for 30 days),
  `ai.reconcile_index` (21:30 UTC, 03:00 in India: re-index drifted sources in bounded
  outbox batches). Rebuilding the RAG index:
  [operations.md](operations.md#rebuild-the-rag-index).
- **First administrator:** `manage.py createsuperuser` (asks for email, first and last name,
  password; `--noinput` with `DJANGO_SUPERUSER_PASSWORD`). Everyone else is invited from the
  Users page.
- **Erasure requests:** `manage.py erase_lead <id> --by <admin email> [--yes]`
  ([privacy.md](privacy.md#erasure)).
- **Every operational procedure:** [runbooks.md](runbooks.md).
- **Invitation emails that went dead** (for example, SMTP down for over an hour) are
  re-sent by an admin with *Resend invitation*.
- **Capacity:** start with 2 api replicas × `WEB_CONCURRENCY` 4, 1 default/email worker, 1 ai
  worker, 1 beat. Scale api on p95 latency and CPU, and workers on oldest-pending age.

## Images (Phase 9)

- Base images are pinned **by digest** in the Dockerfiles (the tag names the series). Move a
  digest deliberately (Dependabot or a reviewed edit) and rebuild; OS packages are upgraded
  at build time, so a rebuild also picks up security updates published since the base.
- The application code is owned by root and read-only to the runtime user (uid 10001); only
  Next.js's `.next/cache` is writable. Run every container with a **read-only root
  filesystem** (a tmpfs `/tmp`, and `/app/.next/cache` for the web image), **no Linux
  capabilities** and **no privilege escalation**: both Compose files do, and the whole
  pipeline was verified that way (Phase 11). Gunicorn's control socket is off (it wanted
  to write under `/app`).
- The backend build context is an **allowlist** (`backend/.dockerignore`): the image holds
  `arkray/` (without tests), `config/`, `manage.py`, `gunicorn.conf.py` and the lockfile,
  never tests, caches, local environments or stray files. Build from a clean checkout.
- Responses carry `Server: arkray` (gunicorn's product and version are not disclosed).

## Attachments storage

Note attachments (product enhancement phase, [activities.md](activities.md#attachments)):

- **Where**: `ATTACHMENT_STORAGE=s3` and a **private** bucket (block public access; the
  objects are written private, server-side encrypted, never overwritten) for production;
  `filesystem` with `ATTACHMENT_ROOT` on a private volume mounted into **the web and worker
  containers** (the image creates `/var/lib/arkray/attachments` owned by the runtime user;
  `docker-compose.yml` mounts the `attachments` volume there) for a single host. Never under a
  path a web server serves: downloads always go through the API, which re-checks access.
- **Proxy**: the upload route (`/api/v1/workspaces/*/activities/*/attachments`) needs a body
  limit a little above `ATTACHMENT_MAX_BYTES` (the reference nginx allows 11 MB there and 1 MB
  everywhere else, and buffers uploads so a slow client never holds a web worker).
- **Malware scanning** (files stored before a scanner was configured are queued for scanning
  by the hourly housekeeping, 500 at a time; until clean they don't download): run a ClamAV
  daemon (`clamav/clamav`, kept updated by freshclam)
  reachable from the workers and set `ATTACHMENT_SCANNER=clamd://clamav:3310`; files then stay
  "being checked" until clean, infected ones are blocked and deleted. Watch
  `arkray_outbox_events{queue="default"}` and the dead-event alert: a scanner outage keeps
  files pending and retries.
- **Backup**: the database backup holds the files' metadata only. Back up the bucket with
  versioning and cross-region replication (or the volume with snapshots) on the database's
  schedule and retention; restore both from compatible points in time. Erasure (`erase_lead`)
  deletes objects; versioned buckets keep earlier versions until their lifecycle expires them
  (as backups keep erased rows, R79).
- **Housekeeping**: `activities.housekeeping` (hourly) removes the objects of deleted, failed
  or blocked files and gives up on uploads abandoned for an hour. Idempotent.

