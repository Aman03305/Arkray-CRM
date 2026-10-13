# Operations

Day-to-day procedures for operators. Runbooks for alerts, incidents, restores, secret
rotation, erasure requests and the rest are in [runbooks.md](runbooks.md).

## Rebuild the RAG index

Ask Arkray's semantic index (`ai_knowledge_chunk`) is **derived data**: every row is rebuilt
from the CRM records, so it can be deleted at any time without losing anything. While it is
being rebuilt, Ask Arkray's note search finds fewer passages; structured answers, the CRM and
global search are unaffected.

```bash
# Re-index everything now (unchanged text is skipped; safe to run any time, any number of times)
docker compose run --rm backend python manage.py ai_reindex

# Start from an empty index (after a model change, or to prove rebuildability)
docker compose run --rm backend python manage.py ai_reindex --rebuild

# Let the ai_index workers do it in the background (bounded batches through the outbox)
docker compose run --rm backend python manage.py ai_reindex --enqueue
```

- The command runs in bounded batches of 200 sources and prints progress. It never changes
  its database session (safe behind a transaction pooler): only the `--rebuild` delete
  lifts the statement timeout, with `SET LOCAL` inside its own transaction; every other
  statement runs under the connection's usual timeout.
- Throughput is CPU-bound (local embedding model): 5-7 chunks per second per 3-thread
  process for typical notes, 1.5-2 for long ones, on the benchmark machine; 486,646 notes
  took about 3 h 25 min in 8 processes. Run several workers on queue `ai_index` (with
  `--enqueue`) for a large backfill (see [rag-architecture.md](rag-architecture.md#performance)).
- Verify: `SELECT count(*), count(DISTINCT source_id) FROM ai_knowledge_chunk;` against the
  number of notes with text, then ask a question whose answer is in a known note.

## Check the embedding model files

```bash
docker compose run --rm worker-ai python manage.py ai_fetch_model --verify
```

The image fetches and verifies the pinned files at build time
(`python -m arkray.ai.model_files`); a mismatch fails the build.

## Rate limits while Redis is down

The API's request rates (`API_THROTTLE_*`) are counted in the Redis cache, keyed by user or
by client address (an IPv6 client by its /64; `arkray.core.throttling`). When a process
can't reach the cache (connection refused, the circuit breaker open, a timeout) it counts
in its own memory instead: a token bucket per key at the configured rate, at most 10,000
keys per process (the least recently used dropped first). Each process logs
`throttle_local_fallback` at most once a minute while it does. When the cache answers again
the counts go back to it (starting afresh: it never saw the outage's requests).

What that means, precisely (R127):

- **Never "no limit", never "refuse everyone"**: the CRM keeps answering without Redis, and
  every request rate still applies.
- **Per process, not shared**: a client whose requests are spread over P API processes
  (gunicorn workers x pods; up to 8 workers per pod) can reach P times the rate while the
  outage lasts. With 2 pods of 4 workers that is 8x: 160 sign-in-route requests a minute
  per address instead of 20, 960 searches a minute per user instead of 120.
- **A process restart forgets its counts** (gunicorn recycles workers after a number of
  requests, and a deploy restarts them all): a client gets a fresh budget in that process.
- **More than 10,000 distinct clients hitting one process during an outage** push the
  oldest buckets out; a client pushed out starts with a full bucket again.
- **An IPv6 /64 is one client**, at all times, not only during an outage: everyone in an
  office whose network hands out addresses from one /64 shares the per-address budgets
  (sign-in route, anonymous requests), exactly as everyone behind one IPv4 NAT address
  already does (R77). Size `API_THROTTLE_AUTH` for the people who sign in within a minute
  from one office, whichever protocol it uses.
- **What does not depend on Redis at all**: sign-in failures (per account and browser, and
  per address /64) and password-reset requests per address are counted in PostgreSQL
  (`identity.throttling`); Ask Arkray's pending questions, data exports (pending and per
  hour per administrator) and upload slots are capped in the database or per process; the
  edge proxy's per-address limit on the sign-in, reset and invitation routes is in nginx's
  own memory ([below](#edge-rate-limits)). One-time links are 256-bit tokens: guessing one
  is not a rate question.

Alert: `ArkrayCacheDown` (or the platform's own); the fallback buys time, it
isn't a way to run.

## Edge rate limits

The reference nginx limits `/api/v1/auth/(login|password-reset|invitations)` to 5 requests a
second per client, bursts of 30, answered 429 in the API's JSON shape. "Per client" is
`$arkray_client_key` (`infrastructure/nginx/arkray.conf`): an IPv4 address as is, an IPv6
address reduced to its /64 written as four groups (`2001:db8:0:5::1` and
`2001:db8:0:5:1:2:3:4` are both `2001:db8:0:5`). The map reads nginx's own text of the
address and finds the first four groups by where `::` is;
`tests/architecture/test_edge_rate_limits.py` checks it against a port of nginx's
formatting for 20,000 random addresses and every compression shape, and it was checked live
in the pinned nginx through `real_ip` with 3,226 addresses (0 mismatches). Behind another
proxy or load balancer, set nginx's `real_ip` module to take the client address from it,
or the whole site shares one bucket: **required behind a CDN** (adversarial review of
73ff1e3, L13). `arkray.conf` carries the lines, commented out where the key is made:
`set_real_ip_from` for each of the proxy's published ranges only (never `0.0.0.0/0`: any
client could then name any address) and `real_ip_header CF-Connecting-IP` (an ALB:
`X-Forwarded-For` with `real_ip_recursive on`); and only those ranges may reach the origin
(a security group, or Cloudflare's Authenticated Origin Pulls). Without them one slow client
holds every limit below for every user.

**Requests to the API** (adversarial review of 73ff1e3, L2: DRF checks permissions before its
throttles, so 401 and 403 answers were never counted and cheap refused requests could keep the
sync web workers busy). Every `/api/` location limits requests per signed-in session (its
cookie, read only in Django's 32-character format) at 20 a second with bursts of 200, per
address for requests without a session at 10 a second with bursts of 100, and per address
in all at 300 a second with bursts of 1,000: twice what the capacity test's 100 users from one
address reached at twice their normal pace (147 a second, [capacity.md](capacity.md#headroom)).
Checked live in the pinned nginx: 300 anonymous requests at once, 105 answered and the rest
429; 400 at once on one session, 212 and the rest 429; 100 sessions from one address at 149
a second for 8 s, none refused. Refusals are 429 in the API's JSON envelope with the request
id and `Retry-After`. Django's own limits (R77) stay the authority for everything they count.

The same key also limits **requests in flight** (`limit_conn`, deep security audit H-1): 256
per client in every location; request bodies that can reach a temp area (declared over 16 KB,
or not declared) 8 per session, or per address without one, and 16 per address in all; 4
attachment uploads per client (a zone of their own, so an office's page loads never count
against its uploads), and 2 data-export downloads per client (4 in all), refused 429 in the
API's JSON shape. A sign-in or an ordinary save (a body of 16 KB or less, kept in memory)
takes no JSON-body slot: one person trickling bodies on an office's shared address held all 8
of its slots and blocked everyone's saves and sign-ins (adversarial review of 73ff1e3,
L13). Small bodies have a ceiling of their own instead, **128 per address**, half the
request slots, so whatever they hold the address keeps room for its reads and page loads
(review round 1, INFRA-3: 256 small trickled bodies had held every request slot of their
address). **Pages** take only GET and HEAD without a body: anything else is answered 405
before any slot is taken (256 trickled `POST /login` page requests had blocked an address's
sign-ins, page loads, saves and uploads). Uploads don't count against the JSON bodies'
slots (forged sessions' chunked JSON trickles had blocked co-workers' uploads); headers must
arrive within 10 s in all. Checked live: with 8 bodies of 1 MB trickled a byte every 5 s by one
session, a sign-in, a co-worker's save and a co-worker's 20 KB note all got through and only
the trickler's ninth body was refused; forged session cookies held 8 more slots, then 429;
256 trickled page POSTs blocked nothing; 16 chunked JSON bodies under two forged sessions
left uploads working. Signs in the access log: bursts of 429 on `/api/v1/workspaces/*/activities/*/attachments`; a 429 on
`/api/v1/admin/privacy/exports/*/download` means the export slots are busy. An upload
without a session is answered 401 by the edge's session check (`auth_request` to
`/api/v1/auth/me`) before its body is read; a 503 on every upload while the API is up
points at that check (its timeout is 10 s). The upload area (`/var/cache/nginx`, 64 MB
tmpfs) or the JSON-body area (`/var/cache/nginx-body`, 40 MB) full shows as 503
`service_unavailable` from the edge on uploads or on large JSON writes, and as `No space
left on device` lines in nginx's error log (it logs `crit`, so they appear). They empty
themselves as the slow clients time out (10 s without a byte of a body) or finish. The export spool
volume `edge-export-spool` can be emptied at any time with nginx stopped (nothing in it
outlives a request). If a legitimate office hits the per-client limits, raise them in
`arkray.conf` together with the tmpfs sizes and the container's memory limit (the tests
check they stay consistent).

What remains possible without signing in (deep security audit H-1 and its review; R129;
review round 1 of the adversarial review of 73ff1e3, INFRA-3). The root of the "one address"
rows: nginx bounds the time *between* two reads of a body (10 s) but has no deadline for a
whole body, so a client sending a byte every few seconds keeps its request open indefinitely,
and a session cookie can be forged before anything checks it:

| What | Bound | Addresses to exhaust | Effect while it lasts |
|---|---|---|---|
| Upload area (64 MB) | not reachable: the body is read only for a confirmed session | cannot, without an account | none |
| JSON-body area (40 MB), bodies over 16 KB | 8 x 1 MB per session (or address); 16 x 1 MB per address, forged sessions included | 3 | JSON writes over 16 KB get 503 site-wide |
| JSON-body slots of one address, held with forged session cookies | 16 per address | 1 (the office's own) | that address's JSON writes over 16 KB get 429 |
| Small-body slots of one address (16 KB or less), trickled anonymously or under forged sessions | 128 per address | 1 (the office's own) | **that address's sign-ins and saves get 429**; its page loads, reads and uploads work (checked live) |
| JSON-body area, undeclared HTTP/2 bodies on API routes (not counted) | 256 x 1 MB per client, area capped | 1 | JSON writes over 16 KB get 503 site-wide |
| Export download slots (4 in all), held by trickling a chunked or undeclared body | 2 per client | 2 | administrators' export downloads get 429; nothing spooled |
| The address's own upload slots, held by forged-session uploads being discarded after their 401 | 4 per address, about 30 s each (`lingering_time`) | 1 (the office's own) | that address's uploads get 429 |
| Requests in flight (memory) | 256 per client | many (a DDoS) | the platform's DDoS protection is the bound |
| Connections still sending their headers (not counted by `limit_conn`: nginx counts a request once its headers are read) | `worker_connections` (1,024 per worker process in the image) and the 10 s header deadline | 1, with enough connections | **every client** gets refused or stalled connections while it lasts (slowloris) |

**A shared office address needs a front proxy with a total request deadline.** For any
deployment where many people share one public address (an office behind NAT, a guest
network), the rows marked "1 (the office's own)" and the header row mean one person there can
deny their colleagues sign-ins and saves, or (headers) everyone service, without an account.
nginx alone can't end that; a proxy in front that bounds the *whole* time to receive a
request, headers and a small body together, and the connections per client address, does:
HAProxy (`timeout http-request 10s` with `option http-buffer-request`, a per-source
connection cap with a stick table), or a CDN or load balancer documented to do the same (then
nginx's `real_ip` from it, above). It is a production requirement for that topology
([deployment-readiness.md](deployment-readiness.md); production-checklist). Verify it once
through the real front: a sign-in POST whose 1 KB body arrives a byte every 4 s must be cut
off at the proxy's deadline, and a client opening connections without finishing their headers
must be capped.

With an account the per-client limits still apply: a signed-in user from 2 addresses can fill
the upload area (4 x 11 MB each); their uploads are attributable (session, audit) and others'
get 503 while it lasts.

## Production-shaped stack

```bash
# once: every secret generated (ARKRAY_PROD_*), owner-only, outside the repository
scripts/init-prod-env.sh /secure/arkray-prod.env
# docker compose --env-file <it> -p arkray-prod -f docker-compose.yml
#   -f infrastructure/compose.production.yml --profile app, with every ARKRAY_PROD_* unset first
prod=(scripts/prod-compose.sh /secure/arkray-prod.env)
"${prod[@]}" up -d --build
"${prod[@]}" ps                                 # every long-running service (healthy)
docker exec arkray-prod-backend-1 python manage.py check --deploy
docker exec arkray-prod-worker-1 python manage.py preflight --only credentials,database,redis
"${prod[@]}" down -v                            # this project only
```

**Every** `docker compose` command for this project goes through `scripts/prod-compose.sh
<secrets file>` (`--project <name>` for another project), which names the secrets file with
`--env-file` (adversarial review of 73ff1e3, M4). Compose then reads that file instead of
the checkout's `.env`; without it Compose stops at the first missing `ARKRAY_PROD_*`
variable, and no development file defines one. **Never load the file into a shell**: a
variable exported in the shell wins over `--env-file` (Compose's precedence), so an
`ARKRAY_PROD_*` left there by `set -a; . file` (or another environment's file) would
silently replace the file's value; the wrapper unsets every `ARKRAY_PROD_*` and
`COMPOSE_ENV_FILES` before it runs Compose (review round 1, INFRA-5), and a plain `. file`
without `set -a` exports nothing at all (Compose then looked in `.env`, where
`.env.example`'s published superuser password filled `POSTGRES_PASSWORD`). Before anything else
starts, `credential-guard` checks every secret (`config/credentials.py`): missing, a value
the repository publishes or a placeholder, under 32 characters, too few or too unevenly
used characters, predictable, a repeated part, a keyboard or alphabet run, words and
numbers joined, a common password word, or one value for two secrets, and the whole stack stays down,
its log naming the variable (`"${prod[@]}" logs credential-guard`), never a value. The
secrets embedded in connection URLs (database and Redis passwords) use letters, digits and
`- . _ ~` only; the generator writes 64 hex characters for each.

Who reaches the database (`infrastructure/postgres/pg_hba.conf`): the superuser only on
the server's local socket, inside the PostgreSQL container (`"${prod[@]}" exec postgres psql
-U arkray`) or in the one-shot `db-roles` job, the only other container mounting the
socket's volume (it has no network at all), physical replication included (pg_basebackup,
pgBackRest, WAL-G beside the server: [runbooks.md](runbooks.md#database-backup));
`arkray_owner` only from `db-owner` (`10.252.17.16/28`, internal), which only `migrate` joins,
so `run --rm migrate python manage.py replay_erasures ...` and the other owner commands run
there; `arkray_app` over TLS from the stack's networks; both only to `arkray` and to
`arkray_restored`, the database a dump is restored into before it is renamed into place;
SCRAM everywhere, everything else rejected. Backups and restores run as the owner in one-off
containers of the PostgreSQL image on `db-owner`, exactly as
[runbooks.md](runbooks.md#restore-from-backup) writes them (checked live, end to end: dump,
restore into `arkray_restored`, the grant and replay as the owner, the release check as
`arkray_app`, the swap; the same `pg_dump` from `default` is refused). A rule change takes
effect on `SELECT pg_reload_conf();` on the socket, without a restart.

Checks worth repeating after a change to it: clamd answers `PONG` from the `worker`
container and is unreachable (no such host) from every other one; `/login` through
https://localhost:8443 (curl `--resolve localhost:8443:127.0.0.1`); with the frontend
stopped, a page is the 503 page with a reference; with the backend stopped, an API call is
the 503 JSON envelope; the ClamAV drill from the `scanner` network
(`python tests/drills/clamav_drill.py --scanner clamd://clamav:3310 --max-bytes 10485760
--stream-max-length 16777216`, in a container of the backend image attached to
`arkray-prod_scanner` with the source mounted).

## Backups: transport and signature

`scripts/backup.sh` and `scripts/restore.sh` refuse a remote database connection weaker than
`sslmode=verify-full` (they set it when the URL names no mode; loopback and Unix sockets
are exempt), so pass the server's CA in the URL (`sslrootcert=/path/ca.crt`, or
`sslrootcert=system` for a public CA) or `PGSSLROOTCERT`. Every encrypted dump is signed
with the backup host's OpenPGP signing key (`BACKUP_GPG_SIGNING_KEY_FILE`); restore checks
the detached `.sig` with that key's public half (`BACKUP_SIGNER_KEY_FILE`) before
decrypting. Make the signing key once, on the backup host:
`gpg --quick-generate-key "Arkray backup host <ops@example.com>" ed25519 sign never`,
export its secret half to the file the backup job reads (owner-only) and its public half to
wherever restores happen. The restore drill (`tests/drills/restore_drill.py`) checks both,
and that a copy altered after the backup, with a fresh `.sha256`, is refused.

## ClamAV signature freshness

The reference `clamav` container's health check (`infrastructure/clamav/healthcheck.sh`)
fails when clamd doesn't answer `PING` **or** when its daily signatures, as clamd itself
reports them (`VERSION`), are more than 3 days old (`CLAMAV_MAX_SIGNATURE_AGE_S`). freshclam
failing quietly (the mirror unreachable or rate-limiting, a full volume) would otherwise
leave clamd healthy while it scans with ever older signatures. An unhealthy `clamav` doesn't
stop scanning; alert on it (container health, or the platform's equivalent) and look at
freshclam's lines in `docker compose logs clamav`. An installation without internet access
needs its own signature mirror (`DatabaseMirror` / `PrivateMirror` in freshclam.conf).

## Proxy trust

`X-Forwarded-For` is believed only from `TRUSTED_PROXY_ADDRESSES` (the edge proxy's own
addresses as Django sees them). Anything else that connects to gunicorn directly, a worker
container or a probe, is keyed and logged by its own address whatever header it sends
(adversarial review P2-2). In the reference stack nginx has a fixed address on `api`
(`10.252.17.10`), an internal network it shares with the API alone, and isn't on the network
the API shares with the data services and workers; the web tier is on `web` with nginx and
has no route to the API. If the edge's address changes (another subnet, more replicas),
change `TRUSTED_PROXY_ADDRESSES` and `FORWARDED_ALLOW_IPS` with it: a request through an
untrusted address is keyed by the proxy's address, so the whole site would share one
rate-limit budget.
