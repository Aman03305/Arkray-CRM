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
2. Cache misses cost requests nothing: the cache breaker checks Redis on a background
   thread. An Ask hand-off can still stall its request about 10 s while that breaker probes;
   the cool-downs grow to 2 minutes (R75). Semantic Ask
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
4. Support sessions and administrator-set passwords: the security events feed (Admin home,
   *Security activity*; `GET /api/v1/admin/security-events`) lists who started a session for
   whom and why, how it ended, who set whose password, and sign-ins with a temporary
   password. Every write during a session carries its `support_session_id` in the audit
   trail (`audit_event.support_session_id`): list a session's actions with
   `SELECT action, target_type, target_id, occurred_at FROM audit_event WHERE
   support_session_id = '<id>' ORDER BY occurred_at`.

## File storage

*Alert: `attachment_storage_failed` / `attachment_upload_failed` repeating; housekeeping
storage failures; `attachment_malware_blocked`; pending scans growing. The metric-based
alerts have their own sections: [storage degraded](#attachment-storage-degraded),
[object missing](#attachment-object-missing), [reconciliation](#attachment-reconciliation).*

1. Uploads and downloads answer 503 `storage_unavailable` while the bucket (or volume) is
   unreachable; nothing else in the CRM depends on it
   ([Attachment storage degraded](#attachment-storage-degraded)). Check the bucket's
   credentials, policy and region (`ATTACHMENT_S3_*`), or the volume's mount and free space.
2. Uploads that failed are marked failed (no row without a whole object can be downloaded);
   objects of deleted, failed or blocked files are removed by the hourly
   `activities.housekeeping`, idempotently, once storage is back.
3. Scanner down (`ATTACHMENT_SCANNER`): new files stay "being checked" and don't download;
   the scan jobs retry and then go dead (the dead-events alert). Restore clamd, then retry
   the dead `activities.scan_attachment` events ([Dead events](#dead-events)).
4. Malware blocked: the file was never downloadable, its object is deleted, the event is
   `attachment.rejected` (note and uploader in the audit trail). Treat the uploader's
   account as possibly compromised: [Security incident](#security-incident).

## Attachment storage degraded

*Alert: `ArkrayAttachmentStorageDown` (`arkray_attachment_storage_up` 0 for 5 minutes),
`ArkrayAttachmentUploadsFailing` (over 20 % of uploads failing on storage in an hour).*

1. **What users see:** uploads and downloads answer 503 "File storage is unavailable right
   now. Try again in a few minutes." with `Retry-After: 30`; notes, deals and everything
   else work, and readiness stays `ok` (storage is never a readiness dependency, by
   design). A failed upload leaves no file on the note (its row is `failed`, never
   downloadable).
2. **What the server does:** every storage call has a 12 s deadline, at most 4 run at once
   per process, and after 3 outage-like failures in a row a process answers 503 at once for
   15 s (doubling to 2 minutes), checking in the background whether storage is back
   (`attachment_storage_circuit_opened` once per process per outage).
   [reliability.md](reliability.md#attachment-storage) has the settings.
3. **Find out why:** `attachment_storage_failed` lines carry `operation`, `error_class` and
   `backend` (never a key or a name); `arkray_attachment_storage_errors_last_hour` counts
   them. `access_denied` is configuration: the credentials, the bucket policy (the role
   needs `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on the prefix, and
   `s3:ListBucket` on the bucket: without it S3 answers 403 instead of 404 for a missing
   object), the region; on a volume, its permissions. `timeout` / `connection`: the
   endpoint, DNS, the network path, the provider's status page. `server_error`: the
   provider (503 SlowDown is throttling: fewer clients or a higher limit). `no_space`: the
   volume, or the bucket's quota.
4. When it is fixed, the next background check closes each process's breaker within its
   cool-down (at most 2 minutes) and `arkray_attachment_storage_up` returns to 1 within
   30 s. Nothing to replay: failed uploads were refused, never half-stored; the hourly
   housekeeping removes what they may have written. Run a
   [reconciliation check](#attachment-reconciliation) afterwards if the outage involved
   lost or restored data, not for a plain outage.

## Attachment object missing

*Alert: `ArkrayAttachmentObjectMissing` (a download or a scan found a stored file's object
gone), or a reconciliation's `missing` count.*

1. **What users see:** that one file answers 410 "This file is no longer available. Ask an
   administrator." (code `attachment_unavailable`), distinct from an outage's 503. The rest
   of the note, and other files, work.
2. Treat it as possible tampering or data loss until explained: the log line
   `attachment_object_missing` (WARNING, `security: true`) names the attachment id and the
   operation, never the key or the file name. Was the object deleted outside the
   application (bucket lifecycle rule, a person with bucket access; check the provider's
   access log for the key, which the operator can read from `activities_attachment`), or
   was the bucket restored from an older backup than the database?
3. Run `manage.py reconcile_attachments` ([below](#attachment-reconciliation)) to see how
   many files are affected. One or a few: if the object can be recovered (a bucket version,
   a backup), put it back under the same key and the file works again. Otherwise run
   `--repair`: each lost file is marked unavailable (state `failed`, audited as
   `attachment.marked_unavailable`), leaves the note's list of files and stops alerting;
   its row, its audit trail and its note stay. Tell the note's author.
4. Many at once: stop. That is a wrong bucket or prefix (`ATTACHMENT_S3_BUCKET`,
   `ATTACHMENT_S3_PREFIX`), an unmounted volume, or a restore in progress, not lost files.
   `--repair` marks at most 50 per run unavailable for this reason.

## Attachment reconciliation

*Alerts: `ArkrayAttachmentReconcileMismatch`, `ArkrayAttachmentReconcileFailed`,
`ArkrayAttachmentReconcileNotRun` / `NeverRun`, `ArkrayAttachmentUploadStuck`.*

`manage.py reconcile_attachments` compares every row of `activities_attachment` with the
objects in storage: rows by keyset on the unique `storage_key` index, objects by listing
the bucket's prefix (or walking the volume) a page at a time, merged in key order; each
difference is confirmed on its own before it is reported. 300,000 files cost about 300
listing calls and 600 queries.

- **`--check`** (the default, and what the daily `activities.reconcile_check` runs at 22:10
  UTC): strictly read-only. Exit 0 healthy, 1 mismatches, 2 errors (storage failing: after
  5 failures in a row the pass stops instead of reporting files missing). The report names
  attachments by id; `--json` for scripts; `--show-keys` adds orphaned object keys
  (operators only: a key leads to a customer's file; never paste it into a ticket).
  `--limit`, `--batch-size`, `--rate` (storage calls per second, default 50) bound the
  cost; `--verify-hash` also downloads and hashes a sample (`--hash-sample`, default 1 %,
  at most `--hash-limit`, default 100).
- **What it finds:** `missing` (a stored file without its object), `orphaned` (an object no
  row names), `pending` (an upload unfinished past `--stale-after`, default 1 h),
  `pending_scan` (a malware scan pending past `--scan-stale-after`), `failed` (a deleted,
  failed or blocked file whose object is still there, or whose purge never finished),
  `restorable` (a file marked unavailable whose object came back), `size_mismatch`,
  `hash_mismatch`.
- **`--repair` is safe to run any time** (idempotent, resumable, each fix under a brief
  SKIP LOCKED row lock, re-checked against the row as it was seen; uploads younger than the
  grace period are never touched): stale uploads are finished when their object is whole
  (size, and hash with `--verify-hash`) or failed otherwise; lost objects' files are marked
  unavailable (at most `--max-repair-missing`, default 50); leftover objects of deleted,
  failed or blocked files are purged through the normal purge path (audited per run as
  `attachment.reconciled`). It **never** deletes a row, never deletes an object no row
  names, never touches `size_mismatch`, `hash_mismatch` or `restorable` files: those are
  decisions ([orphans](#orphaned-attachment-objects),
  [restores](#attachment-bucket-lost-or-restored)). Run it when a check reports mismatches
  and storage is healthy; not during an outage (it would only count errors) and not while a
  bucket restore is running.
- `pending_scan`: the scanner is down or its jobs died ([File storage](#file-storage),
  step 3). `size_mismatch` / `hash_mismatch`: the object was changed outside the
  application; restore it from a bucket version or backup, or mark the file failed by hand
  (`UPDATE activities_attachment SET state = 'failed', purged_at = now() WHERE id = ...`,
  **owner**) and tell the note's author.
- The daily check is off with `ATTACHMENT_RECONCILE_DAILY=false`; it stops after 15
  minutes (outcome 2). Its last outcome is on the metrics endpoint
  (`arkray_attachment_reconcile_*`).

## Orphaned attachment objects

An object no row names is never deleted automatically, not even by `--repair`. Rows are
written before their objects, so an orphan is not an upload in progress: it is a leftover
of a database restored to an earlier point than the bucket, a failed migration or test
data, or something written into the bucket by hand.

1. List them: `manage.py reconcile_attachments --show-keys --json` (keys are
   `YYYY/MM/<random>`: the month is when it was written).
2. Leave anything younger than 30 days: it may belong to a database restore you are about
   to redo.
3. Older ones: check a few against the provider's access log (who wrote them), then delete
   them with the provider's tools (with versioning, the old versions expire with the
   bucket's lifecycle rule). Record what you deleted and why in the incident or change log.

## Attachment bucket lost or restored

The database backup is not a backup of the files ([Restore from backup](#restore-from-backup),
step 6). After restoring either side:

1. Restore the bucket (or volume) to a point no earlier than the database's, then run
   `manage.py reconcile_attachments` (a check).
2. `missing` files: their objects aren't in the restored bucket. Look for them in a later
   bucket version, put them back under their keys, check again. What can't be found:
   `--repair` marks them unavailable ([Attachment object missing](#attachment-object-missing)).
3. `orphaned` objects: the database is older than the bucket. Keep them until you are sure
   the database restore is final, then follow [Orphaned attachment objects](#orphaned-attachment-objects).
4. `restorable` files (marked unavailable earlier, their object back now): check the object
   (`--verify-hash` compares its SHA-256 with the row's), then make the file live again by
   hand (`UPDATE activities_attachment SET state = 'stored', purged_at = NULL WHERE id = ...`,
   **owner**); reconciliation never does this on its own.
5. `failed` objects of deleted or erased files that the restore brought back: `--repair`
   deletes them again, as erasure requires ([privacy.md](privacy.md#erasure)).

## Security incident

1. Contain: deactivate the affected accounts (their sessions end at once); if a secret
   leaked, [rotate it](#rotate-a-secret).
2. Read what happened: the audit trail (`audit_event`, append-only) by actor, target and
   time; correlation ids join it to the access and proxy logs.
3. Notify as the law and the organisation's policy require ([privacy.md](privacy.md#breaches)).

## Deploy and roll back

[deployment.md](deployment.md#release-process): migrate as the owner with
`grant_app_privileges arkray_app`, `check --deploy --database default` with the
application's credentials, roll out, beat last. Roll back by redeploying the previous image
only while it can still write to the new schema (additive migrations since it; not across
`pipeline.0006` / `identity.0004`): otherwise roll forward with a fix or
[restore the backup](#restore-from-backup). Down-migrations past the v1.0 release candidate are
**refused** (`RefuseReverse`: nothing is undone and the message names the way out), so
`migrate pipeline 0005` fails safely instead of dropping the negotiated-price history: the only
ways back are the pre-upgrade backup or a forward fix
([deployment.md](deployment.md#rollback)).

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
6. **The database backup is not a backup of the files** (product enhancement phase, R93): it
   holds pipelines, stages, custom field definitions and values, negotiated price history and
   attachment *metadata*, but the attachment bytes live in object storage. Restore the bucket
   (or the attachments volume) from a point in time compatible with the database's: an
   object without a row is invisible (housekeeping never sees it), a row without an object
   downloads as 410 `attachment_unavailable`. Then run `manage.py reconcile_attachments`
   and follow [Attachment bucket lost or restored](#attachment-bucket-lost-or-restored). Support sessions in the backup end at
   once: their browser sessions are gone.

## Vacuum after a restore or a bulk load

A restore, a `VACUUM FULL` or a bulk import leaves an empty visibility map: index-only
figures (the dashboards) run about three times slower until vacuumed (R55). `scripts/
restore.sh` does it; otherwise: `VACUUM (ANALYZE);` (minutes at a million leads).

## Rotate a secret

- **`DJANGO_SECRET_KEY`**: set the new key and put the old one in
  `DJANGO_SECRET_KEY_FALLBACKS` for at least a day (the 12-hour session lifetime): sessions,
  sealed page links and trusted-browser cookies keep working; then remove it. A leaked key is
  rotated without the fallback: everyone signs in again.
- **Database passwords**: as an administrator, in `psql`, `\password arkray_app` (it prompts
  and sends only the hash: an `ALTER ROLE ... PASSWORD '...'` statement would land in
  `~/.psql_history` and, where `log_statement` is `ddl`, in the server log). Update
  `DATABASE_URL` in the secret manager, roll the pods.
- **The owner role (`arkray_owner`)**: `\password arkray_owner` the same way; update the
  migration job's `DATABASE_URL` (and `ARKRAY_OWNER_PASSWORD` where the deployment keeps it).
  Nothing long-running uses it, so nothing needs rolling.
- **The database superuser / administrator**: through the managed service's console (or
  `\password` on self-hosted PostgreSQL); update `POSTGRES_PASSWORD`/the administrator
  secret. The application never uses it.
- **SMTP (`EMAIL_URL`)**: create a new credential at the mail provider, update `EMAIL_URL`,
  roll the web tier and the `email` worker, check an invitation arrives, then revoke the old
  credential.
- **Attachment storage (S3)**: preferably an IAM role (nothing to rotate). With static keys
  (`ATTACHMENT_S3_ACCESS_KEY_ID`/`ATTACHMENT_S3_SECRET_ACCESS_KEY`): create a second access
  key, update both values, roll the web tier and the workers, check an upload and a
  download, then deactivate and delete the old key.
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
