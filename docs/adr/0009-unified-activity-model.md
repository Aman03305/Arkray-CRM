# 0009. One activity model for tasks, meetings and notes

Status: Accepted (refined by [0020](0020-activity-integrity.md) and [0021](0021-materialized-timeline.md))
Date: 2026-09-30

## Context
Activities start with Task and Meeting; Call, Email, WhatsApp and Note are expected later.
Timelines, dashboards and Ask Arkray query across activity types. Notes are needed in v1 for
the lead timeline and for Ask Arkray summaries.

## Decision
- A single `activities_activity` table with a `type` discriminator (`task`, `meeting` and
  `note` in v1), shared columns (title, description, owner, lead, opportunity, status,
  timestamps) and nullable type-specific columns (priority, due_date, starts_at/ends_at,
  location, meeting_url).
- Per-type rules are CHECK constraints (allowed statuses, required fields, time ordering) plus
  a per-type spec in code (validation, timeline event names, allowed transitions).
- Notes are activities of type `note`, replacing a free-text `notes` column on leads.
- The API exposes one `activities` resource with a type discriminator.

## Consequences
- Timelines, "my activities" and dashboard counts are single indexed queries with no UNIONs.
- Adding a type means an enum value, columns, CHECKs and a spec, in one migration.
- Some columns are NULL for some types (bounded and CHECK-guarded).

## Alternatives considered
- A table per type: UNIONs everywhere, plus duplicated ownership and authorization code.
- Multi-table inheritance: joins on every read.
- JSONB for type-specific fields: loses DB-level validation of core fields.
