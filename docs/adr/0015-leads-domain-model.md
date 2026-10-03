# 0015. Leads as the central record; configurable statuses and sources by immutable key

Status: Accepted
Date: 2026-09-30

## Context
Arkray CRM has no Company, Account or Product entity: the lead is the sales-prospect
record. Statuses (New, Contacted, Qualified, Unqualified, Converted) and sources (Website,
Referral, ...) are given, but the business expects to add or rename values later, and
reports must stay consistent (no free-text "cold call" vs "Cold Call"). The application
must not be hard-wired to today's lists, without building a status-management system now.
Names don't follow one cultural pattern, and ownership must be unambiguous.

## Decision
- **One `leads_lead` table** holds person and organisation attributes; `organization_name`
  is a text attribute, never a hidden Company entity.
- **Statuses and sources are configuration rows** (`leads_lead_status`, `leads_lead_source`)
  seeded by a migration, referenced by an **immutable key** through a foreign key
  (`status_key`, `source_key`). The key is what the API and filters speak; the name is a
  display label. Statuses carry a **category** (`open`, `qualified`, `unqualified`,
  `converted`) and business rules use the category, never the name. Retired values stay
  valid where used but can't be chosen again. No management UI yet.
- Rating is a small fixed enum (`hot`, `warm`, `cold`) with a CHECK constraint.
- **One ownership column** (`owner_id`), changed only by an explicit reassignment
  operation; `created_by` records provenance.
- Both name fields are optional; a CHECK requires a person's name or an organisation.
  `display_name` is a PostgreSQL generated column, not an editable duplicate.
- Leads are archived, never deleted.

## Consequences
- New statuses/sources are a data change; the frontend loads options from the API.
- Reports group by key or category and stay correct across renames.
- FK validation replaces CHECK lists, and filters compare keys without joins.
- Keys must never be edited (they are identifiers); a future admin screen edits names only.

## Alternatives considered
- Code enums + CHECK constraints: every new value is a migration and a deploy.
- Free-text statuses/sources: inconsistent reporting.
- UUID-keyed lookup rows: unreadable API payloads and filters, and a join for every filter.
- A Company entity for organisations: explicitly out of scope for this product.
