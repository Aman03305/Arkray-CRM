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
5. One user's work is most of the backlog (`ArkrayQueueDominatedByOneUser`,
   `arkray_outbox_top_key_share` near 1): `manage.py outbox_requeue --status` lists, per
   queue, the pending events, how many users have some and the busiest users' ids (read only).
   Others are still served in turn (the relay takes turns between users, adversarial review of
   73ff1e3, M6), so this is about the account: a runaway script, a bulk import, or abuse. Talk
   to them, lower `ACTIVITY_MAX_CREATED_PER_USER_PER_DAY` if needed, or deactivate the
   account; its queued work drains at the workers' speed. If the alert recurs, scale
   `worker-index` to 2 or more (each loads its own embedding model, about 0.5-1 GB): it halves
   how long a newcomer's job can wait behind work already handed to the broker.

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
3. `workspace.accessed`: which administrator opened whose workspace, once per window (an
   Admin's team overview is target `all` with metadata `scope: team`).
   Unexpected access: [Security incident](#security-incident).
4. Support sessions and administrator-set passwords: the security events feed (Admin home,
   *Security activity*; `GET /api/v1/admin/security-events`) lists who started a session for
   whom and why, how it ended, who set whose password, and sign-ins with a temporary
   password. Every write during a session carries its `support_session_id` in the audit
   trail (`audit_event.support_session_id`): list a session's actions with
   `SELECT action, target_type, target_id, occurred_at FROM audit_event WHERE
   support_session_id = '<id>' ORDER BY occurred_at`.
5. `mfa.locked` / `ArkrayMfaLocked` / `ArkrayMfaLockedUntilReset`: see
   [Second factor locked](#second-factor-locked).
6. `ArkraySignInLocked` / `auth.sign_in_locked` (adversarial review of 73ff1e3, L1): an
   account reached 50 failed sign-ins from new browsers in a day, so its sign-in from new
   browsers is locked (its trusted browsers still work). Look at the sources (`login_failed`
   lines by address). The owner was emailed an unlock link (24 hours; it trusts the browser
   that opens it). If they can't use it, confirm who is asking out of band, then *Clear
   sign-in lock* on the Users page (step-up, audited `auth.sign_in_unlocked` by
   `administrator`), or have them reset the password. Repeated locks mean the password is
   under attack: advise a new password. Super Admin accounts also have mandatory two-step
   verification; Admin and User accounts do not. It lifts by itself after 30 days.
7. `ArkraySignInFailureStorm`: more than 200 failed sign-ins of real accounts in an hour, for
   10 minutes: guessing or password spraying. Check the source addresses (`login_failed`); if
   one network dominates, block it at the edge or the CDN.
8. `ArkrayAdministratorPromoted` (L3): someone was made a Super admin or an Admin (either
   counts, ADR-0035). Confirm with the promoting super admin and the person
   (`user.role_changed` in the security events). Not intended: make an Admin a User on the
   Users page; demote a super admin with the operator procedure
   ([security.md](security.md#administrator-account-protection)); or deactivate, and treat it
   as a [Security incident](#security-incident). A promotion refused with "can't be
   made an administrator until …" is the 7-day cooling-off after an administrator changed the
   account's password, email or second factor, or an Admin created it: wait, or have the
   owner confirm the change. "This user hasn't accepted their invitation yet…": promote them
   once they have signed in, or invite the administrator with their role.
   A password, email or second-factor change refused with "This account stopped being an
   administrator in the last 7 days…" is its mirror after a demotion: wait, or the owner uses
   *Forgot password* themselves.

## Administrator deactivated

*Alert: `ArkrayAdministratorDeactivated` (an administrator account was deactivated in the
last hour; it stays raised for that hour).*

1. Security events (Admin home, *Security activity*): who deactivated whom, when
   (`user.deactivated` with role `admin`, a Super admin, or `team_admin`, an Admin).
2. Planned off-boarding: nothing to do (for an Admin, see [Admins and teams](#admins-and-teams)
   for their team). Otherwise call the deactivated administrator on a
   known number. A super admin can deactivate any other administrator (only the last active
   super admin is protected; an Admin can't deactivate anyone outside their own team): if it
   wasn't intended, treat it as a [Security incident](#security-incident).
3. Reactivating them (Users page) doesn't bring the old password back: they get a reset link
   at their own address and keep their second factor (ADR-0033, R126). A reactivated Admin
   still has their team (a deactivation leaves it assigned).

## Admins and teams

[ADR-0035](adr/0035-super-administrators-and-teams.md): a **Super admin** (role `admin`)
manages everyone; an **Admin** (role `team_admin`) manages only the users in their team
(the users whose Admin they are), sees only their own and their team's CRM records, and
can't configure shared pipelines, read the security events or handle privacy requests. All of
the following is a super admin's job, on the Users page (each change asks for a recent second
factor):

- **After the upgrade**: every existing administrator is a Super admin and every existing
  user is in no team, so nothing changes until a super admin creates Admins and assigns
  users to them ([production-checklist.md](production-checklist.md)).
- **Make someone an Admin**: *New user* with the role Admin (always invited: they choose their
  own password), or *Change role* on an existing user (refused while the user is still
  invited: invite the administrator with their role instead; while an administrator-set
  password is still unchanged; and for 7 days after an administrator changed the account's
  password, email or second factor, or an Admin created it). They set up two-step
  verification at their next sign-in where it is required. A user made an Admin leaves their
  own team.
- **Give users an Admin** (put them in a team): *Change role or Admin* on the user, or the
  Admin field when adding one; only an Admin whose account is active or invited can be chosen.
  *Filter by Admin* → *No admin* lists the users in no team (`GET /api/v1/admin/users?manager=none`).
  API: `PATCH /api/v1/admin/users/{id} {manager, version}` (`manager: null` takes them out of
  every team). Audited `user.manager_changed`; nobody is signed out; it takes effect at each
  person's next request (a write already under way completes). A temporary password the
  previous Admin set for that user, still unchanged, is voided: the user is mailed a reset
  link (`auth.temporary_password_voided`).
- **An Admin's own day-to-day**: they add users (into their own team only, with an
  invitation or an initial password), and edit, deactivate, reactivate, re-invite, set
  passwords for, reset the second factor of and clear sign-in locks for their team's users.
  Anything about another account is a 404 to them.
- **Off-board an Admin**:
  1. Decide where their users go: *Filter by Admin* → that Admin, then move each user to
     another Admin, or to *No admin* (the super admins then manage them).
  2. Deactivate the Admin. Their sessions end, the invitations they sent are revoked, and the
     temporary passwords they set that are still unchanged are voided (those users are mailed
     a reset link). A deactivated Admin's team stays assigned to them (R174): until it is
     moved, only super admins manage those users.
  3. Reassign their own open work (*Change owner* on their deals) as for any leaver
     ([Staff leaving](#staff-leaving)).

  Or, if they stay but stop being an Admin, *Change role* to User: their team is unassigned
  in the same step (every member to *No admin*; the count is in the audit event), their
  pending invitations are revoked, the temporary passwords they set are voided, and their
  password, email and second factor stay out of other administrators' reach for 7 days
  (demote-then-unshield).
- **An Admin's credentials** are theirs alone, as a super admin's: no administrator sets their
  password, changes their email or resets their second factor. A lost second factor is the
  operator's `reset_mfa` ([Lost second factor](#lost-second-factor)).

## Lost second factor

A user without their authenticator and without any recovery code.

- **Ordinary user**: an administrator resets it (Users page, *Reset two-step verification*;
  `POST /api/v1/admin/users/{id}/mfa/reset`, needs the administrator's own recent code): a
  super admin for anyone, an Admin for their own team's users. Their
  sessions end; they sign in with their password and may set it up again.
- **Administrator** (a Super admin or an Admin): never another administrator
  (ADR-0030/0033/0035). Confirm the person's
  identity out of band (a call on a known number, their manager), then on a server:

  ```sh
  manage.py reset_mfa --email bina@example.com --by anita@example.com --reason "lost phone, ticket 1234"
  ```

  `--by` names the active **super** admin who asked for it ("--by must name an active super
  admin" otherwise: an Admin can't; the sole super admin may name themselves). Audited as
  `mfa.reset` (by `operator`, that super admin as the actor, the
  reason kept as an expiring detail); the owner is emailed; their sessions end; they sign in with their password and must set up a new authenticator before
  anything else.
- **Locked until an MFA reset** (they see "Resetting your password won't unlock it";
  `mfa_locked_until_reset`, R133): the same two resets are the only way out, but only after
  the steps in [Second factor locked](#second-factor-locked): the mailbox secured and the
  password the guesser knew replaced first, or the reset hands them the account.
- **After a `DJANGO_SECRET_KEY` rotation**: run `manage.py mfa_reencrypt` before removing the
  old key from `DJANGO_SECRET_KEY_FALLBACKS`; it exits non-zero if a secret is under no
  configured key (put the old key back, or reset those accounts).

## Second factor locked

*Alerts: `ArkrayMfaLocked` (a user's two-step verification was locked by too many wrong codes
in the last hour) and `ArkrayMfaLockedUntilReset` (one was locked past what a new password
unlocks); each stays raised for that hour.*

Codes are asked for only after the right password (or in a signed-in session), so 20 wrong
codes in a row mean someone else very probably has that user's password (deep security audit
M-4, R133); a new password unlocks that lock. The wrong codes since the last right one also
add up across new passwords: at 60 (`MFA_MAX_FAILURES_BEFORE_RESET`, three whole locks) the
factor is locked **until an MFA reset**, and no new password unlocks it (R133). Whoever typed
those codes got hold of each new password, so very probably reads the owner's mailbox too.
The account's owner was emailed when it locked (and about the first wrong code).

1. Security events (Admin home, *Security activity*): `mfa.locked` (whose; with `until:
   mfa_reset` for a lock until an MFA reset), and the `auth.login_failed` events before it
   (reasons `wrong_code`, `wrong_recovery_code`, `mfa_locked`); the audit trail's expiring
   detail has the client addresses. Which lock it is now: `mfa_lock` (`password_reset` or
   `mfa_reset`) on `GET /api/v1/admin/users/{id}`, or the counts in step 5. The user sees
   "Reset your password" for the first kind, "Resetting your password won't unlock it"
   (`mfa_locked_until_reset`) for the second.
2. Call the user on a known number. If the codes weren't theirs, treat it as a
   [Security incident](#security-incident): their password is known to someone else (and,
   locked until an MFA reset, very probably their mailbox).
3. Unlock a lock that a new password lifts (`mfa_lock` `password_reset`), any one of:
   - the user resets their password with *Forgot password* (the emailed link unlocks it and
     replaces the password the attacker knew): **preferred**;
   - an administrator sets a temporary password for an ordinary user (Users page,
     *Set password*): it unlocks it, the authenticator is kept;
   - an administrator resets an ordinary user's two-step verification (Users page), or an
     operator runs `manage.py reset_mfa` for an administrator (see
     [Lost second factor](#lost-second-factor)): the authenticator is removed too.

   Changing the password in a signed-in session does not unlock it (that session could be the
   attacker's).
4. Unlock a lock until an MFA reset (`mfa_lock` `mfa_reset`), in this order (a new password
   alone leaves it locked; resetting the factor first would let whoever has the old password
   sign in without a second factor, or set up their own as an administrator):
   1. On the call, the owner secures their mailbox first (its password, forwarding rules,
      other signed-in sessions).
   2. Replace the password the guesser knew: for an ordinary user an administrator sets a
      temporary one (Users page, *Set password*) and gives it on that call; an administrator
      resets their own with *Forgot password* once the mailbox is safe (a reset works while
      the factor is locked).
   3. Reset the second factor: an administrator for an ordinary user (Users page,
      *Reset two-step verification*), an operator with `manage.py reset_mfa` for an
      administrator ([Lost second factor](#lost-second-factor)). The owner then signs in with
      the new password and sets up a new authenticator (an administrator before anything
      else); its counts start from nothing.
5. Check the counts (read only): `SELECT u.email, d.consecutive_failures,
   d.cumulative_failures, d.failure_notified_at FROM identity_mfa_device d JOIN identity_user
   u ON u.id = d.user_id WHERE d.consecutive_failures > 0 OR d.cumulative_failures > 0 ORDER
   BY d.cumulative_failures DESC;` (`cumulative_failures`: wrong codes since the last right
   one, which only a right code or an MFA reset forgets.) Never set either count by hand
   without one of the steps above: the lock exists because the password (and, at the cap,
   probably the mailbox) is known.

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
`ArkrayAttachmentReconcileNotRun` / `NeverRun`, `ArkrayAttachmentUploadStuck`, `ArkrayAttachmentScanOverdue`.*

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

**Back to before the migrate job** (the release can't be fixed forward and the previous
images can't run on the new schema): [Emergency maintenance](#emergency-maintenance) on; stop
web, workers and beat; [Point-in-time recovery](#point-in-time-recovery) to T0 (noted just
before the migrate job: [Schema migration for a release](#schema-migration-for-a-release)), or
[Restore from backup](#restore-from-backup) with the dump taken before the upgrade; deploy the
**previous** images against it; the ledger replay and the
[Recovery validation checklist](#recovery-validation-checklist); maintenance off. Everything
recorded after T0 is lost; files uploaded since are orphans (R93).

## Emergency maintenance

*When: a restore or recovery, a stopped-deploy migration, an incident that needs the site
closed now.* The reference edge closes the site itself (`infrastructure/nginx/arkray.conf`);
nothing reaches Django or Next.js.

1. **On**: on the edge host, `touch infrastructure/nginx/maintenance/on` (the directory is
   mounted read-only at `/etc/nginx/maintenance`). No reload: from the next request every
   HTTPS request gets 503 with `Retry-After: 60` and `Cache-Control: no-store`; on `/api/*`
   the API's envelope (`service_unavailable`, "Arkray CRM is down for maintenance", the
   request id), elsewhere a minimal page with the request id. Open pages show "Arkray is
   temporarily unavailable" and don't retry by themselves. The container's health check
   (`/edge-health`, plain-HTTP port) keeps answering, so nothing restarts the edge.
2. **Check**: `curl -sI https://<host>/` → 503, `retry-after: 60`; the access log shows
   `upstream=-` for these requests.
3. **The switch doesn't stop background work**: stop workers and beat yourself when the
   database must not be written.
4. **Try the application behind it** (optional): add one address to `geo
   $arkray_maintenance_hold` in arkray.conf (`203.0.113.7/32 0;`) and reload (`docker compose
   ... exec nginx nginx -s reload`); remove it and reload afterwards. One host only: never an
   office's shared address or a proxy's (everyone behind it gets in). Behind Cloudflare or a
   load balancer it works only when nginx's `real_ip` takes the client address from the
   proxy's own ranges; otherwise every client has the proxy's address: don't. Alternatively
   test the API pods directly (a port-forward): the switch is the edge's only.
5. **Off**: `rm infrastructure/nginx/maintenance/on`; a page loads, `/api/v1/auth/csrf` 204.

Never commit the flag (the directory's `.gitignore` keeps it out): every deployment of that
commit would start closed. **Kubernetes**: mount a ConfigMap (the whole directory, not a
`subPath`, or updates never arrive) at `/etc/nginx/maintenance`; add the key `on` to close
(`kubectl patch configmap arkray-maintenance -p '{"data":{"on":""}}'`), remove it to reopen;
the kubelet syncs within about a minute, no restart. **A platform edge without nginx** (ALB,
Cloudflare): prepare the equivalent rule beforehand: a fixed 503 response for every path
except the health checks (the JSON body above for `/api/*`), `Retry-After`, `no-store`.

Verified live in the pinned nginx (2026-10-12): flag created and removed on the host through
the read-only mount, no reload; every route (the API in any case, pages, `/health/*`,
sign-in, an upload before its session check, HEAD) answered by the maintenance response,
the health check 200 and plain HTTP 308 throughout; an allowlisted address reached the
upstreams, a client sending `X-Forwarded-For` with that address did not
(`tests/architecture/test_edge_maintenance.py`).

## Fresh database provisioning

Once per environment, before the first release ([deployment.md](deployment.md#release-process)).

**Managed (RDS or equivalent)**

1. Create the instance: PostgreSQL 16, encrypted storage, not publicly accessible, deletion
   protection, automated backups with point-in-time recovery (retention 30 days: the dumps'
   and [privacy.md](privacy.md#retention)'s; RDS allows up to 35), a parameter group with the
   settings in [deployment.md](deployment.md#database) (`shared_preload_libraries =
   pg_stat_statements`, `log_min_error_statement = panic`, `log_error_verbosity = terse`,
   `log_min_duration_statement = -1`, memory, `max_connections`) and TLS required
   (`rds.force_ssl = 1`). Keep the provider's CA bundle for `sslrootcert`.
2. From a host inside the network, as the administrator (the master user): read the two
   passwords from the secret manager without echo or history (`read -rs OWNER_PASSWORD;
   read -rs APP_PASSWORD; export OWNER_PASSWORD APP_PASSWORD`), then `psql
   "$ADMIN_DATABASE_URL" -v database=arkray -f infrastructure/postgres/roles.sql` (psql 15
   or newer; never the passwords on the command line). On RDS the master user isn't a
   superuser: grant it membership of `arkray_owner` before the database is created
   (`GRANT arkray_owner TO <master user>`, after the role exists), and the per-database
   logging settings need `rds_superuser`'s rights (or the parameter group). It creates `arkray_owner`,
   `arkray_app`, the database owned by the owner, the four extensions, revokes `PUBLIC` and
   sets R63's logging per database. A platform with one fixed database (Supabase): run its
   role and privilege statements by hand ([hosting-compatibility.md](hosting-compatibility.md#supabase-checklist-database-only)).
   *roles.sql has been run against self-hosted PostgreSQL only; check its output on the
   managed service the first time.*
3. The first release's migrate job, as the **owner** over a direct connection:
   `manage.py migrate --noinput && manage.py grant_app_privileges arkray_app`, then with the
   application's credentials `manage.py check --deploy --database default` and
   `manage.py migrate --check` ([Schema migration for a release](#schema-migration-for-a-release)).
4. Record the instance, its backup window and retention, and confirm the latest restorable
   time moves (within 5 minutes) before any real data arrives.

**Self-hosted (one VM, PostgreSQL in a container or from packages)**

1. PostgreSQL 16 with pgvector 0.8 (the production stack's image:
   `pgvector/pgvector:0.8.7-pg16-trixie@sha256:40d828ed...`), the data directory on a volume of
   its own (encrypted), `--shm-size` of at least 256 MB under Docker, the settings table of
   [deployment.md](deployment.md#database), TLS with a certificate for its host name.
2. **WAL archiving before any data** ([Database backup](#database-backup)): `pgbackrest
   stanza-create` and `pgbackrest check` (or WAL-G's `wal-g wal-push` working), then the first
   full base backup.
3. roles.sql as the superuser, then step 3 above. The superuser connects on the server's
   local socket only, the owner from the release path only, the application role over TLS
   (`infrastructure/postgres/pg_hba.conf`, which the production-shaped stack mounts;
   adversarial review of 73ff1e3, M4). On that stack the `db-roles` job runs roles.sql over
   the socket, and `scripts/prod-compose.sh <secrets> run --rm db-roles` runs it again;
   roles.sql refuses role passwords under 32 characters or one for both roles.

Drilled: `tests/drills/pitr_drill.py` provisions its database exactly this way (roles.sql with
the roles renamed, migrate as the owner, the grant: 10 statements, `check`, `migrate --check`
as the application role) before taking its base backup.

## Schema migration for a release

The migrate job runs the new image as the **owner** (never through a transaction pooler:
[deployment.md](deployment.md#database-poolers)).

1. **Rolling or stopped?** The release notes and
   [deployment.md](deployment.md#which-previous-release-can-run-beside-the-new-schema): a
   rolling deploy only when the previous release can insert into the new schema (every new
   NOT NULL column has a database default). From the v1.0 RC: stopped deploy only.
2. **Before** ([deployment.md](deployment.md#before-an-upgrade)): `scripts/backup.sh` (checked,
   encrypted, signed); `manage.py showmigrations > pre-upgrade-migrations.txt`; `manage.py
   migrate --plan` with the new image; the previous images kept by SHA; on a managed service,
   the latest restorable time noted.
3. **Rolling deploy**: migrate job `manage.py migrate --noinput && manage.py
   grant_app_privileges arkray_app`; release check with the application's credentials
   `manage.py check --deploy --database default`; `manage.py preflight`
   ([deployment.md](deployment.md#preflight)); roll out api, workers and web; beat last.
4. **Stopped deploy**:
   1. [Emergency maintenance](#emergency-maintenance) on.
   2. Stop web, workers and beat (each worker finishes its task: up to 130 s, the default
      queue's worker up to 18 minutes when the nightly attachment check is running; plan
      the window outside 22:10 UTC). Check nothing
      of the application is connected: `SELECT count(*) FROM pg_stat_activity WHERE usename
      = 'arkray_app';` → 0.
   3. **Note T0**, the time now in UTC (`SELECT now();` on the database): the point-in-time
      recovery target if the release has to be undone ([Deploy and roll back](#deploy-and-roll-back)).
   4. The migrate job and the grant as in 3; the release check; `manage.py preflight` with
      the new image ([deployment.md](deployment.md#preflight)).
   5. Start the new release (api, workers, web, beat last); sign in from the allowlisted
      address and open a deal; then maintenance off.
5. A failed migration: each one runs in its own transaction (`CREATE INDEX CONCURRENTLY` ones
   don't: look for invalid indexes, `SELECT indexrelid::regclass FROM pg_index WHERE NOT
   indisvalid;`). Fix forward, or [Deploy and roll back](#deploy-and-roll-back). Down-migrations past the RC are
   refused before anything changes.
6. **The adversarial-review release** (on top of `73ff1e3`): if `identity.0008` logs
   `pseudonym_domain_accounts_not_pseudonymised`, give each account it lists a real address on
   the Users page: an administrator gave it an address at `pseudonymised.invalid` before this
   release, and the migration left it live (those listed as moved aside held a former user's
   pseudonym, which went back to its owner; review round 1, IDENTITY-4 and IDENTITY-5). Deploy
   the workers before the web tier, or re-run the dead purge events afterwards
   ([Erasure ledger behind](#erasure-ledger-behind)).

Measured (2026-10-06): RC → current at 200,000 leads, 8.6 s of migrations (`pipeline.0006`
7.0 s). The window is backup + migrations + smoke test.

## Database backup

Two layers ([deployment.md](deployment.md#backup-restore-and-disaster-recovery)): continuous
WAL archiving for point-in-time recovery (RPO ≤ 5 minutes), and daily encrypted, signed
logical dumps (`scripts/backup.sh`) in another region. Neither holds the attachment files
(R93: back the bucket up on the same schedule) or the erasure ledger (by design).

**Managed**: point-in-time recovery on, retention 30 days, cross-region copies of the
automated backups (RDS: cross-Region automated backup replication) or daily snapshot copies;
check the latest restorable time daily (RDS: `aws rds describe-db-instances --query
'DBInstances[0].LatestRestorableTime'`) and alert when it is more than 10 minutes old (RDS
uploads the transaction log every 5 minutes; a lag beyond 10 breaks the 5-minute RPO).
Logical dumps daily and before every release.

**Self-hosted: WAL archiving to object storage** with pgBackRest or WAL-G (both stream WAL
segments and base backups to S3 and restore to a point in time). The bucket: in another
region, private, versioned, SSE-KMS, lifecycle expiring noncurrent versions after 7 days; the
VM's IAM role may write and delete only under its prefix (expiry needs delete). Never the
attachments or ledger bucket.

The stock `pgvector/pgvector` image has neither tool: build a small image from it (pinned by
digest like the others) that adds `pgbackrest` (Debian package) or the `wal-g` binary, since
`archive_command` runs inside the PostgreSQL server's own container. Never put a comment after
a value in the files below: neither pgBackRest's configuration nor an environment file strips
it (the whole rest of the line becomes the value).

`postgresql.conf` (both; `archive_command` from the tool's section below):

    wal_level = replica
    archive_mode = on
    # a quiet database still ships its WAL every minute (RPO)
    archive_timeout = 60
    max_wal_senders = 3

pgBackRest (`/etc/pgbackrest/pgbackrest.conf`, owner postgres, mode 600; credentials from the
instance's IAM role: `repo1-s3-key-type=auto`; retention in days):

    [global]
    repo1-type=s3
    repo1-s3-bucket=arkray-pg-backups-<env>
    repo1-s3-region=ap-south-2
    repo1-s3-endpoint=s3.ap-south-2.amazonaws.com
    repo1-s3-key-type=auto
    repo1-path=/pgbackrest
    repo1-cipher-type=aes-256-cbc
    repo1-cipher-pass=<from the secret manager; also kept offline with the backup keys>
    repo1-retention-full-type=time
    repo1-retention-full=30
    compress-type=zst
    start-fast=y
    archive-async=y
    spool-path=/var/spool/pgbackrest
    process-max=2

    [arkray]
    pg1-path=/var/lib/postgresql/data

  In `postgresql.conf`: `archive_command = 'pgbackrest --stanza=arkray archive-push %p'`.
  Once: `pgbackrest --stanza=arkray stanza-create`, `pgbackrest --stanza=arkray check`.
  Schedule (cron or a systemd timer, as postgres): Sunday `pgbackrest --stanza=arkray
  --type=full backup`, other days `--type=diff`. Status: `pgbackrest info` (alert when the
  newest backup is older than 26 hours).

WAL-G (a configuration file the PostgreSQL server reads through `--config`, because
`archive_command` runs as the server's process and doesn't see a login shell's environment:
`/etc/wal-g/walg.json`, owner postgres, mode 600; credentials from the instance's IAM role):

    {
      "WALG_S3_PREFIX": "s3://arkray-pg-backups-<env>/wal-g",
      "AWS_REGION": "ap-south-2",
      "WALG_S3_SSE": "aws:kms",
      "WALG_COMPRESSION_METHOD": "zstd",
      "WALG_LIBSODIUM_KEY": "<32 random bytes, hex; from the secret manager, also kept offline>",
      "WALG_LIBSODIUM_KEY_TRANSFORM": "hex",
      "PGHOST": "/var/run/postgresql"
    }

  In `postgresql.conf`: `archive_command = 'wal-g --config /etc/wal-g/walg.json wal-push %p'`
  (and, for a recovery, `restore_command = 'wal-g --config /etc/wal-g/walg.json wal-fetch %f
  %p'`). Daily: `wal-g --config /etc/wal-g/walg.json backup-push $PGDATA`, then `wal-g
  --config /etc/wal-g/walg.json delete retain FULL 30 --confirm` (30 daily full backups and the
  WAL they need). Status: `wal-g --config /etc/wal-g/walg.json backup-list --detail`.

**Their connection on the production-shaped stack** (`infrastructure/postgres/pg_hba.conf`;
review round 1, INFRA-2): the tool runs beside the server, in its container, and PostgreSQL
admits only the superuser there, on the local socket, with SCRAM (`local all arkray` and
`local replication arkray`; nothing replicates over the network). So pgBackRest takes
`pg1-user=arkray` and `pg1-socket-path=/var/run/postgresql` in its `[arkray]` stanza, WAL-G
`"PGUSER": "arkray"` beside its `"PGHOST"`, and both the password from a `.pgpass` file:
one line `*:*:*:arkray:<the superuser's password>` (the `*` database matches `replication`
too), mode 600, owned by uid 999, mounted read-only (the root filesystem is read-only, so not
in the home directory) and named by `PGPASSFILE` in the PostgreSQL service's environment.
Checked live: `pg_basebackup -h /var/run/postgresql -U arkray` streamed a base backup over
the socket; the same from the application's network, as the superuser or as `arkray_app`, was
refused (`pg_hba.conf rejects replication connection`), and over the socket without a
password too.

Monitoring (both): `pg_stat_archiver` (alert when `failed_count` grows or `last_archived_time`
is more than 5 minutes behind while there are writes: WAL then piles up in `pg_wal` and the
RPO grows); the newest base backup's age; disk space of `pg_wal`. Lose the encryption
passphrase/key and the archive is unreadable: keep it with the backup key holders
([Backup encryption keys](#backup-encryption-keys)).

**Logical dumps** (both): `scripts/backup.sh` daily and before every release
([Backup encryption keys](#backup-encryption-keys)); `--prune` 30 days honouring
`LEGAL_HOLD`. On the production-shaped stack the database admits the owner only from
`db-owner` (review round 1, INFRA-1), so the dump runs in a one-off container of the pinned
PostgreSQL image on that network, with the stack's CA, the backup host's keys and the owner's
password read from the secrets file into that container alone (never the shell, never a
command line); `/srv/arkray-backups` writable by uid 999:

    project=arkray-prod
    secrets=/secure/arkray-prod.env
    image='pgvector/pgvector:0.8.7-pg16-trixie@sha256:40d828eda66737aaacb934387a337babf1455ebd56e9bdd13fc08e211257c972'
    docker run --rm --user 999:999 --network "${project}_db-owner" \
      --env-file <(grep '^ARKRAY_PROD_DB_OWNER_PASSWORD=' "$secrets") \
      -v "${project}_tls-ca:/tls:ro" -v "$PWD/scripts:/scripts:ro" \
      -v /secure/backup-keys:/keys:ro -v /srv/arkray-backups:/backups \
      -e BACKUP_GPG_RECIPIENT_FILE=/keys/backup-public-key.asc \
      -e BACKUP_GPG_SIGNING_KEY_FILE=/keys/backup-signing-key.asc -e HOME=/tmp \
      --entrypoint bash "$image" -c 'DATABASE_URL="postgres://arkray_owner:${ARKRAY_PROD_DB_OWNER_PASSWORD}@postgres:5432/arkray?sslmode=verify-full&sslrootcert=/tls/ca.crt" exec bash /scripts/backup.sh /backups' A dump restores into any PostgreSQL 16 ([Restore from backup](#restore-from-backup));
it is also the copy that survives a broken WAL archive.

Drill: `tests/drills/pitr_drill.py` archives with a plain `archive_command` to a volume (the
same mechanics: base backup, archived segments, `restore_command`).

## Point-in-time recovery

*When: data damaged or deleted by mistake, a bad release after the migrate job, a lost
database. A dump restore is the same with T = the dump's time ([Restore from backup](#restore-from-backup)).*

Everything committed after T is lost (re-entered by people, or recovered by hand from the
old database). Erasures after T come back and are re-applied by the ledger. **Security
changes after T are undone too**: sessions that ended, deactivations, password, role and MFA
changes, ended support sessions, used or revoked one-time links (step 7).

1. **Choose T** (UTC, with its offset): just before the damage, from the log line or audit
   event that shows it, or T0 noted before a migrate job. Recovery stops before the first
   transaction that committed after T.
2. **Stop traffic and writes**: [Emergency maintenance](#emergency-maintenance) on; stop
   web, workers and beat. Leave the old database untouched (forensics).
3. **Keep what will be lost**, if the old database is readable (owner; on the
   production-shaped stack the superuser on the socket, `scripts/prod-compose.sh <secrets>
   exec postgres psql -U arkray -d arkray`, since `pg_hba.conf` admits no one else there):
   `\copy (SELECT id, occurred_at, action, actor_id, target_type, target_id, subject_user_id
   FROM audit_event WHERE occurred_at > '<T>' ORDER BY id) TO 'after-T-events.csv' CSV HEADER`
   (ids only), and note the deals and activities created or changed after T if they must be
   re-entered.
4. **Recover into a new database**:
   - *Managed (RDS)*: `aws rds restore-db-instance-to-point-in-time
     --source-db-instance-identifier arkray-prod --target-db-instance-identifier
     arkray-prod-<YYYYMMDDHHMM> --restore-time <T as 2026-10-12T05:56:05Z>
     --db-parameter-group-name <arkray-pg16> --db-subnet-group-name <...>
     --vpc-security-group-ids <...> --no-publicly-accessible [--multi-az]`, then `aws rds
     wait db-instance-available ...`. Name the parameter group (else R63's logging settings
     and `pg_stat_statements` are missing); set backup retention, deletion protection and
     monitoring as on the original. The new instance has a new endpoint. (Platforms that
     restore in place, such as Supabase's PITR, overwrite the database: do step 3 first.)
   - *Self-hosted*: the recovered cluster starts with the same `hba_file`
     (`infrastructure/postgres/pg_hba.conf`) and keeps the database's name, `arkray`, so the
     roles connect as before; the archive tool connects as the superuser on the socket
     ([Database backup](#database-backup)).
   - *Self-hosted (pgBackRest)*: on a new volume or host, PostgreSQL stopped:
     `pgbackrest --stanza=arkray --type=time "--target=<T>" --target-action=promote restore`
     (it picks the newest backup before T, writes `restore_command`, the target and
     `recovery.signal`); start PostgreSQL.
   - *Self-hosted (WAL-G)*: `wal-g backup-list --detail` → the newest backup that finished
     before T; `wal-g backup-fetch $PGDATA <that backup>`; `touch $PGDATA/recovery.signal`;
     in `postgresql.auto.conf`: `restore_command = 'wal-g --config /etc/wal-g/walg.json
     wal-fetch %f %p'`,
     `recovery_target_time = '<T>'`, `recovery_target_action = 'promote'`; start.
   - Either: PostgreSQL's log must show `recovery stopping before commit of transaction ...,
     time <after T>`, `selected new timeline ID`, `archive recovery complete`, and `SELECT
     pg_is_in_recovery();` must be `f`. `recovery ended before configured recovery target was
     reached` means the archive lacks WAL up to T: choose an earlier T or find the segments.
     Archive the new timeline (pgBackRest: the same stanza once the old server is stopped;
     WAL-G: a new `WALG_S3_PREFIX`) and take a full base backup now.
5. **End every session** (owner): `DELETE FROM django_session;`. A dump leaves sessions out; a
   point-in-time recovery brings back every session as at T, including ones signed out,
   deactivated or ended by a password or role change since (drilled). Everyone signs in
   again.
6. **Credentials and privileges**: role passwords are as at T; if `arkray_app` or
   `arkray_owner` was rotated after T, set it again (`\password`). Point the migrate job's
   `DATABASE_URL` at the new database; `manage.py grant_app_privileges arkray_app` (owner);
   `manage.py check --deploy --database default` (application credentials).
7. **Re-apply the erasures** ([Restore from backup, step 4](#restore-from-backup)): the API stays
   closed (503 `erasure_reconciliation_required`) until it has run.
8. **Redo the security changes after T** from step 3's list: deactivate again, reset MFA
   again, change roles again, end support sessions; resend invitations created after T (they
   are gone). One-time links used or revoked after T work again until they expire
   (invitations and temporary passwords 72 h, resets 1 h): revoke them if that matters.
9. **Validate** ([Recovery validation checklist](#recovery-validation-checklist)), point the
   application's `DATABASE_URL` at the new database, start api, workers, web, beat last;
   check from the allowlisted address; maintenance off.
10. Keep the old database until the recovery is accepted; then delete it (it holds the data
    erased since T until then).

Measured (2026-10-12, `tests/drills/pitr_drill.py`, development machine, PostgreSQL 16 in
Docker): a base backup of the schema-only cluster (41 MB) 1.9 s; restore of the base backup
0.8 s; WAL replay to T and promotion 1.6 s for 32 MB of WAL, **11-24 s for 1,040 MB** (redo
8-19 s; 45-95 MB/s with the archive copies, two runs); ledger replay 1.8-2.2 s; `VACUUM
(ANALYZE)` 0.3 s; recovery to the API reopening 10.7 s, with the whole validation 27.8 s.
Estimate a real one as: fetching the base backup (its size over the storage's throughput) +
the WAL since it (up to about 25 s per GB here; R64: 50,000 note edits wrote 3.2 GB) +
vacuum (13-21 s at a million leads) + the validation and cutover (allow 15-30 minutes of
people's time).

## Restore from backup

1. Prefer [point-in-time recovery](#point-in-time-recovery) into a **new** instance; the
   steps below are for a dump.
2. From a dump: prepare the target with `infrastructure/postgres/roles.sql`
   (`-v database=<new name>`, as the administrator: the database, the roles, the
   extensions), bring in the backup private key ([Backup encryption keys](#backup-encryption-keys)),
   then, as the **owner**:
   `TARGET_DATABASE_URL=postgres://arkray_owner:...@host/<new name>
   BACKUP_GNUPGHOME=<keyring> scripts/restore.sh arkray-<stamp>.dump.gpg`. It checks the
   encrypted file's checksum, decrypts into a private temporary directory (removed
   afterwards), and refuses a dump without its `.sha256` (unless
   `RESTORE_UNVERIFIED=1`) and a database that already has tables, leaves out the
   extensions' own entries (roles.sql made them), restores, vacuums and prints row counts:
   about 7 minutes at a million leads. Verified as `arkray_owner` (Phase 11): every table
   owned by the owner, the grant and the release check clean afterwards.
3. `manage.py grant_app_privileges arkray_app` (**owner**), then
   `manage.py check --deploy --database default`.
4. **Re-apply the erasures made since the backup, before any traffic** (privacy remediation
   P2-8): the restored database is behind the erasure ledger, so the API answers 503
   `erasure_reconciliation_required` and readiness fails until, with the **owner**'s
   credentials and the same `ERASURE_LEDGER_URL`, `ERASURE_LEDGER_KEY` (and
   `ERASURE_LEDGER_VERIFY_KEYS`, during a key rotation) as production:
   `manage.py replay_erasures --by <admin email> --report restore-<date>.json --dry-run`
   (what it would do), then without `--dry-run`. Exit 0: complete, the API reopens within a
   minute; 1: an entry failed (the report names it; fix and rerun, the API stays closed);
   2: the ledger can't be read, was tampered with, or doesn't hold what the database has
   applied (an empty or another environment's ledger: check the URL and the volume; nothing
   changed: restore the ledger from its own versioned store, never edit
   `core_erasure_ledger_state` to match). A gap of at most `ERASURE_LEDGER_AUTO_RECONCILE_MAX`
   entries is replayed by the automatic reconciliation within a minute if you don't; run
   the command anyway to keep the report. Keep the
   report with the restore's record. **If the ledger is lost** the API stays closed: recover
   it from its bucket's versions or Object Lock copies; reopening without it would serve
   people who asked to be erased.
5. Point the deployment at it; readiness `ok`; sign in; spot-check the dashboard's figures.
6. Sessions aren't in dumps: everyone signs in again (a point-in-time recovery brings them
   back: [empty them](#point-in-time-recovery), step 5). Any restore undoes the security
   changes made after the backup (deactivations, password, role and MFA changes, revoked
   links): redo them from the security events after T (R140).
**On the production-shaped stack** (review round 1, INFRA-1: its `pg_hba.conf` admitted the
roles to `arkray` alone, and the restore had nowhere to go). Steps 2 to 5 are these commands,
run from the checkout with the stack's secrets file; `arkray_restored` is the one other
database `pg_hba.conf` admits the owner (from `db-owner`) and the application role to, so no
rule changes and no reload. Everything stops but the data services for the swap. The secrets
reach containers through the secrets file or the services' own environment, never the shell
or a command line:

    project=arkray-prod
    secrets=/secure/arkray-prod.env
    prod=(scripts/prod-compose.sh "$secrets" --project "$project")
    image='pgvector/pgvector:0.8.7-pg16-trixie@sha256:40d828eda66737aaacb934387a337babf1455ebd56e9bdd13fc08e211257c972'
    # 2a. the empty database, its extensions and settings: roles.sql as the superuser, on the socket
    "${prod[@]}" run --rm --no-deps -T db-roles psql -h /var/run/postgresql -U arkray -d postgres \
      -v database=arkray_restored -f /deploy/roles.sql
    # 2b. the restore, as the owner, from db-owner; the backup private key's keyring brought in
    #     (writable: gpg locks it); a work area with room for the dump twice
    docker run --rm --user 999:999 --network "${project}_db-owner" \
      --env-file <(grep '^ARKRAY_PROD_DB_OWNER_PASSWORD=' "$secrets") \
      -v "${project}_tls-ca:/tls:ro" -v "$PWD/scripts:/scripts:ro" -v /srv/arkray-backups:/backups:ro \
      -v /secure/restore-keyring:/keyring -v /secure/backup-keys/backup-signing-public-key.asc:/signer.asc:ro \
      -v /srv/arkray-restore-work:/work -e BACKUP_GNUPGHOME=/keyring -e BACKUP_SIGNER_KEY_FILE=/signer.asc \
      -e RESTORE_WORK_DIR=/work -e HOME=/tmp --entrypoint bash "$image" -c \
      'TARGET_DATABASE_URL="postgres://arkray_owner:${ARKRAY_PROD_DB_OWNER_PASSWORD}@postgres:5432/arkray_restored?sslmode=verify-full&sslrootcert=/tls/ca.crt" exec bash /scripts/restore.sh /backups/arkray-<stamp>.dump.gpg'
    # 3-4. the grant, nothing unapplied, the replay (owner); the release check (application role):
    #      each service's own DATABASE_URL, pointed at arkray_restored inside the container
    restored='export DATABASE_URL="${DATABASE_URL/5432\/arkray\?/5432\/arkray_restored?}"'
    "${prod[@]}" run --rm --no-deps -T -v "$PWD/restore-reports:/reports" migrate bash -c "$restored
      python manage.py grant_app_privileges arkray_app && python manage.py migrate --check &&
      python manage.py replay_erasures --by <admin email> --report /reports/restore-<date>.json"
    "${prod[@]}" run --rm --no-deps -T backend bash -c "$restored
      exec python manage.py check --deploy --database default"
    # 5. the swap: stop what connects, set the current database aside, the restored one in its place
    "${prod[@]}" stop backend worker worker-outbox worker-index worker-ai beat
    "${prod[@]}" exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -U arkray -d postgres -v ON_ERROR_STOP=1' <<SQL
    SELECT pg_terminate_backend(pid) FROM pg_stat_activity
      WHERE datname IN ('arkray', 'arkray_restored') AND pid <> pg_backend_pid();
    ALTER DATABASE arkray RENAME TO arkray_replaced_$(date -u +%Y%m%d%H%M);
    ALTER DATABASE arkray_restored RENAME TO arkray;
    SQL
    "${prod[@]}" up -d

(`restore-reports/` writable by uid 10001, the image's user; the replay's `--dry-run` first, as
in step 4.) The set-aside database keeps its name until the restore is accepted, then `DROP
DATABASE arkray_replaced_<stamp>;` on the same socket (it holds data erased since the backup).
Only the superuser reaches it meanwhile, inside the PostgreSQL container: the owner and the
application role are refused on any name but `arkray` and `arkray_restored`. Verified live
(2026-10-12, a throwaway project): with the earlier rules the restore stopped at `pg_hba.conf
rejects connection ... database "arkray_restored"`; with these, the dump (signed, encrypted)
restored as the owner, the grant, `migrate --check`, the release check as `arkray_app` and the
replay passed, the swap brought the API back healthy on the restored rows, and the old
database was refused to the owner.

7. **The database backup is not a backup of the files** (product enhancement phase, R93): it
   holds pipelines, stages, custom field definitions and values, negotiated price history and
   attachment *metadata*, but the attachment bytes live in object storage. Restore the bucket
   (or the attachments volume) from a point in time compatible with the database's: an
   object without a row is invisible (housekeeping never sees it), a row without an object
   downloads as 410 `attachment_unavailable`. Then run `manage.py reconcile_attachments`
   and follow [Attachment bucket lost or restored](#attachment-bucket-lost-or-restored). Support sessions in the backup end at
   once: their browser sessions are gone.

## Recovery validation checklist

After any restore or recovery, before maintenance is switched off. Keep the results with the
restore's record.

- [ ] **The database**: point-in-time: PostgreSQL's log shows `recovery stopping before
      commit ..., time ...` later than T and `archive recovery complete`;
      `pg_is_in_recovery()` is `f`. Dump: `restore.sh` printed `checksum: ok`, `signature: ok`.
- [ ] **Row counts**: `restore.sh` prints them (compare with the source's counts noted at the
      backup); for a recovery, `SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY
      1;` after the vacuum below, and the leads, deals and activities counts against the last
      known dashboard figures.
- [ ] **Schema**: `manage.py migrate --check` exits 0 and `manage.py showmigrations`' last
      `[X]` per app are the deployed release's leaf nodes (the images match the database).
- [ ] **Privileges and the release check**: `manage.py grant_app_privileges arkray_app`
      (owner); `manage.py check --deploy --database default` (application credentials): no
      `arkray.E001` (the runtime role could rewrite the audit trail) or `arkray.W002`
      (statement logging, R63).
- [ ] **Sessions** (point-in-time only): `DELETE FROM django_session;` done.
- [ ] **Erasure ledger**: `replay_erasures` exit 0, report kept; `manage.py ledger_status`
      ok; `arkray_erasure_ledger_gate_open` 1 on every process.
- [ ] **Vacuum**: `VACUUM (ANALYZE);` (owner; `restore.sh` did it for a dump): a restored
      copy's dashboards are about three times slower until vacuumed (R55).
- [ ] **Outbox**: `SELECT status, count(*) FROM core_outbox_event GROUP BY 1;` pending drains
      once the workers run (the replay queues purges); `in_flight` rows from before the
      restore are re-run after their 5-minute lease; dead ones: [Dead events](#dead-events).
- [ ] **Attachments** (R93): `manage.py reconcile_attachments`: objects uploaded after T are
      `orphaned` (keep them 30 days: [Orphaned attachment objects](#orphaned-attachment-objects));
      rows whose objects were deleted after T are `missing` (restore the versions, or
      `--repair` marks them unavailable): [Attachment bucket lost or restored](#attachment-bucket-lost-or-restored).
- [ ] **Search and Ask Arkray's index**: no chunks for the erased (the replay deletes them);
      `manage.py ai_reindex` only if the index is suspect ([Re-indexing](#re-indexing)).
- [ ] **Smoke test** from the allowlisted address ([Emergency maintenance](#emergency-maintenance)):
      sign in as an administrator (two-step), `/api/v1/auth/me` 200, the organisation
      dashboard's figures plausible, a deal and its negotiated price history, a note's file
      downloads.
- [ ] **Monitoring**: `arkray_db_up` 1, readiness ok, error rate at baseline, the outbox's
      oldest due falling, no `erasure_ledger_gate_closed` lines.
- [ ] **Record**: T, the start and end times, the replay and reconciliation reports, who
      approved reopening.

## Erasure ledger behind

Symptom: the API answers 503 `erasure_reconciliation_required` and readiness fails although
nothing was restored; the gate logged `erasure_ledger_gate_closed` with status `behind`. An
erasure's transaction failed after its ledger entry was written (a worker killed mid-erasure,
a slow ledger write cut short). Within a minute the reconciliation (`privacy.reconcile_ledger`,
beat) carries the erasure out and logs `erasure_ledger_reconciled`; the API reopens within
`ERASURE_LEDGER_CHECK_INTERVAL_S` after that (final production audit ARCH-3). It stays closed,
and logs an error naming the command, when:

- `erasure_ledger_reconciliation_required`: more than `ERASURE_LEDGER_AUTO_RECONCILE_MAX`
  entries are missing (a restore): follow [Restore from backup](#restore-from-backup), step 4;
- `erasure_ledger_reconciliation_refused` (`LedgerMismatch`, `LedgerTampered`): the ledger
  isn't the one this database applied, or was tampered with; check `ERASURE_LEDGER_URL`, the
  volume or bucket and the keys, never the database's state;
- `erasure_ledger_reconciliation_failed`: an entry couldn't be applied, typically a lead
  whose lost reasons only the schema owner may redact: run `manage.py replay_erasures --by
  <admin> --report <file>` with the owner's credentials.

Beat must run for this (one instance; [deployment.md](deployment.md)). Set
`ERASURE_LEDGER_AUTO_RECONCILE_MAX=0` to require the operator every time.

The worker containers' health check (`manage.py ledger_status`) keeps the chain it verified in
its temporary directory (`/tmp/arkray-ledger-status-*.json`: positions and MACs, no personal
data, signed with the ledger key) and re-verifies the whole chain at least hourly, a slice per
probe (adversarial review of 73ff1e3, M3). After restoring or replacing the ledger, restart
every backend process, then run `manage.py ledger_status --from-scratch` in one worker
container: a restarted process (or a probe whose state file was deleted) starts from the
newest signed checkpoint, and only the hourly pass, `--from-scratch` or `preflight --only
ledger` read every entry. `--from-scratch` answers `tampered` (and keeps that verdict for the
probes, logged as the security event) or clears an earlier one when the whole chain verifies;
on a large ledger it may take minutes (one GET per entry). A previous
release's `replay_erasures` can't apply `attachment_deleted` entries that name several files
(it reports them failed and the API stays closed): replay with this release's image or later.

Since the review round 1 (LEDGER-2): `manage.py ledger_status` answers
`erasure_ledger_gate=verifying` (exit 1) while a container's first verification is spread over
its probes, 5,000 entries each (from the newest checkpoint: usually one probe; the health
checks' start period covers it); each probe stops after 15 s of verification and keeps its
progress. **Checkpoints** live beside the entries, never among them (on S3 the sibling prefix
`<prefix>.checkpoints/`, or `_checkpoints/` at a bucket root; a `checkpoints/` subdirectory on
a volume; one per 1,000 entries, signed with the ledger key, no personal data): never edit or
delete them by hand; a missing, invalid or unreadable one only makes a start read more (logged
`erasure_ledger_checkpoint_unavailable`: give the ledger's credentials read and write on that
prefix, [deployment.md](deployment.md#configuration)), and an old entry altered after its
checkpoint was written is caught by the hourly pass from scratch, not at a process's start.

**`tampered` from a health probe** (review round 2, LEDGER2-1; each such probe logs
`erasure_ledger_tampered`, a security event that pages): the verdict is sticky until a pass over
the whole chain from scratch succeeds, so the container stays unhealthy meanwhile. Check
`ERASURE_LEDGER_URL`, the volume or bucket and the keys first (another environment's ledger
or a wrong key list reads the same way), then treat it as a
[Security incident](#security-incident): an entry altered or removed comes back from the
bucket's versions or Object Lock copies, never by editing `core_erasure_ledger_state`; restart
the backend processes afterwards (a web process that started from a checkpoint above the
altered entry may still answer until then). The event can also come from gunicorn's master or
the Celery parent, at start or before a fork (review round 3 and 4): that process then stays
closed too. The verdict lives in the container's temporary directory: a restart loses it (the
probes then start from the newest checkpoint and the pass from scratch follows; run
`ledger_status --from-scratch` instead of waiting). An old entry replaced by something that
isn't an entry (not JSON, an error page, JSON that isn't an object) is tampering like any
mismatch; `erasure_ledger_malformed` (WARNING) now means only a **new** entry, beyond anything
verified, that isn't JSON, for that check alone: if it repeats for the same entry, check the
store and any proxy in front of it (one answering 200 with an error page). A probe that can't
write its state logs `erasure_ledger_status_unsaved` (WARNING) and verifies from the first
entry every time (never from a checkpoint): on a long ledger that container then answers
`verifying` until its temporary directory is writable. On a platform without worker health
checks, a daily `manage.py preflight --only ledger` (or `rotate_erasure_ledger_key --check`,
or `ledger_status --from-scratch`) stands in for the hourly pass
([deployment.md](deployment.md#worker-and-beat-health)).

**`ahead`** (the ledger holds fewer entries than the database applied, or another entry where
the database's last one was): erasures answer 503 and write nothing until the right ledger is
back, so they never regrow a rolled-back ledger onto the database's position (review round 3,
LEDGER3-1), and the gate's `ahead` stands under the same status (the alert keeps firing) until
a read finds the right ledger, even while the store is slow or failing.

An erasure's files are purged by one paged job (`activities.purge_deleted_attachments`, a
minute of purging a run, then a continuation); a file the store won't delete is skipped and
retried on its own, from 5 minutes doubling to 24 hours, its count and next try kept on its
row (`activities.0013`; review round 3, LEDGER3-3: the backoff had lived in the cache), so any
number of stuck files never hides the rest (`attachment_purge_failed`, ERROR from the sixth
attempt: look at the bucket's permissions or Object Lock on that object); the job itself
retries only when nothing could be purged or three different files failed in a row (the store
is down). A dead job is re-run with `manage.py outbox_requeue --topic
activities.purge_deleted_attachments --yes`, and the hourly attachment housekeeping purges what
is left meanwhile. **During a rolling deploy** from a release before this one, a worker of the
previous release fails a value-purge job naming several fields (`pipeline.purge_custom_values`
with `field_ids`: retried, then dead after 8 attempts) and has no handler for the paged
attachment-purge topic (dead): deploy the `default`-queue workers before the API, or re-run
those dead events with `outbox_requeue` once every worker runs the new release, and let the
outbox drain before a rollback
([deployment.md](deployment.md#which-previous-release-can-run-beside-the-new-schema)).

## Rotate the erasure ledger key

When: on a schedule your security policy sets, when someone who knew the key leaves, or at
once if it may have leaked. Nothing is re-signed or lost (final production audit ARCH-5;
`core.ledger.rotate_key`). Never rotate production keys from a development machine.

1. Generate the new key in the secret manager (48+ random characters; not `dev-`/`test-`).
   Keep the old one there, versioned: losing every key that verifies the chain closes the
   API until a key is recovered.
2. Deploy `ERASURE_LEDGER_VERIFY_KEYS=<new key>` everywhere (web, every worker, beat; the old
   key still signs). Every process now accepts entries signed with either.
3. Deploy `ERASURE_LEDGER_KEY=<new key>` and `ERASURE_LEDGER_VERIFY_KEYS=<old key>` everywhere.
   New entries are signed with the new key.
4. `manage.py rotate_erasure_ledger_key --check` (verifies the whole chain, reports the key ids),
   then `manage.py rotate_erasure_ledger_key`: it appends a `key_rotated` entry signed with the
   new key, naming it by its id and anchoring every entry before it. From here an entry signed
   with the old key is refused as tampering. Running it again is harmless; interrupted, it either
   wrote nothing (run it again) or left the database one entry behind, which the
   reconciliation applies within a minute.
5. Retire the old key: deploy without it in `ERASURE_LEDGER_VERIFY_KEYS`. Entries it signed stay
   verifiable through the anchor (a restore included: `replay_erasures` needs only the current
   key). Keep the retired key in the secret manager's history until every backup older than
   the rotation has expired; destroy it after that. A rotation retires the keys that signed
   entries before it: a key listed in `ERASURE_LEDGER_VERIFY_KEYS` that never signed anything
   stays accepted until you remove it, so list only the keys of this rotation and remove them
   promptly.

Skipping step 2 makes processes that haven't got the new key yet read the new entries as
tampering and close the API until they are deployed (fail closed, never open). Removing the old
key before step 4 does the same.

## Vacuum after a restore or a bulk load

A restore, a `VACUUM FULL` or a bulk import leaves an empty visibility map: index-only
figures (the dashboards) run about three times slower until vacuumed (R55). `scripts/
restore.sh` does it; otherwise: `VACUUM (ANALYZE);` (minutes at a million leads).

## Rotate a secret

When: on the schedule your security policy sets, when someone who knew a secret leaves, and
**at once** when one may have leaked (then skip the grace steps marked *grace*, and treat the
leak as a [security incident](#security-incident)). Which process holds which secret is
`infrastructure/env/production.env.example`; give a new secret to exactly those. After every
rotation run `manage.py preflight` ([deployment.md](deployment.md#preflight)) in the scanning
worker's container: it proves each new credential works without printing one. Keep retired
secrets in the secret manager's history (versioned) until nothing can need them, then destroy
them. Every new value is generated (`openssl rand -hex 32`: `config/credentials.py`'s rule,
never a value used anywhere else). On the production-shaped stack the secrets file
(`--env-file`) holds them under their `ARKRAY_PROD_*` names: change the value there, then
`scripts/prod-compose.sh <it> up -d` recreates what reads it (the credential guard first); a database role's password changes in PostgreSQL first (below), since the database
keeps the one it was given.

| Secret | Lives in | Used by | Rotated wrongly |
|---|---|---|---|
| `DJANGO_SECRET_KEY` (+ `DJANGO_SECRET_KEY_FALLBACKS`) | secret manager | every backend process | without the old key in the fallbacks: everyone signed out, sealed page links and trusted-browser cookies void; old key removed before `mfa_reencrypt`: administrators' authenticators unreadable (`reset_mfa` each) |
| `arkray_app` password (`DATABASE_URL`) | secret manager | api, workers, beat | processes not yet rolled can't open connections: errors, readiness 503 |
| `arkray_owner` password | secret manager | the migrate job, restores, `replay_erasures` | the next migrate (the release) or restore fails |
| database administrator (`ADMIN_DATABASE_URL`; the production-shaped stack's `ARKRAY_PROD_DB_SUPERUSER_PASSWORD`) | managed service / secret manager | provisioning (`roles.sql`) only | nothing running |
| backup role's password | backup host | `scripts/backup.sh` | the nightly backup fails |
| Redis broker password (`CELERY_BROKER_URL`) | secret manager | every backend process | background work waits (durable in PostgreSQL); questions in flight fail |
| Redis cache password (`REDIS_CACHE_URL`) | secret manager | every backend process | the cache is skipped: slower, rate limits per process (R127) |
| SMTP credential (`EMAIL_URL`) | secret manager | every backend process (worker-outbox sends) | emails retry, then go dead: invitations and resets don't arrive |
| attachments' S3 keys (`ATTACHMENT_S3_*`), if not a workload identity | secret manager | api, workers | uploads and downloads 503, scans wait |
| the S3 ledger's credentials (AWS's default chain: the workload identity, or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) | secret manager | every backend process | the ledger unreadable: each process closes the API after `ERASURE_LEDGER_MAX_STALE_S` (1 h) |
| `ERASURE_LEDGER_KEY` (+ `ERASURE_LEDGER_VERIFY_KEYS`) | secret manager, with the ledger's own secrets | every backend process | swapped alone: entries read as tampered, the API closes ([its runbook](#rotate-the-erasure-ledger-key)) |
| `METRICS_TOKEN` | secret manager | api (checks it), the scraper | scrapes 404: the metric alerts go blind |
| `ANTHROPIC_API_KEY` | secret manager | worker-ai only | answers fall back to retrieval (the breaker opens) |
| backup encryption key pair | public: backup host; private: offline, two people | backup.sh / restore.sh | dumps encrypted to a key nobody holds are unrecoverable |
| backup signing key | secret: backup host; public: where restores happen | backup.sh / restore.sh | restores refuse new dumps until the public half arrives |
| the edge's TLS key | the edge only | nginx | browsers refuse the site (HSTS: no click-through) |

- **`DJANGO_SECRET_KEY`**: (1) generate a new key (50+ characters). (2) Deploy
  `DJANGO_SECRET_KEY=<new>` and `DJANGO_SECRET_KEY_FALLBACKS=<old>` to every backend process
  in one roll (a process still on the old key alone rejects what the new ones signed: keep the
  roll short). (3) `manage.py mfa_reencrypt` once, from any backend container (it exits
  non-zero if a secret is under no configured key: put the old key back and run it again).
  (4) *grace*: after at least 12 hours (the absolute session lifetime; a day is safer), remove
  the fallback and roll. **Leaked**: the same steps with no grace: remove the fallback right
  after `mfa_reencrypt` (minutes); everyone signs in again.
- **`arkray_app`'s password**: as the administrator, `\password arkray_app` in `psql` (it sends
  only the hash: `ALTER ROLE ... PASSWORD '...'` lands in `~/.psql_history` and, with
  `log_statement = ddl`, the server log); update `DATABASE_URL` in the secret manager; roll
  api, workers and beat at once. Open connections keep working; a process that opens a new one
  before it is rolled fails until it is (a minute or so with `DB_CONN_MAX_AGE_S` 60). For no
  error at all, alternate two login roles that are members of `arkray_app` (switch
  `DATABASE_URL` to the other, roll, then change the first one's password): not verified
  here; check `database.role` in preflight before relying on it.
- **`arkray_owner`**: `\password arkray_owner` the same way; update the migrate job's and the
  restore operator's copies (`ARKRAY_PROD_DB_OWNER_PASSWORD` on the production-shaped stack).
  Nothing long-running uses it.
- **The database administrator**: the managed service's console (`\password` when
  self-hosted: on the production-shaped stack `docker compose exec postgres psql -U arkray`,
  the local socket being its only way in); update `ADMIN_DATABASE_URL`, or
  `ARKRAY_PROD_DB_SUPERUSER_PASSWORD` in that stack's secrets file (its `POSTGRES_PASSWORD`
  only seeds a new data directory). The application never uses it.
- **The backup role**: `\password` it; update the backup host's `DATABASE_URL`; run
  `scripts/backup.sh` once by hand.
- **Redis**: with ACLs, add the new password beside the old (`ACL SETUSER <user> >new`),
  update `CELERY_BROKER_URL` and `REDIS_CACHE_URL`, roll every backend process, check
  preflight's `redis.*`, then remove the old (`ACL SETUSER <user> <old`). A managed service's
  token rotation that keeps both for a while (ElastiCache `ROTATE`, then `SET`) works the same.
  With `requirepass` alone (one password) there is a window: change it and roll at once.
- **SMTP**: create a second credential at the provider, update `EMAIL_URL`, roll, `manage.py
  preflight --only smtp --send-test-email <you>`, then revoke the old one.
- **S3 keys** (attachments, and the ledger's when it isn't a workload identity): create a second
  access key, update the values, roll the web tier and every worker (every backend process for
  the ledger's), `manage.py preflight --only storage,ledger` (the probe uploads, reads and
  removes; the ledger is read), then deactivate the old key, wait a day, delete it. Never leave
  the ledger unreadable for an hour: every process would close the API.
- **`ERASURE_LEDGER_KEY`**: [Rotate the erasure ledger key](#rotate-the-erasure-ledger-key);
  never by swapping the key alone.
- **`METRICS_TOKEN`**: the API accepts one token: update the scraper's secret and the
  deployment together and silence the absent-metrics alerts for the minute in between.
- **`ANTHROPIC_API_KEY`**: create a new key, update worker-ai's secret (and only its), roll it,
  ask one question that needs the model, revoke the old key.
- **Backup keys**: [Backup encryption keys](#backup-encryption-keys). After a new signing key,
  hand its public half to every place restores happen **before** the next backup, and keep the
  old public half to verify older dumps until they expire.
- **The edge's certificate and key**: renew (ACME or the CA), install, `nginx -s reload` (no
  connection is dropped); the key never leaves the edge.

## Restore drill

Quarterly, and after any change to the backup, restore or ledger code:
`python tests/drills/restore_drill.py --container <postgres container> --report drill.json`
(backend directory, against a PostgreSQL whose superuser it may use: it creates and drops
two throwaway databases). It seeds synthetic people, takes an encrypted `backup.sh` dump
with a throwaway key, erases a lead, deletes a file and a custom field's values and
pseudonymises a former user (ledgered), restores with `restore.sh`, checks the API is refused
while the database is behind and while the ledger is unavailable, replays, checks every
erasure holds again and a bystander is untouched, and replays once more (all already
applied). Keep `drill.json` (ids and counts only). Last run: 2026-10-12, PASS (backup
0.7 s, restore 0.9-1.1 s, 5 entries re-applied, then all already applied).

**Point-in-time recovery**: `python tests/drills/pitr_drill.py --report pitr-drill.json`
(backend directory; Docker and the development Redis; it starts its own throwaway
PostgreSQL containers from the production image and removes them). It provisions a
database with roles.sql, archives its WAL, takes a base backup, writes through the
application before and after a target time T (an erasure each side, a signed-in browser
that signs out after T, rows and a file after T), recovers into a new server to T, and
checks: every table exactly as at T, the rows after T absent, the API closed until
`replay_erasures`, the erasures re-applied, sessions emptied, the grant, `migrate --check`,
the release check, the attachment reconciliation and the vacuum; it prints the timings.
`--wal-load-mb 1024` adds a gigabyte of WAL before T to time the replay. Last run:
2026-10-12, PASS (database steps 4.9 s; 1 GB of WAL replayed in 11-24 s).

**On the real platform**: a provider point-in-time recovery into a new instance (never the
production one) and steps 5-9 of [Point-in-time recovery](#point-in-time-recovery)
against it, once before go-live and then quarterly: the drills above prove the
application's side, not the provider's.

## Backup encryption keys

`scripts/backup.sh` encrypts every dump to an OpenPGP public key
(`BACKUP_GPG_RECIPIENT_FILE`); the backup host holds **only** that public key.

- **Create** the key pair offline, on a machine that never runs the CRM:
  `gpg --quick-generate-key "Arkray backups <ops@example.com>" ed25519 cert never`, then
  `gpg --quick-add-key <fingerprint> cv25519 encr 2y`; export the public key
  (`gpg --armor --export`) to the backup host, and the private key
  (`gpg --armor --export-secret-keys`) to two offline copies held by two named people
  (e.g. a password manager vault with dual control and a sealed hardware token).
- **Restore** brings the private key into a temporary keyring (`BACKUP_GNUPGHOME`), used
  only for the restore and deleted afterwards.
- **Rotate** yearly (the subkey's expiry): add a new encryption subkey, distribute the new
  public key, keep the old private key until the last backup encrypted to it has expired
  (30 days).
- **Lost private key**: the backups encrypted to it are unrecoverable; take a new backup at
  once with a new key. **Leaked private key**: rotate, and treat every backup still kept as
  exposed (an incident).
- `BACKUP_ALLOW_UNENCRYPTED=1` exists for throwaway drills only.
- **Signing key** (R128): the backup host also signs each encrypted dump (a detached
  `.sig`), so a restore can tell a dump the backup host made from one placed in the bucket by
  anyone else. Create it on the backup host's side:
  `gpg --quick-generate-key "Arkray backup host <ops@example.com>" ed25519 sign never`; the
  secret half stays on the backup host (`BACKUP_GPG_SIGNING_KEY_FILE`, owner-only), the
  public half goes wherever restores happen (`BACKUP_SIGNER_KEY_FILE`). `restore.sh` checks
  the signature in a throwaway keyring before decrypting and refuses an unsigned or altered
  dump (`RESTORE_UNVERIFIED=1` only for a drill). Rotate it like the encryption key; a leaked
  signing key is an incident (rotate, re-sign nothing: take new backups).
- Both scripts refuse a remote database connection weaker than `sslmode=verify-full` (they
  set it when the URL names no mode; loopback and Unix sockets are exempt): give
  `sslrootcert` for a private CA.

## Legal holds

When the organisation must preserve a person's data (a dispute, an investigation),
`manage.py legal_hold place --lead <id>` (or `--user <id>`) `--reference CASE-123 --by <admin>`
stops their erasure, pseudonymisation, audit-detail expiry, attachment purges and custom-value
deletion until `manage.py legal_hold release --lead <id> --by <admin>`; `manage.py
legal_hold list` shows what is held. For backups, create a file named `LEGAL_HOLD` in the
backup directory: `backup.sh --prune` then deletes nothing. Record the matter outside the
application; the reference is only its number. Ask Arkray conversations a hold keeps are
kept even when their asker deletes them (hidden from them, `ai_conversation.forgotten_at`
set) and are deleted by the first hourly housekeeping after the release
([privacy.md](privacy.md#legal-holds)). Placing a hold while the previous release's pods
still run (a rolling deploy of the release that added this) lets those pods answer a deletion
409 and list kept conversations: finish the deploy first.

## Erasure request

[privacy.md](privacy.md#erasure): `manage.py erase_lead <lead id> --by <admin email>` (a
dry run), then `--yes`; **owner** credentials when the lead's opportunities were lost with
a reason (the command says so). Refused under a legal hold. If it reports that the erasure
ledger failed, nothing was erased: fix the ledger's store and run it again.

## Access request

Verify the requester's identity outside the application (the request's ticket is the
reference), then the customer's page or the user's details → **Export data**; Admin →
**Data requests** shows its progress and the download. Download the ZIP within 24 hours, **review it** (free text staff wrote may name other people), and send it
through the organisation's secure channel; never by plain email. The export's request,
download and expiry are audited.

## Export file cleanup

An ended export's file is deleted by `privacy.housekeeping` (hourly), retried until it is
confirmed gone ([privacy.md](privacy.md#access-requests)). Signals:
`arkray_export_file_purges{state="pending"}` (with `arkray_export_file_purge_oldest_seconds`),
`{state="stuck"}` (failed 6 times or more; each further attempt logs
`export_file_purge_failed` at ERROR with `reason`), `{state="retained"}` and the audit event
`privacy.export_file_retained`, and the reconciliation's log lines
(`export_orphans_reconciled`, `export_reconcile_refused`, `export_reconcile_failed`).

- **Stuck** (`reason`: `connection`, `server_error`, `access_denied`, `missing_bucket`,
  `partial`): fix the store or the credentials (they need `s3:ListBucketVersions`,
  `s3:DeleteObjectVersion` and `s3:DeleteObject` on the exports prefix). Nothing else to do:
  the next attempt (at most a day later) deletes it; to retry now,
  `DataExport.objects.filter(purged_at=None).exclude(purge_requested_at=None).update(purge_next_at=timezone.now())`
  in `manage.py shell`, then run `privacy.housekeeping`.
- **Retained**: S3 Object Lock (a retention period or a legal hold on the version) refuses
  the deletion; so does a missing `s3:DeleteObjectVersion` permission (also AccessDenied).
  Check the version's lock (`aws s3api get-object-retention` / `get-object-legal-hold`). A
  bucket-wide default retention must not apply to the exports prefix: remove it, or accept
  that the ZIP lives until it ends (record that decision with the access request). Once the
  lock ends, the daily retry deletes it.
- **Reconciliation** `refused` = 1 (ERROR `export_reconcile_refused`): `EXPORT_S3_PREFIX` or
  `EXPORT_ROOT` overlaps the attachments' store. Give exports their own prefix or directory;
  nothing is deleted meanwhile. `unknown` objects (names no export is written under) are
  never deleted: look at them and remove them by hand if they are not yours.

## Staff leaving

Deactivate the user (Users page) at once; reassign their open work. When the organisation no
longer needs to know who they were (its retention policy), pseudonymise them: `manage.py
pseudonymise_user <user id> --by <admin email>` (a dry run, then `--yes`) or the user's page
([privacy.md](privacy.md#staff)). A former **administrator** (a Super admin or an Admin) is
pseudonymised only on the server, by an operator, with `--allow-administrator` added (the API
refuses it, ADR-0030). Every operator command's `--by` names a super admin (an Admin holds
none of the capabilities they check). A leaving Admin's team: [Admins and teams](#admins-and-teams).
Pseudonymisation is final (adversarial review of 73ff1e3, L4): the account can't be renamed,
reactivated, re-invited or given a password again (422 `account_pseudonymised`); someone who
comes back is invited as a new user.

## Minimise legacy audit details

Once, after upgrading to the release with expiring audit details (ADR-0032), as the
**owner**, off-peak: `manage.py audit_minimise_legacy --by <admin email>` (what would move),
then `--yes`. It moves the client addresses, old/new emails, reset requesters' addresses and
support reasons that older events kept in the append-only row into expiring details (those
older than the retention period are deleted by the next hourly `audit.housekeeping`), in one
transaction with the trigger disabled per statement, and records `audit.legacy_minimised`
with the counts and a SHA-256 of every value moved. Idempotent. The one transaction holds
the audit table's rows it rewrites, and every audited write (each sign-in, each CRM change)
waits behind it for up to its 600 s statement timeout: run it in a **maintenance window**
with the web tier and workers stopped (a large legacy table: time `--dry-run`'s count
first; a batched variant is a known follow-up, R120).

## Forget names of deleted files

Once, after the same upgrade: `manage.py forget_attachment_names --by <admin email>` (a
count), then `--yes`: files deleted, blocked or never finished before it keep their names
and hashes until then. Audited (`attachment.metadata_removed`, a count). Legal holds are
respected.

## Purge old audit events

Only under a retention period agreed with the business, as a DBA with the **owner** role,
**off-peak**: disabling the trigger locks `audit_event` against writes until the
transaction ends, and every sign-in and CRM change writes an audit row (each waits up to
the 5 s lock timeout, then fails). So delete in small batches, each its own short
transaction, and repeat until a batch deletes nothing:

```sql
BEGIN;
ALTER TABLE audit_event DISABLE TRIGGER audit_event_append_only;
CREATE TEMP TABLE purge_batch ON COMMIT DROP AS
    SELECT e.id FROM audit_event e
    WHERE e.occurred_at < now() - interval '2 years'
      -- never an event about someone under a legal hold (by them, about them, or on them)
      AND NOT EXISTS (
          SELECT 1 FROM core_legal_hold h
          WHERE h.released_at IS NULL
            AND (h.subject_id = e.actor_id OR h.subject_id = e.subject_user_id
                 OR h.subject_id::text = e.target_id))
    LIMIT 5000;
-- An event's expiring detail (normally long gone; kept only under a legal hold) goes first.
DELETE FROM audit_event_detail WHERE event_id IN (SELECT id FROM purge_batch);
DELETE FROM audit_event WHERE id IN (SELECT id FROM purge_batch);
ALTER TABLE audit_event ENABLE TRIGGER audit_event_append_only;
COMMIT;
```

The batch leaves out every event about a held person (`manage.py legal_hold list` shows them).

Record that it was done (who, when, the cut-off) outside the application.

## First administrator and locked-out users

- First administrator: `manage.py createsuperuser`. With `MFA_REQUIRED_FOR_ADMINS` on, they
  set up two-step verification at their first sign-in (only Settings answers until then).
- A user who lost their authenticator signs in with a recovery code, then turns two-step
  verification off and on again in Settings (or replaces the codes). Every such change is
  emailed to the account's own address: someone who reports a notice for a change they didn't
  make is a [Security incident](#security-incident) (reset their password and second factor).
- A setup that wasn't confirmed within 10 minutes, or after five wrong codes, is gone: start
  it again in Settings. Without any code: see
  [Lost second factor](#lost-second-factor).
- A user locked out by failed sign-ins waits the lockout (at most 15 minutes) or resets
  their password; an administrator can reactivate a deactivated user on the Users page.
- A user whose sign-in from new browsers is locked by the daily cap ("Too many failed
  sign-ins for this account, so it can't be signed in to from a new browser for now", 50
  failures in a day; a browser they used here before still works) uses
  the unlock link emailed to them, or resets their password; an administrator can *Clear
  sign-in lock* on the Users page after confirming who is asking (see
  [Security events](#security-events), step 6).
- Every Super Admin sets up two-step verification at their next sign-in. Admin and User
  accounts sign in with their password and have no MFA enrolment or step-up flow.
- A user whose two-step verification is locked (20 wrong codes in a row; they see "Too many
  wrong codes...") resets their password with Forgot password, which unlocks it; see
  [Second factor locked](#second-factor-locked).
