# Hosting compatibility: Supabase PostgreSQL, Render, low-cost pilots

Arkray CRM is plain Django + PostgreSQL + Redis + Celery behind any HTTP proxy, with files in
S3-compatible storage. It does not depend on a platform's database features beyond three
extensions, and Django remains the only authentication and authorisation layer: nothing here
uses (or should use) Supabase Auth, its Data API (PostgREST), Realtime or row-level security.

**What was verified.** The database side below was checked by reading every migration and every
raw SQL statement in the application (final remediation, 2026-10-06) and by the local
production-shaped stack ([capacity.md](capacity.md)). **Nothing was run on Supabase or Render
themselves**: every item under "Verify at deployment" is a check to perform on the real service,
and R80/R99 stay open until then ([risk-register.md](risk-register.md)).

## PostgreSQL features the application uses

| Feature | Where | Needs a superuser? | Supabase / Render |
|---|---|---|---|
| `CREATE EXTENSION vector` (pgvector) | `ai.0001`, `roles.sql` | yes on stock PostgreSQL (not a trusted extension) | available on both; enable it from the platform's console or as the platform's admin role |
| `CREATE EXTENSION pg_trgm`, `btree_gin` | `identity.0002`, `activities.0008`, `roles.sql` | no (trusted) | available |
| `pg_stat_statements` | optional, slow-statement review only | needs `shared_preload_libraries` | on by default on Supabase; a Render parameter |
| Triggers (`arkray_forbid_mutation`, plain PL/pgSQL, append-only audit and history tables) | `core/db.py` | no | works; **not** `SECURITY DEFINER`, nothing escalates |
| Generated (stored) columns, partial and covering indexes, `CREATE INDEX CONCURRENTLY` | pipeline, leads, activities | no | works; the concurrent builds need a direct (non-pooled) connection |
| `pg_advisory_xact_lock*` (transaction-level only) | user administration, idempotency, sign-in throttling, erasure | no | safe behind a transaction pooler (session-level locks would not be) |
| `SET LOCAL` / `set_config(..., true)` (search statement timeout) | `search/selectors.py` | no | safe behind a transaction pooler |
| `REPEATABLE READ, READ ONLY` transactions | dashboard and board reads | no | works |

Not used anywhere: `ALTER SYSTEM`, `LISTEN/NOTIFY`, `pg_cron`, `COPY ... PROGRAM`, large objects,
logical replication, foreign data wrappers, row-level security, session-level advisory locks.

## Connections and poolers

