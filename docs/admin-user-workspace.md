# Admin user workspace

An administrator opens a user's CRM by clicking the user's **name** on the Users page,
and lands on that user's Dashboard.
From there they move through the user's Dashboard, Pipeline, Leads and Activities
without signing out and without impersonating the user. Phase 6 completed and hardened
this journey. The design decisions are [ADR-0005](adr/0005-admin-workspace-without-impersonation.md)
(scoped, audited access instead of impersonation) and [ADR-0010](adr/0010-frontend-workspace-routing.md)
(URL-derived workspaces, shared module views). This page describes how the pieces fit
together and how they are verified.

**There is one CRM, not two.** The selected user's pages are the module views everyone
uses for their own records. The selected user's API is the one everyone uses:
`/api/v1/workspaces/{userId}/...` instead of `/api/v1/workspaces/me/...`. No admin-only
CRUD endpoint exists, and no frontend component is admin-only.

## Actor and workspace subject

Example: Anita Rao (admin) opens Rahul Sharma's workspace.

| | Who | Where it shows |
|---|---|---|
| **Actor**: the authenticated user | Anita | The session, `request.user`, every `created_by`, `completed_by`, `cancelled_by`, stage-history `actor` and audit `actor_id`; "Signed in as Anita Rao" in the banner; the header's account menu (the drawer's account block on phones) |
| **Subject**: whose records these are | Rahul | The URL (`/admin/users/{rahul}/…`), the `AccessScope` (`kind=user`, `owner_ids={rahul}`), the owner of everything created here, audit `subject_user_id`; "Viewing CRM for: Rahul Sharma" in the banner |

| Anita does this in Rahul's workspace | Owner | Created / completed by | Audit |
|---|---|---|---|
| Create a lead | Rahul | Anita | `lead.created`: actor Anita, subject Rahul |
| Edit a lead | Rahul (unchanged) | Anita | `lead.updated`: actor Anita, subject Rahul |
| Create an opportunity, move its stage | Rahul (follows the lead) | Anita; stage history actor Anita | `opportunity.created`, `opportunity.stage_changed` |
| Create or complete a task or meeting | Rahul (follows the lead) | `created_by` and `completed_by` Anita | `task.created`, `task.completed`, `meeting.completed` (and last contact via the Phase 4 service) |
| Add a note | Rahul (follows the lead) | Author Anita (only she can edit it) | `note.created`; the note text is never in audit metadata |

Nothing is ever recorded as done by Rahul. `tests/security/test_admin_workspace.py`
performs all of the above through the API and asserts that every write's audit actor is
Anita and every subject is Rahul.

## No impersonation

There is no "log in as", session switch, user token, cookie swap or hidden endpoint. The
admin's session is the only session. Selected-user access is always *the admin's session +
`resolve_workspace` + an explicit `AccessScope`*. Tests assert that the session key and
`/auth/me` stay the admin's through a whole workspace visit, and that no URL route matches
`impersonat|login-as|become|switch-user|sudo|act-as`.

## Support sessions

The business asked that an administrator can "log in as any user". That is met **without
anyone learning, using or resetting the user's password and without impersonation**
([ADR-0026](adr/0026-user-pipelines-support-sessions-attachments.md),
`backend/arkray/identity/support.py`):

- **Start**: Users → a user → *Access as user* (optional reason) →
  `POST /api/v1/admin/support-sessions {user, reason}` (`support.access`). Only for an
  **active, non-administrator** user with a CRM (422 otherwise: deactivated users stay
  inaccessible this way). The session is bound to the administrator's browser session (a
  SHA-256 of its key) and lasts `SUPPORT_SESSION_TTL_S` (30 min), never extended; starting one
  ends any earlier one of the same administrator (at most one live: a partial unique index).
