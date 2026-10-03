# Testing strategy

Tests are part of every phase's definition of done. A phase does not pass while critical
tests fail.

## Layers

| Layer | Backend | Frontend |
|---|---|---|
| Unit | pure logic: `AccessScope`, capability policy, backoff, metadata sanitising, formatters | pure logic: workspace resolution, navigation model, API client |
| Integration (real PostgreSQL) | services and selectors with constraints, triggers, `SKIP LOCKED`, pgvector | — |
| API | DRF test client: status codes, envelopes, CSRF, throttles | — |
| Authorization | route matrix and cross-user suite (below) | permission-driven rendering (for example, Users link only with `users.manage`) |
| Component / interaction | — | Vitest + Testing Library: shell, banner, forms; loading, empty and error states |
| Architecture guards | deny-by-default, route inventory, no floats, UUID keys, append-only triggers, production-settings safety, import-linter layering | — |
| E2E (Phase 11) | Playwright against `docker compose --profile app` | |
| RAG | deterministic fake model + golden dataset; opt-in live eval | |

**Backend tests always run against real PostgreSQL** (the Compose service). SQLite would
silently skip exactly the behaviour we rely on: CHECK/partial-unique constraints, triggers,
`SELECT … FOR UPDATE SKIP LOCKED`, `NUMERIC` arithmetic and pgvector.

## Tooling

- Backend: `pytest`, `pytest-django`, `factory_boy` (`tests/factories.py`), `pytest-cov`.
  Coverage in Phase 0: 96 % of `arkray/`. Coverage is monitored, not gamed; services and
  selectors are expected to be near-fully covered.
- Frontend: `vitest` + `jsdom` + Testing Library (`@testing-library/react`, `user-event`,
  `jest-dom`). API calls are mocked with a small typed fetch router (`src/test/render.tsx`:
  routes keyed `"METHOD /path"`, every call recorded with its query, body and headers)
  rather than MSW: it runs in jsdom without a service worker, and tests assert the exact
  requests sent (for example, the CSRF header and allowlisted filters).
- Real concurrency: `@pytest.mark.django_db(transaction=True)` plus
  `tests.helpers.run_concurrently` (each call in its own thread and database connection,
  released by a barrier).
- Static gates: `ruff` (lint, format, bandit rules), `mypy --strict` (tests relaxed),
  `lint-imports`, `pip-audit`; `eslint`, `tsc --noEmit`, `next build`.
- Everything runs via [`scripts/check.sh`](../scripts/check.sh) and CI
  (`.github/workflows/ci.yml`).

## Critical cross-user security suite

Built incrementally in Phases 2–8. Fixtures: **User A**, **User B** (sales users) and
**Admin**, each with leads, opportunities, tasks, meetings and notes. User A must not obtain
User B's data through any channel:

