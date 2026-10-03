# Pipeline and opportunities

**Built in Phase 3.** Code: [`backend/arkray/pipeline/`](../backend/arkray/pipeline/) and
[`frontend/src/features/pipeline/`](../frontend/src/features/pipeline/). Decisions:
[ADR-0018](adr/0018-pipeline-integrity-by-composite-keys.md) (status and ownership enforced
by composite foreign keys; lock order) and [ADR-0019](adr/0019-lead-conversion.md) (what
"Converted" means).

The canonical domain term is **opportunity** everywhere: models, API, services, events,
audit and UI. ("Deal" appears only in sample titles.)

## Model

```
Pipeline  1 ── * Stage          (configuration, seeded)
Lead      1 ── * Opportunity  * ── 1 Stage (and its Pipeline)
Opportunity 1 ── * StageHistory (append-only)
```

A lead has zero, one or many opportunities. There is still no Company, Account, Contact
or Product: an opportunity is a potential sale **to one lead**.

### Pipelines

`pipeline_pipeline`: `id`, immutable `key`, `name` (unique), `is_default` (at most one,
partial unique index; the default must be active), `is_active`, timestamps. v1 seeds one,
**Sales Pipeline** (`sales`). Nothing assumes there is only one: stages, opportunities,
the board, filters and forms all take a pipeline, and a second pipeline is a data change
(tested).

### Stages

`pipeline_stage`: `pipeline`, immutable `key` (unique per pipeline), `name` (unique
among the pipeline's *active* stages, case-insensitively), `position` (unique per
pipeline, checked at commit so a reorder can swap positions in one transaction),
`probability` (`NUMERIC(5,2)`, 0-100), `category` (`open` | `won` | `lost`), `is_active`.

| Position | Key | Name | Probability | Category |
|---|---|---|---|---|
| 10 | `new` | New | 10 % | open |
| 20 | `qualified` | Qualified | 25 % | open |
| 30 | `proposal` | Proposal | 50 % | open |
| 40 | `negotiation` | Negotiation | 75 % | open |
| 50 | `won` | Won | 100 % | won |
| 60 | `lost` | Lost | 0 % | lost |

- **Won/lost behaviour follows the category, never the name.** CHECKs: a won stage is
  100 %, a lost stage 0 %.
- Column order is `position`, never creation time.
- **Future configuration without rewriting opportunities** (no admin screen yet; the
  schema is ready): renaming a stage, changing its default probability or position, or
  retiring it (`is_active = false`) touches only the stage row. Opportunities keep their
  stage id and the probability they adopted; stage history keeps the names it recorded.
  A retired stage accepts no new opportunities or moves but still shows on the board
  while it holds any. A stage's **category** (and pipeline) can't change while any
  opportunity uses it: the composite foreign key below refuses it.
- Configuration API: `GET /api/v1/config/pipelines` (every signed-in user): pipelines with
  their stages in order, retired ones flagged. The UI hard-codes no stage list.

### Opportunities