- **During it** the administrator is still themselves: `/auth/me` returns them (plus
  `support_session`), the session key never changes. Only the target's workspace opens
  (`resolve_workspace` refuses `me`, `all` and any other user: 403
  `support_session_active`), and identity and security operations are refused (user
  administration, setting passwords, changing one's own password, security events, another
  support session). The UI shows a persistent banner — *Support session · Rahul Sharma ·
  24 min left · Exit · Signed in as Anita Rao* — and keeps navigation inside that workspace.
- **Every change** records the administrator as the actor and the user as the subject, plus
  the session's id: a `support_session_id` column on audit events and negotiated prices.
  Nothing is ever recorded as the user's own act.
- **Ending**: *Exit* (`DELETE /api/v1/admin/support-sessions/current`), signing out, expiry,
  the browser session changing (a new sign-in rotates its key: a copied marker in another
  session is worthless), or conditions failing (the user deactivated or made an administrator,
  the administrator losing the capability) — checked on every request, so nothing outlives
  its conditions. An hourly sweep closes expired sessions nobody used again.
- **Audit**: `support_session.started` (administrator, user, reason, expiry) and
  `support_session.ended` (how: exited, expired, signed_out, not_allowed, session_changed;
  the system is the actor when it ended it), both carrying the session id; they appear in the
  security events feed.

The existing selected-user workspace (`/admin/users/{id}/…`, "Viewing CRM for") is
unchanged; a support session is the explicit, time-boxed form of it.

## Routing

| Route | Page |
|---|---|
| `/admin/users/{id}` | Redirects to `/admin/users/{id}/dashboard` (only a valid id is ever redirected; anything else is 404) |
| `/admin/users/{id}/dashboard` | The user's Dashboard (Phase 5 view) |
| `/admin/users/{id}/pipeline`, `…/pipeline/new`, `…/pipeline/{opportunityId}`, `…/{opportunityId}/edit` | Pipeline |
| `/admin/users/{id}/leads`, `…/leads/new`, `…/leads/{leadId}`, `…/{leadId}/edit` | Leads |
| `/admin/users/{id}/activities`, `…/activities/{activityId}` | Activities (tasks and meetings are created and edited in dialogs) |

- **One builder.** Every workspace link comes from `frontend/src/lib/workspace.ts`
  (`workspaceHref`, `leadHref`, `opportunityHref`, `activityHref`, `newLeadHref`,
  `newOpportunityHref`, `userWorkspaceHref`, `sectionBack`). No component hardcodes `/leads`,
  `/pipeline` or `/activities`. A test walks every link on every workspace page and fails if
  one leaves `/admin/users/{id}/` (apart from Users and Settings).
- **The URL is parsed once.** `userIdFromPathname` percent-decodes the user segment exactly
  once, which is how Next.js decodes the layout's `userId` param, and accepts only a UUID. Any
  other segment under `/admin/users/` names **no** workspace (`null`). That is a "not found"
  page, never a fallback to the viewer's own or the organisation's records.
- **One address per workspace.** An upper-case or percent-encoded spelling of the id is
  replaced with the canonical lower-case URL before anything renders.
  `UserWorkspaceFrame` renders the module only when the URL's user and the layout's user
  are the same.
- The API accepts only the canonical hyphenated UUID; `me`, `all`, braces, `urn:uuid:`, bare
  hex, encoded slashes and NUL are all 404.

## Authorization

The backend is authoritative. The frontend shows or hides by capability only for
convenience.

| Capability | Grants | Without it |
|---|---|---|
| `workspace.view_any` | Opening any user's workspace (`resolve_workspace`, audited) | 404 for every `/workspaces/{uuid}/…`, the same as for a user who doesn't exist; user names on the Users page are plain text, not links |
| `crm.manage_any` | Creating and changing records in another user's workspace (`authorize_write`) | 403 on writes; the UI hides create, edit and lifecycle actions |
| `crm.assign_any` | Creating leads for another user, reassigning | 403; no Reassign button |
| `users.manage` | The Users page | The banner offers "Back to Dashboard" instead of "Back to Users" |

