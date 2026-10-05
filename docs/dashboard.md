# Dashboard and Admin Home (Phase 5)

The dashboard answers "where do I stand today?" with six figures and three short lists. It
is a **read-only view over the authoritative domains**, not an analytics subsystem: it has
no tables, no cache and no formulas of its own. Every figure is the owning module's
selector, called with the request's `AccessScope`.

> **Leads ([ADR-0028](adr/0028-opportunity-creates-its-lead.md)).** Every opportunity created
> in the pipeline creates its lead in the same transaction, so one new opportunity is one more
> lead in "Total leads" and, on the day it is created (Asia/Kolkata), in "New leads today":
> both are counted from the Lead table alone, never from opportunities. The "New leads" list
> comes **before Upcoming meetings**; each row opens the lead's (read-only) page, which links
> to its opportunity. ([ADR-0027](adr/0027-leads-removed-from-the-ui.md) had hidden all three;
> there is still no Leads list page, so the two lead figures are plain tiles.)

Code: [`backend/arkray/dashboard/`](../backend/arkray/dashboard/) (module above
`activities` in the [layering](architecture.md#dependency-rules)),
[`frontend/src/features/dashboard/`](../frontend/src/features/dashboard/).

## What it shows

| Area | Figure | Definition | Authority | Opens |
|---|---|---|---|---|
| Total leads | count | leads in the workspace that are **not archived** (any status); a lead whose only opportunity was archived still counts | `leads.selectors.lead_summary` | (a plain tile: no Leads list) |
| New leads today | count | of those, the ones **created during today's business day** | `leads.selectors.lead_summary` | (a plain tile; its leads are the New leads list) |
| Pipeline value | ₹ | `SUM(value)` of **open**, non-archived opportunities, **in every pipeline** | `pipeline.selectors.pipeline_totals` → `pipeline.metrics` (Phase 3) | Pipeline board, filters cleared |
| Weighted pipeline | ₹ | `SUM(value × probability / 100)` of the same, exact, rounded once to paise | same | same |
| Meetings | today / upcoming | scheduled or completed meetings starting today; scheduled meetings from now on | `activities.selectors.activity_summary` (Phase 4) | Activities: today's meetings (the Activities page's own shortcut) |
| Tasks | open / due today / overdue | open tasks; those due today (earlier today included); those due before now | `activities.selectors.activity_summary` (Phase 4) | Activities: Tasks tab (open, soonest due first) |

Won, lost and archived opportunities never count towards the pipeline; completed and
cancelled tasks are never open; cancelled and archived meetings never count; archived
activities never count. These are the Phase 3 and Phase 4 definitions, unchanged
([pipeline.md](pipeline.md), [activities.md](activities.md#dashboard-figures-for-phase-5)).

A lead counts for its **current owner**: a lead created today and reassigned is the new
owner's new lead (owner-based visibility, like every other read). Records a previous owner
keeps after a reassignment (closed opportunities, completed meetings) count for whoever
holds them, as everywhere else ([R41](risk-register.md), [R45](risk-register.md)).

The pipeline figures cover **all** pipelines the workspace may see (Phase 3's summary
without a pipeline filter): the shared ones, the owner's own and any pipeline holding one of
its deals ([pipeline.md](pipeline.md#configuration)); the dashboard says "all pipelines".
The board shows one pipeline at a time, so its totals are that pipeline's share (tested).
The weighted pipeline uses each deal's probability, which is its stage's (whatever the
pipeline's stages are called and however their probabilities were set) unless set by hand,
and the **installation price** (`value`): a negotiated price never changes the totals (R96).

### Supporting lists (at most 5 rows each)

| List | Rows | Order | Authority |
|---|---|---|---|
| New leads | the leads "New leads today" counts, each with its opportunity (its first non-archived one in the workspace: id, name, instrument; or none) | newest first (ties: newest id first) | `leads.selectors.new_leads_today`, `pipeline.selectors.first_opportunities` |
| Upcoming meetings | scheduled meetings from now on | soonest first | `activities.selectors.upcoming_meetings` (Phase 4) |
| Tasks requiring attention | open tasks | due time, so overdue first, then today's, then later, undated last (ties by id): the first rows of the Activities page's Tasks tab | `activities.selectors.next_open_tasks` |

Each row links to the record's page **in the same workspace** (a new lead to its lead page);
the meetings' and tasks' footers open the full list ("View upcoming meetings", "View open
tasks"), the new leads' footer the Pipeline ("Newest 5 of 7 · View pipeline").
Rows carry only what the dashboard shows (security review): a lead's name, organisation,
created time, assigned user and its opportunity's id, name and instrument; a meeting's or task's title, type, status, start or due
time, "overdue", its lead and its owner. People are `{id, full_name, is_active}`, never an
email; no description, agenda, author or version is sent, and no lead contact data. A new
lead's row shows its name, its instrument, (organisation-wide) its owner and "Today 2:15 pm".
Organisation-wide the UI writes the owner on every row; in one person's workspace the rows
don't repeat their name.

## Workspaces

The dashboard follows the existing workspace rules exactly; nothing is dashboard-specific
([authorization.md](authorization.md#admin--user-workspace-built-resolution-and-banner-endpoint-in-phase-0-module-views-in-phases-25-completed-in-phase-6)):

| URL | Viewer | API workspace | Shows |
|---|---|---|---|
| `/dashboard` | sales user | `me` | their own records |
| `/dashboard` (and `/admin`, which redirects there) | administrator | `all` | **Admin Home**: the organisation's figures and lists |
| `/admin/users/{id}/dashboard` | administrator | `{id}` | that user's records only, under the "Viewing CRM for: Rahul Sharma" banner |

- A sales user asking for `all` or another user's id gets the same 404 as for a user that
  doesn't exist (enumeration-safe; 15 odd spellings tried by the security review).
- An administrator's own records are at `/api/v1/workspaces/me/dashboard`; the UI's
  top-level dashboard for administrators is the organisation (ADR-0010).
- **No impersonation:** viewing Rahul's dashboard is the administrator reading Rahul's
  scope. Workspace resolution audits the delegated access (`workspace.accessed`, at most
  once per actor and workspace per 15 minutes); refreshing the dashboard writes nothing more,
  and a salesperson viewing their own dashboard writes nothing at all.
- There is no second admin analytics application: Admin Home *is* the organisation
  workspace's dashboard (the planned `/api/v1/admin/overview` was not needed).

## Time zone

"Today" is the business day in `CRM_TIME_ZONE` (Asia/Kolkata), computed once per request by
`core.business_time` ([ADR-0012](adr/0012-business-day-time-zone.md)), the same helper the
Leads date filters and the Phase 4 activity summary use. The day is half-open,
`[00:00, next 00:00)` IST: 00:15 IST on 3 October belongs to 3 October although UTC still
says 2 October, a lead created at 23:59:59.999999 IST on 2 October is yesterday's, and one
stamped at the next midnight or later (a clock ahead, an import) is not today's. The view
reads the clock **once**: every figure, list and row's "overdue" flag use the same instant,
and the response states the `business_date` and `time_zone` it used. The "New leads today"
and "Meetings" cards open their lists filtered by that server-side business date, not the
browser's. A dashboard left open refreshes itself once when the business day changes.

## Money

Amounts are PostgreSQL `NUMERIC`, summed exactly and rounded once (Phase 3's
`pipeline.metrics`), serialised as decimal strings with two places (`"1500000.00"`), and
formatted in the browser as text (`lib/money.ts`: `₹15,00,000`, paise when present, Indian
grouping). No amount ever passes through a JavaScript number: tested with
`9007199254740993.01` (2^53 + 1 rupees), which a `Number` would turn into …992. A long
amount wraps only after a group's comma, never inside a group. The brief's example: open
₹10,00,000 at 50 % and ₹5,00,000 at 80 %, won ₹2,00,000 → pipeline value ₹15,00,000.00,
weighted ₹9,00,000.00 (won excluded).

## API

`GET /api/v1/workspaces/{workspace}/dashboard` (authorization matrix rule `workspace`; any
active user; read-only: other methods 405; any query parameter 400).

```json
{
  "currency": "INR",
  "time_zone": "Asia/Kolkata",
  "business_date": "2026-10-03",
  "leads": {"total": 1234, "new_today": 7},
  "pipeline": {"pipeline_value": "1500000.00", "weighted_pipeline": "900000.00", "open_count": 2},
  "activities": {"open_tasks": 9, "tasks_due_today": 2, "overdue_tasks": 1,
                 "meetings_today": 3, "upcoming_meetings": 6},
  "new_leads": [{"id": "…", "display_name": "Asha Mehta", "organization_name": "Apollo Diagnostics",
                 "owner": {"id": "…", "full_name": "Rahul Sharma", "is_active": true},
                 "created_at": "2026-10-03T04:42:00Z",
                 "opportunity": {"id": "…", "title": "Asha Mehta — Adams 8380 V-lite",
                                 "instrument_name": "Adams 8380 V-lite"}}],
  "upcoming_meetings": [{"id": "…", "type": "meeting", "title": "Product demo", "status": "scheduled",
                         "due_at": null, "starts_at": "2026-10-06T05:30:00Z", "is_overdue": false,
                         "lead": {"id": "…", "display_name": "Asha Mehta", "organization_name": "Apollo Diagnostics",
                                  "restricted": false},
                         "owner": {"id": "…", "full_name": "Rahul Sharma", "is_active": true}}],
  "next_tasks": ["…the same row shape…"]
}
```

`pipeline` and `activities` are rendered by the pipeline's and the activities' own
serializers, so the OpenAPI contract has one `PipelineTotals` and one `ActivitySummary`
schema and the frontend one TypeScript type for each (generated; the drift gate covers it).
Errors: 401 signed out, 404 for a workspace the caller may not open, the standard error
envelope otherwise. Responses are `Cache-Control: no-store`.

## Consistency and freshness

- **One snapshot.** The seven queries run in one `REPEATABLE READ, READ ONLY` transaction (as
  the Phase 3 board does), so "3 new leads today" can never list four rows and every figure
  describes the same moment. Afterwards the connection is back to `read committed`
  (verified by the domain review, also after an error inside the snapshot).
- **No cache anywhere.** The server reads the authoritative tables on every request; there
  is no analytics store, snapshot table or Redis cache. The browser never serves the
  dashboard from its cache either: the query uses `staleTime: 0`, `gcTime: 0` and
  `refetchOnMount: "always"`, so each visit (Back included) reads the figures afresh and
  nothing outlives the page. Returning to the browser tab refreshes them (marked
  "Updating…" meanwhile). A lead created, an opportunity moved to Won, a task completed: the
  next dashboard load shows it (tested through the real write APIs and in the live
  walkthrough).

## Failure behaviour

| Situation | Behaviour |
|---|---|
| Redis (cache) down | the dashboard works; delegated viewing is then audited on every request (the audit window lives in the cache; fail towards more auditing). Tested, under 10 s total with the fail-fast cache. |
| Broker / Celery down | the dashboard works; nothing is queued by reading. Tested. |
| AI provider down | not involved. |
| A query fails or times out (10 s statement timeout) | 500 with the standard envelope; the UI shows "The dashboard couldn't be loaded" with the request reference and Try again, never the server's text |
| 403 / 404 | "You can't view this dashboard" (no retry) / the standard not-found page alone |
| Network failure, or offline | "Could not reach the server. Check your connection." (the request is made even when the browser reports offline, so the skeleton never waits forever) |
| A refresh fails | the error **replaces** the figures: nothing stays on screen that the server no longer gives (while the refresh and its retries run, the figures are marked "Updating…") |

## Performance

### Queries per request (pinned exactly, identical at 10 and 100 records per module and at 2 and 10 users)

| Dashboard | In tests | In production |
|---|---|---|
| own (`me`) | 10: session, user, the eight | 11 (+ `SET TRANSACTION`), between BEGIN and COMMIT |
| a selected user's | 12 (+ the workspace's user exists-check, the audit window read) | 13 |
| organisation (`all`) | 11 (+ the audit window read) | 12 |

The eight: lead figures, pipeline totals, the activity figures (two aggregates: open tasks;
meetings from today on), today's newest leads, the next meetings, the next open tasks, and
(ADR-0028) the new leads' opportunities: one `DISTINCT ON (lead_id)` query for the at most
five new leads, through `pipeline_opp_lead_idx` (measured on `arkray_bench_enh`, 300,000
opportunities: 0.26 ms warm and 3-5 ms cold for the heaviest owner's 20,000 deals, 0.12 ms
organisation-wide), skipped when there are no new leads. The
first delegated view in a 15-minute window adds the audit insert. Owner and lead names in
the lists come from the same joins: no query per row or per user
([`test_query_counts.py`](../backend/arkray/dashboard/tests/test_query_counts.py)).

### Benchmark

[`tests/performance/bench_dashboard.py`](../backend/tests/performance/bench_dashboard.py):
EXPLAIN ANALYZE of the exact SQL the dashboard runs (best of three, warm cache), and the
whole selector call (best of five, wall clock, including round trips), PostgreSQL 16 in
Docker on a laptop, JIT off as the application runs. **403k**: the Phase 4 database (61
users, 100,000 leads, 50,000 opportunities, 403,000 activities). **Growth**: the
2M-activity database extended to 501 users, 1,000,000 leads (two years at ~1,230 a day, 3 %
archived, one owner with 66,700) and 300,000 opportunities (60 % open, 25 % won, 15 % lost,
3 % archived), 1,998,067 activities.

| Query (ms) | Heaviest owner 403k / growth | Typical owner 403k / growth | Organisation 403k / growth |
|---|---|---|---|
| lead figures | 0.73 / 6.7 | 0.17 / 0.20 | 12.6 / 45 |
| pipeline totals | 1.9 / 10.5 | 0.58 / 0.31 | 14.2 / 30 |
| activity figures (two aggregates) | 0.97 / 0.84 | 0.27 / 0.03 | 12.7 / 12.0 |
| today's newest leads, next meetings, next tasks | ≤ 0.10 each | ≤ 0.08 each | ≤ 0.22 each |
| **all seven (server)** | **3.8 / 18.2** | **1.2 / 0.6** | **40 / 88** |
| whole selector (wall clock) | 23 / 35 | 21 / 19 | 54 / 101 |
| after 10,000 lead edits not yet vacuumed (growth) | 24 (whole 60) | 0.8 (whole 17) | 114 (whole 117) |

The wall-clock floor (~17–20 ms for a typical owner whose SQL takes 0.6 ms) is this
machine's Docker Desktop round trips (about 1.2 ms per statement; ten statements with
BEGIN/SET/COMMIT) plus ~5 ms of ORM SQL compilation, measured with cProfile; on a server
network a round trip is ~0.1–0.3 ms. A **disk-cold** first read is far slower (performance
review: 10 s organisation-wide, 5 s for the heaviest owner on a fresh copy; 0.2 s once the
operating system has cached the tables). The organisation's working set (≈ 400 MB at this
scale) is larger than this container's 128 MB `shared_buffers`; production memory sizing
is a Phase 10/11 item.

### What the benchmark and the review changed

1. **Lead figures from the index alone** (benchmark). At growth scale the heaviest owner's
   lead count read every one of their 66,700 rows to check the archive state (93–188 ms
   measured across runs) and the organisation's scanned 1,000,000 leads (75–177 ms).
   `leads_owner_created_idx` (the Phase 2 per-owner index) now carries `archived_at` as an
   `INCLUDE` column (migration `leads.0005`), so both counts are **index-only scans**:
   6–9 ms and 45–53 ms. Same name, key columns and order, so every Phase 2 list plan still
   uses it (`test_lead_query_plans.py` unchanged; the performance review compared 112 list
   shapes with the old and new definition: identical plans). No new index on the write
   path. Rejected after measuring: a partial index `(owner_id, created_at) WHERE archived_at
   IS NULL` (6 ms / 37 ms, but PostgreSQL then preferred it for other Phase 2 lead queries,
   which the Phase 2 plan tests caught), and a covering partial index for the open pipeline
   (nothing organisation-wide: the cost there is the NUMERIC arithmetic over 195,000 open
   opportunities).
2. **Visibility map kept fresh** (performance review, **P1**). Index-only scans skip the
   table only for pages marked all-visible, and every lead edit clears that mark on two
   pages (lead updates are never HOT: `updated_at` is indexed). With PostgreSQL's defaults
   (vacuum after 20 % of the table changes, analyze after 10 %: 200,000 / 100,000 edits at
   1,000,000 leads) the planner kept choosing the "index-only" count while fetching most heap
   rows: the reviewer measured the organisation's lead figures at up to ~1 s and the
   heaviest owner's at 115 ms after 30,000–90,000 edits; reproduced here (30,000 random
   edits, each committed: the heaviest owner 7 → 44–70 ms with 36,770 heap fetches, still
   degraded minutes later). Migration `leads.0006` sets `leads_lead`'s autovacuum and
   autoanalyze thresholds to **1 %**: in the same experiment autovacuum ran within ~2
   minutes and restored 8 ms / 53 ms with no heap fetches. Between runs at most ~1 % of the
   table has changed: measured at 10,000 edits, 11 ms for the heaviest owner's lead figures
   and 71 ms organisation-wide (the planner then switches to a sequential scan). Cost: a
   background vacuum of 1,000,000 leads (3–5 s, 13 indexes) every ~10,000 edits.
3. **Activity figures as two bounded aggregates** (performance review, P2). Phase 4's one
   aggregate over "open task OR meeting from today" read the heaviest owner's whole history
   (13–25 ms) and, as open work grew (+20,000 open tasks, +20,000 stale scheduled meetings),
   135 ms organisation-wide (154–243 ms with JIT, after a flip to a sequential scan). Split
   into open tasks and meetings from today's start on, each an index-only range of the
   schedule index: 0.8 ms / 12 ms on the growth data, **4.8 ms / 70 ms** on the grown copy,
   figures identical. The definitions are unchanged; the activity summary endpoint costs
   one more query (4).
4. **JIT off** for application connections (`-c jit=off`; review, P3): the dashboard's
   aggregates sit near the JIT threshold, where compilation added 10–30 ms each (290–430 ms
   above the inlining threshold) to queries that take milliseconds.
5. **Index swap without blocking anyone** (security review P3, performance review P2,
   domain review P3). Dropping the old index first held ACCESS EXCLUSIVE on `leads_lead` for
   the whole build (every lead read stalled 2–5 s at 1,000,000 leads); even a drop after the
   build let one long reader stall every lead request for the 5 s lock timeout and fail the
   deploy. `leads.0005` now builds `CONCURRENTLY` under a temporary name, drops the old index
   `CONCURRENTLY` and renames (SHARE UPDATE EXCLUSIVE); it is non-atomic and lifts the
   statement and lock timeouts for its session, restoring them at the end in both
   directions. Measured at 1,000,000 leads with an 8 s reader and a 500 ms statement
   timeout: forwards and backwards both succeed (8 s, waiting for the reader), and the
   slowest concurrent lead read is 7–8 ms.

**Accepted (administrators only, risk R53):** the organisation-wide dashboard reads every
live lead's index entry, every open opportunity and the organisation's open work: 88 ms of
SQL at growth scale (114 ms just before an autovacuum), one request per Admin Home visit,
far under the 10 s statement timeout. No cache was added: nothing measured needs one.

Also accepted (performance review, P3): the `INCLUDE` index can't use B-tree
deduplication (64 MB against 56 MB at 1,000,000 leads) and grows under bulk multi-row
updates, after which PostgreSQL prefers the sequential scan, i.e. the pre-Phase-5 plan; the
next-meetings and next-tasks lists can filter past one owner's stale current work until
autoanalyze catches up (5–6 ms for 20,000 stale items; 0.07–0.3 ms after); today's newest
leads walk today's archived leads (8 ms for 50,000 archived in one day).

Query shapes are pinned by
[`test_dashboard_query_plans.py`](../backend/tests/performance/test_dashboard_query_plans.py)
(lead figures index-only from `leads_owner_created_idx`, today's leads without a sort, the
activity figures and lists from the schedule indexes, one owner's totals never scanning the
organisation, the new leads' opportunities from an index, never a scan) and `test_activity_query_plans.py` (each half of the activity summary a range
of the schedule index). Those tests vacuum their data first, as autovacuum keeps it; the
`--churn` option of the benchmark measures the state between autovacuum runs.

## Frontend

- **Layout:** the page header (Dashboard; Your records / Selected user's records /
  Organization overview), "Key figures" (six cards: Total leads, New leads today, Pipeline
  value, Weighted pipeline, Meetings, Tasks; one column on phones, two on tablets, three on
  desktops; amounts wrap only at their commas), then the three lists in this order: **New
  leads**, Upcoming meetings, Tasks requiring attention (stacked on phones and tablets, two
  columns from 1024 px, three from 1280 px; names wrap rather than being cut off, so an
  assigned user's name and "(deactivated)" always show), then, on Admin Home, "Recently
  added users".
- **Cards are links** to this workspace's Pipeline and Activities (the two lead cards are
  plain tiles: there is no Leads list; their leads are listed below). A card opens the
  list that shows exactly what it counts by presetting that list's in-memory filters
  (`presetActivityList`, `presetBoard`; the same presets as the Activities
  page's own shortcuts, now one function `summaryFilters`), never by putting filters in the
  URL. The preset runs only for a navigation in the same tab (`onNavigate`): a card opened
  in a new tab (Ctrl/Cmd/Shift- or middle-click) shows that module's default list and leaves
  this tab's remembered filters alone (frontend review). Admin viewing Rahul → Tasks opens
  Rahul's Activities on the Tasks tab, not the organisation's.
- **States:** a skeleton while loading (never zeros) and one persistent `role="status"`
  region that says "Loading the dashboard" / "Updating the dashboard"; real zeros with empty
  states for a new user ("No new leads yet today.", "No upcoming meetings.", "No open
  tasks."); errors as above; a 404 is the not-found page alone (one `h1`).
- **Workspace isolation:** the view is keyed by workspace and every query key starts with
  `["dashboard", <workspace>]`; with `gcTime: 0` nothing of a previous workspace (or a
  previous signed-in user) can be shown. Tested: Rahul → Priya (slow, then failing), Back
  (read again), A → B → A, a different user signing in on the same page, a refresh the
  server refuses; the frontend review also tried rapid switching with late responses and
  React StrictMode double mounting.
- **Accessibility:** one `h1`, an `h2` per section (each a labelled region), cards are
  keyboard-reachable links whose accessible names read in order ("Tasks 9 open 2 due today
  · 1 overdue"), every list link names its list, overdue is written out (not colour alone),
  times are `<time>` elements, errors use `role="alert"`, secondary text meets AA contrast (slate-500 or darker since the
  whole-software audit, which measured the earlier slate-400 at 2.63:1; links inside
  sentences are underlined).

## Tests

| What | Where |
|---|---|
| Definitions (brief's example, exact decimals rounded once, Phase 4 task and meeting semantics, IST boundaries at both ends, ties, bounds, no contact data, zeros, all pipelines, `business_date` from the request's clock), each figure = its module's selector in every scope kind | `arkray/dashboard/tests/test_figures.py`, `arkray/leads/tests/test_summary.py`, `arkray/activities/tests/test_summary.py` |
| API: workspace resolution and odd spellings, identical 404s, 401, strict parameters, read-only, no-store, audit once per window, no CRM data in logs, one clock read | `arkray/dashboard/tests/test_api.py` |
| Freshness through the real write APIs | `arkray/dashboard/tests/test_freshness.py` |
| Query counts and the read-only snapshot | `arkray/dashboard/tests/test_query_counts.py` |
| Aggregate isolation: A, B and the admin with distinctive amounts; exact figures per workspace; A's response byte-identical before and after B's records exist; reassignment moving figures exactly | `tests/security/test_dashboard_cross_user.py` |
| Review regressions (slim rows, the concurrent index swap, autovacuum settings, JIT off) | `tests/security/test_phase5_review_regressions.py`, `tests/architecture/test_migrations.py` |
| Redis and broker outages | `tests/security/test_outages.py` |
| Query plans | `tests/performance/test_dashboard_query_plans.py`, `test_activity_query_plans.py` |
| Frontend: figures, exact money, states, rows, card targets and presets (also new-tab clicks and the board), workspace isolation, refresh and offline behaviour, the day boundary, accessibility | `frontend/src/features/dashboard/dashboard.test.tsx` |
