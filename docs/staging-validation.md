# Staging validation: the repeatable test plan

The capacity, failure and recovery evidence so far was measured on one laptop
([capacity.md](capacity.md), [reliability.md](reliability.md#failure-drills-phase-10)). This is
the plan that repeats it on a **staging environment equal to the target topology**, with the
same tools, in the same order, every time something material changes (a new release
candidate, a new instance size or database tier, a new hosting platform). It also says what a
local run proves and what only staging can ([below](#local-validation-vs-real-hosting)).

The tools (all in `backend/`, run with `uv run python ...` from there):

| Tool | What it does |
|---|---|
| `tests/performance/bench_*.py --seed` | synthetic benchmark data ([Data](#1-data)) |
| `tests/performance/capacity_test.py` | accounts (`prepare`), the 100-user run (`run`), and the targeted runs: `races`, `isolate`, `search`, `ask`, `abuse`, `attach` (its module docstring is the reference; options go **after** the sub-command) |
| `tests/drills/resilience_drill.py` | the resilience drills under load, one fault each, with pass criteria (`list`, `plan`, `run`, `restore`) |
| `tests/drills/restore_drill.py` | encrypted backup, restore, erasure replay ([runbooks.md](runbooks.md#restore-drill)) |
| `tests/drills/pitr_drill.py` | point-in-time recovery with the erasure ledger (its docstring) |
| `tests/drills/clamav_drill.py` | the scanner against a real clamd ([runbooks.md](runbooks.md#file-storage)) |

Every result is labelled with the environment it ran on (`--environment local|staging|...`
for the drills; the report file names and the summary for the rest). **A local number is never
quoted as a staging number**, and the other way round.

## Prerequisites

**The environment.** Staging matches the target topology ([deployment.md](deployment.md#production-topology)):
the same images (digests) as the release candidate, the same instance sizes and
`WEB_CONCURRENCY` for the API, web and each worker deployment, the same managed PostgreSQL
tier, version, extensions and parameters (`work_mem`, `max_connections`, PgBouncer if
production has it: [deployment.md](deployment.md#database-poolers)), separate broker and
cache Redis instances, the private S3 bucket with production's policy (R99), ClamAV, the
same load balancer or platform proxy and TLS setup, the production settings
([production-checklist.md](production-checklist.md)) and the alert rules. Every difference
from production is written down with the results (it limits what they say).

**Synthetic data only.** Never a copy of production, never real people: `capacity_test.py
prepare` gives the picked accounts one known password, and the drills write, erase and
reset data. The benchmark generators below create everything.

**The load generator.** A machine of its own in the region the users will be in (not on the
staging hosts), 4 or more vCPU, its clock synchronised. Its CPU stays under about 70 % during
a run (watch it): past that the generator, not the system, is being measured. All virtual
users come from its one address, so the edge's per-address limits apply to them together
([capacity.md](capacity.md#abuse), [operations.md](operations.md#edge-rate-limits)).

**Access the tools need.** The public URL (the load); the API's own address for
`/health/ready`, `/health/live` and `/health/metrics` (nginx doesn't route `/health`: an
internal address or a tunnel from the generator) and `METRICS_TOKEN`; a SQL connection to the
staging database (`capacity_test.py` reads `pg_stat_activity` and verifies the data;
`prepare` also writes the picked accounts' password hashes, so it needs a role that may
update `identity_user`; everything else only reads; give the reading role `pg_monitor`); for
the drills, `docker` on a single-VM staging host that runs compose, or the platform's console
for managed services (the `manual` driver).

**A single-VM staging host that runs compose** (`infrastructure/compose.production.yml`):
its own secrets file, generated for staging alone (`scripts/init-prod-env.sh
/secure/arkray-staging.env`, never production's values, never the repository's `.env`), with
every Compose command run through `scripts/prod-compose.sh /secure/arkray-staging.env
--project <project>` (adversarial review of 73ff1e3, M4 and its review round 1;
[operations.md](operations.md#production-shaped-stack)). Its PostgreSQL publishes no port and
admits only `arkray_app` (TLS, the stack's own networks) and the owner from the migrate job's
network (`infrastructure/postgres/pg_hba.conf`). For the campaign's SQL access, add to the
staging copy of that file one `hostssl arkray <reader or owner role> <the tunnel's address>/32
scram-sha-256` line per role the tools use, above the final `reject` lines, reached through
an SSH tunnel to a port published on 127.0.0.1 only; remove them afterwards. Never a line
for the superuser (`scripts/prod-compose.sh ... exec postgres ...` on the host is its way
in). A staging host behind a shared office address needs the front proxy
[operations.md](operations.md#edge-rate-limits) requires; run the campaign through it.

**The target.** `capacity_test.py` and the drills (whose load is its virtual users) take the
target from `--base`: for this machine's stack (a loopback `--base`) everything is as it always
was (`Host: localhost:8443`, saved sessions under `localhost.local`, the self-signed certificate
not checked); for any other host the `Host` header is the host's own, saved sessions are filed
under it, and **its certificate is checked**. `--host` overrides the header, `--ca-file` names a
private CA, `--insecure` skips the check (explicitly, never by default), and `--metrics-host`
sets the `Host` the metrics endpoint is asked with (one in `DJANGO_ALLOWED_HOSTS`: the probes'
host, [deployment.md](deployment.md#configuration)). Without `--pg`, or with a database the
monitor can't reach, its database figures read "not sampled" and the rest is still sampled;
the run's own checks (its start time, `verify`) still need SQL access.

**Before the first staging run:** `bench_search.py --seed-text` refuses a database whose name
doesn't contain `bench_search` (see [Data](#1-data)).

## 1. Data

On an empty database migrated by the release's `migrate` job, as the owner role, from the
generator host (`DATABASE_URL=<staging, owner> DJANGO_SETTINGS_MODULE=config.settings.test`):

```
uv run python tests/performance/bench_activities.py --seed     # 100,000 leads, 50,000 deals, ~403,000 activities, 60 owners
uv run python tests/performance/bench_dashboard.py --grow      # +440 users (501), +900,000 leads (1,000,000), +250,000 deals (300,000)
uv run python tests/performance/bench_search.py --seed-text    # realistic, skewed text (database name must contain bench_search: see above)
uv run python tests/performance/bench_rag.py --prepare         # then --seed --shard i/N in N processes, then --build-index (hours of CPU)
uv run python tests/performance/bench_enhancement.py --seed    # personal pipelines, negotiated prices, ~300,000 attachment rows, 2M audit events
```

That is the scale [capacity.md](capacity.md#data) measured (about 1M leads, 300k deals, 2M
activities, 532k index chunks). Two gaps: the step from ~403,000 to ~2M activities (four
older 60-day periods of closed history) was the performance reviewer's own method and has no
script, so a staging copy built this way has about 400,000 activities unless that step is
repeated by hand; and `bench_enhancement.py`'s attachment rows have **no objects** in the
bucket (the daily reconciliation reports them missing; their downloads answer 410), so the
file tests use files uploaded during the runs. Then `VACUUM (ANALYZE)` the large tables
([runbooks.md](runbooks.md#vacuum-after-a-restore-or-a-bulk-load)): a bulk load leaves the
visibility map empty and every index-only count slow ([reliability.md](reliability.md#performance-baseline-phase-10)).
The generators were written phase by phase against the schema of their time (each docstring
names its own prerequisites, such as `manage.py migrate` after the text seeding); run the chain
once on a scratch database of the release's schema before relying on it for staging.

**A pilot** (a first organisation, a smaller tier): `bench_activities.py --seed` alone
(100,000 leads) is enough; record which scale was used.

## 2. Accounts

```
uv run python tests/performance/capacity_test.py prepare --pg <staging, owner> \
    --password <generated, kept out of the repository> --vus 105 --admins 3 --heavy 15 --out users.json
```

The 15 heaviest owners plus a pseudo-random sample, and three administrators (the benchmark
data may hold fewer: promote two of the salespeople not picked before running `prepare`).
Production requires administrators to use two-step verification: the first sign-in enrols
them through the API and keeps their secrets in `users.json`. Keep `users.json` and
`sessions.json` private and delete them after the campaign.

## 3. Capacity: 100 simultaneous active users

```
uv run python tests/performance/capacity_test.py run --base https://<staging host> \
    --users users.json --sessions sessions.json --pg <staging, reader> --db-name <database> \
    --metrics-url http://<api internal address> --metrics-host <the probes' allowed host> \
    --metrics-token "$METRICS_TOKEN" --containers <names on a compose host; empty elsewhere> \
    --stages 10,25,50,75 --stage-seconds 120 --vus 100 --warmup 60 --hold-seconds 600 \
    --out staging-<date>-capacity.json
```

About 20 minutes (plus up to 5 minutes of sign-in the first time: 20 a minute from one
address). Pass: [capacity.md](capacity.md#pass-criteria-the-product-owners) (error rate under 1 %, reads p95
under 750 ms, dashboard and board under 1 s, search under 2 s, writes under 1 s, no duplicate,
orphan, lost update, deadlock or cross-user data, queues drained, nothing growing without
bound), judged on the hold. Compare each endpoint with the local table in
[capacity.md](capacity.md#the-100-user-run-ramp-then-a-20-minute-hold): the difference is
mostly network round trips (a request makes 3 to 14 queries: [reliability.md](reliability.md#performance-baseline-phase-10)).

## 4. Saturation and headroom

The same users, less think time, a shorter hold, one run each:

```
... capacity_test.py run ... --stages 100 --stage-seconds 60 --vus 100 --think-mean 1.0 --warmup 30 --hold-seconds 360 --out staging-<date>-think1.json
... capacity_test.py run ... --stages 100 --stage-seconds 60 --vus 100 --think-mean 0.3 --warmup 30 --hold-seconds 360 --out staging-<date>-think03.json
```

Reset `pg_stat_statements` before each (`SELECT pg_stat_statements_reset();`) and watch:

| Where | What | Saturated when |
|---|---|---|
| API | CPU per instance against its limit; gunicorn workers busy (8 sync per instance); request p95 | CPU at the limit with throughput flat while think time drops (locally the first bottleneck: [capacity.md](capacity.md#headroom)) |
| PostgreSQL | CPU; `arkray_db_server_connections` against `arkray_db_max_connections`; lock waits (`pg_stat_activity.wait_event_type = 'Lock'`); `temp_files`/`temp_bytes` (`pg_stat_database`); the top statements | CPU above about 80 %, connections near the budget ([reliability.md](reliability.md#connection-budget)), temp files growing, a statement's mean rising |
| Redis | `INFO` ops/s and latency, memory against `maxmemory` | latency in milliseconds, evictions on the broker (must be none) |
| Workers | outbox pending and oldest due (`arkray_outbox_*`), broker queue lengths | backlog growing through the hold |
| Edge | 429s and 5xx in the access log (`upstream_status`) | 429s from the per-address limits mean the generator, not the system ([capacity.md](capacity.md#abuse)) |
| Generator | its CPU | above about 70 %: add a second generator host |

```
SELECT calls, round(total_exec_time) AS total_ms, round(mean_exec_time::numeric, 1) AS mean_ms,
       rows, temp_blks_written, left(query, 120) AS query
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
```

Record the request rate and p95 at each think time, which resource reached its limit first
(the saturation point), and the headroom: the request rate at the 100-user run against the
rate where the first p95 target was missed.

## 5. Targeted runs

Each from the same accounts (`--users users.json --sessions sessions.json --pg ...` and the
`--base` of step 3 after the sub-command):

| Run | Command | Pass |
|---|---|---|
| races | `capacity_test.py races` | every scenario `pass` ([capacity.md](capacity.md#targeted-races-live-through-nginx)) |
| isolation | `capacity_test.py isolate --hold-seconds 120` | 0 leaks; inside a support session every other workspace and identity route refused (403/404); dashboard arithmetic exact |
| search | `capacity_test.py search --vus 100 --hold-seconds 180` | p95 under 2 s per term class, no `search_busy`, 0 leaks |
| Ask Arkray | `capacity_test.py ask --vus 50 --hold-seconds 150` with `AI_LLM_PROVIDER=none` on staging for the run | every question answered or, past the 40-question cap, refused 503; none left unanswered past the 90 s deadline; 0 leaks; the rest of the CRM unaffected (locally: structured p95 286 ms, retrieval p95 16.2 s with one questions worker). A real model's latency is R69's, not this run's |
| abuse | `capacity_test.py abuse --vus 50` | each abuser throttled (429) by its limit; the normal users see no errors and stay inside the page targets of [capacity.md](capacity.md#pass-criteria-the-product-owners) ([capacity.md](capacity.md#abuse) for the local figures) |
| attachment storage | `capacity_test.py attach` | **local only**: it drives the fault-injecting S3 fake's control port. On staging, R99's checks instead: upload, download and delete through the application against the real bucket, then an outage: a temporary deny in the bucket policy (a 403: uploads and downloads answer 503 at once, the breaker stays closed, working again as soon as the deny is lifted) and a network rule that drops the traffic to the storage endpoint (the 12 s deadline, then the breaker: 503 `storage_unavailable` at once, working again within its cool-down); everything else unaffected throughout ([reliability.md](reliability.md#attachment-storage), [runbooks.md](runbooks.md#attachment-storage-degraded)) |

## 6. Resilience drills

`tests/drills/resilience_drill.py` runs a steady load (capacity_test's own virtual users, in
its process) for the whole series and, per drill, a baseline, ONE fault at a fixed offset,
the restore, and the recovery; then, with the load paused, the integrity checks. `list`
prints every drill's expected behaviour and pass criterion, `plan` the exact commands and
durations, `run` does it, `restore` undoes every drill's fault after a hard crash (the
`run` itself always restores: on failure, Ctrl+C and exit). Each drill ends PASS, FAIL or
INCONCLUSIVE (a criterion that couldn't be measured is never a pass: no readiness probe, no
`--pg`, an operator who didn't confirm, the generator stalling).

| Drill | Fault | Expected behaviour (summary; `list` has the criteria) |
|---|---|---|
| `api-worker-kill` | one gunicorn worker SIGKILLed | at most the request in flight fails (503 envelope); the master replaces the worker |
| `redis-cache-stop` | cache Redis stopped 30 s | no errors, latency barely moves; readiness `degraded`, back to `ok` within the breaker's cool-down |
| `redis-pause` | cache Redis paused 30 s (stalled) | waits bounded by the 1 s socket timeouts until the breakers open; no errors |
| `redis-broker-stop` | broker Redis stopped 30 s | CRM unaffected; outbox backlog drains after, none dead |
| `beat-restart` | beat stopped 30 s, started | CRM unaffected; exit 0; backlog drains |
| `worker-restart` | the relay worker (`worker-outbox`) SIGTERM, 60 s, started | warm shutdown, exit 0; backlog drains; `--target worker` for the default-queue worker |
| `worker-kill` | the indexing worker SIGKILLed mid-task, started 20 s later | every event done exactly once (the killed one after its 5-minute lease), every note indexed, none dead |
| `api-restart` | the API stopped (SIGTERM) under load, started | requests being served finish; only 503 `service_unavailable` while it is down; no 401; healthy within a minute. With two or more API instances: no error at all |
| `postgres-stop` | PostgreSQL stopped 45 s | 503 `service_unavailable` at once, readiness 503, liveness 200, recovered within 30 s without restarts, no data lost |
| `postgres-stall` | PostgreSQL paused 45 s (R81) | no request waits beyond the proxy's 35 s; only 503s; resumes at once |
| `edge-limits` | one client over the edge's limits | 413 `file_too_large` / 429 `rate_limited` envelopes; nginx not OOM-killed or restarted; users unaffected |
| `attachment-scan` | EICAR and a clean file uploaded | EICAR blocked and purged, never served; the clean file served intact |
| `clamav-stop` | clamd stopped 60 s while files are uploaded | uploads accepted, nothing served before its scan, every file clean after the restart |
| `backup-restore` | `restore_drill.py` | its PASS (local Compose PostgreSQL only; on a managed database the operator reports the restore of [step 7](#7-backup-recovery)) |

On a single-VM staging host that runs compose, the `docker` driver drives everything (`--prefix`
is the compose project's name and a dash). With managed PostgreSQL and Redis, those services go
to the operator (`--manual-services postgres,redis,redis-cache`): the drill prints the action
(for `postgres-stop` a reboot **without** failover; for `postgres-stall` a Multi-AZ failover or
a rule that drops the application's packets to 5432; for Redis a node reboot or a reject rule)
and waits for Enter, timing from the confirmation.

```
uv run python tests/drills/resilience_drill.py plan --drills all --prefix <project>- --manual-services postgres,redis,redis-cache
uv run python tests/drills/resilience_drill.py run --environment staging --drills all \
    --base https://<staging host> --users users.json --sessions sessions.json \
    --pg <staging, reader> --health-url http://<api internal address> --health-host <allowed host> \
    --metrics-token "$METRICS_TOKEN" --vus 100 --prefix <project>- \
    --manual-services postgres,redis,redis-cache
```

About 50 to 100 minutes for all of them (`plan` prints each drill's), plus the sign-in. The
reports: `resilience-staging-<UTC time>.json` (every check, the windows, a 5-second timeline,
readiness changes, the fault's steps) and the same name `.md` (the summary table and every
check). A drill that fails stops nothing; a fault that couldn't be restored, or readiness not
200 before the next drill, stops the series. Without `--health-url` the docker driver probes
from inside the API container every 5 s (recovery times then known to about 5 s). The target
options are capacity_test's (`--host`, `--ca-file`, `--insecure`: [Prerequisites](#prerequisites));
the edge drill's own hand-written requests use the same `Host` header and certificate checking.

## 7. Backup recovery

- **Logical dump and erasure replay:** `tests/drills/restore_drill.py --container <postgres
  container> --report restore-drill.json` against a PostgreSQL whose superuser it may use (it
  creates and drops two databases); quarterly and after any change to backup, restore or the
  ledger ([runbooks.md](runbooks.md#restore-drill)).
- **Point-in-time recovery:** `tests/drills/pitr_drill.py --report pitr-drill.json
  [--wal-load-mb 1024]` proves the self-hosted equivalent (WAL archive, base backup, recovery
  to a time, the ledger replay) on its own containers. On a managed database, the staging run
  is the provider's own: restore to a point in time into a **new** instance, point a staging
  copy of the application at it, check the API is refused until `replay_erasures` has run
  ([runbooks.md](runbooks.md#restore-from-backup)), then replay and check.
- Record for both: the backup's age at the target time (RPO), the time from the decision to
  restore to the API answering again (RTO), the replay's counts, and the vacuum's duration.

## 8. What to record

Per campaign, in one place, named `<environment>-<date>-...`:

- the environment label, the date, the release (commit and image digests), the topology as
  run (instance sizes, database and Redis tiers, settings that differ from production), the
  data scale (users, leads, deals, activities, chunks), the generator host (region, vCPU);
- every command line as run, and every report file (`capacity_test.py --out`,
  `resilience_drill.py`'s JSON and Markdown, the restore and PITR drill reports);
- per run and drill: PASS / FAIL / INCONCLUSIVE against the criterion stated here or in
  [capacity.md](capacity.md#pass-criteria-the-product-owners), with the reason for every non-PASS;
- the saturation point and headroom (step 4), and anything that surprised.

Summaries go into [capacity.md](capacity.md) under a heading that names the environment and
date, never into the local tables; the drill tables keep the environment in every result cell.

## Local validation vs real hosting

The production-shaped local stack ([operations.md](operations.md#production-shaped-stack),
[capacity.md](capacity.md#topology)) runs the same images, settings, proxy and limits on one
machine. What a local run **proves**: the application's behaviour. Correctness under
concurrency (no duplicate, lost or orphaned writes, exact dashboards), isolation between
users, every degradation path and its error envelope, the timeouts, breakers, leases and
retries working as designed, recovery without restarts, the backlog draining, the integrity
of a backup restore and of the erasure replay, and the relative cost of each endpoint on a
realistic data set.

What **only staging can prove**:

- **Real network latency**: every request makes several database and cache round trips;
  locally each is microseconds over a Docker bridge, across hosts or zones each is a
  fraction of a millisecond to milliseconds, multiplied per request.
- **Managed failover behaviour**: an RDS/Cloud SQL reboot or Multi-AZ failover (DNS switch,
  dropped connections, the time it really takes), an ElastiCache failover; whether the
  application's connect timeouts, TCP keepalives and `tcp_user_timeout` bound the waits on a
  real network the way a paused container does locally.
- **S3 (R99)**: the real bucket's consistency, IAM and bucket-policy errors, encryption,
  multipart behaviour above 8 MB, its latency; locally an S3-compatible fake.
- **TLS hops**: the load balancer's termination and re-encryption, certificates from real
  CAs, `sslmode=verify-full` against the provider's CA.
- **Platform proxies**: the client address arriving through `real_ip` and
  `TRUSTED_PROXY_ADDRESSES`, the platform's own idle and request timeouts, body limits,
  HTTP/2 handling and its own 502/504 pages in front of the API's envelopes
  ([hosting-compatibility.md](hosting-compatibility.md)).
- **Noisy neighbours and quotas**: CPU steal and burst credits on shared instances, the
  managed database's IOPS limits, connection limits of the chosen tier.
- **The orchestrator**: probes (a TCP liveness probe, readiness draining), rolling deploys
  with two or more API instances (where `api-restart` must show no error at all), restart
  policies, log shipping and alert delivery.
- **Separation of the generator**: locally the load generator shares the CPU with the stack
  it measures; staging gives each its own.

A local PASS is therefore necessary, not sufficient: it shows the design holds; staging shows
the hosting does.

## Running it locally

The same steps on the production-shaped stack, labelled `local`: the capacity runs' recipe
([capacity.md](capacity.md#reproducing-a-run)), then

```
uv run python tests/drills/resilience_drill.py run --environment local --drills all \
    --users users.json --sessions sessions.json --pg postgres://arkray:...@127.0.0.1:55432/arkray_load \
    --prefix arkray-load- --container postgres=arkray-postgres-1 --vus 50 --out local-<date>-drills.json
```

Docker commands can hang under heavy load (every docker call the drill makes is bounded), and
a laptop that sleeps makes every timing across the sleep meaningless (the drill reports a
stall of 10 s or more as INCONCLUSIVE; check the power events before trusting a long run).