Today only the Admin role holds these. A future role that may only *view* workspaces (an
auditor, say) would see the workspace read-only, which a test simulates. Opening one's own
id (`/admin/users/{self}`) resolves to one's own workspace (SELF scope) and the banner says
"(you)".

## Selected-user lifecycle

1. **Open.** The layout validates the id (UUID or 404) and checks `workspace.view_any`.
   `UserWorkspaceFrame` then sends one `GET /api/v1/workspaces/{id}`, which authorises the
   access, writes the `workspace.accessed` audit event, and returns
   `{kind, subject: {id, full_name, status}}`: only what the banner needs, with no email,
   role, capabilities or session data. The module's own request runs in parallel, scoped by
   the same URL, so opening a workspace adds no extra round trip.
2. **Banner.** "Viewing CRM for: Rahul Sharma · Active" and "Signed in as Anita Rao. Changes
   you make here are recorded as yours." Until the API has named the user, the banner shows
   a placeholder, never a name. The banner is rendered by the layout, so it stays on every
   list, detail, create and edit page. It is a labelled region (`Workspace context`) with no
   heading of its own, so each page keeps its single `h1`.
3. **Navigate.** The navigation (the desktop rail, or the drawer on phones) labels its four
   modules "CRM for Rahul" ("CRM for Rahul Sharma" in the drawer) and links them inside the
   workspace; the header's "+" creates records in Rahul's workspace, and not at all while
   their account is invited or deactivated. The current module is marked with `aria-current="page"`, a bar and
   a heavier weight, and Users is not marked at the same time. Companies and Products don't
   exist. Settings stays the administrator's own profile.
4. **Leave.** "Back to Users" (or "Back to Dashboard" without `users.manage`), the sidebar's
   Users link, or the browser's Back button. Nothing about the selected user is stored
   outside the URL and the query cache: no browser storage, no global "selected user".
   Two tabs on two users stay independent.

## Deactivated and invited users

- A deactivated user's workspace **stays readable**: their leads, opportunities,
  activities and figures are history (ADR-0005). The banner shows "Deactivated" and explains
  that new records can't be added for them.
- New current work can't go to an inactive owner. The domain rules are unchanged: creating
  a lead in the workspace gives 400 *"This user's account isn't active, so new leads can't be
  added to their workspace."* Opportunities, tasks and meetings on their leads give 422
  *"This lead's owner is deactivated. Reassign the lead to an active user first."* The New lead
  page explains this up front instead of offering a form that can't be saved.
- Nothing is reactivated or reassigned automatically. Reassigning the user's leads to an
  active user is an explicit admin action (`crm.assign_any`).