| Field | Rules |
|---|---|
| `title` | required, one line, ≤ 200 characters (same text rules as lead names) |
| `lead` | required; a lead **in the caller's workspace**; fixed for the opportunity's lifetime |
| `owner` | never sent by clients: always the lead's owner (see [Ownership](#ownership)) |
| `pipeline`, `stage` | the pipeline defaults to the default one, the stage to its first active open stage; stages change only through the move operation |
| `status` | `open` / `won` / `lost` = the stage's category, database-enforced; read-only |
| `value` | `NUMERIC(14,2)`, 0 to 999,999,999,999.99, in INR |
| `probability` | `NUMERIC(5,2)`, 0-100; see [Probability](#probability) |
| `probability_overridden` | read-only flag: set manually rather than the stage's default |
| `weighted_value` | computed by PostgreSQL on every read (never stored, never computed in the browser) |
| `expected_close_date` | optional **date** (no time, no zone), 2000-01-01 to 2099-12-31 |
| `description` | optional, ≤ 5,000 characters |
| `lost_reason` | optional, ≤ 500 characters, only while lost |
| `closed_at` | set exactly while won or lost (CHECK); cleared on reopen |
| `created_by` | provenance; never changes |
| `archived_at`, `version`, `created_at`, `updated_at` | as for leads |

Generated columns (never written): `open_owner_id` (the owner while open, else NULL: the
key of the ownership foreign key), `expected_close_sort` (`COALESCE(expected_close_date,
'9999-12-31')`) and `closed_sort` (`COALESCE(closed_at, '1900-01-01')`): NOT NULL sort keys,
so keyset cursors bound index scans (the Phase 2 lesson from leads' last-contact sort).

## Ownership

**An open opportunity is always owned by its lead's owner.** Nobody picks an owner: the
create and convert operations take none (an `owner` field is a 400), and the service API
has no parameter for one.

- **Enforced by PostgreSQL**: `FOREIGN KEY (lead_id, open_owner_id) REFERENCES
  leads_lead (id, owner_id) DEFERRABLE INITIALLY DEFERRED`. An open opportunity whose owner
  differs from its lead's owner can't be committed by any code path (raw SQL included),
  and neither can a lead reassignment that leaves one behind. `leads_lead` gained the
  (trivially unique) `UNIQUE (id, owner_id)` this references (`leads.0004`).
- **Lead reassignment** (`LeadReassigned`, same transaction, [ADR-0017](adr/0017-in-transaction-domain-events.md)):
  every **open** opportunity of the lead, archived ones included, moves to the new owner
  (version + 1, one `opportunity.owner_changed` audit event each, actor = the admin who
  reassigned, subject = the previous owner, reason `lead_reassigned`).
  **Won and lost opportunities keep the owner who closed them** (they record who did the
  work). Historical attribution never changes: `created_by`, audit actors and stage-history
  actors stay as they were. If moving them fails, the whole reassignment rolls back.
- Consequence (owner-based visibility): after a reassignment the new owner sees the lead
  and its open opportunities; the previous owner keeps seeing the won/lost opportunities
  they closed, with the lead shown as **restricted** (`{"id": null, "restricted": true}`:
  its name is not sent), because it now lives in someone else's workspace. Administrators
  see everything organisation-wide.
- **Reopening** a closed opportunity makes it open again, so it must belong to the lead's
  *current* owner: if the lead changed hands meanwhile, reopening moves it to them
  (`opportunity.owner_changed`, reason `reopened`), which only someone whose workspace
  includes the lead's owner may do (the previous owner gets a 422 asking an administrator).
- New opportunities (and reopened ones) need an **active, assignable** owner: a lead owned
  by a deactivated user must be reassigned first (422).

## Probability

- Entering a stage sets the opportunity's probability to **the stage's default**.
- **Overrides** (v1: allowed for whoever may edit the opportunity): `PATCH
  {probability: "62.5"}` on an open opportunity sets it and marks
  `probability_overridden`; `{probability: null}` returns to the stage's default. Asking
  for exactly the stage's default is not an override. At creation, `probability` works the
  same way.
- **An override belongs to the stage it was made in**: every stage change (including
  reopening) resets the probability to the new stage's default and clears the flag.
  Predictable, and a stale estimate never survives new information.
- Won is always 100 % and lost 0 % (CHECKs on stages *and* opportunities; an override on a
  closed opportunity is refused, and the flag can't be set while closed).
- Changing a stage's default later applies to opportunities entering it afterwards;
  existing ones keep what they adopted (no mass rewrite).

## Money

- INR (`CRM_CURRENCY`), `NUMERIC(14,2)` / Python `Decimal` / JSON decimal **strings**
  (`"1250000.00"`); boards and summaries echo the currency.
- **Input**: a JSON string of plain digits with at most two decimals (`"1250000"`,
  `"1250000.50"`), or a JSON integer. Refused with a 400: JSON numbers with a fraction
  (the JSON parser would make them binary floats), exponents (`1e6`), signs, separators,
  `NaN`/`Infinity`, non-ASCII digits (`Decimal()` would read Devanagari or Arabic-Indic
  digits), more than 12 whole digits or 2 decimals. The services re-check (Decimal or int
  only, finite, ≥ 0, ≤ the maximum, ≤ 2 places), so no caller can pass a float.
- **Weighted value** = value × probability / 100, computed as `value * probability * 0.01`:
  a product of exact decimals (PostgreSQL chooses the scale of a *division* and would
  round there, which could round twice), rounded once to paise, half away from zero.
- **Pipeline value** = `SUM(value)` and **weighted pipeline** = `SUM(value × probability /
  100)` over **open, non-archived** opportunities in the caller's scope; won, lost and
  archived never count. The weighted total is the exact sum rounded once, so it can differ
  from the sum of the rounded per-opportunity figures by at most half a paisa per
  opportunity (tested: three ₹0.03 at 50 % are ₹0.02 each but ₹0.05 in total).
- These are defined once, in [`pipeline/metrics.py`](../backend/arkray/pipeline/metrics.py)
  (`open_pipeline_totals` applies the OPEN filter itself so no caller can forget it), and
  used through `selectors.pipeline_totals(scope, filters)`, which applies the scope first.
  **Phase 5's dashboard uses this; it must not re-derive the formula.**
- Worked example (tested end to end): A ₹10,00,000 at 50 % open, B ₹5,00,000 at 80 % open,
  C ₹2,00,000 won → pipeline value ₹15,00,000.00, weighted pipeline ₹9,00,000.00.
- The browser never does arithmetic on amounts: [`lib/money.ts`](../frontend/src/lib/money.ts)
  formats and parses decimal **strings** only (Indian grouping `₹12,50,000`, paise shown
  when non-zero, no `Number()`/`parseFloat`, tested), and an optimistic move adjusts only
  card positions and counts, never money totals (they reload from the server).

## Stage transitions

**One operation**, `POST …/opportunities/{id}/move {stage, version, lost_reason?}`
(`services.move_opportunity`), for every path: board drag and drop, the card's **Move**
menu, the detail page's Move to stage / Mark as won / Mark as lost / Reopen. Generic
editing can't change the stage (a `stage` in a PATCH is a 400).

| From → to | Effect | Audit action | Events |
|---|---|---|---|
| open → open | probability = new stage default, override cleared | `opportunity.stage_changed` | `OpportunityStageChanged` |
| open → won | closed_at = now, 100 % | `opportunity.won` | `OpportunityStageChanged`, `OpportunityWon` |
| open → lost | closed_at = now, 0 %, optional `lost_reason` | `opportunity.lost` | `OpportunityStageChanged`, `OpportunityLost` |
| won/lost → open | **reopen**: closed_at and lost_reason cleared, stage default restored; owner = lead's owner | `opportunity.reopened` (+ `owner_changed` if the owner changed) | `OpportunityStageChanged` (+ `OpportunityOwnerChanged`) |
| won/lost → won/lost | refused (422): reopen first, so every close is a deliberate step | — | — |
| to the current stage | no-op success (no version, history or audit): retries are harmless; a lost reason sent with it is a 400 (never silently dropped) | — | — |

Checks (in order, under the locks): scope (404), `authorize_write` (403), version (409),
not archived (422), target stage active and of the same pipeline (400), lost reason only
for a lost target (400). Moving backwards between open stages is allowed.

The UI asks for confirmation before closing (won/lost, with an optional reason for lost)
and before reopening; open → open moves happen at once.

## Stage history

`pipeline_stage_history` (append-only: `AppendOnlyModel` + a PostgreSQL trigger refusing
UPDATE and DELETE): one row per transition **and** one for the creation (`from_stage` NULL).
Each row keeps `from_stage`/`to_stage` ids **and** copies of their names and categories
(readable after a stage is renamed or retired), the value and probability entering the
stage, the lost reason, the actor and the time. A CHECK keeps rows complete (a creation
has no "from"; any other row has a whole one). It is written in the transaction that
moves the stage, under the opportunity's lock, so `from_stage` is always the true previous
stage (tested under concurrency: every history is an unbroken chain ending in the current
stage). `GET …/opportunities/{id}/history` pages it, newest first (50 per page).

## Archive

As for leads: no DELETE anywhere (FKs `PROTECT`); `…/archive` and `…/restore {version}`
(audited, idempotent). Archived opportunities leave boards, lists (except the `archived`
view) and **all totals**; the detail page still opens them, read-only (422 on edit or
move). An **archived lead** gets no new pipeline: creating, converting, reopening a closed
opportunity and restoring an archived one are refused (422, "restore the lead first");
its open opportunities can still be moved and closed. **Closed is not archived**: a won deal stays on the Won column and in history.
Archived *open* opportunities still follow their lead on reassignment (the ownership key
covers them).

## Conversion

"Converted" now means **the lead has entered the opportunity process: it has at least one
opportunity** ([ADR-0019](adr/0019-lead-conversion.md)).

- **Convert** (`POST /api/v1/workspaces/{ws}/leads/{id}/convert {version, title, value,
  pipeline?, stage?, probability?, expected_close_date?, description?}`, with an
  `Idempotency-Key`): creates the opportunity **and** moves the lead to the first active
  status of category *converted*, in one transaction. It returns `{lead, opportunity}`
  (201). Nothing else is created: no Company, Account or Contact.
- All or nothing (tested by failing it after the opportunity insert, and by a failing
  subscriber): no opportunity, no status change, no audit, no consumed idempotency key.
- **No duplicates**: the lead is locked `FOR UPDATE` first; a second conversion finds it
  already converted (422) or its version changed (409). Three simultaneous conversions
  create exactly one opportunity (tested); with one `Idempotency-Key`, retries replay the
  first conversion.
- Refused: archived lead (422), already converted (422), stale lead version (409), a
  deactivated owner (422), someone else's lead (404). A lead left *Converted* without an
  opportunity of its owner's (Phase 2 allowed that) can be converted properly now.
- **The plain status change** (`…/leads/{id}/status`) into a *converted* status, and
  creating a lead directly as Converted, are refused (422, "Use Convert…") unless the lead
  already has an opportunity **of its current owner's** (archived and closed ones count:
  conversion is history; one another user closed before the lead was reassigned doesn't,
  because its owner can't see it and the answer must not reveal it: review). The
  pipeline module vetoes them from a `LeadStatusChanged`/`LeadCreated` subscriber, so
  `leads` still never imports `pipeline`. Moving a lead *out of* Converted stays allowed;
  converting it again creates another opportunity.
- Audit: `opportunity.created` (via `conversion`), `lead.status_changed`, and
  `lead.converted {workspace, opportunity_id, from_status, to_status}`.

## Authorization

The same model as leads ([authorization.md](authorization.md)): workspace-nested routes,
`resolve_workspace` → `AccessScope`, 404 outside the scope, `authorize_write` for every
write (own workspace: `crm.access_own`; a user's workspace or `all`: `crm.manage_any`).
Creating an opportunity in someone's workspace needs no `crm.assign_any`: its owner is not
chosen, it is the lead's owner. Every read, **every total, count and per-stage value** is
computed from `scope.apply()` first; a sales user's `lead`, `stage` or `pipeline` filter
can only narrow their own records, and `owner` exists only organisation-wide.

| Workspace | Opportunities, totals and history visible |
|---|---|
| `me` | the caller's own (opportunities they own) |
| `{user_id}` (admin, `workspace.view_any`) | that user's, and that user's totals only |
| `all` (admin, `crm.view_all`) | everyone's; `owner` filter narrows |

Cross-user suite (User A vs User B both ways): [`tests/security/test_pipeline_cross_user.py`](../backend/tests/security/test_pipeline_cross_user.py).

## Lock order

Every operation takes its locks in this order, so no two can wait for each other in a
cycle (the concurrency tests contain interleavings that deadlock if any operation ever
locked an opportunity before its lead; a deliberate mutation of `_lock` produced
`deadlock detected` in them):

1. **the lead**: `FOR NO KEY UPDATE` (create, move, edit, archive, restore: the lead's
   owner then can't change until commit), or `FOR UPDATE` from the start when the
   operation changes the lead itself (conversion; reassignment in `leads`), never upgraded;
2. **opportunities**: `FOR NO KEY UPDATE OF` the opportunity only, several only in
   ascending id order (the reassignment subscriber). **Stage rows are never locked** by
   opportunity writes, only `KEY SHARE`-checked by the foreign keys: the review found that
   a lock over the opportunity-stage join also locked the shared stage row, so two users
   moving cards in opposite directions deadlocked (P1, fixed, pinned by real-thread tests);
3. **user rows**: `FOR SHARE` (`lock_assignable_user`), leaf locks;
4. inserts: stage history, audit, idempotency records.

Phase 4 extends the order with **activities** after opportunities and before user rows
(lead → opportunities → activities → user rows → inserts); activity writes lock the lead
and never the opportunity ([activities.md](activities.md#lock-order)).

The opportunity's `lead_id` is read first (unlocked: it never changes), then the lead is
locked, then the opportunity is locked through the scope and re-checked.

## Concurrency

Every write requires the version the client saw (409 otherwise) and bumps it; reassignment
bumps the versions of the opportunities it moves.

| Race (real threads, own connections) | Outcome |
|---|---|
| move × move, edit × edit, move × edit, archive × move | one wins, the other 409; no lost update, no mixed state |
| reassignment × move / close | serialised on the lead: either the move lands first (then an open result follows the lead, a closed one stays with its closer) or the reassignment does (then the move is 404 in the old workspace) |
| reassignment × create | the new opportunity is created and moved, or the create is 404 |
| conversion × conversion (×3) | one opportunity, one `lead.converted` |
| conversion × reassignment | serialised; the opportunity ends with the new owner |
| a storm of 12 mixed writers × 4 rounds on one lead | no deadlock; invariants hold after every round |
| different users moving cards in opposite directions (New ↔ Qualified, Won ↔ Negotiation), 3 × 8 at once and with both holding their locks | all succeed (before the review fix: 59 of 80 deadlocked) |
| the same conversion submitted twice with one key while the first is in flight | one conversion; the duplicate replays it |

## The board

`GET /api/v1/workspaces/{ws}/pipeline-board?pipeline=&expected_close_from=&expected_close_to=&owner=&lead=&probability_min=&probability_max=&cards_per_stage=`

- Returns the pipeline, the currency, the **open-pipeline totals**, and one column per
  stage in position order (retired stages only while they hold opportunities): `count`,
  `total_value`, `weighted_value`, `ordering`, at most `cards_per_stage` cards (default 20,
  0-50) and `next`: the opportunities list continuing after those cards (same stage,
  filters and order).
- **Bounded whatever the data**: 3 queries on opportunities (per-stage aggregates, totals,
  and the cards as **one** `UNION ALL` of an index-backed `LIMIT` per stage), never
  "all rows", run in one `REPEATABLE READ, READ ONLY` transaction so counts, totals and
  cards describe the same moment (review). Filters apply to cards, counts and totals alike.
  A column with more opportunities than cards shown has a `next` link (with
  `cards_per_stage=0`, to the stage's first page).
- **Card order** (deterministic, no manual ranking in v1): open stages by expected close
  date, soonest first, undated last, then oldest first, then id; won and lost stages by
  closing time, latest first, then id.
- `GET …/pipeline-summary` returns just the totals (the figures Phase 5 shows).
- `GET …/opportunities` lists with the same filters plus `stage`, `status`, `archived`,
  `ordering` (`-created_at` default, `created_at`, `-value`, `value`, `expected_close`,
  `-updated_at`, `-closed_at`) and keyset pagination (1-100 per page). Unknown parameters
  are a 400. **Amounts never travel in cursors**: the value sorts use a private sort key
  (like the Leads name sort), so a page link holds the boundary row's id and the amount is
  re-read from it (review: deal values were readable in URLs).

## Frontend

| Route | Workspace |
|---|---|
| `/pipeline`, `/pipeline/new[?lead=]`, `/pipeline/{id}`, `/pipeline/{id}/edit` | own (sales users); organisation-wide (admins) |
| `/admin/users/{id}/pipeline`, `…/new`, `…/{opportunityId}`, `…/edit` | that user's, under the "Viewing CRM for" banner |

- **Wide screens (≥ 1024 px)**: the Kanban board (horizontal controlled scrolling), totals
  (pipeline value, weighted pipeline, open count), filters (expected close range; owner
  organisation-wide; pipeline when there is more than one). Crowded columns end in
  "View all N", which opens that stage's paginated list.
- **Phones and narrow tablets**: stage tabs (with counts; arrow keys move between them)
  and one stage's paginated list; the board is requested with `cards_per_stage=0`.
- **Moving**: drag and drop on the board, or the card's **Move** menu (keyboard and
  touch), both calling the one move API. Open → open moves are optimistic: the card moves
  at once (counts adjust; money totals don't), and if the server refuses (409, 404, 422,
  500, network) the board is put back exactly as it was, the reason is shown, and the
  board reloads. The rollback goes to the board the move was made on, even if the filters
  changed meanwhile, and the server's answer (the new version above all) is written into
  every cached board and stage list at once, so a second move never sends a stale version
  (review). A move started while another is saving is explained, not dropped. Won, lost
  and reopen ask first; retrying in that dialog after a 409 uses the reloaded version.
  After a move, focus stays on the moved card's Move button in its new column; if the
  card leaves the list shown (a stage list), focus goes to the message saying where it
  went. When a dialog's opener disappears (Mark as won → Reopen), focus goes to the page
  heading.
- Opportunity page: summary (value, probability with "set manually"/"stage default",
  weighted value, expected close with "(overdue)" in words), lead (link, or restricted),
  description, stage history, record details; Move to stage (radio list), Mark as won /
  lost, Reopen, Edit, Archive/Restore.
- Create/edit form: lead picker (searches the workspace's own leads only), title, value
  (typed as `12,50,000`, `1,250,000` or `1250000.50`, sent as an exact string; commas that
  don't group digits properly, such as `12,50,00`, are refused rather than guessed),
  stage, optional manual probability, expected close date, description, lost reason when
  lost. Idempotency key per identical body; edit conflicts (409) offer "Apply my changes
  to the latest version" (keeping someone else's manual probability, review) or
  "Discard"; an opportunity archived meanwhile keeps the form and the typing on screen.
- Filters: an inverted expected-close range is explained next to the dates and never sent;
  the board keeps the last valid range. Stage lists start from their first page whenever
  the filters change.
- Lead page: an **Opportunities** section (this workspace's opportunities of the lead,
  10 per page, "+ Opportunity") and **Convert** (a dialog; lands on the new opportunity).
- Since Phase 4 the opportunity page also shows its **open work** (+ Task, + Meeting) and its
  **timeline** (creation, stage changes, won/lost/reopened and its activities) with a note
  box; opportunity writes mark timelines and activity views stale.
- **No stale data across workspaces**: views are keyed by workspace, every query key
  starts with `["pipeline", kind, <workspace>]`, and "keep the previous data while loading"
  applies only within the same workspace (and, for stage lists, the same stage). Tested:
  Rahul's board → Priya's never shows a Rahul card or total, even while Priya's loads;
  Back to Rahul likewise. Lead writes (reassignment) mark pipeline data stale too.
- Status and outcome are always written out (Open / Won / Lost with icons); colour only
  reinforces them.

## Performance

Query counts per request are pinned (identical at 10 and 100 opportunities,
[`test_query_counts.py`](../backend/arkray/pipeline/tests/test_query_counts.py)):

| Endpoint | Queries |
|---|---|
| board (own / organisation) | 7: session, user, pipeline, stages, stage aggregates, totals, cards (one UNION ALL); +1 `SET TRANSACTION` outside tests (the snapshot) |
| board (a user's workspace) | 8 (+ workspace check) |
| list, any filter or sort, following a cursor | 3 (following a value-sorted cursor: 4, the boundary's amount is re-read by id) |
| list filtered by stage | 4 (+ the stage's category, so the board's partial indexes apply) |
| detail | 3 |
| summary | 3 |
| history | 4 |
| pipeline configuration | 4 |
| move (a write) | 14 (12 before Phase 4; + the timeline's stage-name snapshot and entry), independent of how many opportunities the lead has |
| lead reassignment moving 1 or 25 open opportunities | 12 either way (10 before Phase 4; + the activities' lock query and the timeline entry; review: it was 9 + N) |

Benchmark: 300,000 opportunities (100,000 leads, 60 owners, one with 20,100; 60 % open,
25 % won, 15 % lost, 3 % archived, 20 % undated), EXPLAIN ANALYZE of the exact SQL the
selectors run ([`tests/performance/bench_pipeline.py`](../backend/tests/performance/bench_pipeline.py));
best of three, PostgreSQL 16 in Docker on a laptop:

| Query | Heaviest owner (20,100) | Typical owner (4,743) | Organisation |
|---|---|---|---|
| board: all columns' cards (one UNION ALL) | 0.40 ms | 0.44 ms | 2.4 ms |
| board: per-stage counts and values | 8.1 ms | 4.4 ms | 46 ms (seq scan) |
| board/summary: open totals | 4.1–4.6 ms | 2.1–2.3 ms | 37–41 ms (seq scan) |
| stage list page (the board's `next`), any stage | 0.09–0.11 ms | 0.11–0.14 ms | 0.10–0.19 ms |
| default list (newest / oldest), archived view | 0.11–9 ms | 0.09–0.4 ms | 0.14–0.2 ms |
| list sorted by value / expected close / updated / closed | 6.5–8.2 ms | 7.4–8.1 ms | **114–133 ms** (seq scan + top-N sort) |
| open, closing in a month | 3.5 ms | 1.8 ms | 16 ms |
| a lead's opportunities; reassignment's lock query; conversion guard; ownership FK check | — | — | 0.01–0.05 ms |

Before the benchmark the board's card query took 27–30 ms per owner: the per-stage query
filtered by stage only, so PostgreSQL couldn't use the board's partial indexes (partial on
status). Stating the stage's category (always equal, database-enforced) fixed it (0.4 ms);
the same applies to stage-filtered lists. Accepted: organisation-wide non-default list
sorts and organisation-wide aggregates (admin-only, bounded, under the 10 s statement
timeout; risk R40).
