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
or the whole site shares one bucket.

## Production-shaped stack

```bash
# secrets in a file outside the repository: DJANGO_SECRET_KEY POSTGRES_PASSWORD
# ARKRAY_OWNER_PASSWORD ARKRAY_APP_PASSWORD REDIS_PASSWORD METRICS_TOKEN ERASURE_LEDGER_KEY
. /secure/prod.env
docker compose -p arkray-prod -f docker-compose.yml -f infrastructure/compose.production.yml \
    --profile app up -d --build
docker compose -p arkray-prod ... ps            # every long-running service (healthy)
docker exec arkray-prod-backend-1 python manage.py check --deploy
docker compose -p arkray-prod ... down -v       # this project only
```

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