- An **invited** user (who hasn't accepted the invitation) is treated the same way: readable,
  and unable to receive new work until activated.
- Restoring archived *open* work (an open deal, an open task, a scheduled meeting) is new
  current work too, so it is refused for an inactive owner in the same way. Restoring closed
  history is still allowed.
- The deactivation race (Anita writes in Rahul's workspace while another admin deactivates
  him) runs for real in `tests/security/test_admin_workspace_races.py`, with threads and
  separate connections. It covers 8 kinds of write (lead, opportunity, conversion, task,
  meeting, note, task reopen, deal reopen), both orders, and bursts of all of them. Either the
  write commits first and the deactivation waits, or the write waits and is refused with
  nothing written. No deadlock, and nothing is ever given to Rahul after his deactivation.
  `lock_assignable_user`'s `FOR SHARE` lock orders the race at the database (Phase 2). In the
  same tab, a deactivation, reactivation or rename on the Users page refreshes the banner
  at once.

## Cache isolation

Rahul's data must never appear under Priya's banner, not for one rendered frame. The
following measures ensure this:

- **Keys carry the workspace.** Every workspace-sensitive TanStack Query key contains the
  workspace segment (`["leads", "detail", "{rahulId}", leadId]`, `["dashboard", "{rahulId}"]`,
  `["workspace", "{rahulId}"]`, …). The only keys without one are workspace-independent
  configuration: lead options, pipelines, assignees.
- **Views are keyed by workspace.** `features/workspace/views.tsx` mounts every module view
  with `key={segment}` (and the record id). Moving from Rahul to Priya starts from fresh
  component state: no filters, cursors, drafts or open dialogs carry over.
- **Placeholders never cross workspaces.** List `placeholderData` keeps the previous page
  only when the previous query had the same workspace segment. The dashboard is never cached
  (`gcTime: 0`).
- **Late answers land in their own key.** A response for Rahul that arrives after the switch
  to Priya fills Rahul's cache entry, which Priya's screen doesn't observe.
- **Failures fail closed.** A failed request shows an error with a retry. It never shows
  another workspace's data and never falls back to `/workspaces/all` or `/me`. If the
  workspace description fails on first load, the frame shows the error instead of the page.
- **No browser storage.** The selected user lives only in the URL.

`frontend/src/features/workspace/admin-user-workspace.test.tsx` checks this with a fake
backend that marks every record of Rahul's and Priya's (`RAHUL-ONLY-LEAD`, ₹1,11,111,
`PRIYA-ONLY-TASK`, ₹8,88,888, …). A `MutationObserver` records every text node and link
that reaches the DOM after a switch. The scenarios cover all four modules: Rahul → Priya;
Rahul's answer arriving after Priya's; rapid Rahul → Priya → Rahul with answers in reverse
order; Priya's request failing; and Back/Forward across Users → Rahul Dashboard → Rahul Lead
→ Priya Dashboard → Priya Activity.

## Mutation invalidation

- **Writes into the cache** (`setQueryData` / `setQueriesData`) target only the current
  workspace's keys: the saved record's detail, and the boards and lists of the same
  workspace for a moved card or a completed task. A test completes Rahul's task with
  Priya's records cached and asserts that Priya's entries are byte-identical afterwards.
- **Invalidation** is deliberately broader (for example every lead query after a lead
  write). Invalidating only marks entries stale, so it can't put Rahul's data under Priya's
  key. It is needed because a change in Rahul's workspace also changes the organisation's
  view, and on reassignment the new owner's. Inactive stale entries refetch only when their
  page is opened again. The dashboard is always read afresh.
- **Reassignment out of the workspace** (Phase 4 behaviour, unchanged): the lead page
  returns to the workspace's Leads list (`/admin/users/{id}/leads`) with a notice. The lead's
  cached copy under this workspace is dropped once the page is gone. There is no 404 flash
  and no refetch loop (`features/leads/lead-detail.test.tsx`,
  `features/activities/review-regressions.test.tsx`).

## Audit behaviour

- `workspace.accessed` is written **at most once per actor and user per 15 minutes**
  (`WORKSPACE_ACCESS_AUDIT_WINDOW_S`). It is not written per component request. A test
  visits all four modules (21 requests) for Rahul, then Priya, then Rahul again, and finds
  exactly two rows. The window opens only after the row commits. With the cache down, every
  access is audited.
- Requests already in flight when a window opens can each write a row, because the
  marker is written only after the row commits. That means up to three rows: the Activities
  page sends three requests at once. The review measured this with simultaneous threads.
  On the live stack, with the window cleared, Users → Rahul's name → all four modules wrote
  one row, and a cold hard load plus reload of Priya's Activities also wrote one. Either way
  every row records a real access; nothing goes unaudited.
- Every write is audited individually by its domain service, with actor = admin and
  subject = user (table above).
- Log lines of a delegated request carry `user_id` (the actor) and `subject_user_id` (the
  selected user, or `all`), alongside the request id. CRM content (note text, descriptions,
  contact data, amounts) is never logged.

## Error behaviour

| Status | In a selected workspace |
|---|---|
| 401 | The session ended: the whole app reloads onto sign-in (dropping every cached record) and returns to the same URL afterwards (`safe-redirect.ts`) |
| 403 | "You don't have permission to do that." on the action; the page stays |
| 404 (workspace) | One "Page not found" page, identical for a missing user and one the admin may not open, with no banner and no module data |
| 404 (record) | "Page not found" with a way back to *this workspace's* module (for example "Back to Leads" → `/admin/users/{id}/leads`) |
| 409 | The module's existing conflict handling (reload the latest version, keep the user's edits) |
| 422 | The domain's message (for example the deactivated-owner rule) |
| 500, timeout, network | The error and a retry in place of the data. Never another workspace's data, never the organisation's |

