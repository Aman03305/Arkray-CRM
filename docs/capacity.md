# Capacity: what was measured, how, and what it does and does not say

Final remediation, 2026-10-06. Target: **100 simultaneous active users** on a production-shaped
stack with realistic data. The tool is `backend/tests/performance/capacity_test.py` (its module
docstring is the reference for every sub-command); the older closed-loop `load_test.py` measures
raw single-connection throughput and stays for that purpose.

> The numbers describe **this machine, this topology and this data**. They are evidence for the
> configuration listed under [Topology](#topology), not a formula, and nothing here says what
> more users, more data or cheaper hardware would do (see [Headroom](#headroom)).

## What "100 active users" means here

A *registered account* is a row in the user table; the data set holds 501. A *virtual user* is
one signed-in person using the CRM: it loops **think, then do one thing**. The think time is
gamma-distributed with a mean of `--think-mean` seconds (2.0 in the capacity runs: a new page
or action about every two seconds, brisker than most people work). One thing is a page view,
which fires the API calls the real page fires together (a dashboard; the board with its
pipelines and summary; a deal with its history, price history and notes; ...), or one of the
smaller writes. It is **not** 100 threads hammering without pauses: that is the stress run
(`--think-mean 0.3`, or `0` for saturation), reported separately as headroom.

### Load mix (the product owner's, kept)

| Share | Action | API calls it makes |
|---|---|---|
| 20 % | Dashboard | `dashboard` |
| 15 % | Lead list | `leads` (page 1, sometimes the next page) |
| 10 % | Lead detail | `leads/{id}`, `leads/{id}/timeline` |
| 15 % | Pipeline board | `pipeline-board`, `pipelines`, `pipeline-summary` |
| 10 % | Opportunity detail | `opportunities/{id}`, `/history`, `/negotiated-prices`, the lead's timeline |
| 10 % | Global search | `search?q=` (45 % common words, 25 % the user's own token, instruments, Unicode, a per-run word) |
| 5 % | Tasks and meetings | 70 % reads (open tasks, summary, upcoming meetings), 30 % a new task or meeting |
| 5 % | Notes | 50 % a lead's timeline, 50 % a new note |
| 5 % | Writes | 40 % new opportunity (always with an `Idempotency-Key`), 40 % stage move, 10 % negotiation entry or price revision, 10 % lead edit |
| 5 % | Other | `auth/me`, `opportunity-options`, a calendar range, the pipelines list |

About 9 % of actions are writes. Three of the 100 virtual users are administrators: they view
the organisation (`all`: the heaviest scope) 60 % of the time and a user's workspace (`{userId}`)
40 %, reads only. Two per cent of every user's actions are an *attack probe* (another user's
opportunity, lead, timeline, dashboard, the organisation workspace, the admin API, another
user's canary token in a search, a move of someone else's deal): each must be refused.

Ask Arkray (retrieval and structured questions) is **not** in this mix: a model call has other
capacity characteristics, so it is measured separately (`ask`, provider disabled) and a real
model never receives load.

## Data

A copy of the benchmark database (`arkray_load`, from `arkray_bench_enh`; the original is never
touched): 501 users (498 salespeople, 3 administrators after the run's preparation), about
1,000,000 leads, 300,000 opportunities, 2,005,148 activities (notes, tasks, meetings), 532,739
semantic-index chunks, 1,501 pipelines, 72,764 negotiated-price rows, 300,021 attachment rows,
2.0 million audit events. The data is skewed like the benchmarks: the biggest owner holds
about 66,000 leads. The 100 users are the 15 biggest owners plus 85 chosen pseudo-randomly
(median owner: about 1,700 leads), so the test is neither the heaviest nor the lightest crowd.

## Topology

(Recorded exactly in the report below.) nginx 1.27 with TLS (self-signed) and HTTP/2 in front of
gunicorn (sync workers, production settings, restricted database role) and the Next.js standalone
server; three Celery workers (CRM `outbox,default,email` x2, indexing x1, questions x2) and beat;
PostgreSQL 16 + pgvector; Redis 7 with a password. Everything runs in Docker Desktop (WSL2) on one
Windows 11 laptop (24 logical cores, 16 GB; the VM has 7.6 GB), with CPU and memory limits on the
application containers (below). PostgreSQL is the development server's container, with the
**default** configuration of the image (`shared_buffers` 128 MB, `work_mem` 4 MB,
`max_connections` 100): a small managed database tier, not a tuned server.

## Reproducing a run

```
# 1. a copy of the benchmark data, migrated; the stack from a scratch Compose overlay
#    (docs: infrastructure/compose.production.yml + a project-specific overlay that points the
#    application services at the database and applies resource limits)
# 2. accounts
uv run python tests/performance/capacity_test.py prepare --password ... --vus 105 --admins 3 --heavy 15 --out users.json
# 3. the ramp and the hold (sessions are saved and reused between runs)
uv run python tests/performance/capacity_test.py run --users users.json --sessions sessions.json \
    --stages 10,25,50,75 --stage-seconds 120 --vus 100 --warmup 60 --hold-seconds 1200 --out run.json
# 4. the targeted and separate runs
... races | isolate | search | ask | abuse
```

`run` signs everyone in (paced under the 20-per-minute sign-in limit, which stays on), writes
each salesperson a canary deal and note, ramps through the stages, warms up at 100, holds, stops,
waits 30 s for the queues to drain, and **verifies from the database**: duplicates, orphans,
lost updates, impossible states, dashboard arithmetic (each user's figures against an independent
SQL computation), and cross-user leaks (every user owns a token `ZQnnnZ` written into the deals
and notes they create; every response a user reads is scanned for any other user's token).

## Pass criteria (the product owner's)

Error rate below 1 % (0 % application errors preferred); normal reads p95 under 750 ms;
dashboard and board p95 under 1 s; global search p95 under 2 s; writes p95 under 1 s; no
duplicate logical creates, no orphan leads, no lost updates, no deadlocks, no cross-user data,
queues drained afterwards, no resource that only ever grows.

## Results (2026-10-06/07, final code)

### Topology as measured

| Piece | Configuration |
|---|---|
| Host | Windows 11 laptop, 24 logical cores, 16 GB RAM; Docker Desktop (WSL2) VM 7.6 GB; other projects' containers running alongside |
| Edge | nginx 1.27, TLS 1.3 (self-signed), HTTP/2; limits 1 vCPU / 256 MB |
| API | gunicorn, `sync` workers, `WEB_CONCURRENCY=8`, 30 s timeout, production settings, restricted role `arkray_app`; limits **4 vCPU / 3 GB** |
| Web | Next.js 16 standalone server; 2 vCPU / 768 MB |
| Workers | Celery `outbox,default,email` x2 (2 vCPU / 1 GB), `ai_index` x1 and `ai` x2 (1 vCPU / 1.5 GB each), beat |
| PostgreSQL | 16 + pgvector 0.8.6, image defaults (`shared_buffers` 128 MB, `work_mem` 4 MB, `random_page_cost` 4, `max_connections` 100); no CPU limit (it used at most 2.9 cores) |
| Redis | 7.4, password, AOF; 1 vCPU / 256 MB |
| Load generator | `capacity_test.py` on the same host (asyncio, HTTP/1.1 keep-alive, up to 6 connections per user) |

### The 100-user run (ramp, then a 20-minute hold)

Accounts: 501 registered (498 salespeople, 3 administrators); 100 concurrent virtual users (97
salespeople, 3 administrators), all signed in. Think time mean 2 s.

| Stage | Seconds | API requests | req/s | Errors | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|
| 10 users | 120 | 1,116 | 9.3 | 0 | 24 ms | 123 ms | 178 ms |
| 25 users | 120 | 2,641 | 22.0 | 0 | 35 ms | 115 ms | 200 ms |
| 50 users | 120 | 5,271 | 43.9 | 1 (0.02 %) | 34 ms | 107 ms | 266 ms |
| 75 users | 120 | 7,940 | 66.2 | 0 | 42 ms | 131 ms | 232 ms |
| **100 users (hold)** | **1,200** | **106,593** | **88.8** | **0** | **38 ms** | **137 ms** | **237 ms** |

The single error was one request at 50 users that got no answer (status 0 after 2.1 s); nothing
in the server logs explains it, and nothing like it happened in the 106,593 requests of the hold.
No request was throttled (429). Page views (all of a page's calls together) at 100 users: p50
52 ms, p95 167 ms, p99 280 ms.

| Endpoint at 100 users | n | p50 | p95 | p99 | Target (p95) |
|---|---|---|---|---|---|
| Dashboard | 11,541 | 45 ms | 117 ms | 272 ms | < 1 s |
| Lead list | 8,415 | 25 ms | 73 ms | 136 ms | < 750 ms |
| Lead detail (page with timeline) | 5,999 (5,706 pages) | 23 ms | 74 ms (page 109 ms) | 137 ms | < 750 ms |
| Pipeline board (page: board + pipelines + summary) | 8,648 | 88 ms (page 91 ms) | 208 ms (page 224 ms) | 339 ms (page 419 ms) | < 1 s |
| Opportunity detail (page: 4 calls) | 5,778 | 32 ms (page 55 ms) | 109 ms (page 165 ms) | 182 ms (page 247 ms) | < 750 ms |
| Global search | 5,672 | 77 ms | 220 ms | 344 ms | < 2 s |
| Writes: opportunity create | 1,087 | 80 ms | 163 ms | 232 ms | < 1 s |
| Writes: stage move / negotiation / lead edit | 1,074 / 256 / 293 | 42 / 41 / 32 ms | 87 / 87 / 77 ms | 142 / 130 / 103 ms | < 1 s |
| Writes: note / task / meeting | 1,403 / 551 / 363 | 48 / 44 / 45 ms | 100 / 99 / 104 ms | 166 / 147 / 150 ms | < 1 s |

Resources during the hold (sampled every 5 s): API container 1.7 cores average, 2.6 at p95, 3.8
max (of 4); PostgreSQL 0.8 cores average, 1.5 p95, 2.9 max; nginx 0.09 cores; Next.js 0.03.
**PostgreSQL connections: 8 to 14 (average 9.5, p95 11)**: gunicorn's 8 workers keep one each
(`CONN_MAX_AGE` 60 s) and the Celery workers connect when they work, so 100 users never meant
100 connections. Redis: 43 clients, 1.9 ms average PING (4.7 ms p95), 234 operations a second.
Celery: the outbox never held more than 32 pending events, the oldest due at most 4.5 s; queues
empty within 30 s after the run; no dead events; the indexing queue peaked at 27 and drained.
No lock waits (0 sampled), no deadlocks, the longest transaction 0.8 s.

**Memory over the hold** (averages of the first, middle and last thirds): API 586 / 619 / 615 MB
(gunicorn recycles its workers every ~2,000 requests); Celery 151 / 150 / 149 MB; Next.js 157 /
156 / 154 MB; Redis clients 43 / 43 / 43; PostgreSQL 1,992 / 2,062 / 2,083 MB, which is its page
cache filling with the 7 GB data set (it levels off), not a leak. Nothing grows monotonically.

### Data integrity under that load (verified from the database afterwards)

1,418 logical opportunity creations, each with its own `Idempotency-Key`; 154 of them were
re-sent with the same key (lost-answer retries and double submits) and replayed. Result: 1,418
opportunities, 1,418 leads, 1,418 idempotency records, **0 duplicate logical creates, 0 orphan
leads, 0 lead/opportunity owner mismatches**. 5,002 opportunity versions checked against the
run's own successful writes: **0 lost updates**; 1,382 moved opportunities, each with exactly one
history row per successful move; **0 impossible states** (closed without a close date, open with
one, negative values, a negotiation-stage deal without a price); **0 deadlocks**. After every
create the user's own dashboard showed exactly the expected lead total (1,086 checks, 0 wrong);
after the run every user's dashboard figures (total leads, pipeline value, weighted pipeline,
open opportunities, open tasks) equalled an independent SQL computation for 97 of 97 users.

### Authorization and isolation under that load

Every response a user read was scanned for any other user's canary token. In the final hold the
scanner reported two hits, both in board responses; both were random matches inside sealed page
cursors (URL-safe base64): one was `ZQ415Z`, a token no user has and no record contains. The
scanner now skips cursors, and the 2x-rate run below (53,027 requests) found **0**. 1,081 attack
probes (another user's opportunity, lead, timeline, dashboard, the organisation workspace, the
admin API, a stage move of someone else's deal, another user's token in a search): **every one
refused** (403/404) or empty. `isolate` (2 minutes, 9,329 requests): six users hammering
dashboard, board, search and lists while one administrator switched between users and another
entered and left **97 support sessions**: 0 leaks; inside a session the subject's workspace
opened and every other workspace and every identity route (users list, set password, change
email) answered 403; dashboard arithmetic exact for all six users. Every API response carries
`Cache-Control: no-store, private` and `Vary: Cookie`, and no layer caches them.

### Targeted races (live, through nginx)

| Scenario | Result |
|---|---|
| 20 parallel moves of one deal, same version | one transition (1 history row, version +1); the rest 409, or 200 for the stage it already reached |
| owner and administrator move the same version | one 200, one 409 |
| 20 parallel entries into Negotiation, different prices and CPTs | one 200; 19 answered 400 "already in this stage"; 1 history row; the winner's price `1234567.89` exact; actor and CPT recorded |
| 20 parallel price revisions, same version | one 200, 19 x 409, 1 history row |
| same `Idempotency-Key` x2, x20, **x100** in parallel | 1 opportunity + 1 lead each time; all 201 with one id, n-1 flagged replayed |
| same key, other details | 422, nothing written |
| new key, same details | a second opportunity (legitimate duplicates are allowed) |
| no key | 400, nothing written |

### Global Search alone (100 users searching, 3 minutes, 8,807 searches)

| Kind of query | p50 | p95 | p99 | Outcome |
|---|---|---|---|---|
| common word | 65 ms | 181 ms | 922 ms | 200 |
| customer name | 69 ms | 202 ms | 859 ms | 200 |
| instrument | 66 ms | 165 ms | 471 ms | 200 |
| rare (own token) | 71 ms | 178 ms | 254 ms | 200 |
| two words | 64 ms | 208 ms | 999 ms | 200 |
| Unicode | 65 ms | 168 ms | 612 ms | 200 |
| one or two letters, `%`, `--` | 16 ms | 86 ms | 709 ms | 400 (too short; nothing searched) or 200 |
| 300 characters | 16 ms | 32 ms | 90 ms | 400 (refused) |

No `search_busy` (the 2 s per-statement limit) fired; 9 database connections; 0 leaks.

### Ask Arkray alone (provider disabled, 50 users, 150 s)

Structured questions (answered from the CRM's tools): p50 43 ms, p95 286 ms end to end.
Retrieval questions (semantic search over 532,739 chunks, local embedding model): accepted at
once (p95 251 ms) and answered by the `ai` worker in p50 0.57 s, **p95 16.2 s** end to end,
because the single 1-vCPU questions worker became the queue (up to 38 waiting; 3 questions
refused with 503 when the 40-question cap was reached). The rest of the CRM was unaffected.
Retrieval capacity scales with `ai` worker processes; a real model's latency (R69) is not
measured here.

### Abuse

One abusive user at a time, 8 parallel connections without pauses, while 50 normal users work.

| Abuse | Abuser got | Normal users' p95 (baseline 96 ms) |
|---|---|---|
| sign-in guessing (anonymous, new connections) | 1,683 x 429 (edge), 218 x 403, 9 x 400 in 45 s | 315 ms (p99 915 ms); before the edge limit 818 ms (p99 3.2 s) |
| Global Search | 125 answered, 6,702 x 429 | 178 ms |
| opportunity creation | 642 created, then 5,135 x 429 (the 600/min user limit) | 183 ms |
| attachment upload | 30 stored, 14,794 x 429 (30/min) | 156 ms |
| Ask Arkray | 21 accepted, 16,925 x 429 | 129 ms |

No errors for the normal users in any phase; back to 74 ms p95 at once.

### Failure drills

50 users; the fault injected 40 s into a 200 s hold.

| Fault | Errors seen by users | Latency effect | Recovery |
|---|---|---|---|
| a gunicorn worker killed (SIGKILL), then another stopped (SIGTERM) | 1 (the request in flight on the killed worker: 502) | none | immediate (the master restarts it) |
| Redis paused 30 s (stalled, not refused) | 0 | p95 1.5 s for the first 10 s (until each process's breaker opened), then normal | immediate |
| Redis stopped 30 s (refused) | 0 | none | immediate |
| the CRM Celery worker stopped 60 s | 0 | none | outbox backlog 130 events, oldest 63 s, drained within 10 s of the restart |

Integrity after each drill: 0 duplicates, 0 orphans, 0 lost updates, 0 deadlocks, dashboards exact.

### Attachment storage failures

30 users; 8 more uploading and downloading continuously; the store an S3-compatible fake endpoint
switched between faults.

The first run found a real fault: a black-holed or 5 s-slow store held all 8 web workers (each
storage call waits up to the 12 s deadline, and the breaker, per process, needed three deadlines
in each process), so every other user's pages took **9-11 s** (p95). Fixed: the breaker and a cap
of 3 storage calls in flight are now shared by every process through Redis
(`ATTACHMENT_STORAGE_MAX_IN_FLIGHT_SHARED`; per process when Redis is down). After the fix:

| Fault (35 s each) | Uploads / downloads | Everyone else's p95 |
|---|---|---|
| none (baseline) | 201 / 200 (p95 70 / 53 ms; 12 of 208 refused "busy" with 8 people uploading at once) | 83 ms |
| 503s from the store | 503 at once (p95 53 ms) | 89 ms |
| black hole (accepted, never answered) | the first few wait the 12 s deadline, then 503 at once | **111 ms** |
| every request 5 s slow | downloads 5 s, uploads 503 | **122 ms** |
| 403 (IAM) | 503 at once | 86 ms |
| 500 | 503 at once | 89 ms |
| objects gone (404) | downloads **410 `attachment_unavailable`**, uploads 503 | 69 ms |
| download cut mid-body | 503, never a truncated 200 | 76 ms |

After each fault healed, uploads and downloads worked again within the breaker's cool-down.
Errors for everyone else: 0 throughout.

### Query audit (pg_stat_statements over the final run)

The heaviest statements are the per-user list and timeline queries (5,916 calls each, mean 6-11
ms) and the dashboard's pipeline totals (25,530 calls, mean 2 ms). Three statements spilled sorts
to temporary files, all **organisation-wide** (administrators): the activity calendar range (64
calls, mean 472 ms, max 1.5 s, 160 MB written in all), the organisation lead list (65 calls, mean
57 ms) and the organisation opportunity list (65 calls, mean 82 ms). The sequential scans of the
big tables (leads 723, opportunities 2,305 during the hold) are the organisation-wide aggregates of
the administrators' dashboards and boards (mean 94-119 ms). These sorts would stay in memory with
the `work_mem` the deployment guide recommends (16-32 MB; this test ran PostgreSQL's 4 MB default).

### Headroom

The same 100 users at **twice the request rate** (think time 1 s, 6 minutes): 147 req/s, **0
errors**, p95 572 ms, p99 852 ms (dashboard page p95 574 ms, board 740 ms, search 675 ms,
opportunity create 618 ms); integrity checks and the leak scan all clean. The API container sat at
its 4-vCPU limit (3.8 cores average) while PostgreSQL used 2: **the next bottleneck is the web
tier's CPU** (4 vCPU, 8 sync workers), not the database or its connections (14 at most). Adding web
capacity (more vCPU and `WEB_CONCURRENCY`, or a second API instance) is the first step for more
load; this test does **not** show how many users that would serve.

### What this does and does not say

100 simultaneous active users, at the stated pace, on this topology and this 1M-lead data set,
were served with 0 errors in a 20-minute hold, every endpoint inside its target, no data
corruption, no cross-user data, and bounded behaviour under abuse and faults. It says nothing
about a different machine, a smaller database tier, a real S3 bucket, a real language model, or
more users; re-run `capacity_test.py` against the real deployment before relying on it there.
