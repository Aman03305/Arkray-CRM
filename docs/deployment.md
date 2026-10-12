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
    WK0[worker-outbox<br/>queues: outbox, email] --> PGB
    WK1[worker-default<br/>queue: default] --> PGB
    WK3[worker-index<br/>queue: ai_index] --> PGB
    WK2[worker-ai<br/>queue: ai] --> PGB
    BEAT[beat<br/>exactly 1 replica] --> BR
    PGB --> PG[(PostgreSQL 16 + pgvector<br/>managed, PITR backups)]
    API --> RC[(Redis cache<br/>allkeys-lru)]
    WK1 & WK2 & WK3 --> BR[(Redis broker<br/>AOF, noeviction)]
    WK0 --> SMTP[SMTP provider]
    WK1 -->|private network only| AV[ClamAV clamd<br/>+ freshclam]
    WK2 -->|optional| AIP[Anthropic API]
```

| Component | Image / command | Scaling | Probes |
|---|---|---|---|
| api | backend image, default `gunicorn` CMD | horizontal; stateless | live: TCP on the port (an HTTP probe shares the request workers, which a stalled database pins: [reliability.md](reliability.md#failure-and-degradation-matrix)), or `/health/live` with more than 35 s of tolerance; ready `/health/ready` |
| web | frontend image, `node server.js` | horizontal; stateless | HTTP GET `/login` |
| worker-outbox | `celery -A config worker -Q outbox,email --concurrency 2 --hostname outbox@%h` (the relay's 5 s tick, the erasure ledger's reconcile every minute, email delivery: short tasks people wait on) | 1-2 replicas; small. Grace period 130 s (the 120 s task limit) | `python manage.py ledger_status --ping-worker outbox` ([below](#worker-and-beat-health)) |
| worker-default | `celery -A config worker -Q default --hostname celery@%h` (attachment scans and clean-up, data exports, the nightly checks) | horizontal | `... --ping-worker celery`. **Grace period at least 18 minutes**: the nightly attachment check (`activities.reconcile_check`) may run 17; a shorter one kills it on every deploy that lands at 22:10 UTC (R128). A deploy during that check holds this queue's new work (scans, exports) until the new worker starts; the relay, email (password-reset requests included) and the ledger reconcile don't wait |
| clamav | `clamav/clamav` (clamd and freshclam), [Attachments storage](#attachments-storage) | 1 replica (or a sidecar per worker) | clamd `PING` (`clamdcheck.sh`) |
| worker-index | `celery -A config worker -Q ai_index --concurrency 1 --hostname index@%h` (Ask Arkray's indexing: CPU-bound embedding, loads the model) | 1 replica normally; scale out temporarily for a large backfill | `... --ping-worker index` |
| worker-ai | `celery -A config worker -Q ai --concurrency 2 --hostname ai@%h` (Ask Arkray's questions only; loads the embedding model, ~0.5 GB RAM per process) | horizontal, sized to question volume and the provider's rate limits | `... --ping-worker ai` |
| beat | `celery -A config beat --schedule /tmp/celerybeat-schedule` (a writable path: the image's `/app` isn't writable by the app user, and beat dies at start without it, `Errno 13`) | **exactly one** replica, never autoscaled: duplicates cause duplicate relay ticks and housekeeping runs (harmless but wasteful) | `python manage.py ledger_status` |
| migrate | `python manage.py migrate --noinput` | one-off job per release | exit code |

Every process from the backend image (api, every worker, beat, migrate and one-off
management commands such as `replay_erasures`) needs the **same erasure ledger**: the same
`ERASURE_LEDGER_URL` and `ERASURE_LEDGER_KEY`, and for a `file:///` ledger the same volume
mounted at that path, read-write, owned by the app user (uid 10001, mode 0700) and never by
the web or proxy containers. A worker that sees another (or an empty) directory reads the
ledger as rolled back and refuses every task (final audit ARCH-1, where the AI workers and
beat had the URL without the volume). Across hosts or instances use an S3 ledger: a disk
attached to one instance is not shared.

### Worker and beat health

Beat's schedule file only records when each entry last ran: in tmpfs it is lost on a
restart, which costs nothing (the relay ticks every 5 s; the slower entries are crontabs and
fire at their time whatever happened before). `manage.py ledger_status` prints this
process's view of the erasure ledger gate and exits 1 unless it is open; with
`--ping-worker <name>` the worker started with `--hostname <name>@%h` on the same host must
also answer a ping through the broker within 5 s. It is a short Django process (it reads and
verifies the ledger and queries the database once), so probe every 60 s with a 30 s timeout,
not every few seconds; both Compose files do (`start_period` 120 s). Unhealthy means the
worker refuses or can't take work: its gate is closed (it logs `background_task_skipped`,
`background_task_deferred` or `background_task_dropped` at ERROR, [reliability.md](reliability.md#failure-and-degradation-matrix)),
the worker is hung or dead, or the broker is down (every worker at once). Docker only reports
it; an orchestrator's liveness probe restarts the container, which helps a hung worker but
not a closed gate: alert on it instead (`erasure_ledger_gate_closed`).

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
  to the number of proxies that append `X-Forwarded-For`, **and `TRUSTED_PROXY_ADDRESSES`
  to those proxies' addresses (or CIDR ranges) as Django sees them connect**: the header is
  read only from those peers, and anything else that reaches the app server directly is
  keyed by its own address (adversarial review P2-2: with the count alone, a container that
  could reach gunicorn got a fresh rate-limit budget per request by rotating the header).
  Production refuses a count without addresses, an unparsable entry and `0.0.0.0/0` or
  `::/0`. Set `FORWARDED_ALLOW_IPS` for gunicorn to the same addresses. Client IPs drive
  rate limits and audit records, so they must be correct and unspoofable. In the reference
  stack the edge has a fixed address (`10.252.17.10`) on an internal network it shares with
  the API alone.
- The edge proxy must **overwrite** (never pass through) `X-Forwarded-Proto` and
  `X-Forwarded-For`. If it also sets `X-Request-ID`, enable `TRUST_INCOMING_REQUEST_ID`.
  Otherwise Django mints its own correlation ids, and client-sent ids are logged only as
  `client_request_id`.
- Production settings refuse to disable secure cookies, the HTTPS redirect or HSTS, or to
  use a non-HTTPS `APP_BASE_URL`, unless `DJANGO_ALLOW_INSECURE_LOCAL_HTTP=true` is set.
  Only the local Compose stack sets it, and it is refused unless every
  `DJANGO_ALLOWED_HOSTS` entry is local (localhost, a loopback address or a single-label
  container name). An email backend that doesn't deliver (console, file, memory) is
  refused even with it.
- **Don't log the paths `/activate/*` and `/reset-password/*`** at the proxy (or redact
  their last segment). They carry one-time secrets until used. The pages themselves send
  `Referrer-Policy: no-referrer` and `Cache-Control: no-store`, and Django never sees
  them.