## Security tests

| What | Where |
|---|---|
| Every workspace route × {anonymous, another sales user, the user themselves, admin}, plus admin × a user who doesn't exist: identical error bodies, 404 for anything the caller may not open | `backend/tests/security/test_admin_workspace.py` |
| Malformed and ambiguous workspace segments | same |
| Marked records: no response in Rahul's workspace (21 reads across four modules) contains anything of Priya's, and the reverse | same |
| Object substitution: every record route and method with Rahul's workspace and Priya's lead, opportunity or activity is indistinguishable from a missing record and changes nothing; all three activity kinds × every action; new work linked to Priya's records is refused | same |
| Actor vs subject for lead, opportunity, task, meeting and note writes, including audit and stage history; no payload field (owner, created_by, workspace, user_id, actor, role, capabilities, completed_by, version, id) moves a write out of the URL's workspace | same |
| Deactivation race, delegated-access audit window, no impersonation, query counts | same |
| Earlier cross-user suites (sales users, admin workspaces per module) | `tests/security/test_{leads,pipeline,activities,dashboard}_cross_user.py` |
| Frontend: name link, banner, sidebar, every page's links and API calls, create and edit flows, switch, slow-response race, rapid switching, failures, Back/Forward, deep link, tabs and storage, URL canonicalisation, deactivated users, view-only managers, mutation cache isolation | `frontend/src/features/workspace/admin-user-workspace.test.tsx`, `src/lib/workspace.test.ts`, `src/components/shell/*.test.tsx` |

## Query counts

Measured per request of an admin's visit to Rahul's workspace, after the first request has
opened the audit window. Each count is the same as in Rahul's own workspace plus the
workspace check (1 query). Counts don't grow with records (`test_admin_workspace.py` pins
them).

| Request | Queries |
|---|---|
| Users list (`GET /api/v1/admin/users`) | 3, constant in users and their records |
| Open the workspace (`GET /workspaces/{id}`) | 4 |
| Dashboard | 10 (session 2 + workspace 1 + snapshot and six bounded reads 7) |
| Leads list | 4 |
| Pipeline board | 8 |
| Activities list | 4 (plus 5 for the summary request) |

The first request of a 15-minute window adds one audit `INSERT`. Phase 6 changed no query
shape, so the Phase 5 benchmarks (1M leads, 2M activities) still apply.

## Live walkthrough

141 checks on the rebuilt containerised stack, in headless Chromium, as Anita (admin), Rahul,
Priya and a deactivated user. They cover:

- the admin journey through all four modules, with creates, edits, a stage move, task and
  meeting completion, and notes;
- the actor in the UI and in the audit trail;
- Back/Forward, refresh, deep links and two tabs;
- object and workspace substitution;
- encoded, upper-case and malformed workspace URLs, and the deactivated workspace;
- the stale-response race;
- 320, 375 and 768 px layouts and the mobile drawer;
- the container logs and the audit volume (one `workspace.accessed` row per user for the
  whole walkthrough).