PostgreSQL connections are the scarce resource on a small database
([reliability.md](reliability.md#connection-budget)). Measured in the capacity test: **100
concurrent active users used 8 web connections plus the Celery workers' (about 13 in all), never
100**, because gunicorn's sync workers hold one connection each.

- **Direct connection** (no pooler): works as is. The default for the web service and workers on
  a database with `max_connections` of 60 or more (Supabase "micro" and above).
- **Transaction-mode pooler** (PgBouncer, Supavisor port 6543): set `DB_TRANSACTION_POOLER=true`
  on the web service and workers. The application then sends no startup `options` (the poolers
  refuse that parameter), disables server-side cursors and automatic server-side prepared
  statements, and relies on **role-level settings** for the timeouts. Run once, as the database
  administrator:

  ```sql
  ALTER ROLE arkray_app SET statement_timeout = '10s';
  ALTER ROLE arkray_app SET lock_timeout = '5s';
  ALTER ROLE arkray_app SET idle_in_transaction_session_timeout = '60s';
  ALTER ROLE arkray_app SET jit = off;
  ALTER ROLE arkray_app SET search_path = public, extensions;  -- Supabase: pgvector's schema
  ```

  The workers' own role needs `statement_timeout = '60s'` (the Compose stack sets
  `DB_STATEMENT_TIMEOUT_MS=60000` for them): use a second role, or leave the web role's 10 s for
  both and accept it. **Migrations never go through a transaction pooler**: they use a direct or
  session-mode connection as the owner (they lift timeouts with `SET LOCAL`, and
  `CREATE INDEX CONCURRENTLY` cannot run in a pooled transaction).
- **Session-mode pooler** (Supavisor port 5432, IPv4): behaves like a direct connection; no
  special setting.
- **Supabase direct connections are IPv6** unless the IPv4 add-on is bought; Render's outbound
  network may not reach IPv6-only hosts: use the session-mode pooler host there. Verify at
  deployment.

## Supabase checklist (database only)

1. **Do not expose the database through the Data API.** Supabase grants new `public` tables to its
   `anon`, `authenticated` and `service_role` roles by default and publishes them over REST. The
   CRM's tables hold customer data and have no row-level security: turn the Data API off for the
   project (or expose no schema), and
   `REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;`
   `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;`
   as the owner role. A pilot must not go live before this is checked from outside (an anonymous
   request to the project's REST URL must be refused).
2. There is one database (`postgres`): `roles.sql` creates a database; instead run its role and
   privilege statements by hand against the existing one (`arkray_owner`, `arkray_app`, then
   `manage.py migrate` as the owner and `manage.py grant_app_privileges arkray_app`). The startup
   check that refuses a privileged role (R74, `DB_REQUIRE_RESTRICTED_ROLE`) stays on: `postgres`
   on Supabase is not a superuser, but the application must still connect as `arkray_app`.
3. pgvector index builds (Ask Arkray's semantic index) need more `maintenance_work_mem` than a
   small compute tier has: build indexes after raising it for the session, or keep the semantic
   index small on a pilot (the structured questions need none of it).
4. `pg_stat_statements` is on; the application logs no values, so database logs stay
   value-free (R63: the `roles.sql` log settings are per-database `ALTER DATABASE` statements:
   apply them with the platform's console if `log_min_error_statement` is not changeable).
5. Backups: the platform's PITR/daily backups replace `scripts/backup.sh`; take a logical dump
   (`pg_dump`) before every release as [deployment.md](deployment.md#rollback)
   requires.

## Render checklist (services)

Render runs the four deployables independently, all configured only by environment variables;
nothing needs a persistent local disk:

| Service | Render type | Image / start | Notes |
|---|---|---|---|
| API | Web Service (Docker) | backend image, default CMD | listens on `$PORT` (`gunicorn.conf.py`); set `WEB_CONCURRENCY` for the instance size; health check path `/health/ready` |
| Frontend | Web Service (Docker) | frontend image | `API_ORIGIN` is a **build argument** (the `/api` rewrite is resolved at build time): put the API service's internal URL in it; in production prefer routing `/api` to the API at the edge |
| Worker | Background Worker | `celery -A config worker --queues outbox,default,email --concurrency 2` | the three queue groups may be one or three workers |
| Index / AI workers | Background Worker | `--queues ai_index` / `--queues ai` | load the embedding model (about 0.5 GB each): only when Ask Arkray is enabled |
| Beat | Background Worker | `celery -A config beat` | **exactly one** instance |
| Migrations | pre-deploy command | `python manage.py migrate --noinput && python manage.py grant_app_privileges arkray_app` | as the owner role, on a direct connection |
| Redis | Key Value | two instances (broker with `noeviction` and AOF, cache with an eviction policy) | `rediss://` with a password; production refuses Redis without one |
| Files | external S3-compatible bucket | `ATTACHMENT_STORAGE=s3` + `ATTACHMENT_S3_*` | Render has no object storage; `ATTACHMENT_STORAGE=filesystem` needs a persistent disk and a single instance, so it is for rehearsals only |

- **Proxy count.** Render's edge appends the client address: set `TRUSTED_PROXY_COUNT` to the
  number of proxies in front of the API (verify with a request to a debug route in staging;
  getting it wrong lets clients choose their own address for rate limits or puts everyone behind
  one address). `FORWARDED_ALLOW_IPS` must name the platform's proxy range.
- `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` and `APP_BASE_URL` name the public host
  names; cookies are `__Host-` cookies, so the pages and `/api` must share one origin.
- Health checks: `/health/live` for liveness (no dependencies), `/health/ready` for readiness
  (PostgreSQL required; Redis degraded; attachment storage is **never** part of readiness: see
  [observability.md](observability.md)).

## Low-cost pilot sizing

The measured configuration ([capacity.md](capacity.md)) is not a minimum. A pilot for about 20
active users can run on: one API instance with 2 vCPU / 1 GB (4 gunicorn workers), one worker
instance (concurrency 2), a database with 1 GB RAM and 60+ connections, one small Redis for the
broker and one for the cache, a 0.5 GB frontend. Re-run `capacity_test.py` (documented in
capacity.md) against the real topology before sizing anything for 100 users on cheaper
hardware: the numbers describe the tested machine, not a formula.