## Reverse proxy

`infrastructure/nginx/arkray.conf` is the reference edge (nginx 1.27.3 or newer; the
reference stack runs 1.30, the stable series). What the
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
- **The public authentication routes are rate limited per address at the edge**
  (`/api/v1/auth/login`, `password-reset`, `invitations`: 5 a second, bursts of 30, answered
  429 in the API's JSON shape with `Retry-After`), so a flood from one address is refused before
  it takes a gunicorn worker: in the capacity test it had raised every other user's p99 to 3.2 s
  ([capacity.md](capacity.md#abuse)). Django's own limits stay the authority. There is
  deliberately **no** global per-address *rate* limit: an office shares one address (the limit
  on requests *in flight* below is far above what an office reaches). A platform edge
  without nginx needs an equivalent rule. **An IPv6 client counts as its /64** (R127), like
  in Django: the `$arkray_client_key` map turns nginx's text of the address into its first
  four groups (an IPv4 address stays as it is), so one host can't rotate through its /64
  for fresh budgets ([operations.md](operations.md#edge-rate-limits)). The limit lives in
  nginx's shared memory: it holds whatever happens to Redis.
- **Memory is bounded before anyone signs in** (deep security audit H-1, R129). nginx writes
  every request body over 16 KB, and every response its client reads slowly, to a temp area
  before Django has seen the request; in the container that is a tmpfs, and a tmpfs is memory
  charged to the container. Uncapped, 24 slow 10 MB uploads from one address got the edge
  OOM-killed (256 MB) and cut every user off. Now:
  - **Every tmpfs of the edge has a size**: uploads and slowly read responses in
    `/var/cache/nginx` (64 MB), JSON bodies over 16 KB in an area of their own
    (`/var/cache/nginx-body`, 16 MB), `/var/run` and `/tmp` 1 MB (82 MB in all, at most half
    the memory limit). A full area refuses only what needs it (503 in the API's JSON shape).
  - **An attachment upload's body is read only after Django confirms the request's
    session**: nginx's `auth_request` asks `GET /api/v1/auth/me` (an internal subrequest with
    the browser's headers and no body) in its access phase, before the body is read. Without
    a session the upload gets 401 in the API's envelope (with `WWW-Authenticate` and the
    request id) and its body is discarded, never spooled; a failed check fails closed (503).
    The check is one extra cheap request per upload (3 SELECTs, no write, no audit event),
    counted against the user's API rate.
  - **Per client** (`$arkray_client_key`, the sign-in limit's key): at most **256 requests in
    flight** anywhere, **8 requests with a body** (a GET, HEAD or OPTIONS without a declared
    body doesn't count; uploads count too) and **4 attachment uploads** (a zone of their
    own), refused 429 in the API's JSON shape with `Retry-After` (a page: a minimal HTML 429
    with the request id).
  - Headers and bodies that stall for **15 s**, and clients that stop reading for **30 s**,
    are dropped.
  - No one API response is spooled beyond **16 MB** (`proxy_max_temp_file_size`: every 10 MB
    attachment still fits whole, so a slow download frees its gunicorn worker at once; 0
    would hold a sync worker for every slow download). **Data-export downloads** (up to
    100 MB) have a location of their own and are spooled whole on a disk volume
    (`edge-export-spool`, `/var/cache/nginx-exports`; 112 MB each): 2 per client and 4 in
    all, at most 448 MB on disk, written only after Django answered; a request declaring a
    body there gets 413. A slow line gets the whole ZIP and never holds a gunicorn worker
    (with the 16 MB cap, gunicorn's 30 s timeout cut a slow export short behind a 200).
    Spool writes are charged to the container as reclaimable page cache: four parallel
    80 MB downloads reached the 256 MB limit without an OOM kill, and it fell back to 24 MB.

  Keep the sizes consistent if you change them: uploads-per-client x the upload body limit
  must fit three quarters of the upload area, bodies-per-client x the JSON body limit half
  of the JSON area, and `proxy_max_temp_file_size` must be between `ATTACHMENT_MAX_BYTES` and
  a quarter of the upload area (`tests/architecture/test_edge_limits.py`). A platform edge
  without nginx needs the equivalent (bounded request buffering, per-client concurrency,
  short body/header timeouts, and an upload body read only for a confirmed session, or the
  acceptance that anonymous clients can spool upload bodies). What remains possible without
  signing in: [operations.md](operations.md#edge-rate-limits).
- **Errors nginx answers itself name the request**: on API routes the API's JSON envelope
  (502/504 become 503 `service_unavailable`, 413, 429) with `request_id`; on pages (the web
  tier, Next.js, unreachable or too slow) a minimal HTML page with the request id as a
  reference, 503 with `Retry-After`, under a CSP that lets nothing load
  (`default-src 'none'`), `nosniff`, `DENY` framing and `no-store`. Every one carries
  `X-Request-ID` and HSTS.
- **The container's health check** is `GET /edge-health` on the plain-HTTP port, answered by
  nginx itself to loopback only (404 to anyone else).
- Request bodies are JSON only: 1 MB at the proxy (Django refuses more than 2.5 MB anyway),
  11 MB on the attachment upload route; the proxy's read timeout (35 s) is longer than
  gunicorn's (30 s).

## Release process

1. CI builds both images once per commit, runs all gates, and pushes images tagged with the
   commit SHA.
2. **Once per environment**, a database administrator runs `infrastructure/postgres/roles.sql`
   (the database, `arkray_owner`, `arkray_app`, the extensions, R63's logging settings).
3. Run the **migrate job** with the new image, as `arkray_owner`, after the
   [pre-upgrade checklist](#before-an-upgrade):
   `manage.py migrate --noinput && manage.py grant_app_privileges arkray_app`. Migrations
   normally follow expand → migrate → contract ([database.md](database.md#migrations)), so the
   running old version keeps working; **not across the product-enhancement release**: from
   the v1.0 RC, stop web, workers and beat first ([Upgrading](#upgrading)). The grant gives
   new tables to the application role.
4. **Check the release** with the application's credentials:
   `manage.py check --deploy --database default`. It fails on a runtime role that could
   rewrite the audit trail (R74) and warns when PostgreSQL would log failing statements or rows (R63).
   Django's HSTS notes (`security.W005`, `security.W021`) are policy choices: include
   subdomains and preload only when every subdomain is HTTPS.
5. Roll out api, workers and web (rolling update; readiness gates traffic). The web server
   and the workers check their database role at start and refuse to run as a superuser or a
   role that owns the append-only tables (`DB_REQUIRE_RESTRICTED_ROLE`, on in production).
6. Beat restarts last.
7. Rollback: [below](#rollback). Down-migrations are refused; the way back is the backup
   taken before the upgrade (with the previous images), or a forward fix.

## Upgrading

### Which previous release can run beside the new schema

A rolling deploy (old and new pods at once), and redeploying the previous images without a
restore, both need the previous release's code to work on the new schema. Its INSERTs fail
on any new NOT NULL column it never names that has no database default. Tested with each
previous release's own models (the migrations' historical models at its leaf nodes:
`tests/integration/test_upgrade_from_release.py`):

| Database (and code) before the upgrade | Leaf nodes | Upgrade to the current release |
|---|---|---|
| **v1.0 RC** `64bb641` | `pipeline.0005`, `identity.0003`, `activities.0009`, `audit.0002` (`leads.0006`, `core.0005`, `ai.0003`) | **Stopped deploy only**: stop web, workers and beat, migrate, start the new release. On the new schema the RC can't create opportunities (`opportunity_date`, `account_name`, `customer_name`, `contact_phone`, `contact_email`, `address`, `instrument_name`, `work_load`, `custom_fields` have no database default), pipelines (`version`), stages (`is_negotiation`) or users (`password_change_required`). A database default can't fix it: the account and customer names may not be empty. Its reads and edits of existing rows would work; its creates fail. |
| `0c31aee` (product enhancements) | `pipeline.0006`, `identity.0004`, `activities.0010`, `audit.0004` | rolling deploy possible: since then only `expected_cpt` and `agreed_cpt` are new, both with a database default |
| `5b05177` (ADR-0027) | `pipeline.0007` | rolling deploy possible (same) |
| `989830b` (ADR-0028) | `pipeline.0008` | rolling deploy possible (`agreed_cpt` keeps its default) |

Every new migration is checked the same way before a rolling deploy is planned (add the
release to `CANNOT_INSERT` in that test).

### Before an upgrade

1. **Back up and verify**: `scripts/backup.sh` (it checks the archive reads back and writes
   its SHA-256); note the point-in-time-recovery timestamp too. Before a release that can't
   run beside its predecessor (the table above), restore the dump into a scratch database
   (`scripts/restore.sh`) and compare row counts with the source: that copy is the rollback.
2. **Record the schema state**: `manage.py showmigrations > pre-upgrade-migrations.txt` (the
   last `[X]` per app are the leaf nodes: they say which release the backup belongs with),
   and `manage.py migrate --plan` with the new image to see what will run.
3. **Keep the previous release's images** (by commit SHA) with the backup: a rollback needs
   both.
4. **Size the window**: backup + restore check + migrations + a smoke test. Measured
   2026-10-06 (development machine, PostgreSQL 16 in Docker), RC → current at 200,000 leads,
   60,000 opportunities, 100,000 activities and 200,000 audit events: **8.6 s** of migrations,
   `pipeline.0006` 7.0 s (its backfill UPDATE alone 5.7 s, holding the opportunity table),
   `pipeline.0007` 1.1 s (concurrent), the rest 0.05-0.14 s each. At 300,000 opportunities
   `pipeline.0006` took 21 s ([database.md](database.md#migrations)). It ran under a 1 s
   statement timeout (`DB_STATEMENT_TIMEOUT_MS=1000`) and passed: 0006 lifts the timeout for
   its own transaction, as the table-wide migrations must. Every opportunity's id, stage,
   value, owner, version and timestamps had the same checksum before and after.
5. **Two-step verification (the release with ADR-0033)**: `identity.0005` adds two tables and can't be reversed; the backend image gains the `segno` dependency (rebuild the images). After the deploy, each administrator sets up two-step verification at their next sign-in (nothing else answers until they do); agree the identity check for the operator's `manage.py reset_mfa` before go-live ([runbooks.md](runbooks.md#lost-second-factor)).
6. Stopped deploy: stop web, workers and beat (no writes during the migration), run the
   migrate job, then start the new release (step 3 above).

## Rollback

**Rollback and downgrade policy.** Schema down-migrations are **not supported past the v1.0
RC** (`64bb641`). Every migration after it (`pipeline` 0006-0009, `identity.0004`,
`activities.0010`, `audit` 0003-0004, `identity.0005`) refuses to be reversed: `migrate pipeline 0005` (or any
target that would undo one of them) stops with `IrreversibleError: <migration> can't be
reversed: that would delete ... restore the database backup taken before the upgrade (with
that release), or deploy a forward fix` **before changing anything**, whether or not anything
was recorded since (tested on an empty database and on one holding negotiated price
history: the migration records, the schema and every row unchanged). Reversing them would
delete the negotiated price history and agreed CPTs, user pipelines and custom fields, every
opportunity's customer, contact, instrument details and Expected CPT, support sessions,
attachments' records and note edit stamps ([database.md](database.md#reversibility)).

To go back:

- **Restore the backup taken before the upgrade** ([runbooks.md](runbooks.md#restore-from-backup))
  into a new database, and deploy the previous release's images against it. Everything
  recorded after the backup is lost unless re-entered (point-in-time recovery to just before
  the migrate job is the same). Attachment files uploaded since stay in the bucket without a
  row (invisible; R93).
- **Or roll forward**: fix the problem in a new release on the new schema. Prefer this
  when people have already recorded work on it.
- **Redeploying the previous images without a restore** works only from a release the
  [table above](#which-previous-release-can-run-beside-the-new-schema) marks rolling-deploy
  possible, never from the v1.0 RC.

## Verifying a release

`infrastructure/compose.production.yml` runs the images exactly as production must, on one machine:
HTTPS at the reference proxy (a self-signed certificate), secure cookies and HSTS, both
database roles (migrations as the owner, everything else as `arkray_app`), Redis with a
password, read-only containers, no insecure opt-in, only the proxy published:

```
export DJANGO_SECRET_KEY=... POSTGRES_PASSWORD=... ARKRAY_OWNER_PASSWORD=... \
       ARKRAY_APP_PASSWORD=... REDIS_PASSWORD=... METRICS_TOKEN=... ERASURE_LEDGER_KEY=...
docker compose -p arkray-prod -f docker-compose.yml -f infrastructure/compose.production.yml \
    --profile app up -d --build
```

Every container of the backend image mounts the one `ledger` volume (`x-ledger-volume`;
`tests/architecture/test_compose_ledger.py` fails on a service with `ERASURE_LEDGER_URL` and
no volume), and the workers and beat report `healthy` only while their ledger gate is open
([Worker and beat health](#worker-and-beat-health)). After a restore, replay through migrate
(it has the owner's credentials and the volume; the report goes to a directory writable by
uid 10001, the root filesystem being read-only):
`docker compose ... run --rm -v ./restore-reports:/reports migrate python manage.py
replay_erasures --by <admin> --report /reports/<date>.json`.

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
| `DJANGO_SECRET_KEY` | yes | at least 50 characters with at least 5 distinct ones, production-only (not `dev-`, `test-` or `django-insecure-`); startup fails otherwise. Rotation: [runbooks.md](runbooks.md#rotate-a-secret) |
| `DJANGO_SECRET_KEY_FALLBACKS` | no | comma-separated earlier keys, during a rotation only: sessions, sealed page links and trusted-browser cookies signed with them keep working until they're removed; each must be as strong as the key |
| `MFA_REQUIRED_FOR_ADMINS` | no | `true` in production: administrators must set up two-step verification (TOTP, [ADR-0033](adr/0033-administrator-mfa-and-reactivation.md)) at their next sign-in and give a code again (step-up) for user administration, support sessions, export requests and pseudonymisation; `false` is refused unless `DJANGO_ALLOW_INSECURE_LOCAL_HTTP=true`. The authenticator secrets are encrypted with a key derived from `DJANGO_SECRET_KEY`: before retiring a key from `DJANGO_SECRET_KEY_FALLBACKS`, run `manage.py mfa_reencrypt` ([runbooks.md](runbooks.md#lost-second-factor)). Wrong codes lock the factor at `MFA_MAX_CONSECUTIVE_FAILURES` (20 in a row) and the owner is emailed about a wrong code at most every `MFA_FAILURE_NOTICE_INTERVAL_S` (6 hours) and when it locks: both fixed in `config/settings/base.py`, not environment variables (deep security audit M-4, [runbooks.md](runbooks.md#second-factor-locked)) |
| `DJANGO_ALLOWED_HOSTS` | yes | the public host(s) **and** the probes' host |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | yes | `https://crm.example.com`; production refuses `http://` origins and wildcards (`https://*.example.com`) |
| `APP_BASE_URL` | **yes** | the public web URL for emailed links, `https://crm.example.com`; startup fails if unset or not `https://` |
| `DJANGO_SECURE_COOKIES`, `DJANGO_SECURE_SSL_REDIRECT` | no | `true`; turning either off needs `DJANGO_ALLOW_INSECURE_LOCAL_HTTP` |
| `DJANGO_HSTS_SECONDS`, `DJANGO_HSTS_INCLUDE_SUBDOMAINS` | no | one year (production refuses less than a day), `false` (the edge proxy also sends HSTS, [below](#reverse-proxy)) |
| `DJANGO_ALLOW_INSECURE_LOCAL_HTTP` | no | **never in a real deployment**: the local development stack's opt-in for plain HTTP, Redis without a password and the like; refused when any allowed host is a public name |
| `TRUSTED_PROXY_COUNT` | **yes** | proxies appending `X-Forwarded-For` (1 behind [the reference proxy](#reverse-proxy)); startup fails if unset |
| `TRUSTED_PROXY_ADDRESSES` | **yes** when the count is above 0 | the proxies' addresses or CIDR ranges, comma-separated, IPv4 or IPv6 (`10.252.17.10`, `10.0.0.0/24,fd00:1::/64`): `X-Forwarded-For` is believed only from these peers. Startup fails without it (count above 0), on an entry that isn't an address or range, and on `0.0.0.0/0` / `::/0` |
| `FORWARDED_ALLOW_IPS` | yes | gunicorn's trusted proxy addresses: the same as `TRUSTED_PROXY_ADDRESSES` (never `*` in production) |
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
| `DATABASE_URL` | yes | the **application role** (`arkray_app`) for web and workers, the owner (`arkray_owner`) for the migrate job ([Database](#database)); PgBouncer's address when it's in place. **Verified TLS** for any remote host: `...?sslmode=verify-full&sslrootcert=/path/ca.crt` (or `sslrootcert=system`); startup fails otherwise ([security.md](security.md#backend-tls)) |
| `BACKEND_TLS_PRIVATE_HOSTS` | no | empty. Single-label container/service names (`postgres,redis`) reached without TLS on a private network; a dotted name or an IP address is refused. Loopback is always allowed |
| `DB_REQUIRE_RESTRICTED_ROLE` | no | `true` in production: the web server and workers refuse to start as a superuser or as a role that could rewrite the audit trail (R74) |
| `DB_CONNECT_TIMEOUT_S` | no | 5 |
| `DB_STATEMENT_TIMEOUT_MS` | no | 10,000 (web); set 60,000 for workers |
| `DB_LOCK_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | no | 5,000; 60,000 |
| `DB_CONN_MAX_AGE_S` | no | 60 (persistent connections; ignored with the pool) |
| `DB_TRANSACTION_POOLER` | no | `false`. `true` when `DATABASE_URL` names a transaction-mode pooler (PgBouncer, Supavisor): no startup `options`, no server-side cursors or prepared statements; the timeouts then come from the role ([Database poolers](#database-poolers)) |
| `DB_POOL_ENABLED`, `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_TIMEOUT_S` | no | `false`, 1, 4, 5: a per-process psycopg pool, for threaded servers ([reliability.md](reliability.md#connection-budget)) |

**Redis, background work and email**

| Variable | Required | Default and notes |
|---|---|---|
| `CELERY_BROKER_URL` | yes | the broker Redis, with a password and verified TLS: `rediss://:password@host:6380/0?ssl_cert_reqs=required` (`&ssl_ca_certs=/path/ca.crt` for a private CA); startup fails without them |
| `REDIS_CACHE_URL` | yes | the cache Redis, **a separate instance with no persistence** (`--save '' --appendonly no`: its rate-limit keys hold client addresses), with a password and the same TLS parameters |
| `OUTBOX_DONE_RETENTION_DAYS` | no | 7: finished background work kept for investigation, then purged hourly |
| `EMAIL_URL` | **yes** | `smtp+tls://user:pass@smtp.example.com:587` (STARTTLS; `smtps://` means the same) or `smtp+ssl://...:465` (implicit TLS); startup fails if unset, a console, file, in-memory or dummy backend (they would put one-time account links in logs or on disk, or drop them), or plain `smtp://` to a remote relay |
| `EMAIL_TLS_CA_FILE` | no | a private CA (PEM) for the relay's certificate; otherwise the system store. Certificates and host names are always verified, TLS 1.2+ |
| `DEFAULT_FROM_EMAIL` | **yes** | the sender, on a domain with SPF, DKIM and DMARC ([Email](#email)); production refuses to start without it, or with a reserved domain (`.local`, `.localhost`, `.internal`, `.invalid`, `.test`, `.example`) |
| `EMAIL_TIMEOUT_S` | no | 10 |

### Attachments

Files on notes ([activities.md](activities.md#attachments)). The bytes are **never** in
PostgreSQL: a database backup is not a backup of the files ([below](#attachments-storage)).

| Variable | Required | Default and notes |
|---|---|---|
| `ATTACHMENT_STORAGE` | **yes** in production | `filesystem` (a private directory, development) or `s3` (private S3-compatible object storage) |
| `ATTACHMENT_ROOT` | with `filesystem` | `backend/var/attachments`; a private volume never served by a web server |
| `ATTACHMENT_S3_BUCKET` | with `s3` | a **private** bucket (block all public access); objects are written `private`, server-side encrypted, never overwritten |
| `ATTACHMENT_S3_ENDPOINT_URL`, `ATTACHMENT_S3_REGION` | no | for S3-compatible stores (MinIO, R2, ...); the endpoint must be `https://` (startup fails otherwise) |
| `ATTACHMENT_S3_CA_BUNDLE` | no | a private CA bundle for a self-hosted store; certificates are always verified |
| `ATTACHMENT_S3_PURGE_VERSIONS` | no | `false`. `true` with a versioned bucket: purging a deleted file removes every earlier version and delete marker too (needs `s3:ListBucketVersions`, `s3:DeleteObjectVersion`); otherwise the bucket's lifecycle rule (expire noncurrent versions after 30 days) must ([privacy.md](privacy.md#attachments)) |
| `ATTACHMENT_S3_ACCESS_KEY_ID`, `ATTACHMENT_S3_SECRET_ACCESS_KEY` | no | prefer an instance/workload role; keys, if used, may only read, write and delete in the bucket's prefix |
| `ATTACHMENT_S3_PREFIX` | no | `attachments` |
| `ATTACHMENT_S3_CONNECT_TIMEOUT_S`, `ATTACHMENT_S3_READ_TIMEOUT_S`, `ATTACHMENT_S3_MAX_ATTEMPTS` | no | `3`, `10`, `2` (attempts in total): a dead endpoint costs a request about 7 to 12 s, inside the 30 s worker timeout (botocore's defaults took 113 s against a black hole and could pin every web worker: final audit SRE-1) |
| `ATTACHMENT_STORAGE_DEADLINE_S`, `ATTACHMENT_STORAGE_MAX_IN_FLIGHT` | no | `12`, `4`: a request stops waiting for storage after 12 s whatever the retries add up to, and each process runs at most 4 storage calls at once (more answer 503 at once) ([reliability.md](reliability.md#attachment-storage)) |
| `ATTACHMENT_STORAGE_MAX_IN_FLIGHT_SHARED` | no | `3`: storage calls in flight at once across every process sharing the cache; more answer 503 at once. Keep it well below `WEB_CONCURRENCY` so a failing store can never hold most web workers ([capacity.md](capacity.md#attachment-storage-failures)) |
| `ATTACHMENT_STORAGE_BREAKER_FAILURES`, `ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S` | no | `3`, `15`: after 3 outage-like storage failures in a row a process answers 503 at once for 15 s (doubling to 2 minutes while it lasts), checking in the background whether storage is back |
| `ATTACHMENT_RECONCILE_DAILY` | no | `true`: the daily read-only `reconcile_attachments --check` (22:10 UTC), its outcome on the metrics endpoint ([runbooks.md](runbooks.md#attachment-reconciliation)) |
| `ATTACHMENT_MAX_BYTES` | no | 10 MB per file; the proxy's body limit on the upload route must be a little above it ([below](#reverse-proxy)) |
| `ATTACHMENT_MAX_PER_NOTE` | no | 10 |
| `ATTACHMENT_MAX_BYTES_PER_USER` | no | 2 GiB (`2147483648`): the most storage one person's files may take (deleted files count until purged; 422 `attachment_quota_exceeded` beyond it, deep security audit L-7); `0` turns the quota off; a negative value fails startup (`arkray.E023`) |
| `ATTACHMENT_ALLOWED_EXTENSIONS` | no | `pdf,png,jpg,jpeg,webp,docx,xlsx,csv,txt`; only from the catalog the server can recognise by content (adds: `gif`, `pptx`); anything else fails startup |
| `ATTACHMENT_SCANNER` | **yes** (or the waiver) | `clamd://host:3310`; production refuses to start without it unless `ATTACHMENT_SCANNER_WAIVED=true` states that the organisation accepts unscanned attachments (R94: files are then "not scanned" and downloadable). With `clamd://host:3310` (a ClamAV daemon: files stay "being checked" until clean; infected ones are blocked and deleted). clamd has no TLS: its host must be loopback or listed in `BACKEND_TLS_PRIVATE_HOSTS` (a sidecar or a container on the private network); startup fails otherwise |
| `ATTACHMENT_SCANNER_WAIVED` | no | `false`; `true` only when the organisation accepts unscanned attachments (R94) and runs without `ATTACHMENT_SCANNER`; any value other than `true` or `false` is refused at startup |

**Rate limits** (per user, or per client address for anonymous and sign-in requests)

| Variable | Default | Notes |
|---|---|---|
| `API_THROTTLE_USER` | `600/min` | every signed-in request |
| `API_THROTTLE_ANON` | `60/min` | requests without a session |
| `API_THROTTLE_AUTH` | `20/min` | sign-in, password change and reset requests, **per client address**. Everyone in an office behind one NAT shares it (R77): size it to the number of people who sign in within a minute from one address (60/min for an office of 50 is reasonable). Guessing stays bounded whatever the value: 5 failures per account and browser, and 50 per address, in 15 minutes ([authorization.md](authorization.md#sign-in-throttling)) |
| `API_THROTTLE_SEARCH` | `120/min` | global and list search |
| `API_THROTTLE_ASK` | `20/min` | Ask Arkray questions |
| `API_THROTTLE_ATTACHMENTS` | `30/min` | file uploads (each may move 10 MB to object storage) |

The format is `<count>/<period>` with a period of `s`, `min`, `h` or `day`; production
refuses anything else at startup (DRF would otherwise fail on the first request).

"Client address" is the trusted-proxy-aware one (`TRUSTED_PROXY_COUNT`: the entry that many
places from the right of `X-Forwarded-For`, never one the client wrote), and an IPv6 client
counts as its /64 (`arkray.core.throttling`, R127). The counts live in the Redis cache; while
it is unavailable each API process counts in its own memory instead, at the same rates, so
a Redis outage neither lifts the limits nor refuses everyone. During an outage a client can
therefore reach the rate times the number of API processes its requests land on
([operations.md](operations.md#rate-limits-while-redis-is-down)).

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
| `ANTHROPIC_API_KEY` | empty | **the ai worker only** (`AI_LLM_KEY_HOLDER=true`); a key holder without a key fails startup, and so does a key in a process that isn't the holder |
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
| `AI_MAX_QUESTIONS_PER_USER_PER_DAY` | 200 | questions handed to the ai workers (model or retrieval) per person in any 24 hours, counted from the append-only `ai.question_queued` audit events (deleting conversations doesn't reset it; a question whose hand-off failed still counts); routed questions never count; `0` turns the cap off; over it, 429 `rate_limited` with `Retry-After` (deep security audit L-12) |
| `AI_CONVERSATION_RETENTION_DAYS` | 30 | conversations idle longer are deleted ([privacy.md](privacy.md)) |
| `AI_EMBEDDING_PROVIDER`, `AI_EMBEDDING_MODEL_DIR`, `AI_EMBEDDING_THREADS` | `local`, the image's `/opt/models/bge-small-en-v1.5`, 2 | production accepts only `local` |

**Frontend image (build argument)**

| Variable | Notes |
|---|---|
| `API_ORIGIN` | where Next.js proxies `/api` when the edge proxy doesn't route it (development and the local stack); in production the proxy routes `/api` to Django directly |

**Privacy (ADR-0032; [privacy.md](privacy.md))**

| Variable | Required | Default and notes |
|---|---|---|
| `ERASURE_LEDGER_URL` | **yes** | where erasures are recorded outside the database: `file:///var/lib/arkray/ledger` (a volume of its own, never in a database backup) or `s3://bucket/prefix` (a bucket of its own, versioned, Object Lock in compliance mode). Startup fails without it |
| `ERASURE_LEDGER_KEY` | **yes** | the ledger's HMAC key: at least 32 characters, not the Django secret, not a `dev-`/`test-` key. Keep it with the ledger's own secrets, the same in every process. Rotate it with `ERASURE_LEDGER_VERIFY_KEYS` (old entries keep verifying under the old key; [runbooks.md](runbooks.md#rotate-the-erasure-ledger-key)), never by swapping the key alone |
| `ERASURE_LEDGER_S3_ENDPOINT_URL`, `ERASURE_LEDGER_S3_REGION`, `ERASURE_LEDGER_S3_CA_BUNDLE` | no | an S3 ledger's endpoint (https only), region and private CA bundle |
| `ERASURE_LEDGER_CHECK_INTERVAL_S`, `ERASURE_LEDGER_MAX_STALE_S` | no | 60 s: how often each process compares the database with the ledger; 1 h: how long it stays open through a ledger outage after a verified state |
| `ERASURE_LEDGER_VERIFY_KEYS` | no | empty. Comma-separated earlier or next ledger keys, accepted when verifying entries and never used to write, during a key rotation only ([runbooks.md](runbooks.md#rotate-the-erasure-ledger-key)) |
| `ERASURE_LEDGER_AUTO_RECONCILE_MAX` | no | 10: a database behind the ledger by at most this many entries (an erasure whose commit failed after its entry was written) is reconciled automatically; more (a restore) waits for `replay_erasures`; 0: always the operator |
| `AUDIT_DETAIL_RETENTION_DAYS` | no | 90: how long audit events' personal details (security events' client addresses, old/new emails, reset requesters' addresses, support reasons) are kept; a policy the organisation sets |
| `EXPORT_ROOT` | no | `var/exports`: the private directory for data-subject export files (filesystem storage); a volume of its own |
| `EXPORT_S3_PREFIX` | no | `exports`: their prefix in the attachments bucket with `ATTACHMENT_STORAGE=s3` (outside the attachments' prefix, which reconciliation walks). The application's S3 credentials need `s3:ListBucketVersions` and `s3:DeleteObjectVersion` on this prefix **in every case** (not only with `ATTACHMENT_S3_PURGE_VERSIONS`): an expired or erased subject's export is removed with every version. A bucket-wide default Object Lock retention must not cover this prefix (it would keep exports past their expiry) |
| `EXPORT_TTL_HOURS`, `EXPORT_MAX_BYTES` | no | 24 h; 100 MB (attachments beyond it are listed, not included) |
| `EXPORT_MAX_PENDING_PER_ADMIN`, `EXPORT_MAX_PER_HOUR` | no | 3; 20 per administrator |
| `LOG_EXCEPTION_MESSAGES` | no | `false`; exception messages in log tracebacks, for local development only: production refuses it |

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
  set `DB_TRANSACTION_POOLER=true` when it is in place ([Database poolers](#database-poolers)).
- **TLS, verified**: `sslmode=verify-full` with the provider's CA (`sslrootcert`), the pooler
  included ([security.md](security.md#backend-tls)); the production-shaped stack runs it
  against a private CA.

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
application (as startup options; behind a transaction pooler on the role instead, below).

### Database poolers

A transaction-mode pooler serves each *transaction* from any server connection, so nothing
that lives on the session may be relied on. With `DB_TRANSACTION_POOLER=true` the application
sends no startup `options` (PgBouncer and Supavisor refuse the parameter), disables
server-side cursors and automatic server-side prepared statements, and the timeouts must be
set on the role, once, by the administrator:

```sql
ALTER ROLE arkray_app SET statement_timeout = '10s';
ALTER ROLE arkray_app SET lock_timeout = '5s';
ALTER ROLE arkray_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE arkray_app SET jit = off;
```

The workers want `statement_timeout` 60 s (the Compose stack sets it per container): use a
second login role for them. Everything the application does with advisory locks and
`SET LOCAL` is transaction-scoped, so it is safe behind a pooler. **Migrations never go
through a transaction pooler**: run them on a direct or session-mode connection as the owner
(`CREATE INDEX CONCURRENTLY` cannot run inside a pooled transaction, and they lift the
statement timeout with `SET LOCAL`). Platform notes: [hosting-compatibility.md](hosting-compatibility.md).

## Backup, restore and disaster recovery

- **Primary backup: point-in-time recovery** on the managed service (RPO: minutes).
  **Secondary: a logical dump** (`scripts/backup.sh`: `pg_dump` custom format, checked to
  read back, **encrypted** to the backup public key (`BACKUP_GPG_RECIPIENT_FILE`; the private
  key offline, [runbooks.md](runbooks.md#backup-encryption-keys)), **signed** by the backup
  host's own OpenPGP signing key (`BACKUP_GPG_SIGNING_KEY_FILE`, a detached `.sig`: encryption
  to a public key proves nothing about who wrote the file, and a checksum beside it is
  replaced as easily, R128), with a SHA-256 of the encrypted file next to it; sessions left
  out; created owner-only, the password passed through the environment rather than the
  command line; **over verified TLS only**: a remote server needs `sslmode=verify-full` (the
  default the script sets when the URL names none) and its CA in `sslrootcert` or
  `PGSSLROOTCERT`, anything weaker is refused; the URL is read as libpq reads it, and several
  hosts, a repeated `host` or `sslmode`, percent-encoded parameter names and service files
  are refused outright; only a server whose every address is loopback or a Unix socket is
  exempt), daily to a bucket in another region, kept 30
  days (`--prune`, honouring a `LEGAL_HOLD` file; [privacy.md](privacy.md#retention)).
- **The erasure ledger is not in the backups**, by design: after **any** restore (dump or
  point-in-time), the API stays closed until `manage.py replay_erasures --by <admin> --report
  <file>` (the owner's credentials) has re-applied every erasure made since
  ([privacy.md](privacy.md#restore-safe-erasure)); keep its report with the restore's record.
- **Restore** (`scripts/restore.sh`): into an **empty** database only (it refuses one with
  tables), as the owner, over verified TLS like the backup; first copies the dump, its
  `.sha256` and its `.sig` into a private working directory (`RESTORE_WORK_DIR`, else
  `TMPDIR`: room for the dump twice when encrypted) and from then on reads only those
  copies, never the backup store (adversarial review P2-1); verifies the checksum and,
  **for every dump whatever its name**, the signature (with the signing key's PUBLIC half,
  `BACKUP_SIGNER_KEY_FILE`; an unsigned or wrongly signed dump, a plain `.dump` included, is
  refused unless `RESTORE_UNVERIFIED=1`: adversarial review P1-1), decrypts an encrypted dump with the keyring in
  `BACKUP_GNUPGHOME` (into a private temporary directory removed afterwards), restores in
  parallel, gives the index
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
- **Verified 2026-10-06** (R108 drill, development machine; the scripts unchanged, run in a
  throwaway `pgvector/pgvector:0.8.6-pg16` container on the compose network; the target
  databases prepared by `infrastructure/postgres/roles.sql` with the role names suffixed for
  the drill, `arkray_mig_owner` / `arkray_mig_app`): a new database migrated to the latest
  schema as the owner, `grant_app_privileges`, then filled as the application role through
  the test factories and today's services (4 users, 1,000 leads, 400 opportunities, 170
  activities, 20 deals entered into negotiation at ₹12,34,567.89 and revised to
  ₹11,00,000.50, each with its agreed CPT: 40 price rows; edited notes; a support session).
  `DATABASE_URL=postgres://arkray_mig_owner:...@postgres:5432/<source> scripts/backup.sh /out`
  (248 KB, 1 s), then `TARGET_DATABASE_URL=postgres://arkray_mig_owner:...@postgres:5432/<new>
  scripts/restore.sh /out/arkray-<stamp>.dump` (checksum ok, restored and vacuumed in under a
  second). Compared with the source: all 29 tables, every row's md5 identical (1,966 rows;
  `django_session`'s data is left out by design), the negotiated price history identical to
  the cent, the four append-only triggers in place, every constraint validated and every
  index valid; then `grant_app_privileges`, `manage.py check` and `migrate --check` clean,
  and as the application role (Django test client) sign-in, `/api/v1/auth/me`, the
  opportunity list and a deal's negotiated-price history all 200 with the exact decimals.
  The schema catalogue matched too, except that PostgreSQL re-renders `IN (...)` lists after
  a reload (`= ANY ((ARRAY['a'::character varying, ...])::text[])` comes back as
  `= ANY (ARRAY[('a'::character varying)::text, ...])`) in 26 CHECK constraints, 2 partial
  indexes and 1 generated column: the same meaning, so a catalogue comparison must allow for
  it. `check --deploy` was not run (it needs the production settings).
  **Upgrade from a restored backup**, the same day: a database at the v1.0 RC schema (built
  as the owner) with 200,000 leads, 60,000 opportunities, 100,000 activities and 200,000
  audit events, backed up the same way (24 MB, 12 s), restored into a new database (30 s,
  vacuum 5 s), compared (all 25 tables and 560,118 rows identical), then migrated forward
  **on the restored copy** with a 1 s statement timeout: 4.5 s in all (`pipeline.0006` 3.4 s,
  `0007` 0.8 s). Afterwards every opportunity's id, stage, value, owner, version and
  timestamps had the same checksum as before, each had its account and customer names and
  opportunity date, no constraint was unvalidated and no index invalid, and
  `grant_app_privileges`, `manage.py check` and `migrate --check` were clean.
- **Verified 2026-10-10** (privacy remediation restore drill, `tests/drills/restore_drill.py`
  against the local PostgreSQL): an encrypted `backup.sh` dump of a seeded database, then a
  lead erased, a file deleted, a custom field's values deleted and a former user
  pseudonymised (5 ledger entries), `restore.sh` into an empty database (checksum ok,
  decrypted ok); the restored database brought the erased back and the API answered 503
  (behind), and 503 again with the ledger unavailable; `replay_erasures` re-applied 5 of 5
  (the lead erased, its index chunks gone, the file purged and its name removed, the custom
  values deleted, the user pseudonymised, a bystander untouched) and the API reopened; a
  second replay found all 5 already applied. A dump without encryption was refused unless
  `BACKUP_ALLOW_UNENCRYPTED=1`.
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

## Images

- Base images are pinned **by digest** in the Dockerfiles (the tag names the series). Move a
  digest deliberately (Dependabot or a reviewed edit) and rebuild; OS packages are upgraded
  at build time, so a rebuild also picks up security updates published since the base.
- **Every third-party image in the Compose files and the CI service containers is pinned by
  digest too** (R128): `name:tag@sha256:<digest>`, the digest of the multi-architecture
  index (`docker buildx imagetools inspect name:tag`), the tag kept for people and update
  tools. Pinned: `pgvector/pgvector:0.8.7-pg16-trixie` (the production-shaped stack and CI;
  the development stack keeps `0.8.6-pg16`, bookworm, because its existing volume's
  collations come from bookworm's glibc: move it after a `REINDEX`), `redis:7.4-alpine`,
  `axllent/mailpit:v1.31`, `nginx:1.30-alpine`, `clamav/clamav:1.5`; the one-shot
  certificate jobs use the PostgreSQL image's own `openssl` (no `alpine` with a run-time
  `apk add` any more). Trivy (HIGH and CRITICAL, 2026-10-11): PostgreSQL trixie, Mailpit
  and the web image none; Redis 2, nginx 3, ClamAV 1 (all fixed upstream in OS packages, so
  a newer digest of the same tag clears them: move the pins on each release); the backend
  image 57 with no fixed version (judged in the risk register). A real
  deployment's manifests do the same, and pin its own images by commit SHA.
  `tests/architecture/test_compose_hardening.py` fails on an image without a digest.
- **Least privilege for every container, the data services included** (the production-shaped
  stack, `infrastructure/compose.production.yml`): a read-only root filesystem with tmpfs
  only where a process writes; `cap_drop: [ALL]` and no privilege escalation; PostgreSQL
  (999), Redis (999:1000), Mailpit (nobody) and ClamAV (100:101) run as their own users from
  the start, so they need no capability at all; only nginx (`CHOWN`, `SETGID`, `SETUID`: its
  master switches to its worker user) and the one-shot TLS job (`CHOWN`: it hands each
  server its key) add any back. Each long-running service has memory, CPU and process limits
  and a health check. Verified by starting the whole stack with them. **Every tmpfs has a
  size** (deep security audit H-1): a tmpfs is memory charged to its container, and without
  `size=` it may grow to half the host's memory whatever the container's limit, so a full one
  became an OOM kill rather than a refused write. Sizes follow what is written there: the
  API's `/tmp` 256 MB (an upload spooled per gunicorn worker plus storage fetches), the
  default worker's 256 MB (two export ZIPs of up to 100 MB), clamd's 512 MB (4 threads x
  128 MB), the edge's upload area 64 MB and JSON-body area 16 MB (its export spool is a disk
  volume); the rest 1-64 MB, each service's total at most half its
  memory limit. Services whose tmpfs comes from `docker-compose.yml` replace it in the
  overlay (`tmpfs: !override`), so `/tmp` isn't mounted twice. If you raise
  `ATTACHMENT_MAX_BYTES`, `EXPORT_MAX_BYTES`, the worker concurrency or clamd's limits, raise
  the matching tmpfs (and the memory limit); `tests/architecture/test_compose_production.py`
  fails until they agree.
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
  "being checked" until clean, infected ones are blocked and deleted. The reference stack
  does exactly this (R94): a `clamav` service (pinned by digest, `infrastructure/clamav/`
  for its configuration, about 1.2 GB of memory for the signatures and up to twice that
  while freshclam's new set loads, so a 3 GB limit; a volume for the signatures) on an
  **internal network shared with the scanning worker alone** (queue `default`), freshclam
  reaching the mirrors over a network nothing else joins, `BACKEND_TLS_PRIVATE_HOSTS=clamav`
  (clamd has no TLS). Keep `ATTACHMENT_MAX_BYTES <= StreamMaxLength <= MaxFileSize`
  `<= MaxScanSize` (10 MB, 16 MB, 128 MB, 128 MB: a document's unpacked members count
  against MaxFileSize, and a 10 MB spreadsheet's XML can be several times larger) and
  `AlertExceedsMax yes`, or larger files fail closed forever or a partly scanned archive
  could pass. The container's health check also fails when the signatures are more than 3
  days old ([operations.md](operations.md#clamav-signature-freshness)). Watch
  `arkray_outbox_events{queue="default"}` and the dead-event alert: a scanner outage keeps
  files pending and retries.
- **Backup**: the database backup holds the files' metadata only. Back up the bucket with
  versioning and cross-region replication (or the volume with snapshots) on the database's
  schedule and retention; restore both from compatible points in time. Erasure (`erase_lead`)
  deletes objects; versioned buckets keep earlier versions until their lifecycle expires them
  (as backups keep erased rows, R79).
- **Housekeeping**: `activities.housekeeping` (hourly) removes the objects of deleted, failed
  or blocked files and gives up on uploads abandoned for an hour. Idempotent.