Details are in [testing.md](testing.md#what-exists-after-phase-6).

## Defects found in Phase 6

| Severity | Defect | Fix |
|---|---|---|
| P1 | A percent-encoded spelling of a user id (`/admin/users/%66…/leads`) put the banner on Rahul (the layout's decoded param) while the views, reading the raw pathname, fell back to the organisation (`/api/v1/workspaces/all/leads`): every user's records under Rahul's name | `userIdFromPathname` decodes like Next.js and fails closed (`null`, never organisation or self); the frame renders only when the URL's and the layout's user agree, and replaces non-canonical spellings with the canonical URL. Regression tests fail on the old code |
| P3 | Inside a user's workspace the sidebar marked both the module and "Users" as the current page | Users is current only outside a workspace |
| P3 | "Page not found" for a record in a user's workspace linked to the organisation's Dashboard | `NotFoundView` takes a workspace-aware way back |
| P3 | In a deactivated user's workspace the lead form was offered, then failed with "Choose an active user." (there is nothing to choose there) | The form explains the rule up front; the service says why in that workspace |
| P3 | User names were links even for a manager without `workspace.view_any` (a dead 404 link), and their accessible name didn't say what they open | `UserWorkspaceLink`: a link only with the capability, named "{name}, open CRM workspace" |
| P3 | The workspace description was fetched by two separate implementations (frame and lead form) | One `useWorkspaceSubject` hook; the sidebar shares the same request |

Found by the independent review (three reviewers: API security, frontend, backend domain).
Each was reproduced, fixed, pinned by a regression test, and shown to fail without its fix
(`tests/security/test_phase6_review_regressions.py`, `test_admin_workspace.py`,
`test_admin_workspace_races.py`, `admin-user-workspace.test.tsx`):

| Severity | Defect | Fix |
|---|---|---|
| P1 | **Page cursors measured hidden rows** (since Phase 2/3). A cursor positioned by a private sort value (a lead's name, a deal's amount) re-read its boundary row outside the caller's scope. Someone could replay another user's `next` link (or one harvested before the row moved away) while moving their own record across the boundary, and binary-search the hidden amount or name. The reviewer recovered ₹23,45,678.90 in 60 requests | `core.keyset` re-reads the boundary row only through what the caller may list (the Leads and opportunity lists pass their scope). A boundary outside the scope makes the cursor invalid (400). Paging still follows one's own row when it changes stage or status |
| P1 (by the brief's one-frame rule; needs a race) | A notice naming Rahul's record ("… was converted") could appear under Priya's banner if the navigation it was meant for never landed (Back pressed during the request) | A notice is bound to the path it was sent to. Only that page shows it, and the first page to mount drops it either way |
| P2 | Reopening or restoring a closed deal its closer kept revealed whether the lead's new owner had archived the lead | "Lives elsewhere" is decided before the lead's archive state, and restore checks the archive state only in a workspace that holds the lead (as activities already did) |
| P2 | A reassignment that finished after its lead page was left cached the moved lead as this workspace's fresh copy (Forward showed it with live actions) | The sync drops the lead's entries for this workspace when no page is showing them |
| P3 | Restoring archived open work gave current work back to a deactivated owner | Refused like creating or reopening it |
| P3 | In a user's workspace the admin was told to "Ask an administrator" (reopening work whose lead moved on, adding work to a kept deal) | Delegated workspaces point to the organisation-wide view, where the same admin can do it |
| P3 | The banner and New lead form kept a stale status for up to 30 s after a deactivation in the same tab | User-management successes refresh the user's workspace description |
| P3 | Two workspace states had no `h1` (a workspace that can't be opened; a deactivated user's New lead) | Page headers added |
| P3 | Moving between two users' same module wasn't announced to screen readers (titles don't name users) | A polite live region announces "Viewing CRM for …" |
| P3 | An encoded section name (`%64ashboard`) marked Users as the current page | The active section is decoded like the route param |
| P3 | The docs said one or two audit rows open a window (it can be three); the "deactivation race" test ran in sequence | Docs corrected; real threaded race suite added |
