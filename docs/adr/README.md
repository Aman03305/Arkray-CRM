# Architecture Decision Records

One file per significant decision: context, decision, consequences, alternatives. Records
are immutable once accepted. To change a decision, add a new ADR that supersedes the old one
and update the old one's status line.

| ADR | Decision |
|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith on Django + DRF |
| [0002](0002-postgresql-data-integrity.md) | PostgreSQL as source of truth; data-integrity conventions |
| [0003](0003-session-authentication.md) | HTTP-only session cookies on a single origin |
| [0004](0004-capability-authorization-and-scoping.md) | Capability-based, deny-by-default authorization with owner scoping |
| [0005](0005-admin-workspace-without-impersonation.md) | Admin user workspace as scoped, audited access (no impersonation) |
| [0006](0006-transactional-outbox.md) | Transactional outbox + Celery; one retry layer; bounded in-flight |
| [0007](0007-append-only-history.md) | Append-only audit and history enforced in PostgreSQL |
| [0008](0008-ask-arkray-hybrid-rag.md) | Ask Arkray: deterministic tools + authorized semantic retrieval |
| [0009](0009-unified-activity-model.md) | One activity model for tasks, meetings and notes |
| [0010](0010-frontend-workspace-routing.md) | Next.js with URL-derived workspaces and shared module views |
| [0011](0011-single-tenant.md) | Single-tenant deployment |
| [0012](0012-business-day-time-zone.md) | "Today" is computed in the organisation's time zone |
| [0013](0013-account-lifecycle-and-one-time-tokens.md) | Explicit account lifecycle; stored, hashed one-time account tokens; session epochs |
| [0014](0014-durable-login-throttling.md) | Durable login throttling with per-browser budgets |
| [0015](0015-leads-domain-model.md) | Leads as the central record; configurable statuses and sources by immutable key |
| [0016](0016-composite-keyset-pagination.md) | Composite, NULL-aware keyset pagination with signed cursors |
| [0017](0017-in-transaction-domain-events.md) | In-transaction domain events for cross-module reactions |
| [0018](0018-pipeline-integrity-by-composite-keys.md) | Pipeline integrity by composite foreign keys (status = stage category; open owner = lead owner); one lock order |
| [0019](0019-lead-conversion.md) | Lead conversion creates an opportunity; "Converted" requires one |
| [0020](0020-activity-integrity.md) | Activities: lead-bound, owned by the lead's owner while current, guarded by composite keys |
| [0021](0021-materialized-timeline.md) | The timeline is an append-only table written in-transaction; visibility is decided when read |
| [0022](0022-global-search.md) | Global search: authorised, grouped lexical search with a bounded two-pass window |
| [0023](0023-ask-arkray-implementation.md) | Ask Arkray as built: local embeddings, answers on their own queue, a deterministic fast path |
| [0024](0024-phase-9-security-hardening.md) | Security hardening: sealed and bound page links, prefixed cookies, a nonce CSP, audit outside the cache |
| [0025](0025-production-deployment.md) | Production deployment: restricted database role enforced at start, the edge proxy's contract, erasure on request |
| [0026](0026-user-pipelines-support-sessions-attachments.md) | User-defined pipelines, negotiated prices, support sessions without impersonation, admin-set passwords, note attachments |

Template:

```
# NNNN. Title
Status: Proposed | Accepted | Superseded by NNNN
Date: YYYY-MM-DD

## Context
## Decision
## Consequences
## Alternatives considered
```
