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
- [ ] Two Redis instances with passwords (and TLS across hosts): the broker `noeviction`
      with AOF, the cache `allkeys-lru` ([deployment.md](deployment.md#production-topology)).
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
- [ ] A ClamAV daemon reachable from the workers and `ATTACHMENT_SCANNER=clamd://…`
      (R94); upload the EICAR test file once: it must be blocked.
- [ ] The bucket's backups on the database's schedule and retention (R93).

**Secrets** (from the secret manager, never in the repository or an image)

- [ ] `DJANGO_SECRET_KEY` (≥ 50 characters), database passwords for both roles, Redis
      passwords, SMTP credentials, `METRICS_TOKEN` (≥ 32 characters) and, only with a model,
      `ANTHROPIC_API_KEY` for the ai worker alone ([security.md](security.md#secrets-management)).
- [ ] The secret manager replicated to the disaster-recovery region.

**Configuration** ([deployment.md](deployment.md#configuration))

- [ ] `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `APP_BASE_URL` (https),
      `TRUSTED_PROXY_COUNT`, `FORWARDED_ALLOW_IPS`, `TRUST_INCOMING_REQUEST_ID=true` behind
      the reference proxy.
- [ ] `DATABASE_URL` as `arkray_app` for web and workers, as `arkray_owner` for the migrate
      job only; `DB_STATEMENT_TIMEOUT_MS=60000` for workers.
- [ ] `DJANGO_ALLOW_INSECURE_LOCAL_HTTP` **unset**.
- [ ] `API_THROTTLE_AUTH` sized to the largest office behind one address (R77).
- [ ] Ask Arkray: `AI_ENABLED` decided; with `AI_LLM_PROVIDER=anthropic`, the items in
      [AI configuration](deployment.md#ai-configuration): a data-processing agreement and
      the provider's retention settings **(org)**, the key on the ai worker only, an https
      `AI_LLM_BASE_URL`, an evaluation on real data (R69) **(org)**.

**Observability** ([observability.md](observability.md))

- [ ] JSON logs shipped to the log platform with a retention period **(org)**; the
      log-based alerts of [observability.md](observability.md#alerts-phase-10) (5xx rate,
      latency, sign-in failures, security events) configured there.
- [ ] `/health/metrics` scraped from each pod with the bearer token (`METRICS_TOKEN`), and
      `infrastructure/alerts/arkray.rules.yml` loaded: database, background work (including
      work no worker has taken), broker, cache, Ask Arkray, indexing backlog.

**People and policy (org)**

- [ ] The first administrator created (`manage.py createsuperuser`); everyone else invited.
- [ ] Retention periods agreed: audit trail, backups (30 days), logs
      ([privacy.md](privacy.md#retention)).
- [ ] RPO ≤ 5 min and RTO ≤ 1 h confirmed with the business, and a quarterly restore drill
      scheduled ([runbooks.md](runbooks.md#restore-from-backup)).
- [ ] The team knows the runbooks ([runbooks.md](runbooks.md)).

## Every release

1. CI green on the commit: backend and frontend gates, image smoke test
   (`.github/workflows/ci.yml`; its first run on GitHub is still to come, R80).
2. Images tagged with the commit SHA; base images pinned by digest.
3. Migrate job as `arkray_owner`: `manage.py migrate --noinput && manage.py
   grant_app_privileges arkray_app`.
4. Release check as `arkray_app`: `manage.py check --deploy --database default` (no
   `arkray.E001`; `arkray.W002` means PostgreSQL would log statements or failing rows,
   R63).
5. Roll out api, workers and web; beat last. Readiness gates traffic; the web server and
   workers refuse a privileged database role.
6. Smoke: sign in, open the dashboard, a lead, the board; `/health/ready` OK; no error lines.
7. Rollback = the previous image, while it can still write to the new schema (additive
   migrations since it; not across `pipeline.0006` / `identity.0004`); otherwise roll forward
   or restore the backup ([deployment.md](deployment.md#release-process); never a
   down-migration).

## After go-live

- [ ] A backup restored into an empty database and the release check run on it
      (`scripts/restore.sh`), within the first month, then quarterly.
- [ ] Dead outbox events reviewed weekly (`manage.py outbox_requeue` lists them).
- [ ] Dependency updates: `pip-audit` and `pnpm audit` in CI; base-image digests moved
      deliberately ([deployment.md](deployment.md#images-phase-9)).
- [ ] Open risks reviewed against [risk-register.md](risk-register.md).
