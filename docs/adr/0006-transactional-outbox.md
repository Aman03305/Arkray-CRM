# 0006. Transactional outbox + Celery; one retry layer; bounded in-flight work

Status: Accepted
Date: 2026-09-30

## Context
CRM writes trigger side effects (invitation emails, AI indexing). These must not make writes
fail or slow down when the dependency is down, must not be lost, and must not cause retry
storms or unbounded queues. `transaction.on_commit(task.delay)` loses work if Redis is down at
commit time or the process dies in between.

## Decision
- Services write `core_outbox_event` rows in the same transaction as the business change.
- Celery beat triggers a relay every 5 s on its own `outbox` queue (the tick expires after
  10 s). The relay recovers expired leases, then claims due events with
  `FOR UPDATE SKIP LOCKED`, up to a per-queue in-flight cap. Each claim gets a fresh **claim
  token** and a 30 min dispatch lease, and the relay dispatches
  `core.outbox.process(id, token)`; the message expires with the lease.
- Workers act only if the token is still current. They count the attempt first, switch to a
  5 min running lease and run the handler, then mark the event done, back it off
  exponentially with jitter, or mark it dead after `max_attempts`. `PermanentFailure` goes
  straight to dead.
- Coalescing via `dedupe_key` is best effort: it merges only into never-attempted pending
  events, locking them until the caller commits. It is deliberately not a unique constraint.
- The outbox is the **only** retry layer. Handlers are idempotent; payloads carry IDs only.
- Queues `default`, `email` and `ai` isolate workloads.

Revised during the Phase 0 review. The first version's pending-only unique index could jam
lease recovery, and it let duplicate messages run handlers twice. Claim tokens, the two-lease
model, the dedicated relay queue and constraint-free coalescing replaced it.

## Consequences
- CRM writes succeed with Redis, email or AI down, and work resumes automatically. This was
  verified end to end in Phase 0 across real containers.
- Redis holds a bounded number of messages; the backlog is durable in PostgreSQL.
- At-least-once delivery demands idempotent handlers (a documented rule).
- Side effects take a few seconds (acceptable for email and indexing).

## Alternatives considered
- `on_commit` + Celery directly: loses events on broker failure.
- Celery autoretry: layers multiply with SDK retries, and the queue is unbounded.
- A PostgreSQL-only worker (no Celery): viable, but the requirements specify Celery and it
  brings mature worker management, time limits and tooling.
