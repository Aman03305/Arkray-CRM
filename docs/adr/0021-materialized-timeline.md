# 0021. The timeline is an append-only table written in-transaction; visibility is decided when read

Status: Accepted
Date: 2026-10-03

## Context
Lead and opportunity pages need a history of what happened: lead created, status changes,
reassignments, opportunities created and moved, tasks and meetings created, completed,
cancelled, notes added. Phase 0 sketched a `leads_timeline_event` table in the leads
module. The sources of truth are scattered (the audit log, the stage history, the mutable
activities table) and loading them all into Python to merge and sort is unbounded. The
timeline must not leak: after a reassignment, the new owner must not see the previous
owner's completed meetings or closed opportunities (owner-based visibility), and an
archived note must disappear. Entries must keep their meaning when statuses and stages
are renamed, people change their names or records change hands.

## Decision
- One **append-only** table, `activities_timeline_entry` (ORM guard + trigger), in the
  `activities` module, with `lead_id` (FK), optional `opportunity_id` / `activity_id`
  (FKs; composite keys file each under its own lead), `kind`, `actor_id` and `occurred_at`.
  It lives in `activities` because its entries reference opportunities and activities and
  it is written by subscribers to the lead and pipeline domain events
  ([ADR-0017](0017-in-transaction-domain-events.md)): lower modules never import it.
- Entries are written **in the transaction of the change**: activity services write their
  own; subscribers write lead and opportunity events. One entry per event; no async work.
- `data` is a small, **allowlisted snapshot** per kind: status and stage names as they were,
  owner ids, a meeting's times. Titles, note bodies and contact data are never copied; they
  are read from the live record. People are stable ids rendered by their current name.
- **Visibility is evaluated at read time** in one indexed SQL query: lead events for whoever
  sees the lead; an entry about an activity or opportunity only while that record is
  visible in the same scope and not archived. Keyset pagination on `(occurred_at, id)`.
- Leads and opportunities from Phases 2–3 are **backfilled** once from the audit trail and
  the stage history (`activities.0003`), into an empty table only.

## Consequences
- A timeline page is one index range scan plus one query for the people named in it
  (0.1 ms on a 3,000-entry lead in a 733,000-entry table); constant queries per page.
- Archiving, reassignment and restricted records change what is shown, never what was
  recorded; no stored row ever has to be rewritten.
- Every new event kind is a `TimelineKind` value, its data allowlist and a renderer.
- Lead edits (field changes) stay in the audit log only, deliberately: the timeline records
  meaningful events, not every keystroke.

## Alternatives considered
- A UNION over audit, stage history and activities at read time: no common lead key in the
  audit log, unbounded merges, and mutable activities can't tell a history (complete,
  reopen, complete again).
- The audit log as the timeline: a security trail with workspace-access and admin events
  that must not be user-visible, and no lead key.
- The table in `leads` with plain UUIDs: no referential integrity, and upper modules writing
  a lower module's table.
- Snapshotting titles and bodies: duplicated personal data, and archived or edited notes
  would live on in the timeline.