| Channel | Assertion |
|---|---|
| List endpoints | A's lists contain only A's records |
| Retrieve / update / action by ID | B's IDs → **404** for A (never 403, never 200) |
| Workspace segment | A requesting `/workspaces/{B-id}/…` or `/workspaces/all/…` → 404 |
| Filters and ordering | `?owner=B`, unknown or ORM-style parameters → 400, never B's data |
| Search | B's unique names or terms return nothing for A |
| Dashboard | A's totals equal A's fixtures exactly (B's records don't change A's numbers) |
| Pipeline board | only A's opportunities; moving B's → 404 |
| Activities and timelines | only A's; B's lead timeline → 404 |
| Embedded related records | a related record A can't see renders as `restricted` |
| Ask Arkray | A's golden answers exact; questions naming B's records → "not found" |
| Vector retrieval | B's semantically identical note never returned; stale chunks dropped by re-verification |

Then the same suite runs as **Admin**: `/workspaces/{A-id}` and `/workspaces/{B-id}` return
the respective data, `all` returns both, and each delegated access is audited.

The authorization matrix (`tests/authz_matrix.py`) lists every route with its rule; a
parametrised test generates `route × {anonymous, A, B, admin} × {own, other}` cases, and the
architecture test fails if a route is missing from the matrix.

## Specific test types

- **Constraint tests:** each CHECK and UNIQUE rule is proven by writing invalid data
  (including via raw SQL where the ORM would block it first).
- **Money:** Decimal inputs and outputs as strings, rounding rules, rejection of floats,
  negatives and excess precision; the aggregate example (₹1,000,000 × 50 % = ₹500,000.00)
  checked exactly.
- **Time:** "today" boundaries in `CRM_TIME_ZONE` around midnight IST vs UTC.
- **Concurrency** (`transaction=True`, threads): two simultaneous stage moves give
  consistent history; concurrent reassignments serialise; outbox claims never double-process.
- **Query counts:** `django_assert_max_num_queries` on every list, dashboard and admin-users
  endpoint (N+1 guard), with data volume varied to prove the count is constant.
- **Failure modes:** cache outage (fast misses; audit fails toward more auditing), AI
  provider errors (breaker opens, CRM unaffected), outbox retry, dead-lettering and lease
  recovery.
- **Frontend states:** every data view tests loading (skeleton), empty, error (with retry)
  and populated states, plus permission-driven visibility.

## What exists after Phase 5

- **Backend (2,764 tests, 98 % coverage of `arkray/`)**, adding for the Dashboard:
  the figure definitions (`arkray/dashboard/tests/test_figures.py`: the brief's ₹15,00,000 /
  ₹9,00,000 example with won, lost and archived excluded; exact decimal strings rounded
  once, 25 × the maximum value; equality with the pipeline's own summary over 60 random
  opportunities; Phase 4 task and meeting semantics on fixtures in every state; lead figures
  at the 00:00 IST boundary and both ends of the day; every pipeline counted; `business_date`
  from the request's clock; bounded lists, newest/soonest first, ties; no contact data; a
  new user's zeros; each figure equal to its owning module's selector in all three scope
  kinds), the lead figures equal to the Leads list they open
  (`arkray/leads/tests/test_summary.py`), the next open tasks equal to the Tasks tab's first
  rows, the API (identical 404s for other workspaces and 11 odd spellings, 401, strict
  parameters, read-only methods, no-store, audit once per window and none for one's own
  dashboard, no CRM data in the logs, one clock read), freshness through the real write APIs
  (create lead, opportunity, stage move, Won, task, complete, meeting, cancel, archive),
  exact query counts (own 9, selected user 10, organisation 9; production +1 for the
  snapshot, verified in a transactional test) at 10× the records and 5× the users, the
  **aggregate-isolation suite** `tests/security/test_dashboard_cross_user.py` (A, B and the
  administrator with distinctive amounts: exact figures per workspace, nothing of the other
  user anywhere in the response, A's response byte-identical before and after B's records
  exist, selected-user dashboards identical to the user's own, the organisation = the sum,
  figures following a reassigned lead), Redis and broker outages, query plans
  (`tests/performance/test_dashboard_query_plans.py`), the concurrent-migration guard and
  `tests/security/test_phase5_review_regressions.py`.
- **Frontend (479 tests):** the dashboard (`features/dashboard/dashboard.test.tsx`, 24):
  the six figures as the server sent them, money beyond `Number` precision and wrapping only
  at commas, skeleton (never zeros) with one persistent live region, real zeros and empty
  states, 500/403/404/network/offline, a refresh marked "Updating…" and a failed one removing
  the figures, the refresh at the business-day change, rows naming owners organisation-wide
  only, card and footer targets in the same workspace with their list presets (and none for
  new-tab clicks; the board unfiltered), distinct link names, Rahul → Priya slow/failing,
  Back, A → B → A, a new signed-in user on the same page, the cache key, accessibility.
- **Adversarial review:** four independent reviewers (API security and aggregate leakage,
  backend domain with 25 in-memory mutations, frontend, performance at 1,000,000 leads and
  2,000,000 activities). No P0: no aggregate leak, no IDOR, isolation held under rapid
  switching, StrictMode and failing refreshes. **One P1** (performance: the index-only lead
  count depends on the visibility map, which ordinary lead edits erode; with default
  autovacuum the organisation's figure degraded to ~1 s): fixed by autovacuum thresholds of
  1 % on `leads_lead` (migration `leads.0006`, reproduced and verified). P2s: the index
  migration blocking lead reads (dropped first, then still ACCESS EXCLUSIVE for the drop)
  → `CONCURRENTLY`, non-atomic, timeouts lifted and restored both ways; the activity summary
  reading an owner's whole history and flipping to a sequential scan as open work grew → two
  bounded aggregates; card clicks with modifiers wiping this tab's list filters → presets on
  `onNavigate` only; pipeline cards opening a board narrowed by earlier filters → board
  preset. P3s: activity rows sending previews and authors (slim rows), JIT, the reverse
  migration's timeout, undefined colour tokens, two `h1`s on 404, ambiguous link names,
  stale figures unmarked during a failing refresh, an endless skeleton offline, the day
  boundary, truncated owners, amounts breaking mid-group, the loading announcement,
  contrast, a query-count test failing just after midnight IST, untested upper bounds and
  ties, stale docs. All fixed and pinned by tests, except the accepted, documented R53
  (organisation-wide dashboard cost), R54 (a dev-only `braces` advisory with no fix) and the
  pre-existing R56 (some Phase 2 per-owner list shapes at 1,000,000 leads).
- **Benchmark** (`tests/performance/bench_dashboard.py`, with `--grow` and `--churn`): per
  owner 18 ms of SQL for a 66,700-lead owner and 0.6 ms typical, 88 ms organisation-wide
  at 1,000,000 leads / 300,000 opportunities / 2,000,000 activities / 501 users
  ([dashboard.md](dashboard.md#performance)).
- **Live walkthrough** against the rebuilt containerised stack, headless Chromium: 66
  checks (the stack's sessions with JIT off and the autovacuum settings applied; Rahul: the
  six cards equal to the API and to the selectors computed independently, a new lead, an
  opportunity at ₹1,11,111, a stage move and Won with exact paise arithmetic, a task and a
  meeting moving their figures, slim activity rows, the Tasks card opening his Tasks tab,
  phone and tablet layouts; the administrator: Admin Home equal to the organisation's
  figures and to Rahul + Priya, new leads naming their assigned user, `/admin` → Admin Home,
  Rahul's dashboard equal to his own, Pipeline/Leads/Activities staying in his workspace
  under the banner, his Tasks card opening his Activities, Rahul → Priya and Back (twice)
  with a DOM observer proving no figure or record of the other user was ever drawn, 320,
  375, 768 and 1440 px layouts without horizontal scroll). No page errors and no 5xx; the
  only 4xx were the signed-out session check on the sign-in page. Container logs (37
  dashboard requests) and audit metadata held no names, titles, amounts or emails.

## What exists after Phase 4

- **Backend (2,677 tests, 98 % coverage of `arkray/`)**, adding for Activities:
  every database invariant proven with raw SQL (each type's columns and statuses, NULL-safe
  CHECKs, visible text in titles and notes, the 24-hour meeting limit under a
  daylight-saving session time zone, current work owned by its lead's owner through the
  deferred composite key, the opportunity of the same lead, timeline entries bound to their
  activity's lead, opportunity and type, the append-only timeline refusing UPDATE/DELETE),
  the services (create, edit, complete/cancel/reopen with no-op repeats, archive/restore,
  the archived-lead policy, authorship and historical attribution through reassignments),
  last contact (completed meetings only, MAX semantics, version bump, audit), reassignment
  propagation (current work and notes follow, closed work stays, atomic rollback with a
  failing subscriber, constant queries), timelines (every kind, snapshots, visibility per
  entry, restricted opportunities, keyset pages, the backfill from Phase 2–3 history and its
  equivalence with what the live subscribers record), the HTTP API (strict input per type,
  every system field refused, explicit action endpoints taking exactly a version, filters
  and their contradictions, every ordering, cursors), the summary (business-day boundaries
  at 00:15 IST, scope, each figure equal to the list its shortcut opens), exact query counts
  at 10 and 100 rows for every endpoint (also organisation-wide cursor pages), real-thread
  races and lock-order storms, query-plan shapes for every list, summary and timeline shape
  (`tests/performance/test_activity_query_plans.py`), migrations lifting the statement
  timeout, Redis and broker outages, and the **cross-user suites**
  `tests/security/test_activities_cross_user.py` (131 cases, both directions),
  `test_activities_api_surface.py` (167: every route 404 for other users, mass assignment,
  action bodies, link schemes, cursor hygiene, canonical ids),
  `test_activities_reassignment_flows.py`, `test_activities_oracles.py` and
  `test_phase4_review_regressions.py`.
- **Frontend (454 tests):** the Activities page (tabs, filters as allowlisted
  parameters, shortcuts, table and cards, row actions with versions, 409s, empty and error
  states), the task and meeting dialog (India-time inputs, idempotency key reuse, conflict
  merge, a meeting's end following its start, discard confirmation), the activity page
  (actions, meeting links, note editing by its author only, conflicts), timelines and the
  note box, open work on lead and opportunity pages, no stale data between Rahul's and
  Priya's workspaces (also Back and failed refetches), and
  `features/activities/review-regressions.test.tsx` (22).
- **Adversarial review:** four independent reviewers (backend/domain, API/security,
  frontend, performance) found **one P1** (performance: one owner's newest-first list could
  walk the whole organisation's index, 48 ms at 403k and up to 500 ms at 2M activities), P2s
  (a meeting link with an upper-case `HTTPS://` scheme passed validation but failed the
  database CHECK: a 500; stale cached list versions causing false conflicts; focus lost
  after in-page actions; text typed during a note save lost; the summary's cost growing
  with all history; an organisation-wide lead filter walking the schedule index; the
  timeline backfill able to hit the 10 s statement timeout) and P3s (the previous owner
  able to learn whether the new owner archived a lead; timeline entries not tied to their
  activity's opportunity and type; the 24-hour limit following daylight-saving rules;
  whitespace-only titles; a creation racing a reassignment answering 422 instead of 404,
  revealing the reassignment; shortcut lists not matching their counts; ambiguous control
  names; Complete not offered once a meeting started; typing discarded on Escape; a
  meeting's end left behind; a descending index bloating; deep pages in large tie groups,
  timelines of mostly hidden entries, bulk reassignment cost, the archived view by
  due/start; sequential ids revealing volume). No P0: no IDOR, no timeline or count leak.
  Every one is fixed and pinned by a regression test except the accepted, documented P3s
  (R48 sequential ids; R49–R52 performance edges). The archived-lead policy question (D-5)
  was decided and tested. Re-benchmarking the fixes at 2M activities found one more P2
  (current work and one type of any status by due/start: 124 ms per owner and 0.5 s
  organisation-wide), fixed by partial indexes (0.2–0.7 ms), and an extra query per
  organisation-wide cursor page (P3), fixed and pinned.
- **Live walkthrough** against the rebuilt containerised stack, headless Chromium:
  22 checks (admin in Rahul's workspace: create from the Activities page and the
  lead page, a meeting whose end follows its start, a note, Complete by keyboard, the lead
  and opportunity timelines, last contact recorded, the discard confirmation, reassignment
  to Priya with no request where the lead no longer is, current work following the lead and
  completed work staying with the admin as completer, Rahul → Priya and Back with a DOM
  observer; Rahul: create, schedule, note, edit then complete from the cached list with no
  conflict, focus after Complete, every summary figure equal to its list, Priya's records
  unreachable through UI and API; Priya: only what moved to her, phone and tablet layouts
  without horizontal scroll). No page errors and no 5xx; the only 4xx were the signed-out session check on the sign-in page and the deliberate 404 for Priya's task. Container logs, audit metadata, timeline snapshots and the outbox were free of note text, titles, names and emails (a marker in a note was found only in its own row). The previous walkthrough's finding (an administrator's lead page refetching its timeline in the workspace the lead had just left: a 404) is fixed and pinned.

## What exists after Phase 3

- **Backend (1,932 tests, 98 % coverage of `arkray/`)**, adding for the Pipeline: every
  database invariant proven with raw SQL (status = stage category and the pipeline = the
  stage's pipeline through the composite key; an open opportunity owned by its lead's
  owner through the deferred composite key, including a lead reassigned alone; won 100 %,
  lost 0 %, closed_at, lost reason and override rules; value, probability and date
  ranges; append-only history refusing UPDATE/DELETE from raw SQL; seeded pipeline and
  stage constraints including deferred position swaps), the financial rules
  (`test_metrics.py`: the brief's ₹15,00,000 / ₹9,00,000 example, zero, 25 × the maximum
  value, paise, rounding boundaries, "rounded once" vs per-row rounding, 1,000 random
  amounts SQL = Python, floats refused, filters narrowing totals), the services (create,
  edit, probability override and reset, every transition incl. won/lost/reopen and
  closed→closed, history, audit and events, archive/restore), conversion (atomic under a
  forced mid-way failure and a failing subscriber, idempotent, no double conversion,
  "Converted needs an opportunity" for the status endpoint and lead creation),
  reassignment propagation (the brief's two-open/one-won/one-lost scenario, archived open
  ones, rollback, historical attribution unchanged), the HTTP API (strict decimal input,
  system fields refused, board shape and bounds, card order, filters, every ordering,
  pagination, admin workspaces), exact query counts for every endpoint at 10 and 100
  opportunities, real-thread races and lock-order interleavings that deadlock on a wrong
  order (a deliberate mutation proved it), keyset pagination of every ordering walked
  against PostgreSQL both ways (56 cases), query-plan shapes
  (`tests/performance/test_pipeline_query_plans.py`), the **cross-user suite**
  `tests/security/test_pipeline_cross_user.py` (170 cases, both directions, including
  aggregate leakage), and `tests/security/test_phase3_review_regressions.py`.
- **Frontend (399 tests):** exact INR formatting and parsing on decimal strings (no
  `Number()`), date-only formatting, the board (stage order, exact totals, cards, overdue in
  words, restricted leads, empty and error states, "View all" paging), moving by drag and
  drop and by the keyboard Move menu (one API, the card's version, optimistic placement,
  rollback on 409/404/500/network, focus kept on the card), won/lost/reopen confirmations,
  no stale data between Rahul's and Priya's pipelines (also Back), phone stage tabs,
  filters, the opportunity page, the create/edit form (exact amounts, idempotency key
  reuse, manual probability, conflict merge), the lead page's Opportunities section and
  Convert dialog, and `features/pipeline/review-regressions.test.tsx`.
- **Adversarial review:** three independent reviewers reproduced 1 P1 (found by two of
  them independently: opportunity writes also locked the shared stage row, so different
  users moving cards in opposite directions deadlocked; 23 of 80 HTTP moves returned 500),
  2 frontend P1s (an optimistic rollback restoring the wrong cache when filters changed
  mid-move; a conflict merge silently resetting someone else's manual probability), and
  P2/P3s (in-flight conversion retries answered 409, board figures from different
  snapshots, reopen/restore on archived leads, a lost reason silently dropped, N+1 audit
  inserts on reassignment, legacy Converted leads, the Converted veto revealing a hidden
  deal, deal values readable in cursors, `cards_per_stage=0` columns without `next`, stale
  versions after moves, stage-list cursors surviving filter changes, focus loss after
  moves and dialogs, misplaced commas in amounts, inverted date ranges). No P0: no IDOR, no
  aggregate leak. Every one is fixed and pinned by a regression test.
- **Live walkthrough** against the rebuilt containerised stack (Django + Celery + Next.js),
  headless Chromium: 23 checks (admin: Users → Rahul → Pipeline under the banner, create
  for Rahul with exact amounts checked in paise, drag and drop, keyboard Move with focus
  kept, edit, won, reopen, lost with a reason, closed→closed refused without a request,
  the lead page's opportunities, conversion (and no second one), reassignment moving the
  open opportunity, Rahul → Priya and Back with a DOM observer proving no Rahul card ever
  appears under Priya's banner, 1440 px layout, organisation-wide board; Rahul: own board,
  create and drag, Priya's URLs and APIs 404 with bodies identical to a missing record,
  phone layout with stage tabs; Priya: only her records and totals). It found three issues
  unit tests missed (focus lost after a keyboard move, a stale-version 409 after changing an
  opportunity on its own page, and screen-reader-only text widening the whole page at
  1440 px), all fixed. No console errors, no unexpected API responses, container logs and
  audit metadata free of titles, names, emails, amounts and lost reasons. It also exposed a
  latent Phase 1 test-isolation defect (the identity migration test left `leads`/`pipeline`
  unapplied for later transactional tests), fixed and pinned.

## What exists after Phase 2

- **Backend (1283 tests, 98 % coverage of `arkray/`)**, adding for Leads:
  field rules (names in many scripts, NFC, refused control and bidi characters, emails,
  international phone numbers and their canonical keys, postal codes, countries, last
  contact bounds), every database constraint proven with raw SQL (including the generated
  `display_name`/`search_text` columns and FK-protected configuration), the API (create,
  read, edit with versions and no-ops, status, archive/restore, options, allowlisted and
  invalid parameters, search across names/organisation/email/phone digits, every ordering,
  pagination links, invalid cursors), idempotent create (replay, key reuse, per-user keys,
  failed requests), ownership per workspace and reassignment, admin workspaces and their
  audit, the assignees directory, capability separation (`crm.manage_any`,
  `crm.assign_any`), audit events and domain events per operation (including rollback on a
  failing subscriber), no queued work, no personal data in logs or audit, exact query
  counts at 10 and 100 rows for every endpoint, real-thread races (reassign × reassign,
  edit × edit, archive × edit, status × reassign, reassign × deactivation, idempotent
  double create), keyset pagination walked against PostgreSQL's own ordering for every sort
  and several page sizes (ties, NULLs, backwards, inserts and archiving between pages,
  forged cursors), query-plan shapes (`tests/performance/`), and the **cross-user suite**
  `tests/security/test_leads_cross_user.py` (User A vs User B and the reverse over every
  channel, including metadata and error-body equality).
- **Frontend (250 tests):** the Leads list (own, organisation-wide and a selected
  user's workspace; table and phone cards; empty, filtered-empty and error states; search
  debounce; allowlisted filters and sorting; archived view; cursor paging; row actions;
  in-memory list state that never leaks between workspaces or into URLs/storage; no stale
  rows from another user's workspace while loading), lead detail (sections, reserved
  activity area, status change, 409 handling, archived state, reassignment out of a user's
  workspace, 404 for foreign leads), the form (sections, client and server validation with
  focus, owner rules per workspace, idempotency keys reused only for identical retries,
  double-submit, duplicate assistance, edit conflict recovery by re-applying changes or
  discarding them), and pure helpers (draft/merge rules, list URLs, IST datetime inputs,
  lead links).
- **Adversarial review:** three independent reviewers (API security, backend domain/concurrency/performance, frontend) reproduced 1 P1 (a request-body decompression bomb via `charset=zlib`, present since Phase 1), 7 P2 and 20+ P3 findings, plus two self-found issues and two found by the live walkthrough (a 404 refetch after reassignment; sign-out reloading its own page, from Phase 1). Every one is fixed and pinned by `tests/security/test_phase2_review_regressions.py`, `frontend/src/features/leads/review-regressions.test.tsx`, `src/app/providers.test.tsx` and `src/features/auth/useSignOut.test.tsx`.
- **Live walkthrough** against the containerised stack (Django + Celery + Next.js), headless
  Chromium: 35 checks (admin: Users → Rahul → Leads, create for Rahul, edit, search by name and phone digits, filter, reassign to Priya, back to Users → Priya; organisation-wide list; Rahul on a phone-sized screen: own leads as cards, duplicate assistance that doesn't reveal Priya's lead, create, status, edit, search, filter, Priya's lead and workspace URLs → not found, identical API 404s; Priya in the same browser after Rahul signs out: only her lead, no carried-over filters, Back never shows Rahul's data), with no console errors, no unexpected requests, and container logs free of passwords, contact data, search terms and tracebacks.

## What exists after Phase 1

- **Backend (585 tests, 97 % coverage of `arkray/`):** sign-in (identical failures incl. hasher-call
  counting for timing, fixation, CSRF, trusted-browser throttling, per-IP limits, lockout
  expiry, bounded query cost, index use), sessions (idle and absolute limits, malformed
  timestamps, revocation on deactivation, reactivation, email change and password change,
  demotion), invitation and reset lifecycles end to end through the real outbox relay and
  email job (single use, expiry, supersession, purpose separation, mail outage and retry,
  digest-only storage, idempotent redelivery), admin user management (list, search via
  trigram indexes, filters, strict query parameters, cursor pagination, a constant query
  count, create, edit with versions, email change, lifecycle actions, self-protection,
  last-administrator invariant), the workspace endpoint (audited, enumeration-safe, no
  impersonation), DB constraints proven with raw SQL, real-thread race tests, a
  route × {anonymous, sales user, admin} authorization matrix, CSRF on every unsafe route,
  secret-hygiene scans of logs, audit, outbox, tokens and responses, Redis, broker and mail
  outages, the OpenAPI contract (valid, warning-free, committed), and migration
  forward/backward on Phase 0-shaped data.
- **Frontend (177 tests):** the API client (CSRF bootstrap and a single
  retry, Retry-After), the post-login redirect sanitiser (open-redirect cases), business
  time zone formatting, error presentation (no raw exceptions), the query client (no
  retries on 4xx; 401 → sign-in once, never on public pages), sign-in, forgot, reset and
  activation pages (all states, double-submit prevention, reasons), the session gate and
  capability guard, the Users area (loading, empty, filtered-empty, error, 403, search
  debounce and allowlisted parameters, cursor paging on the same origin, create, edit, 409,
  email change, confirmations, self-protection, keyboard menu), Admin Home (no invented
  figures), workspace banner and 404, profile (change password, sign-out), and the dialog
  focus trap.

- **Adversarial review regressions:** `tests/security/test_review_regressions.py`,
  `frontend/src/features/review-regressions.test.tsx` and `src/app/providers.test.tsx` pin
  every defect the Phase 1 review confirmed, so none can silently return.
- **Live end-to-end walkthrough** (35 checks, run against the real Django + Celery + Next.js
  + Mailpit stack through the Next.js proxy) plus headless-Chrome repro scripts for the
  open-redirect, back/forward-cache, cross-tab and focus findings. These are manual Phase 1
  verification tools; the Playwright E2E suite arrives in Phase 11.

## What exists after Phase 0

- Backend: 147 tests (96 % coverage) covering outbox (enqueue atomicity, coalescing including
  a threaded pre-commit locking test, relay caps, broker failure, claim tokens and stale
  messages, retries, dead-lettering, crash loops, lease recovery with duplicates, relay
  routing, Celery wiring), audit
  (append-only at ORM and DB-trigger level, redaction), identity (email normalisation and
  constraints, capability matrix, workspace resolution and auditing), request context,
  health, error envelope, fail-fast cache, and architecture guards.
- Frontend: 44 tests covering workspace resolution, navigation (no Companies or Products;
  Users only for admins), sidebar context persistence in admin workspaces, banner, shell
  (including keyboard dismissal of the mobile drawer),
  and the API client (CSRF, same-origin enforcement, error envelope, timeouts, network
  errors).
