# Runbooks

What to do when an alert fires ([observability.md](observability.md#alerts-phase-10)) and how
to carry out the routine operations. Commands run in the **backend image** (a one-off
container or job with the deployment's environment): `manage.py ...` below means
`python manage.py ...` there. Commands marked **owner** need the migrate job's database
credentials (`arkray_owner`); everything else runs with the application's. Every
correlation id in a log line, an error response (`request_id`) and the proxy's log is the
same id: start from it.

## Errors spike

*Alert: 5xx above 1 % for 5 minutes.*

1. Split the 5xx by status in the access log (`http_request` lines). **503
   `service_unavailable`** means the database can't be reached: go to
   [Database down](#database-down). **500** is a fault: continue.
2. Find the `django.request` ERROR lines for those routes; their `exc_type` and traceback
   (one JSON string) say where. The request's `correlation_id` joins the proxy line, the
   access line and any worker lines.
3. If it began with a release, [roll back](#deploy-and-roll-back) first and investigate after.
4. Statement timeouts (`QueryCanceled` in the traceback) on one route point to
   [Slow requests and search](#slow-requests-and-search).

## Slow requests and search

*Alert: p95 above 1 s for 10 minutes; search p95 above 1 s.*

1. Which routes? The access log's `duration_ms` by `route`.
2. Organisation-wide lists, dashboards and searches are the known slow shapes (R33, R40,
   R53, R59; [reliability.md](reliability.md#database-review-phase-10)). A name search
   sorted by name across the organisation can take up to ~1.6 s at a million leads (R33).
3. `pg_stat_statements` (created by `roles.sql`; needs
   `shared_preload_libraries=pg_stat_statements`): the top statements by total and mean
   time (normalised, no values). Compare with the measured baseline
   ([reliability.md](reliability.md#performance-baseline-phase-10)).
4. After a restore, a bulk import or a `VACUUM FULL`: [vacuum](#vacuum-after-a-restore-or-a-bulk-load).
5. Saturation instead of slow SQL: `arkray_db_server_connections`, CPU, and the requests
   queued at gunicorn (latency up, throughput flat): add web processes or pods within the
   [connection budget](reliability.md#connection-budget).

## Database down

*Alert: `arkray_db_up` 0 or 503 responses for 1 minute; readiness not ready.*

1. The application is behaving as designed: readiness 503 (the load balancer drains), live
   200 (no restart loops), API 503 `service_unavailable` with `Retry-After: 30`, writes
   refused cleanly, nothing half-written. It reconnects by itself: no restart needed (5-13 s
   after PostgreSQL returned, in the Phase 10 drill).
2. Fix the database (the provider's status, storage, `max_connections`, a failover).
3. When it's back: `arkray_db_up` 1, readiness `ok`, the outbox backlog drains
   (`arkray_outbox_oldest_due_seconds`).

## Database stalled

*Signature: requests hang about 30 s, then the proxy answers 502/504 (`upstream_status`
empty or 502 in its access log); gunicorn logs `WORKER TIMEOUT`; readiness 503;
`arkray_db_up` 0 or the scrape itself times out.*

1. The database accepts connections but doesn't answer (a failover in progress, a paused
   or overloaded host, a network partition). Nothing is lost: transactions that didn't
   commit roll back.
2. The provider's status and failover state; `pg_stat_activity` once reachable (a lock
   pile-up looks the same: [slow requests](#slow-requests-and-search)).
3. Don't restart the web tier: everything resumes the moment the database answers. If
   liveness probes are HTTP with short timeouts, they will restart pods meanwhile: probe at
   the TCP level ([reliability.md](reliability.md#failure-and-degradation-matrix)).

## Redis down

*Alert: readiness `degraded` for 10 minutes; `arkray_cache_up` or `arkray_broker_up` 0;
`cache_circuit_opened` / `ai_dispatch_circuit_opened` repeating.*

1. Nothing is lost: CRM writes keep succeeding and their background work waits in
   PostgreSQL's outbox. Sign-in and reset throttling, Ask Arkray's bulkheads and the
   admin-workspace audit window are PostgreSQL-backed and unaffected. The general request
   rate limit fails open meanwhile.
2. Expect a few requests per process to stall (about 4 s in DNS, 10 s for an Ask hand-off)
   while the breakers probe; their cool-downs grow to 2 minutes (R75). Semantic Ask
   questions fail `ai_unavailable`; routed ones are still answered.
3. Fix Redis. Workers and beat reconnect by themselves; the outbox drains within the
   in-flight caps (406 events in 50 s in the drill).

## Connection budget

*Alert: `arkray_db_server_connections` above 80 % of `arkray_db_max_connections`.*

1. Who holds them: `SELECT usename, application_name, state, count(*) FROM pg_stat_activity
   GROUP BY 1, 2, 3 ORDER BY 4 DESC` (as an administrator).
2. Idle connections count: `DB_CONN_MAX_AGE_S` keeps one per process. Recompute the
   [budget](reliability.md#connection-budget) for the current replicas × processes; scale
   down, lower the per-process count, or put PgBouncer in front.
3. Many `idle in transaction`: a stuck client; the 60 s idle-in-transaction timeout ends
   them.

## Outbox backlog

*Alert: `arkray_outbox_oldest_due_seconds` above 600 on a queue.*

1. Is the broker up (`arkray_broker_up`)? If not: [Redis down](#redis-down).
2. Is beat running (exactly one)? Without it nothing is dispatched.
3. Are the queue's workers running and consuming (`arkray_broker_queue_length` growing)?
   Restart or scale them. `ai_index` backlogs after a bulk change are normal: indexing runs
   at up to 20 events a second per relay ceiling, and at the workers' embedding speed for
   long notes (R70); scale `worker-index` temporarily.
4. Many retries (`outbox_event_retry_scheduled`): a dependency is failing (SMTP for
   `email`); fix it, the backoff resumes by itself.

## Dead events

*Alert: `arkray_outbox_events{status="dead"}` increases.*

1. `outbox_event_dead` lines carry the event id, topic and reason; the event's
   `last_error` holds the exception's type and message.
2. Fix the cause (a bug: release a fix; a dependency: restore it).
3. Re-queue: `manage.py outbox_requeue --topic <topic>` (or `--queue`, `--id`, `--since`)
   lists them; add `--yes` to re-queue with a fresh attempt budget. Work already pending
   again and payloads blanked after their retention are skipped and reported. For a dead
   invitation email, an administrator can also use *Resend invitation*.

## Workers

*Alert: `arkray_outbox_oldest_undelivered_seconds` above 5 minutes (work no worker has
started), a worker absent, retry or dead rates above baseline.*

1. The queue in the alert names the workers: `outbox`/`default`/`email` the main worker,
   `ai_index` the indexing worker, `ai` the question worker. The process supervisor's
   restarts and exit codes. A worker that exits at once with
   `database_role_refused` is connected as a privileged role: fix its `DATABASE_URL`
   (R74).
2. A warm shutdown finishes the running task (130 s grace); a killed worker's task is
   re-delivered or its lease expires (5 minutes) and it runs again: handlers are
   idempotent.

## Ask Arkray degraded

*Alert: `arkray_ai_breaker_open` 1 for 15 minutes, or `ai_unavailable` + `timeout` above
20 % of questions.*

1. Answers still come from the matching records; nobody sees an error.
2. `ai_provider_failed` lines give the `kind` (`rate_limited`, `server_error`,
   `overloaded`, `timeout`, `connection`, malformed). `ai_provider_rejected` with
   `status` 401/403: the key was revoked or lacks access: [rotate it](#rotate-a-secret).
3. The provider's status page; its rate limits against `API_THROTTLE_ASK` and the
   bulkheads.
4. To switch the model off: `AI_LLM_PROVIDER=none` (routed answers and records) or the
   kill switch `AI_ENABLED=false`.
5. Questions spinning for 90 s, then "took too long": the ai workers are down
   ([workers](#workers)); routed questions still answer.
6. "Note search is unavailable" and indexing retries logging `EmbeddingUnavailable`: the
   embedding model's files are missing or broken in the image or volume
   (`manage.py ai_fetch_model --verify`, [operations.md](operations.md#check-the-embedding-model-files)).
   A worker starts without loading the model, so this shows only at the first indexing or
   note search.
7. Ask answers 503 `ai_disabled` while the workers index: the tiers disagree on
   `AI_ENABLED` (a service recreated with another environment). Make it one value
   everywhere, then `manage.py ai_reindex --enqueue` for the notes written meanwhile.

## Web tier down

*Signature: pages answer 504, then 502, at the proxy; the API works; the web probe
(`GET /login`) fails.*

1. The web instances: their restarts and exit codes, memory. The API and the CRM's data
   are unaffected.
2. Recovery is a few seconds after an instance starts; nothing to drain.

## Re-indexing

*Alert: the `ai_index` backlog above 1,000 for 30 minutes, or its oldest due above an hour.*

See [Outbox backlog](#outbox-backlog). To rebuild the whole index (a new embedding model, a
suspect index): [operations.md](operations.md#rebuild-the-rag-index). The nightly
reconciliation re-indexes any source whose chunks drifted.

## Index bloat

*Alert (weekly check): a trigram index above 1.5 × its size after the last rebuild (R64).*

1. Sizes: `SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) FROM
   pg_stat_user_indexes WHERE indexrelname LIKE '%trgm%'`.
2. Rebuild off-peak, without blocking writes (**owner**):
   `REINDEX INDEX CONCURRENTLY activities_note_search_trgm;` (35.8 s for 2M activities in
   Phase 10). Record the new size as the baseline.

## Security events

*Alert: sign-in failures or lockouts spiking; unusual `workspace.accessed` volume.*

1. `login_failed` / `login_throttled` lines (a 12-character key, never the email) and the
   audit trail's `auth.*` events, by source address. Guessing is bounded per account and
   browser (5) and per address (50) in 15 minutes.
2. A whole office being throttled at sign-in is the shared-address case (R77): raise
   `API_THROTTLE_AUTH`, not the failure budgets.
3. `workspace.accessed`: which administrator opened whose workspace, once per window.
   Unexpected access: [Security incident](#security-incident).

## Security incident

1. Contain: deactivate the affected accounts (their sessions end at once); if a secret
   leaked, [rotate it](#rotate-a-secret).
2. Read what happened: the audit trail (`audit_event`, append-only) by actor, target and
   time; correlation ids join it to the access and proxy logs.
3. Notify as the law and the organisation's policy require ([privacy.md](privacy.md#breaches)).

## Deploy and roll back

[deployment.md](deployment.md#release-process): migrate as the owner with
`grant_app_privileges arkray_app`, `check --deploy --database default` with the
application's credentials, roll out, beat last. Roll back by redeploying the previous image:
migrations are backward compatible, no down-migration during the release window.

## Restore from backup

1. Prefer point-in-time recovery on the managed service into a **new** instance.
2. From a dump: prepare the target with `infrastructure/postgres/roles.sql`
   (`-v database=<new name>`, as the administrator: the database, the roles, the
   extensions), then, as the **owner**:
   `TARGET_DATABASE_URL=postgres://arkray_owner:...@host/<new name> scripts/restore.sh
   arkray-<stamp>.dump`. It refuses a dump without its `.sha256` (unless
   `RESTORE_UNVERIFIED=1`) and a database that already has tables, leaves out the
   extensions' own entries (roles.sql made them), restores, vacuums and prints row counts:
   about 7 minutes at a million leads. Verified as `arkray_owner` (Phase 11): every table
   owned by the owner, the grant and the release check clean afterwards.
3. `manage.py grant_app_privileges arkray_app` (**owner**), then
   `manage.py check --deploy --database default`.
4. Point the deployment at it; readiness `ok`; sign in; spot-check the dashboard's figures.
5. Sessions aren't in backups: everyone signs in again.

## Vacuum after a restore or a bulk load

A restore, a `VACUUM FULL` or a bulk import leaves an empty visibility map: index-only
figures (the dashboards) run about three times slower until vacuumed (R55). `scripts/
restore.sh` does it; otherwise: `VACUUM (ANALYZE);` (minutes at a million leads).

## Rotate a secret

- **`DJANGO_SECRET_KEY`**: set the new key and put the old one in
  `DJANGO_SECRET_KEY_FALLBACKS` for at least a day (the 12-hour session lifetime): sessions,
  sealed page links and trusted-browser cookies keep working; then remove it. A leaked key is
  rotated without the fallback: everyone signs in again.
- **Database passwords**: `ALTER ROLE arkray_app PASSWORD '...'` (as an administrator),
  update `DATABASE_URL` in the secret manager, roll the pods.
- **Redis password**: Redis supports two passwords during a change (ACL users); update
  `CELERY_BROKER_URL` and `REDIS_CACHE_URL`, roll everything, remove the old password.
- **`ANTHROPIC_API_KEY`**: create a new key, update the ai worker's secret, roll it, revoke
  the old key.
- **`METRICS_TOKEN`**: update the scraper and the deployment together.

## Erasure request

[privacy.md](privacy.md#erasure): `manage.py erase_lead <lead id> --by <admin email>` (a
dry run), then `--yes`; **owner** credentials when the lead's opportunities were lost with
a reason (the command says so).

## Purge old audit events

Only under a retention period agreed with the business, as a DBA with the **owner** role,
**off-peak**: disabling the trigger locks `audit_event` against writes until the
transaction ends, and every sign-in and CRM change writes an audit row (each waits up to
the 5 s lock timeout, then fails). So delete in small batches, each its own short
transaction, and repeat until a batch deletes nothing:

```sql
BEGIN;
ALTER TABLE audit_event DISABLE TRIGGER audit_event_append_only;
DELETE FROM audit_event WHERE id IN (
    SELECT id FROM audit_event WHERE occurred_at < now() - interval '2 years' LIMIT 5000);
ALTER TABLE audit_event ENABLE TRIGGER audit_event_append_only;
COMMIT;
```

Record that it was done (who, when, the cut-off) outside the application.

## First administrator and locked-out users

- First administrator: `manage.py createsuperuser`.
- A user locked out by failed sign-ins waits the lockout (at most 15 minutes) or resets
  their password; an administrator can reactivate a deactivated user on the Users page.
