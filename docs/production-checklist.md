# Production checklist

The go-live list for a new environment, and the items to re-check at every release. Each
line is something an operator can verify; the details live in the linked sections, which
are authoritative. Items marked **(org)** are decisions or contracts the organisation owns;
the system can't verify them.

## Before the first deployment

**Infrastructure**

- [ ] Managed PostgreSQL 16 with `vector`, `pg_trgm` and `btree_gin`, point-in-time recovery
      and 30-day snapshots ([deployment.md](deployment.md#database)).
- [ ] `infrastructure/postgres/roles.sql` run once by a database administrator: the
      database, `arkray_owner`, `arkray_app`, the extensions, `log_min_error_statement=panic`
      and `log_error_verbosity=terse` (R63) ([deployment.md](deployment.md#release-process)).
- [ ] PostgreSQL settings applied: `shared_preload_libraries=pg_stat_statements`,
      `log_min_duration_statement=-1`, memory and planner settings, `max_connections` from the
      connection budget ([deployment.md](deployment.md#database),
      [reliability.md](reliability.md#connection-budget)).
- [ ] Two Redis instances with passwords and verified TLS (`rediss://...?ssl_cert_reqs=required`):
      the broker `noeviction` with AOF, the cache `allkeys-lru` **without persistence**
      (`--save '' --appendonly no`) ([deployment.md](deployment.md#production-topology)).
- [ ] Every backend hop over verified TLS: `DATABASE_URL` with `sslmode=verify-full` and the
      provider's CA, `smtp+tls://` (STARTTLS) or `smtp+ssl://` (implicit TLS), an https S3 endpoint; ClamAV on the private
      network only; `BACKEND_TLS_PRIVATE_HOSTS` only for single-label container names
      ([security.md](security.md#backend-tls)). Production refuses to start otherwise.
- [ ] The erasure ledger's own store: a bucket (versioning, Object Lock in compliance mode,
      replication) or a volume with its own backups, **never** in the database backups;
      `ERASURE_LEDGER_URL` and `ERASURE_LEDGER_KEY` (≥ 32 characters, its own secret)
      ([privacy.md](privacy.md#restore-safe-erasure)).
- [ ] The backup encryption key pair made offline; only the public key
      (`BACKUP_GPG_RECIPIENT_FILE`) on the backup host; the private key with two named people
      ([runbooks.md](runbooks.md#backup-encryption-keys)).
- [ ] Data-subject exports' private storage (`EXPORT_ROOT` volume, or the bucket's
      `EXPORT_S3_PREFIX`).
- [ ] An edge proxy equivalent to `infrastructure/nginx/arkray.conf`: TLS, HSTS, `/api` to
      Django, `X-Forwarded-For`/`-Proto` overwritten, `X-Request-ID` minted, no query
      strings or one-time links in its logs, its error log at `crit`, a 1 MB body limit
      ([deployment.md](deployment.md#reverse-proxy)).
- [ ] Django reachable only through the proxy (network policy); `/health/*` reachable only
      by the probes and the metrics scraper.
- [ ] Probes: the API's liveness at the TCP level (or HTTP with more than 35 s of
      tolerance: a stalled database pins the request workers, R81), readiness
      `/health/ready`; the web tier `GET /login` ([deployment.md](deployment.md#production-topology)).
- [ ] Real SMTP with SPF, DKIM and DMARC on the sender's domain
      ([deployment.md](deployment.md#email)).
- [ ] Note attachments: a **private** S3 bucket (block public access, versioning,
      server-side encryption, replication) with `ATTACHMENT_STORAGE=s3` and its
      credentials; verify upload, download, delete and an outage against it (R99); the
      proxy's 11 MB limit on the upload route only
      ([deployment.md](deployment.md#attachments-storage)).
- [ ] A ClamAV daemon reachable **only** from the scanning worker (queue `default`) on the
      private network (clamd is plain TCP, no authentication), `ATTACHMENT_SCANNER=clamd://<host>:3310`,
      `BACKEND_TLS_PRIVATE_HOSTS` naming it, no `ATTACHMENT_SCANNER_WAIVED`, freshclam
      updating (R94). Keep `ATTACHMENT_MAX_BYTES` ≤ `StreamMaxLength` ≤ `MaxFileSize` and
      `AlertExceedsMax yes` (`infrastructure/clamav/clamd.conf`); about 1.2 GB of memory,
      3 GB during a signature reload. `python tests/drills/clamav_drill.py --scanner
      clamd://<host>:3310` passes against it after every clamd configuration change, and an
      EICAR upload is blocked. Production refuses to start without a scanner unless
      `ATTACHMENT_SCANNER_WAIVED=true` records the organisation's acceptance of R94 **(org)**.
- [ ] A separate worker for the `outbox,email` queues; the default worker's grace period is
      at least 18 minutes (the nightly attachment check) ([deployment.md](deployment.md#production-topology)).
- [ ] Every image pinned by digest, the pins moved on each release (Redis, nginx and ClamAV
      had fixable OS-package findings that newer digests clear); containers read-only,
      `cap_drop: ALL`, no-new-privileges, CPU/memory/pids limits and health checks (R128).
- [ ] The edge proxy keys IPv6 clients by their /64 (the reference nginx map, or the
      platform's equivalent) and, behind another load balancer, has `real_ip` configured
      (R127).
- [ ] The bucket's backups on the database's schedule and retention (R93).
- [ ] Backups signed and verified: `BACKUP_GPG_SIGNING_KEY_FILE` on the backup host,
      `BACKUP_SIGNER_KEY_FILE` where restores happen, and a database URL with
      `sslmode=verify-full` and `sslrootcert` for both scripts, one host and no service file
      ([runbooks.md](runbooks.md#backup-encryption-keys)); `RESTORE_WORK_DIR` with room for the
      dump twice.
- [ ] The staging campaign of [staging-validation.md](staging-validation.md) run on an
      environment equal to the target topology, every result labelled `staging`: the 100-user
      run with a 600 s hold inside [capacity.md](capacity.md#pass-criteria-the-product-owners)'s criteria, the
      saturation runs (saturation point and headroom recorded), the targeted runs,
      `resilience_drill.py run --environment staging --drills all` with no FAIL and every
      INCONCLUSIVE explained, the restore and point-in-time recovery drills with RPO and RTO
      recorded (R114, R141).
- [ ] Point-in-time recovery on with 30-day retention and cross-region copies, the latest
      restorable time checked daily; or, self-hosted, WAL archiving to another region with
      pgBackRest or WAL-G, daily base backups, 30-day retention, `pg_stat_archiver` alerts
      ([runbooks.md](runbooks.md#database-backup)).
- [ ] The backup repository's encryption passphrase/key (pgBackRest `repo1-cipher-pass` or
      WAL-G `WALG_LIBSODIUM_KEY`) in the secret manager **and** with the backup key holders
      offline.
- [ ] The edge's maintenance switch mounted (`/etc/nginx/maintenance`, read-only) and tried
      once: on → 503 page and JSON with `Retry-After`, the health check still up; off → normal
      ([runbooks.md](runbooks.md#emergency-maintenance)); on a platform edge without nginx,
      the equivalent rule prepared. No `on` file in the deployed directory.
- [ ] Application hosts are x86_64 (the images and the pinned ClamAV image are amd64 only;
      [deployment-readiness.md](deployment-readiness.md#review-by-component)); behind
      Cloudflare or a load balancer, nginx's `real_ip` trusts only that proxy's ranges.
- [ ] The single-host stack's secrets come from an owner-only file outside the checkout,
      generated for this environment (`scripts/init-prod-env.sh`), and every Compose command
      runs through `scripts/prod-compose.sh <secrets file>` (it names the file with
      `--env-file` after dropping the shell's `ARKRAY_PROD_*`); no `ARKRAY_PROD_*` is ever
      exported in an operator's shell; `credential-guard` exited 0 (R151,
      [operations.md](operations.md#production-shaped-stack)).
- [ ] The backup and restore commands of [runbooks.md](runbooks.md#restore-from-backup) run
      once on the stack before go-live (a restore into `arkray_restored`, the replay, the
      release check, the rename swap), and pgBackRest or WAL-G connect as the superuser over
      the socket with a mounted `.pgpass` (`PGPASSFILE`): `pgbackrest check` passes (R151).
- [ ] Where many users share one public address (an office behind NAT, a guest network): a
      front proxy with a total request deadline (headers and small bodies) and a per-address
      connection cap in front of nginx, checked once with a sign-in whose 1 KB body arrives a
      byte every 4 s and with slow headers; without it the residual in
      [operations.md](operations.md#edge-rate-limits) is accepted in writing (R165).
- [ ] `manage.py preflight --only credentials` passes with the deployment's own environment;
      no secret is shared between environments or between two secrets (R151).
- [ ] Self-hosted PostgreSQL: the production `pg_hba.conf` (or its equivalent) in place,
      checked from an application host: the superuser and the owner refused, the application
      role over TLS only; managed: the administrator's account reachable from the
      provisioning host only (R151, [security.md](security.md#database-privileges)).
- [ ] Behind a CDN or load balancer: nginx's `real_ip` set from its published ranges only,
      and the origin reachable from those ranges only (a security group or Authenticated
      Origin Pulls); without it one slow client holds every per-address limit for everyone
      (R165, [operations.md](operations.md#edge-rate-limits)).
- [ ] The public name in `arkray.conf`'s plain-HTTP redirect and `server_name` (R168).
- [ ] Any host installation outside the backend image sets `ARKRAY_RUNTIME=production` in
      every service unit (R169).

**Source control and release (org)**

- [ ] GitHub environments `staging` and `production` exist with required reviewers (prevent
      self-review once there are two maintainers) and "Deployment branches and tags" set to
      exactly branch `main` and tag `v*`; branch protection on `main` requires the CI checks;
      GHCR package visibility and retention decided (the release workflow refuses to run
      without the reviewers, or unless both environments' rules are exactly those two: R166).
- [ ] Dependabot enabled for the repository (version and security updates;
      `.github/dependabot.yml`), and its pull requests reviewed like any change (R166).
- [ ] The commit to release pushed and its GitHub CI run green (R80).

- [ ] `manage.py preflight --base-url https://<public name>` in the scanning worker's
      container: no FAIL, and every WARN fixed or accepted in writing (managed Redis without
      `CONFIG`, bucket settings the application's credentials can't read)
      ([deployment.md](deployment.md#preflight)).

**Secrets** (from the secret manager, never in the repository or an image)

- [ ] `DJANGO_SECRET_KEY` (≥ 50 characters), database passwords for both roles, Redis
      passwords, SMTP credentials, `METRICS_TOKEN` (≥ 32 characters) and, only with a model,
      `ANTHROPIC_API_KEY` for the ai worker alone ([security.md](security.md#secrets-management)).
- [ ] The secret manager replicated to the disaster-recovery region.

**Configuration** ([deployment.md](deployment.md#configuration))

- [ ] The environment built from `infrastructure/env/production.env.example`: every
      `change-me-` value from the secret manager, every `example.com` name the deployment's,
      the `#[service]` lines in that service only (the provider key in worker-ai, the
      owner's password in the migrate job), nothing from the "never" list.
- [ ] `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `APP_BASE_URL` (https),
      `TRUSTED_PROXY_COUNT`, `TRUST_INCOMING_REQUEST_ID=true` behind the reference proxy;
      `TRUSTED_PROXY_ADDRESSES` and `FORWARDED_ALLOW_IPS` set to the edge proxy's own
      addresses, and the edge reaching the API only from them.
- [ ] `DATABASE_URL` as `arkray_app` for web and workers, as `arkray_owner` for the migrate
      job only; `DB_STATEMENT_TIMEOUT_MS=60000` for workers.
- [ ] `DJANGO_ALLOW_INSECURE_LOCAL_HTTP` **unset**.
- [ ] `MFA_REQUIRED_FOR_ADMINS` unset or `true` (ADR-0033); a secret-key rotation includes
      `manage.py mfa_reencrypt` before a fallback key is retired.
- [ ] `MFA_REQUIRED_FOR_ALL_USERS` removed or set to `false`. It is retained only as an
      ignored rolling-upgrade compatibility variable; two-step verification is mandatory
      for Super Admin accounts only.
- [ ] The write budgets confirmed for this organisation (`ACTIVITY_MAX_CREATED_PER_USER_PER_DAY`
      1,000, `ACTIVITY_MAX_PER_CUSTOMER` 5,000; [reliability.md](reliability.md#write-budgets);
      R153).
- [ ] `DEFAULT_FROM_EMAIL` on a real domain with SPF, DKIM and DMARC.
- [ ] `API_THROTTLE_AUTH` sized to the largest office behind one address (R77).
- [ ] Ask Arkray: `AI_ENABLED` decided; with `AI_LLM_PROVIDER=anthropic`, the items in
      [AI configuration](deployment.md#ai-configuration): a data-processing agreement and
      the provider's retention settings **(org)**, the key on the ai worker only, an https
      `AI_LLM_BASE_URL`, a PASS report from the real-model evaluation (`manage.py ai_eval
      --provider anthropic`, [ai-evaluation.md](ai-evaluation.md); three runs recommended)
      **(org: key, spend and retention approvals)**, then a pilot on real data (R69).
- [ ] Before `AI_LLM_PROVIDER=anthropic`: the masking decisions in
      [rag-architecture.md](rag-architecture.md#privacy-and-data-minimisation) (what is kept:
      unnamed local numbers and spaced 3+3+4 digits, meeting IDs, postal PIN codes) accepted by
      the organisation, and `ai_eval --provider anthropic` passes every `minimisation.*` case
      (R152, R69).
- [ ] Ask Arkray evaluation key: a separate Anthropic workspace with a spend limit, the key
      in the operator's shell for the run only, revoked or rotated afterwards **(org)**.

**Observability** ([observability.md](observability.md))

- [ ] Container log rotation in place (the Compose files: 5 x 10 MB); JSON logs shipped to
      the log platform with a retention period (30 days recommended) **(org)**; the
      log-based alerts of [observability.md](observability.md#alerts-phase-10) (5xx rate,
      latency, sign-in failures, security events) configured there.
- [ ] `/health/metrics` scraped from each pod with the bearer token (`METRICS_TOKEN`), and
      `infrastructure/alerts/arkray.rules.yml` loaded: database, background work (including
      work no worker has taken), broker, cache, Ask Arkray, indexing backlog.
- [ ] `ArkraySignInFailureStorm`, `ArkraySignInLocked`, `ArkrayAdministratorPromoted` and
      `ArkrayQueueDominatedByOneUser` routed to people who act on them (R153, R154, R156;
      [runbooks.md](runbooks.md#security-events), [runbooks.md](runbooks.md#outbox-backlog)).
- [ ] `/unlock-sign-in/*` (the emailed sign-in unlock link) redacted from the edge's access
      log like `/activate/*` and `/reset-password/*` (the reference `arkray.conf` does), and
      its `Referrer-Policy: no-referrer` (set by the web tier) not stripped by a platform edge
      (R154).

**People and policy (org)**

- [ ] The first administrator created (`manage.py createsuperuser`, which applies the password
      policy: R163) with a long random password passed only to that command, and two-step
      verification enrolled at once (until then whoever knows the password can enrol first);
      everyone else invited.
- [ ] Every administrator has set up two-step verification (the Users page shows "On") and
      stored their recovery codes; the identity check for an operator's
      `manage.py reset_mfa --by <super admin>` is agreed ([runbooks.md](runbooks.md#lost-second-factor)).
- [ ] Mail delivery checked end to end before administrators enrol: owners are emailed about
      every change to their two-step verification, the rollout's check against someone with
      a phished password enrolling first.
- [ ] The ClamAV container's health (it also fails on signatures older than 3 days),
      `ArkrayAdministratorDeactivated`, `ArkrayAccountsDeleted`, `ArkrayMfaLocked`,
      `ArkrayMfaLockedUntilReset`,
      `ArkrayErasureLedgerGateClosed`, `ArkrayAttachmentScanOverdue` and `ArkrayCacheDown`
      routed to people who act on them (while Redis is down, request limits are counted per
      process: R127).
- [ ] Operators know the order for a factor locked until an MFA reset (mailbox secured,
      password replaced, then the reset: [runbooks.md](runbooks.md#second-factor-locked)).
- [ ] Retention periods agreed: audit trail, audit details (90 days default), backups
      (30 days), logs, Ask Arkray (30 days), customer records
      ([privacy.md](privacy.md#decisions-that-arent-the-systems)).
- [ ] The historical-deal policy confirmed by the product owner (a previous owner sees a
      closed deal without its customer's contact details and without the deal's free text:
      R118; [Owner decisions pending](#owner-decisions-pending)).
- [ ] The access-request procedure: who verifies identity, who reviews an export before
      release, the secure channel used ([runbooks.md](runbooks.md#access-request)).
- [ ] After upgrading from a release before ADR-0032: `audit_minimise_legacy` (owner) and
      `forget_attachment_names`, once ([runbooks.md](runbooks.md#minimise-legacy-audit-details)).
- [ ] RPO ≤ 5 min and RTO ≤ 1 h confirmed with the business, and a quarterly restore drill
      scheduled, including `replay_erasures` ([runbooks.md](runbooks.md#restore-drill)).
- [ ] The team knows the runbooks ([runbooks.md](runbooks.md)).

## Every release

1. CI green on GitHub for the exact commit (the newest commit of a push to main):
   `.github/workflows/ci.yml`: secrets, workflows, backend, frontend, images (Trivy, smoke
   test, release on a new database). A local `scripts/ci-local.sh` run is no substitute (R80).
2. Release workflow run for that commit SHA (`.github/workflows/release.yml`): `verify`
   passed, `publish` approved in the **staging** environment, `promote-production` approved
   in the **production** environment (approvers named in the release record).
3. The two digests from the run's summary recorded with the release (and the previous
   release's digests kept for rollback); the deploy uses `image@sha256:<digest>`, never a
   tag, and `gh attestation verify` passes for both on the deploy host (with the
   `--source-ref` the run printed: R166).
4. Migrate job as `arkray_owner`: `manage.py migrate --noinput && manage.py
   grant_app_privileges arkray_app`.
5. Release check as `arkray_app`: `manage.py check --deploy --database default` (no
   `arkray.E001`; `arkray.W002` means PostgreSQL would log statements or failing rows,
   R63).
6. `manage.py preflight` with the new image (scanning worker, application role): no FAIL
   (every dependency, TLS hop and worker route: [deployment.md](deployment.md#preflight)).
7. Roll out api, workers and web; beat last. Readiness gates traffic; the web server and
   workers refuse a privileged database role.
8. Smoke: sign in, open the dashboard, a lead, the board; `/health/ready` OK; no error lines.
9. Rollback = the previous image, while it can still write to the new schema (additive
   migrations since it; not across `pipeline.0006` / `identity.0004`); otherwise roll forward
   or restore the backup ([deployment.md](deployment.md#release-process); never a
   down-migration).

**Upgrading from `73ff1e3` (the adversarial-review release)** ([deployment.md](deployment.md#which-previous-release-can-run-beside-the-new-schema)):

- [ ] Deployed **stopped** (or the rolling window accepted) if any legal hold is active: the
      previous release's pods list conversations kept for a hold and answer deleting them
      with the old 409; no hold placed, no one pseudonymised and no closed deal corrected
      until every pod runs the new release (R148, R149, R157).
- [ ] Deploy order: the `default`-queue workers (worker-default, worker-outbox) before the API
      and the web: a worker of the previous release fails a value-deletion job naming several
      fields and has no handler for the paged attachment purge; any such dead events re-run
      with `manage.py outbox_requeue --topic <topic> --yes` once every worker runs the new
      release; before a rollback, the outbox drained of those two topics (R150,
      [deployment.md](deployment.md#which-previous-release-can-run-beside-the-new-schema)).
- [ ] `activities.0013` (the file purge's backoff: two columns on `activities_attachment`,
      catalog only) applied by the migrate job; the previous release keeps inserting (one has
      a database default, the other is nullable) and ignores them, so the rolling window holds
      (R150).
- [ ] Super Admins told before the upgrade that each will set up two-step verification at
      their next sign-in. Admin and User accounts continue to sign in with their password
      without an enrolment gate ([development.md](development.md#first-time-setup)).
- [ ] The migrate job's log checked for `pseudonym_domain_accounts_not_pseudonymised`, and each
      account it lists given a real address on the Users page (R157,
      [runbooks.md](runbooks.md#schema-migration-for-a-release)).
- [ ] After the deploy: `manage.py ledger_status` prints ok in every Celery container (a
      first probe may answer `verifying` until it has verified from the newest checkpoint) and
      `/health/ready` answers within 3 s (R150); the ledger's S3 credentials can write and read
      `<prefix>.checkpoints/` (or `_checkpoints/` at a bucket root: an IAM policy on
      `<bucket>/<prefix>*`; [deployment.md](deployment.md#configuration)), and no
      `erasure_ledger_checkpoint_unavailable` warning appears; `replay_erasures` only ever run
      with the new image (a previous one can't apply batched entries).
- [ ] The ledger in a bucket of its own (or at least a prefix of its own: at a bucket root,
      other objects whose names begin with a digit cost the head search a listing page each);
      on a store slower than about 200 ms per GET, the Celery containers' health-check timeout
      raised (60 s, say) or the ledger moved closer (review round 3, I-4 and I-6;
      [deployment.md](deployment.md#worker-and-beat-health)).
- [ ] After restoring or replacing the erasure ledger: every backend process restarted, then
      `manage.py ledger_status --from-scratch` run once in a worker container (exit 0): a
      restarted process starts from the newest signed checkpoint, and only the hourly pass,
      `--from-scratch` or `preflight --only ledger` read every entry (review round 3,
      [runbooks.md](runbooks.md#erasure-ledger-behind)).
- [ ] `erasure_ledger_tampered` routed as a page in the log platform
      ([observability.md](observability.md#alerts-phase-10)); on a platform without worker
      health checks (Render and similar), a daily `manage.py preflight --only ledger` scheduled
      (R150).
- [ ] The restore and point-in-time recovery drills and the 100-user capacity run repeated on
      the merged release, with the write budgets, two-step verification for every user and the
      edge's new rates in place ([testing.md](testing.md#adversarial-review-of-73ff1e3-2026-10-12)).

**Upgrading to the super administrators and teams release** ([ADR-0035](adr/0035-super-administrators-and-teams.md);
`identity.0009`, `ai.0005`; [deployment.md](deployment.md#which-previous-release-can-run-beside-the-new-schema)):

- [ ] No Admin created and nobody promoted to Admin until every pod (web, api and the
      workers) runs the new release: the previous release grants the new role nothing, but
      doesn't count it as an administrator either, so on its pods a super admin could change
      an Admin's email, password or second factor or pseudonymise them, a reactivation would
      skip R126's reset link and a deactivation wouldn't revoke their invitations.
- [ ] A rollback to the previous images without a restore, once Admins exist: each Admin
      made a User (their team is unassigned) or deactivated first; both migrations refuse to
      be reversed.
- [ ] **(org, owner)** If the organisation uses teams: after the upgrade every existing
      administrator is a Super admin (all their powers kept) and every existing user is in no
      team, managed by the super admins only. Create or promote the Admins, then assign each
      user to their Admin on the Users page (*Change role or Admin*; *Filter by Admin* →
      *No admin* lists who is left) ([runbooks.md](runbooks.md#admins-and-teams)). Until a user
      is assigned, no Admin sees their records.
- [ ] **(org, owner)** Each existing administrator who should be an Admin rather than a Super
      admin: a super admin's role can't be changed through the API (ADR-0030), so an operator
      changes it ([security.md](security.md#administrator-account-protection), with
      `team_admin` as the new role), or the account is deactivated and the person invited
      anew as an Admin. At least one active Super admin always remains.
- [ ] Each new Admin has set up two-step verification at their first sign-in (required for
      both administrator roles where it is required for administrators) and knows that a lost
      factor is the operator's `reset_mfa`, whose `--by` names a super admin
      ([runbooks.md](runbooks.md#lost-second-factor)).
- [ ] Team-scope performance watched once real teams exist (R172: search, the activities
      list and Ask Arkray in a team weren't benchmarked at production size).

## After go-live

- [ ] A backup restored into an empty database and the release check run on it
      (`scripts/restore.sh`), within the first month, then quarterly.
- [ ] A point-in-time recovery into a new instance on the real platform, with sessions
      emptied, the ledger replay and the validation checklist, within the first month and
      then quarterly; `tests/drills/pitr_drill.py` after any change to the backup, restore or
      ledger code ([runbooks.md](runbooks.md#restore-drill)).
- [ ] Before a stopped-deploy release: maintenance on, T0 noted, then the migrate job
      ([runbooks.md](runbooks.md#schema-migration-for-a-release)).
- [ ] After any secret rotation or infrastructure change: `manage.py preflight`.
- [ ] Dead outbox events reviewed weekly (`manage.py outbox_requeue` lists them).
- [ ] `arkray_outbox_top_key_share` watched for the first weeks: a recurring
      `ArkrayQueueDominatedByOneUser` means a runaway integration or abuse, and `manage.py
      outbox_requeue --status` names whose work it is (R153).
- [ ] Dependency updates: `pip-audit` and `pnpm audit` in CI; base-image digests moved
      deliberately ([deployment.md](deployment.md#images)).
- [ ] Open risks reviewed against [risk-register.md](risk-register.md).

## Owner decisions pending

**(org)** Defaults the adversarial review of `73ff1e3` and its review rounds 1 to 4 left in place
for the product owner (or, where marked, the operator) to confirm or change before go-live.
Each is implemented and tested as stated; changing one is a setting, a constant in one place,
or a small code change ([ADR-0034](adr/0034-staging-security-remediation.md)).

*Legal holds and Ask Arkray (R148)*

- [ ] The "Forget this conversation?" dialog's wording (`frontend/src/features/ask/AskView.tsx`): the same for everyone, no longer promising an immediate permanent deletion (legal or privacy may prefer other words).
- [ ] A staff member's subject-access export includes the conversations they deleted that a hold keeps (default: included; `privacy/exports.py`, `user_document`): the right of access against telling them a hold exists.
- [ ] The `retained` flag in `ai.conversation_deleted`'s audit metadata (default: recorded; no screen shows it, but database and audit-export access reveal which deletions a hold deferred).
- [ ] Pending questions of a kept conversation are failed with the error code `forgotten` (default; an `arkray_ai_questions_last_hour` label operators see, not counted by `ArkrayAskFailing`).

*Deals (R149, R118, R162)*

- [ ] Who corrects a closed deal's figures (default: administrators, `crm.manage_any`, with a recent second factor and a reason, never in a support session, their own deals included: no four-eyes rule; `pipeline/services.py`, `correct_closed_deal`), and whether corrections should alert (none today).
- [ ] The frozen fields of a closed deal (default: value, probability, expected close date, opportunity date and no new agreed price; text, instrument and custom values stay editable; `services.CLOSED_FROZEN`).
- [ ] Corrections under a legal hold allowed, and refused for an erased customer's deal (default as stated; `correct_closed_deal`).
- [ ] What a closed deal's previous owner keeps (default: no customer identity or contact details and none of the deal's free text; every figure, the organisation and the instrument stay; `pipeline/customer.py`, `WITHHELD_TEXT`; R118). Alternatives: their own text visible to them, or the deal hidden from them entirely.
- [ ] Reopening a won deal needs no reason and no step-up, and a salesperson may create a deal directly in Won or Lost (default; `pipeline/services.py`): reopen, edit and close is how a salesperson changes a closed deal's figures, fully traced in the stage history (value, probability and both dates at every transition). Alternatives: a reason or a manager's approval to reopen; closed deals created by administrators only.
- [ ] The configuration caps (`pipeline/models.py`): at most 100 retired stages per pipeline (`MAX_RETIRED_STAGES_PER_PIPELINE`), 25 archived pipelines per owner and 25 shared (`MAX_ARCHIVED_PIPELINES_PER_OWNER`: restore one and reuse it), 100 removed custom fields per pipeline (`MAX_ARCHIVED_FIELDS_PER_PIPELINE`).
- [ ] Pipeline lists carry no custom fields (default; an API shape change for any other client: one pipeline's own representation carries them).

*Erasure ledger (R150)*

- [ ] The ledger's timing constants (default: a check waits 2 s and re-checks after 5 s, and gunicorn's master refreshes its verified chain for at most 5 s plus one short store call (connect 1 s, read 2 s, one attempt) before each fork and writes no checkpoint past that deadline, `core/ledger_gate.py`; the worker probe re-verifies the whole chain hourly, at most 5,000 entries or 15 s a probe in slices of 500, `ledger_status.py`; files purged 100 at a time within 2 s of storage calls, an erasure's paged purge job 60 s a run, and a file the store won't delete retried from 5 minutes doubling to 24 hours (kept on its row, `activities.0013`), logged at ERROR from its 6th failed attempt, a run stopping after 3 different files fail in a row, `activities/attachments.py`).
- [ ] The value-deletion budget: 100 custom fields a person may ask to delete in any 24 hours (`MAX_VALUE_DELETIONS_PER_USER_PER_DAY`, `pipeline/field_values.py`; it counts fields asked for, values or not; 0 turns it off). Other ledger-producing operations stay bounded by idempotency, batching and the throttles.
- [ ] Signed verification checkpoints every 1,000 entries (`CHECKPOINT_EVERY`, `core/ledger.py`; in `<prefix>.checkpoints/`, so the ledger's credentials need read and write there): a starting process (a new web process included), a running gate more than 1,000 entries behind the head and gunicorn's master refresh no longer re-read the entries before the newest checkpoint, so an old entry altered after the checkpoint was written is caught by the worker containers' hourly pass from scratch (sticky `tampered`, a paging log event; the marker lives in the container's temporary directory and a restart loses it until the pass comes round again), `ledger_status --from-scratch`, replay, rotation and preflight, not by them (review rounds 1 to 3).
- [ ] Checkpoint failures have no metric (lead decision: a counter would see the web process only); the `erasure_ledger_checkpoint_unavailable` warning is the signal.

*Production secrets, edge and release (R151, R155, R165, R166, R169)*

- [ ] The credential rule (default: at least 32 characters, 8 different, 2.5 bits a character and 128 estimated bits, no repeated part, keyboard or alphabet run, joined words and numbers or common password word, never a published value or a placeholder marker; `config/credentials.py`): every secret is generated, and human passphrases are refused.
- [ ] **(operator)** The restore database is always `arkray_restored`, put in place on the single host by renaming (`arkray_replaced_<stamp>` kept until the restore is accepted, then dropped by the operator; `infrastructure/postgres/pg_hba.conf`, [runbooks.md](runbooks.md#restore-from-backup)).
- [ ] The edge's API rates (default: 20 a second per session with bursts of 200, 10 per address without a session with bursts of 100, 300 per address with bursts of 1,000; `infrastructure/nginx/arkray.conf`): raise the ceiling for a larger office behind one address.
- [ ] The edge's body slots and timeouts (default: 8 per session and 16 per address for bodies over 16 KB, 128 small bodies per address, a 40 MB JSON-body area, `client_body_timeout` and the header deadline 10 s, which drop a mobile link stalling longer; pages take only a body-less GET or HEAD, anything else 405; `arkray.conf`, `compose.production.yml`).
- [ ] **A front proxy with a total request deadline is required wherever users share one public address**; without one, that address's sign-ins and saves can be held by one person there, and slow headers affect everyone (R165): require it, or accept the residual in writing.
- [ ] **(operator)** The release refuses unless both GitHub environments' deployment rules are exactly branch `main` and tag `v*` (`release.yml`).
- [ ] **(operator)** `ARKRAY_RUNTIME=production` in the image refuses any other settings module, even on purpose (`config/settings_module.py`, `backend/Dockerfile`).
- [ ] Dependabot's 7-day cooldown for version updates (security updates at once; `.github/dependabot.yml`).

*AI masking (R152; only once a model is enabled)*

- [ ] What is deliberately not masked: meeting IDs without their passcode, postal PIN codes, bare local numbers and unnamed spaced 3+3+4 or 4+3+3 digits, numbers after a currency word or with paise (`ai/formatting.py`).
- [ ] **A label keeps the figure right after it** (review rounds 2 and 3): an abbreviation (`PO 7500012345`, `Ref. …`, `S/N …`, `AWB …`, `A/c …`, `Txn: …`) or a reference word with `no`/`#` (`Order no. …`, `Invoice #…`, `Serial No …`), then the figure, and a currency word before an amount (`Rs 9811155555`); never across a sentence's punctuation or a line break. Ordinary and amount words hide nothing, so `Deal value 9500000000`, `Invoice 9100234567 raised` and `Tracking 8901234567 via DTDC` are masked (a false positive is cheaper than a leak), and a label at a sentence's end (`… the PO: 9811155555`) still keeps its figure (`ai/formatting.py`, `_REFERENCE_WORD`, `_MONEY_WORD`).
- [ ] Kept unless named (review round 2, to keep routed answers readable): a spaced PAN (`BATCH 2026 A`), a mobile in 4-5 joined groups (`QC levels 60-65-70-75-80`), `cell` without a label (`Cell 12345678 per ml`); a value after `pass`/`pw` needs 4 characters (or 4 digits), after a token word 8, a CVV 3-4 digits, a code word a digit; `00` numbers need a real country code; plus round 1's residuals: `pass 482913` or `pw …` without a word saying what they open, a passcode after other words or before its keyword, `Code: 482913` and `door code 4826#`, a six-digit `ATM pin`, mobile digits in spaced pairs with no phone word, unnamed `(212) 555-0100` (`ai/formatting.py`).
- [ ] Accepted over-masking: 10-digit serials in a mobile's shape, commit hashes and other hex of 32+ characters, 13-19 digits passing Luhn (an IMEI included), 12 digits passing Verhoeff, `SO 6000123456`, `PIN 5600`, `Fax 2612 0000`, `Password: reset by IT`, the ordinary-word cases above (`ai/formatting.py`).
- [ ] The markers sent to the model: `[secret]` for passcodes and credentials (the value only), `[id]` widened to Aadhaar and card numbers (`ai/formatting.py`, `ai/prompts.py`).

*Queues and write budgets (R153)*

- [ ] `ACTIVITY_MAX_CREATED_PER_USER_PER_DAY` (default 1,000 in any 24 hours; an administrator spends their own in someone's workspace) and `ACTIVITY_MAX_PER_CUSTOMER` (default 5,000, archived included; a customer past it takes nothing new): environment variables, `0` turns either off.
- [ ] The fairness key is the acting user (default; system and anonymous work share one key), and edits, completions and archives aren't budgeted (`core/outbox.py`, `activities/services.py`).
- [ ] The batch constants (default: 200 sources per `ai.index_lead` job, `ai/indexing.py`; 100 records per export batch and 240 s for an export's build, `EXPORT_TIME_LIMIT_S`, below the outbox's 300 s lease, `privacy/exports.py`; 1,000 rows per erasure statement and 60 s per statement in the worker replay, `privacy/services.py`).
- [ ] What `EXPORT_MAX_BYTES` (100 MB) means: the archive as stored, compressed (default). Every customer within the activity budgets fits with natural text in any script (39-76 MB measured); deliberately incompressible text from a large alphabet (random CJK or emoji) at the full budget, about 150-200 MB, fails `too_large` until an operator raises the cap with the worker's `/tmp` and the edge's spool. Alternatives: a larger default (and infrastructure sizing with it), or accept.
- [ ] Stage moves and agreed-price revisions aren't budgeted (default): an export reaches its 240 s limit after about 1.3 million moves of one customer's deals (about 36 hours of a script). Option: a per-deal or per-user daily budget on them (`pipeline/services.py`).
- [ ] Edits aren't budgeted (default): an account re-editing its own long notes can keep up to the indexing queue's in-flight cap (100) ahead of others, about 12 minutes of stale Ask answers. Options: budget edits, a per-user re-index rate, or more `worker-index` concurrency.
- [ ] `ArkrayQueueDominatedByOneUser` fires when one user's work is over 80 % of a backlog above 500 for 30 minutes, as a ticket (`infrastructure/alerts/arkray.rules.yml`).

*Sign-in and accounts (R154, R156)*

- [ ] The daily sign-in cap (default: 50 failures from new browsers in 24 hours, lifted after 30 days, an unlock link valid 24 hours that trusts the browser opening it unless another account's trust is there, networks counted as IPv4 /24 and IPv6 /64; `config/settings/base.py`, `LOGIN_*`, `SIGN_IN_UNLOCK_TTL_S`).
- [ ] An early warning below the cap (proposed, not built): email the owner at most once a day when failures from new browsers reach 20 in 24 hours, and show it in the security events. Today up to 49 failures a day can go on indefinitely and nobody is told.
- [ ] The 7-day promotion cooling-off after an administrator changed an account's password, email or second factor, or created it with a password (`ADMIN_PROMOTION_COOLING_OFF_S`, `config/settings/base.py`).
- [ ] Two-step verification is mandatory for Super Admin accounts only; Admin and User
      accounts cannot enrol, replace, disable, or use MFA step-up.
- [ ] The owner's notices (default: an email when the daily cap locks the account, with the unlock link, when an administrator sets their password, and on any role change, sent to the address the account had when the notice was queued, whatever its status by then, never for a pseudonymised account; `identity/services.py`).
- [ ] Administrators may clear any account's sign-in lock, another administrator's included (default; it changes no credential; audited).
- [ ] The per-address sign-in limit ignores failures from trusted browsers (default; fairness for shared offices; `identity/throttling.py`).

*Uploads and text (R159, R160, R164, R167)*

- [ ] `HYPERLINK` stays allowed in documents, workbooks and CSVs (default: it fetches only on a click; refusing it is one entry in `activities/storage.py`, `_FETCHING_NAMES`).
- [ ] The upload checks' budgets (module constants in `activities/storage.py`, review rounds 1 to 4): one work pool per upload, `UPLOAD_WORK` 1 million units (a unit about a microsecond; 160 bytes read or 64 parsed a unit, an entry 4, opening a member 16, a parser 48, an embedded package 96: round 3's prices), within which the Word fields' check and the formula scan each stop at 1 million (`WORD_XML_WORK`, `SHEET_XML_WORK`) and the CSV fetching check at 1 million (`CSV_CALL_WORK`); an embedded package at most 16 MiB held in memory and 32 MiB for all of an upload's (`EMBEDDED_PACKAGE_BYTES`, `EMBEDDED_TOTAL_BYTES`); 10,000 entries per upload (`MAX_ZIP_ENTRIES`) and a directory of at most 2 MiB, or 128 bytes an entry left (`DIRECTORY_BYTES`, `DIRECTORY_BYTES_PER_ENTRY`); a content-types or relationships part of at most 20,000 elements (`SMALL_PART_ELEMENTS`, round 3: real parts hold fewer than 10,000 in 1 MiB); a central-directory record's name, extra field or comment of at most 1 KiB, and no more records than entries (`DIRECTORY_FIELD_BYTES`, round 4: Office, LibreOffice and Google Sheets write names under 200 bytes; a zip from another tool with long comments or alignment padding is refused). Past any of them an upload is "too large to check": a Word document with a field in every paragraph past about 10.6 MiB of XML (some 500 pages; it was about 15 MiB), a document of about 10,000 tiny story or relationship parts (accepted before round 3; about 5,000 tiny embedded packages still pass), and with `ATTACHMENT_MAX_BYTES` raised above about 20 MB, workbooks with very large embedded files.
- [ ] The upload checks' CPU deadline: 5 s of the request thread's CPU for all of one upload's checks, past which it is "too large to check" (`UPLOAD_CPU_SECONDS`, a module constant, not a setting; review round 3). The lead chose 5 s over the implementer's 3 s: the worst legitimate files (a 10 MB call log, a 10 MB document with fields) cost 0.5-0.9 s on the development laptop, and a throttled production core three or four times slower would have brought them near 3 s. Alternatives: a setting, or a different value.
- [ ] A part named twice in an Office package (names compared as Office compares them) is refused as a type mismatch, "doesn't match its type" (review round 3).
- [ ] CSV and formula edge cases (`_csv_formula_calls`, `_formula_calls`): a 10 MB call log split by all four separators (`,`, `;`, tab and a `sep=` line) is "too large to check" (commas and semicolons alone pass); a quoted formula cell with text after its closing quote (malformed CSV) is refused as calling out; a defined name that is only a fetching function is refused; blanks between a fetching name and `(` in a workbook are still a call (fail closed); a fetching name as a bare argument (`=CONCAT("x",-(email, call, visit))`) is refused, which Excel evaluates only when those words are defined names.
- [ ] Fetching names passed as values (review round 3, `_VALUE_ONLY_NAMES`): `WEBSERVICE`, `RTD`, `CALL`, `EXEC`, `REGISTER.ID` and the `IMPORT*` functions are refused wherever an operand can stand and be called (`MAP(WEBSERVICE,A1:A2)`, `(WEBSERVICE)`, a defined name `+WEBSERVICE`), so a range named `Call`, `Exec` or `RTD` used as a value inside a call is refused (`=SUM(Call)`); `IMAGE` and `REGISTER` keep the narrower rule as plausible range names (`=SUM(Register)` passes), so a defined name `(IMAGE)` or `+IMAGE`, or `MAP(IMAGE,…)`, passes (residual). A CSV cell has no defined names, so there a value-only name counts only inside a call (`- call, then email` is a note). Alternative: refuse `IMAGE` and `REGISTER` the same way and accept the false positives on such range names.
- [ ] Blank-looking characters are refused with a message, never silently stripped (default; `core/text.py`), and variation selectors are accepted only after the bases they are defined for (review rounds 2 and 3: the 359 emoji with variation sequences, checked by the lead against the official Unicode 18.0 `emoji-variation-sequences.txt`, an exact match; keycaps; `0` and the mathematical symbols with standardized variants; the 1,002 CJK compatibility sequences; an ideographic variation selector after a CJK ideograph; a free variation selector after a Mongolian letter): standardized variants on Myanmar, Phags-pa or Egyptian letters, and registered ideographic variants above U+E013F (the Adobe-Japan1, Hanyo-Denshi and Moji-Joho collections), are refused (rare; `_IVS_IN_USE` widens it). Residual: an ideographic variation selector is accepted per range, not per registered sequence, and a Mongolian free variation selector per letter, not per defined variant.
- [ ] Code points unassigned in the server's Unicode data are refused outside the emoji blocks (review round 3, `core/text.py`): the image's Python 3.12 knows Unicode 15.0, so characters added since (CJK Extension I, scripts such as Tulu-Tigalari and Garay) are refused until the server's Python is upgraded; newer emoji are kept. Alternative: accept unassigned code points and the risk that one renders as nothing.
