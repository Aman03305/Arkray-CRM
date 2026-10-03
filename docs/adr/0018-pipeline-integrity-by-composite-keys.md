# 0018. Pipeline integrity enforced by composite foreign keys; one lock order

Status: Accepted
Date: 2026-09-30

## Context
Opportunities (Phase 3) sit in configurable stages whose category (open, won, lost)
decides behaviour, and the board and the pipeline totals must filter by that category
cheaply (partial indexes can't reference another table). Visibility is owner-based, and
the ownership-coherence rule says an open opportunity is owned by its lead's owner; lead
reassignment (a `leads` operation) moves them through a domain-event subscriber. Races
between reassignment and opportunity writes must never leave an open opportunity with
the wrong owner, and no service may deadlock another.

## Decision
- `pipeline_opportunity.status` is a copy of the stage's category **bound by a composite
  foreign key** `(stage_id, pipeline_id, status) → pipeline_stage (id, pipeline_id,
  category)`. It can't be edited on its own, the pipeline can't drift from the stage's,
  and a stage's category can't change while opportunities use it.
- A generated column `open_owner_id` (the owner while open, else NULL) and a **deferred
  composite foreign key** `(lead_id, open_owner_id) → leads_lead (id, owner_id)` make
  "an open opportunity is owned by its lead's owner" a database invariant, checked at
  commit (the reassignment updates the lead first and the opportunities second). Closed
  opportunities are exempt (NULL key, MATCH SIMPLE) and keep their historical owner.
  `leads_lead` gains a trivially unique `UNIQUE (id, owner_id)` as the key's target.
- One **lock order** for every operation: lead (`FOR NO KEY UPDATE`, or `FOR UPDATE`
  from the start when the operation changes the lead) → opportunities (`FOR NO KEY
  UPDATE OF` the opportunity alone, by id) → user rows (`FOR SHARE`) → inserts. Shared
  configuration rows (stages, pipelines) are never locked, only key-share-checked by the
  foreign keys (the Phase 3 review found a lock over the opportunity-stage join deadlocking
  different users). Services read an opportunity's (immutable) lead id unlocked, lock
  the lead, then the opportunity.

## Consequences
- The services validate first for good messages; the database is the final arbiter even
  for raw SQL or a future bug (tested with raw UPDATEs).
- Test fixtures must respect the invariants (`OpportunityFactory` derives owner, status,
  probability and closed_at).
- Deferred checks surface at commit: a violation would be a 500, which is intended for a
  state that correct code can't reach.
- Real-thread tests contain interleavings that deadlock if an operation ever locks an
  opportunity before its lead (a deliberate mutation proved they catch it).

## Alternatives considered
- Deriving status from the stage at read time: no partial indexes; every total joins the
  stage table; a stage category change would silently rewrite history.
- A separately editable status column: can disagree with the stage.
- Enforcing ownership in services only: correct today, unprotected against the next bug or
  a manual fix in SQL.
- A trigger instead of a foreign key: more code, same guarantee; the key is declarative
  and uses the lead index PostgreSQL needs anyway.
