# 0017. In-transaction domain events for cross-module reactions

Status: Accepted
Date: 2026-09-30

## Context
Reassigning a lead must, from Phase 3/4, move its open opportunities and current
activities to the new owner in the same transaction (ownership coherence). Ask Arkray
(Phase 8), analytics and automation will also react to lead changes. `leads` sits below
`pipeline`, `activities` and `ai` in the module layering and must not import them, and
Phase 2 must not enqueue work nobody consumes.

## Decision
- `arkray.core.domain_events`: services `publish()` small frozen event dataclasses
  (identifiers and keys only) **inside the transaction of the change**; subscribers
  registered by upper modules (from `AppConfig.ready`) run synchronously in that
  transaction. A failing subscriber aborts the whole operation.
- Subscribers do database work only; anything slow or external is written to the
  transactional outbox **by the subscriber** ([ADR-0006](0006-transactional-outbox.md)).
- The lead services publish `LeadCreated`, `LeadUpdated`, `LeadStatusChanged`,
  `LeadReassigned`, `LeadArchived`, `LeadRestored`. Phase 2 registers no subscribers, so
  nothing is queued.

## Consequences
- Phase 3/4 attach the reassignment rule without touching `leads`, and it is atomic.
- Asynchronous work stays durable and bounded through the outbox, created only when a
  consumer exists.
- Subscribers add latency to the write; they must stay small and indexed.

## Alternatives considered
- Outbox events for everything now: meaningless queued work in Phase 2, and cascades that
  must be atomic would become eventually consistent.
- `leads` calling pipeline/activities directly: breaks the module layering.
- Django signals: untyped payloads and easy to fire outside a transaction.
