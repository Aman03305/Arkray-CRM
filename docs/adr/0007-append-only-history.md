# 0007. Append-only audit and history enforced in PostgreSQL

Status: Accepted
Date: 2026-09-30

## Context
Audit events, lead timelines and pipeline stage history must not be editable by ordinary
users, and ideally not by application bugs either.

## Decision
- These models extend `core.models.AppendOnlyModel`: `save()` on existing rows, `delete()`,
  and queryset `update`/`delete`/`bulk_update` raise `AppendOnlyViolation`.
- Each table gets a `BEFORE UPDATE OR DELETE` trigger (`core.db.append_only_trigger`) that
  calls `arkray_forbid_mutation()` and raises, even for raw SQL.
- Production grants the app role only SELECT and INSERT on these tables (no TRUNCATE).
- Audit rows reference actors and targets by plain id (no FKs), so no cascade can alter them.
- An architecture test fails if any append-only model lacks its trigger.

## Consequences
- History is tamper-resistant against bugs and against a compromised app role.
- Corrections are new events, never edits.
- Retention, if ever required, is done by dropping partitions under a privileged role.

## Alternatives considered
- ORM-only guards: bypassed by raw SQL, management shells and bulk operations.
- Revoking privileges only: test flushes and development setups become awkward, and one
  misconfigured grant silently loses the protection. The trigger works everywhere.
