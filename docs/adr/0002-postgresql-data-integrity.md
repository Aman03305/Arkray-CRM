# 0002. PostgreSQL as the source of truth; data-integrity conventions

Status: Accepted
Date: 2026-09-30

## Context
Pipeline values, counts and audit history must be correct. Application-only validation is
bypassed by bugs, scripts and concurrent writes. Ask Arkray needs vector search, and a separate
vector database would split the source of truth.

## Decision
- PostgreSQL 16 with pgvector is the single system of record (CRM data, audit, outbox,
  embeddings).
- Invariants are encoded as FK, CHECK, UNIQUE (including partial and deferrable) constraints
  and triggers; services validate first for friendly errors.
- UUID primary keys for business entities; BIGINT for internal append-only logs.
- Money is `NUMERIC(14,2)` / `Decimal`, serialised as strings. Floats are banned by an
  architecture test. A single organisation currency (`CRM_CURRENCY`) in v1.
- `timestamptz` in UTC; `date` for date-only fields.
- No hard deletes of users or CRM records (deactivate or archive); FKs are PROTECT.
- Tests run on real PostgreSQL only.

## Consequences
- Races and bugs cannot persist invalid states; they surface as IntegrityError.
- Schema changes need care (expand → migrate → contract).
- Multi-currency later needs an additive migration and grouped aggregates.

## Alternatives considered
- A separate vector store: another consistency and authorization surface.
- Integer keys everywhere: enumerable IDs in URLs.
- A currency column now: every aggregate would have to group by currency for a
  single-currency business.
