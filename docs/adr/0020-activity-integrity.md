# 0020. Activities: lead-bound, owned by the lead's owner while current, guarded by composite keys

Status: Accepted
Date: 2026-10-03

## Context
[ADR-0009](0009-unified-activity-model.md) chose one `activities_activity` table for tasks,
meetings and notes with type-specific CHECKs. Building it (Phase 4) settled what Phase 0 left
open: whether an activity may exist without a lead, who owns it, what happens to it when its
lead is reassigned, and how "an activity's lead is its opportunity's lead" and "current work
is the lead owner's" are enforced. Visibility is owner-based ([ADR-0004](0004-capability-authorization-and-scoping.md)),
so ownership decides who sees an activity. Historical attribution (who did the work, who
wrote a note) must never be rewritten.

## Decision
- **Every activity belongs to a lead**, directly or through one of its opportunities;
  `lead_id` is NOT NULL and both links are **fixed for the activity's lifetime** (its
  history is filed under that lead). Personal to-dos without a customer are out of scope.
- **Nobody chooses an owner.** An activity is created for, and owned by, its lead's
  (active) owner; `created_by` is the actor (an admin's note in Rahul's workspace is Rahul's,
  written by the admin). The API has no `owner` field; a single activity can't be handed to
  someone else.
- **Current work follows the lead; history stays.** Open tasks, scheduled meetings and
  notes (the lead's context) are "current": `current_owner_id` (generated: the owner while
  current, else NULL) plus a **deferred composite foreign key** `(lead_id, current_owner_id)
  → leads_lead (id, owner_id)` make "current work is owned by the lead's owner" a database
  invariant (ADR-0018's mechanism). Completed and cancelled tasks and meetings are exempt
  and keep the owner who had them. Reassignment moves current work in the same transaction
  through the `LeadReassigned` subscriber (after the pipeline's: lock order lead →
  opportunities → activities). Reopening closed work makes it the lead's current owner's.
- **Relationship integrity in PostgreSQL**: `(opportunity_id, lead_id) → pipeline_opportunity
  (id, lead_id)` (a new trivially unique key on opportunities), so an activity can never
  name one lead and another lead's opportunity.
- **Type rules are NULL-safe CHECKs**: every comparison of a nullable column is paired with
  `IS NOT NULL` (a CHECK passes on NULL; notes have a NULL status), each proven by raw SQL.
- **Lifecycle is explicit**: complete, cancel, reopen, archive, restore are operations
  (never PATCH); repeating one that already happened is a no-op success; closed work is
  read-only until reopened; a note's text can be edited only by its author.

## Consequences
- Lists, timelines and counts filter one indexed owner column; a user never sees current
  work on a lead they can't open.
- A deactivated owner's lead takes no new activities until reassigned (422).
- After a reassignment the new owner sees the lead's notes and open work, not the previous
  owner's completed meetings (they stay with the person who held them; admins see all).
- Correct code can't reach a violation; one surfacing at commit would be a 500 (intended).

## Alternatives considered
- Optional lead (standalone activities): needs owner choice (and `crm.assign_any`) in every
  workspace, and invariants keyed on a nullable lead; deferred until a need exists.
- Notes keeping their author as owner: the new owner of a lead would lose its notes.
- Per-activity reassignment: breaks the coherence rule visibility depends on.
- Enforcing ownership in services only: unprotected against raw SQL and future bugs.
