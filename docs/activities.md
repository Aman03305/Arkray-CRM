# Activities and timelines

**Built in Phase 4.** Code: [`backend/arkray/activities/`](../backend/arkray/activities/) and
[`frontend/src/features/activities/`](../frontend/src/features/activities/). Decisions:
[ADR-0009](adr/0009-unified-activity-model.md) (one table), [ADR-0020](adr/0020-activity-integrity.md)
(lead-bound, ownership, composite keys), [ADR-0021](adr/0021-materialized-timeline.md)
(the timeline) and [ADR-0027](adr/0027-leads-removed-from-the-ui.md) (Leads removed from the
UI: tasks and meetings are created from an opportunity; its lead is shown as the customer).

Phase 4 supports exactly three types: **task**, **meeting** and **note**. Call, email and
WhatsApp are prepared for (see [Adding a type](#adding-a-type)), not built.

## Model

```
Lead 1 ── * Activity * ── 0..1 Opportunity (of the same lead)
Lead 1 ── * TimelineEntry ── 0..1 Opportunity, 0..1 Activity     (append-only)
```

One table, `activities_activity`, with a `type` discriminator; which columns a type uses and
which statuses it may have are CHECK constraints (the matrix in
[database.md](database.md#activities_activity-built-phase-4)):

| Column | task | meeting | note |
|---|---|---|---|
| `title` (subject, ≤ 200) | required | required | empty |
| `description` (≤ 10,000) | optional description | optional agenda | **required**: the note |
| `status` | `open` → `completed` / `cancelled` | `scheduled` → `completed` / `cancelled` | none (NULL) |
| `priority` | `low` / `normal` (default) / `high` | — | — |
| `due_at` | optional (timestamp) | — | — |
| `starts_at`, `ends_at` | — | required; end after start; ≤ 24 h | — |
| `location`, `meeting_url` | — | optional; the link `https://` only, no credentials | — |
| `completed_at` + `completed_by` | set exactly while completed | same | never |
| `cancelled_at` + `cancelled_by` | set exactly while cancelled | same | never |

Common: `lead` (required, fixed), `opportunity` (optional, fixed, must be the lead's),
`owner` (the authorization key), `created_by` (the author; never changes), `archived_at`,
`version`, `created_at`, `updated_at`. Generated (stored): `current_owner_id` (the owner
while the activity is current work, else NULL), `schedule_sort` (a task's due time with "no
due date" as 9999-12-31, a meeting's start, a note's creation; NOT NULL, the one "when" for
sorting and date filters), `opportunity_key` (the opportunity, with "none" as a value, for
the timeline's composite key) and `created_sort` (a copy of `created_at` for the
organisation-wide sort only; see [Performance](#performance)).

Every activity is about a lead: from the lead's page, or from one of its opportunities
(the lead is then implied). Links never change; to fix a wrong link, archive and recreate.
Since ADR-0027 the UI has no lead pages: it creates activities about an opportunity only,
and shows the lead as the opportunity's customer; the API still accepts a lead alone.

Statuses are stable keys; labels are the UI's. Due dates are timestamps: the form asks for
a date and a time (pre-filled with 18:00, the end of the working day, in India time).

## Tasks

- Created **open**; priority `normal` unless chosen; due time optional.
- **Overdue** = open and `due_at` before now. Never stored: computed when read
  (`is_overdue` in every response, the `overdue=true` filter, the summary), because it
  changes by time passing alone. An undated task is never overdue.
- **Complete** (`POST …/complete {version}`): status `completed`, `completed_at` = now,
  `completed_by` = the actor (an admin completing Rahul's task is recorded as the admin; the
  task stays Rahul's). A task completion is not a customer contact.
- **Double completion** is a **no-op success** (200, unchanged, no audit or timeline entry,
  no version check): retries and double clicks are harmless, and the result is what both
  wanted. (Phase 3 precedent: moving to the current stage.)
- **Cancel** (`…/cancel`): status `cancelled` with who and when; cancelling twice is a no-op.
  Completed and cancelled exclude each other (422 "Reopen it first").
- **Reopen** (`…/reopen`): authorised users may reopen a completed or cancelled task. It
  returns to `open`, the completion or cancellation is cleared on the task, and the earlier
  completion stays in the timeline and the audit trail (history is never rewritten). Open
  work belongs to the lead's current owner (see [Ownership](#ownership)).
- **Edit** (`PATCH {version, …}`): subject, description, priority, due time; only while
  open. Completed and cancelled work is history (422 "Reopen it to make changes").

## Meetings

- Created **scheduled**, with start and end (end after start, at most 24 hours; refused:
  naive timestamps, malformed offsets, years outside 2000–2099). Stored in UTC, shown in
  India time.
- **Complete** records that the meeting took place, and is a customer interaction: the
  lead's last contact advances to the meeting's start (below). It is allowed only once the
  meeting has started (422 "This meeting hasn't started yet. Change its time…"), so a
  future meeting can never count as contact. The API says so in advance (`completable`).
- **Awaiting outcome**: a scheduled meeting whose end has passed (`is_overdue` for
  meetings): shown so it gets completed or cancelled.
- **Cancel**, **reopen** (back to scheduled; a cancelled meeting can be rescheduled by
  reopening and editing its times) and **edit** (only while scheduled) as for tasks. Changing
  a scheduled meeting's start or end is recorded on the timeline as *rescheduled*.
- No video-conferencing or calendar integration.

## Notes

- A note is its text (required) about a lead or one of its opportunities. No title, status,
  due date or times (CHECK-enforced). There is no `Lead.notes` or `Opportunity.notes`
  column: notes live in the activity and timeline system.
- Adding one is two steps on the opportunity page (its Notes tab): type, **Save note**.
- **Author vs owner**: `created_by` is the author, forever (an admin's note in Rahul's
  workspace says *written by the admin*). The note's owner is the lead's owner: a note is
  the lead's context and follows it on reassignment, so the new owner can read it, while the
  author shown never changes.
- **The author edits a note's text, and so may an administrator working in someone's
  workspace** (`crm.manage_any`; others get 403). The note then records **who edited it and
  when** (`edited_by`, `edited_at`) and shows "Edited by Anita Rao", so nobody's words are
  presented as someone else's; the audit has `note.updated` with the actor. Previous text is
  not kept (privacy-minimal, as audit never stores text). Anyone who may write in the
  workspace may **archive** a note (written by mistake, on the wrong lead).
- The deal page's **Notes** tab (`GET …/opportunities/{id}/notes`): whole text, author,
  edits, files and whether the caller may change each note; *+ Add note* with files.
- Note text is never logged, put in audit metadata, timeline data, exception messages,
  analytics or domain events. Lists and timelines carry a 240-character preview only.
  Embeddings for Ask Arkray are Phase 8 (the domain events below are the extension point).

## Attachments

Files on notes ([ADR-0026](adr/0026-user-pipelines-support-sessions-attachments.md);
`attachments.py`, `storage.py`).

- **Allowed types** (`ATTACHMENT_ALLOWED_EXTENSIONS`, from a catalog the server recognises
  by content): PDF, PNG, JPEG, WebP, DOCX, XLSX, CSV, TXT by default (GIF and PPTX can be
  enabled). A file must be what its extension says (magic bytes; OOXML packages with their
  main part and **no active content**: no macro project, OLE object (`*.bin` parts other
  than printer settings) or ActiveX control under any part name, no embedding other than
  Office documents and pictures, no macro, OLE or ActiveX content type declared, no external
  template; the zip directory plus its small XML parts are read, bounded; text is UTF-8
  without NUL bytes). Everything else is refused: executables, scripts (`.exe .dll .bat .cmd .ps1 .sh
  .js`), HTML, SVG, archives, macro documents, double extensions (`invoice.pdf.exe`).
- **Names** are display text only: path components dropped, control and bidi characters
  refused, ≤ 200 characters. Objects live under generated keys (`YYYY/MM/<uuid>`), never the
  uploaded name.
- **Limits**: `ATTACHMENT_MAX_BYTES` (10 MB; checked from Content-Length and again while
  reading: 413), `ATTACHMENT_MAX_PER_NOTE` (10; counted under the note's lock: 422), empty
  files refused. The proxy allows 11 MB on the upload route only.
- **Upload** (`POST …/activities/{note}/attachments`, the raw file, `X-Filename`
  percent-encoded): streamed in 64 KB chunks, hashed (SHA-256), spooled to a temporary file
  above 1 MB; then the note is locked (lead first: the lock order) and a row written
  (`uploading`, generated key); then the object stored (no lock held); then the row marked
  `stored` and `attachment.uploaded` audited (extension and a size bucket, never the name).
  Storage down: 503 `storage_unavailable`, the row `failed`; a crash in between leaves an
  `uploading` row the hourly housekeeping resolves (object deleted if present).
- **Who**: visible exactly when its note is; adding or deleting files changes the note, so
  the note's author or an administrator managing the workspace (403 otherwise).
- **Download** (`GET …/attachments/{id}/download`): the note is re-checked through the
  caller's scope on every request (knowing the id grants nothing; another workspace: 404);
  streamed with `Content-Disposition: attachment` (RFC 6266 `filename*`),
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`,
  `Cross-Origin-Resource-Policy: same-origin`, `Cache-Control: private, no-store`.
  **Preview** (`…/preview`): images only (PNG, JPEG, WebP, GIF, validated at upload), inline,
  same headers. Never HTML or SVG as active content.
- **Virus scanning** (`ATTACHMENT_SCANNER`): none configured — files are `not_scanned` and
  downloadable (the UI says nothing extra); `clamd://host:3310` — files are `pending` until a
  job scans them over clamd's INSTREAM protocol: `clean` files download, `rejected` ones are
  blocked and their object deleted (`attachment.rejected`). A scanner outage keeps files
  pending (the job retries, then goes dead: an alert). No pretend scanner. **Changing the
  setting**: configuring a scanner later queues earlier files for scanning (500 per hourly
  housekeeping run; downloadable once clean); removing it releases files still waiting
  ("not scanned").
- **Delete** (`DELETE …/attachments/{id}`): hidden at once (`deleted_at`, `deleted_by`,
  `attachment.deleted`), the object removed by a job and, failing that, the hourly
  housekeeping (`activities.housekeeping`); idempotent (a missing object counts as removed).
  Once the object is gone the file's **name and content hash go too** (the name becomes
  "removed"; type, size and who/when stay as evidence), likewise for a blocked file and an
  upload that never finished, and the purge is recorded in the erasure ledger. A legal hold
  on the lead defers the purge; `ATTACHMENT_S3_PURGE_VERSIONS` removes a versioned bucket's
  earlier versions too (privacy remediation; [privacy.md](privacy.md#attachments)).
- **Not indexed** for search or Ask Arkray: file contents never reach a language model.
- **Erasure** (`erase_lead`): the files of the lead's notes are deleted, names and content
  hashes blanked, objects queued for removal. An upload still being written when the lead is
  erased is removed when it finishes (the purge leaves uploading rows alone; the upload then
  finds its row deleted, queues the purge and answers 404). Afterwards the lead is archived:
  **an archived lead's notes take no new files or text** (422), so nothing personal can be
  added beyond the erasure's reach.

## Ownership

| Who creates | In workspace | The activity's owner | `created_by` | Needs |
|---|---|---|---|---|
| a salesperson | own (`me`) | the lead's owner = themselves | them | `crm.access_own` |
| an administrator | Rahul's (`{id}`) | Rahul (the lead's owner) | the admin | `crm.manage_any` |
| an administrator | organisation (`all`) | the lead's owner | the admin | `crm.manage_any` |

Nobody chooses an owner: the API has no `owner` field (a 400), and the services have no
parameter for one. Current work is always the lead's owner's, which PostgreSQL enforces
([ADR-0020](adr/0020-activity-integrity.md)). A lead owned by a deactivated user takes no new
activities (422 "…Change the owner to an active user first.": the deal's *Change owner*).

**Historical attribution never changes**: `created_by`, `completed_by`, `cancelled_by`,
timeline actors and audit actors stay as they were, through reassignments, renames and
reopening.

## Lead reassignment

`LeadReassigned` is the one mechanism (Phase 2 publishes it; Phase 3's pipeline and Phase 4's
activities subscribe). In **one transaction**, Lead A → B:

| Record | Follows to B? |
|---|---|
| the lead | yes (the reassignment itself) |
| open opportunities (pipeline) | yes |
| **open tasks** (archived ones too) | **yes** |
| **scheduled meetings** | **yes** |
| **notes** | **yes** (author unchanged) |
| completed / cancelled tasks and meetings | no: they stay with whoever had them |
| won / lost opportunities | no |

Moved activities get a new version, one `{type}.owner_changed` audit event each (actor = the
admin, subject = the previous owner, reason `lead_reassigned`) and an `ActivityOwnerChanged`
event; the lead's timeline records one `lead.reassigned` entry. Constant queries however
much moves (one lock query, one update, one audit insert). If anything fails, everything
rolls back (tested with a failing later subscriber). There is no eventual consistency:
nothing goes through Celery. The database refuses to commit current work left with the
previous owner.

Consequence of owner-based visibility: after the move B sees the lead, its notes and open
work, and the lead's own history; A keeps seeing the meetings A completed (the lead shown as
*in another workspace*); administrators see everything organisation-wide.

## Opportunity integration

- An activity on an opportunity is also about the opportunity's lead (`lead_id` derived;
  `(opportunity_id, lead_id)` is a composite foreign key onto the opportunity).
- Sending a lead and an opportunity of another lead is a 400; someone else's lead or
  opportunity is a 404 (non-enumerating, below).
- Activities may follow up closed opportunities (a thank-you call after a win). New work on
  a closed opportunity that its closer kept after the lead moved on would belong to the
  lead's new owner, so the closer is refused (422 "…ask an administrator").
- Open work on an open opportunity follows the lead with it: never "lead B, open
  opportunity B, open task A".
- A deactivated owner takes no new, reopened or restored current work: restoring an
  archived open task or scheduled meeting is refused (422) like creating one (Phase 6
  review). Closed work stays restorable.
- An archived lead or opportunity takes no new, reopened or restored work (422). Its open
  tasks and meetings can still be edited, completed or cancelled, like its open
  opportunities: archiving a lead doesn't strand work someone is finishing (decided in the
  Phase 4 review, D-5; tested).

## Last contacted

`Lead.last_contacted_at` means **the last completed customer interaction**. Phase 4 rule
(conservative):

| Event | Updates last contact? |
|---|---|
| a meeting **completed** | **yes**: to the meeting's **start time** |
| a meeting scheduled (even in the past), edited, cancelled, reopened | no |
| a task created or completed | no |
| a note added or edited | no |
| a manual edit on the lead (Phase 2 field) | yes, as typed: an explicit correction |

- **MAX semantics**: completing an older meeting after a newer one never moves it back
  (1 Oct, then 3 Oct, then a 25 Sep meeting: it stays 3 Oct). Implemented by
  `leads.services.record_contact`, under the lead's lock (a no-op if not later).
- When it changes, the lead's version is bumped (an edit form opened earlier gets a 409,
  never silently overwrites it), `lead.updated {fields: [last_contacted_at], via:
  meeting_completed, via_id}` is audited and `LeadUpdated` published.
- Reopening a completed meeting keeps the recorded contact (it may have happened; the lead's
  last contact can be corrected on the lead).
- Only authorised domain operations change it: completing someone else's meeting is a 404
  and changes nothing (tested).

## Timeline

Design: [ADR-0021](adr/0021-materialized-timeline.md). An append-only table,
`activities_timeline_entry`, written **in the transaction of each change**:

| Kind | Written by | Snapshot (`details`) |
|---|---|---|
| `lead.created`, `.status_changed`, `.reassigned`, `.archived`, `.restored` | subscribers to the lead events | status key and name; previous and new owner |
| `opportunity.created` (incl. by conversion), `.stage_changed`, `.won`, `.lost`, `.reopened` | subscribers to the pipeline events | stage names as they were; conversion flag |
| `task.created`, `.completed`, `.cancelled`, `.reopened` | activity services | — |
| `meeting.scheduled`, `.rescheduled`, `.completed`, `.cancelled`, `.reopened` | activity services | times when scheduled / rescheduled |
| `note.added` | activity services | — |

- Nothing is fabricated: every entry records something that happened, written by the code
  that did it. Lead edits stay in the audit log (not timeline events). Pre-Phase-4 history
  was backfilled from the audit trail and the stage history (`activities.0003`).
- **Historical integrity**: stage and status names are snapshots (renaming a stage doesn't
  change the past); people are stable ids shown by their current name; the actor is whoever
  acted (the admin, not the workspace owner). Titles and note text are read from the live
  record, so an archived or edited note never lives on in the timeline.
- **Lead timeline** (`GET …/leads/{id}/timeline`): lead events, the lead's opportunities'
  events and its activities, newest first. **Opportunity timeline**
  (`GET …/opportunities/{id}/timeline`): the opportunity's own events and its activities
  (no lead events).
- **Visibility, decided when read** (one SQL query): the lead (or opportunity) must be
  visible (404 otherwise); lead events are visible to whoever sees the lead; an activity's
  or opportunity's entry only while that record is in the same scope and not archived.
  A visible task on an opportunity the reader can't see shows the opportunity as
  restricted (`{"id": null, "restricted": true}`).

## Pagination

Signed composite keyset cursors ([ADR-0016](adr/0016-composite-keyset-pagination.md)).
Activities: `-created_at` (default), `created_at`, `scheduled` (due/start soonest first,
undated last), `-scheduled`; 1–100 per page. Timelines: `(occurred_at, id)` newest first,
1–50 per page (default 20). Malformed, forged or other-ordering cursors are a 400; cursors
carry only timestamps and ids (no titles or text); the scope always comes from the URL,
so a replayed cursor can't widen what anyone sees (tested across workspaces). Responses
carry no counts.

## API

All under `/api/v1/workspaces/{me|all|user_id}/`, every route in `tests/authz_matrix.py`:

| Route | |
|---|---|
| `GET activities` | list: `type`, `status`, `lead`, `opportunity`, `owner` (organisation-wide only), `date_from`/`date_to` (inclusive business dates of the due time / start / creation), `overdue`, `current` (open tasks and scheduled meetings), `upcoming` (current work from now on; undated tasks included), `cancelled` (`false` leaves cancelled tasks and meetings out, `true` keeps only them), `archived`, `ordering`, `cursor`, `page_size`. Unknown parameters and contradictions (`type=meeting&status=open`, `upcoming` with `overdue`, `cancelled` with `status`) are a 400 |
| `POST activities` | create `{type, lead?, opportunity?, …the type's fields}` (`Idempotency-Key` supported) |
| `GET`/`PATCH activities/{id}` | detail; edit `{version, …the type's fields}` |
| `POST activities/{id}/complete` · `cancel` · `reopen` · `archive` · `restore` | `{version}` |
| `GET activity-summary` | open tasks, tasks due today, overdue tasks, meetings today, upcoming meetings |
| `GET leads/{id}/timeline`, `GET opportunities/{id}/timeline` | `cursor`, `page_size` |

Input is strict: undeclared keys are a 400 (`owner`, `created_by`, `status`,
`completed_at`, `version` in a create, `type`/`lead` in an edit, `is_superuser`, …), and each
type accepts only its own fields (a task with a `location` is a 400, not an ignored key).
Output is an explicit allowlist: people as `{id, full_name, is_active}`, related records
only when visible, text previews in lists, `is_overdue` and `completable` computed with the
server's clock.

## Idempotency

| Operation | Protection |
|---|---|
| create task / meeting / note | `Idempotency-Key` (existing `core.idempotency`, operation `activities.create`): a retry replays the first result; the UI derives one key per identical request body |
| complete, cancel, reopen, archive, restore | naturally idempotent: repeating a change that already happened is a no-op success |
| edit | the version: a duplicate submit after a success is a 409, and the UI's busy button prevents it |

## Authorization

The same four layers as everything else ([authorization.md](authorization.md)): signed-in
user, capability, workspace → `AccessScope`, object lookups through `scope.apply()` (404
outside). Writes call `identity.workspaces.authorize_write` (own: `crm.access_own`; another
user's or `all`: `crm.manage_any`). Services apply the scope themselves, so a direct call is
as constrained as HTTP.

- **Relationship fields are scope-aware and non-enumerating**: linking your task to someone
  else's lead or opportunity answers exactly like a lead or opportunity that doesn't exist
  (404, identical bodies; tested). It can't be used to confirm that a record exists.
- **Counts are scoped like rows**: the summary is computed from `scope.apply()` first, and
  is identical whether or not other users have records (tested).
- Timelines: see [Visibility](#timeline). The cross-user suite is
  [`tests/security/test_activities_cross_user.py`](../backend/tests/security/test_activities_cross_user.py)
  (131 cases, both directions).

## Lock order

Extends [ADR-0018](adr/0018-pipeline-integrity-by-composite-keys.md). Every operation takes
its locks in this order, so no two can wait for each other in a cycle:

1. **the lead**: `FOR NO KEY UPDATE` (every activity write, every opportunity write, and the
   last-contact update a meeting completion makes: a non-key update under the same lock,
   never an upgrade), or `FOR UPDATE` from the start when the operation changes the lead's
   owner or status (reassignment, conversion);
2. **opportunities**: `FOR NO KEY UPDATE OF` the opportunity only, several in ascending id
   order. Activity writes never lock opportunities (their owner and archive state only
   change under the lead's lock, which they hold);
3. **activities**: `FOR NO KEY UPDATE OF` the activity only, several in ascending id order
   (the reassignment subscriber, which runs after the pipeline's: pinned by a test);
4. **user rows**: `FOR SHARE` (`lock_assignable_user`), leaf locks;
5. inserts: timeline entries, audit, idempotency records.

An activity's `lead_id` is read first (unlocked: it never changes), then the lead is locked,
then the activity is locked through the scope and re-checked. Configuration rows (stages,
statuses) are never locked. A deliberate mutation (locking the activity before its lead)
made the concurrency suite report `deadlock detected` in the reassignment races and the
storm test.

## Concurrency

Every change requires the version the client saw (409 otherwise) and bumps it.

| Race (real threads, own connections) | Outcome |
|---|---|
| complete × complete (×3) | all succeed (repeats are no-ops); one completion, one timeline entry, one audit event |
| edit × complete, cancel × complete, edit × cancel (tasks and meetings) | one wins, the other 409; exactly one change applied |
| note edit × lead reassignment | the edit lands then the note moves, or the edit is 404; never lost, author unchanged |
| task / meeting / note creation × reassignment | created then moved to the new owner, or 404 |
| creation × the opportunity closing | both succeed |
| completion × reassignment | completed first (stays with the person who did it), or 404 and the open task moves |
| reassignment holding the lead's lock × meeting scheduling | the scheduling waits, then 404; no deadlock |
| older × newer meeting completions (4 at once, shuffled, 4 rounds) | last contact = the newest meeting's start, always |
| a storm of 8 mixed writers × 3 rounds on one lead (creates, completions, cancels, reassignment, an opportunity edit) | no deadlock; current work always with the lead's owner |
| two users on their own leads, both holding their locks at once | both succeed |
| the same create ×3 with one `Idempotency-Key` | one activity, one timeline entry |
| reopen × reassignment | the reopened task ends with the new owner |

## Audit and domain events

| Operation | Audit (`target_type=activity`) | Metadata (never text) | Event |
|---|---|---|---|
| create | `task.created` / `meeting.created` / `note.created` | workspace, owner, lead, opportunity | `ActivityCreated` |
| edit | `{type}.updated` | changed field **names** | `ActivityUpdated` |
| complete / cancel / reopen | `{type}.completed` / `.cancelled` / `.reopened` | (reopen: previous status) | `ActivityStatusChanged` |
| archive / restore | `{type}.archived` / `.restored` | — | `ActivityArchived` / `ActivityRestored` |
| moved with its lead, or reopened for the lead's new owner | `{type}.owner_changed` | from/to owner, lead, reason | `ActivityOwnerChanged` |
| a meeting completion advancing the lead's last contact | `lead.updated` (on the lead) | `fields`, `via`, `via_id` | `LeadUpdated` |

Actor = whoever acted; subject = the owner when that is someone else. No-ops and refusals
write nothing. Events carry identifiers, types and status keys only. They have **no
subscribers in Phase 4** (the timeline is written directly): the consumer is Ask Arkray
(Phase 8), which re-indexes notes and descriptions when they are created, edited, moved,
archived or restored ([rag-architecture.md](rag-architecture.md#indexing-pipeline)).

## Dashboard figures (for Phase 5)

`selectors.activity_summary(scope, now=…)` (two aggregate queries since Phase 5),
`selectors.upcoming_meetings(scope, now=…, limit=5)` and (added in Phase 5)
`selectors.next_open_tasks(scope, now=…, limit=5)` (the first rows of the Tasks tab: open,
soonest due first) are the authoritative definitions; the Phase 5 dashboard uses them
unchanged ([dashboard.md](dashboard.md)):

| Figure | Definition |
|---|---|
| open tasks | open, not archived |
| tasks due today | open, due during today's business day (earlier today included: also overdue) |
| overdue tasks | open, due before now |
| meetings today | scheduled or completed, starting during today's business day |
| upcoming meetings | scheduled, starting now or later |

"Today" is the business day in `CRM_TIME_ZONE` (`core.business_time`, [ADR-0012](adr/0012-business-day-time-zone.md)):
00:15 IST on 3 October belongs to 3 October even though UTC is still 2 October (tested at
the boundary). No separate analytics store. The Activities page shows the same figures as
shortcuts (`GET …/activity-summary`), and each shortcut opens a list of exactly the activities its
figure counts (meetings today: `cancelled=false` and today's dates; upcoming meetings:
`upcoming=true`; tested against the selector, figure by figure).

## Frontend

| Route | Workspace |
|---|---|
| `/activities`, `/activities/{id}` | own (sales users); organisation-wide (admins) |
| `/admin/users/{id}/activities`, `…/activities/{activityId}` | that user's, under the "Viewing CRM for" banner |

- One implementation: the route files render the shared views; the workspace comes from the
  URL. Views are keyed by workspace (and activity), and every query key starts with
  `["activities" | "timeline", kind, <workspace>]`; "keep the previous data while loading"
  applies only within the same workspace. Tested: Rahul's activities → Priya's, Back, Rahul's
  opportunity timeline → Priya's: nothing of Rahul's ever shows. A timeline refetch the
  server refuses (403/404) removes the entries from the screen.
- **Activities page**: summary shortcuts (open, overdue, due today, today's meetings,
  upcoming), tabs All / Tasks / Meetings / Notes (each starting from a sensible status and
  sort), status and sort, and under "More filters" the date range (an inverted range is
  explained and never sent), opportunity (the opportunity picker below), owner
  (organisation-wide) and the archived view. Filters
  live in memory per workspace (never in the URL or storage). Table on tablets and desktops,
  cards on phones; type, status, "Overdue" and "Awaiting outcome" are written out. Complete
  is a button on each row (keyboard and touch, using the row's version; a 409 is explained
  and the list reloads); cancel and archive ask first. After any write, the activity's row
  is updated wherever this workspace's lists and open-work cards hold it, so a list shown
  again before its reload offers the right actions with the right version; a row's menu
  doesn't open while an action on it runs. A scheduled meeting shows Complete within 30 s
  of its start on a page left open (one shared clock; the server decides).
- **Calendar** (the first of the page's tabs; the list stays the default): tasks at their
  due time and meetings from start to end, by month, week (Monday to Sunday) or day, in
  India time like every time on screen (a meeting running past midnight is drawn to the end
  of its day). Cancelled and archived ones are left out; completed ones stay, struck through;
  overdue tasks and meetings awaiting an outcome are marked in words and by icon, not colour
  alone. Organisation-wide, *My calendar* narrows it to the viewer's own and *Everyone*
  (the default) names each entry's owner. A month cell lists three entries, or two and
  "+N more", which opens the day; on phones a cell shows dots and opens the day. Each day's
  **+** (and, with a mouse, a click on an empty part of a day, or on a time in the week and
  day views, to the half hour) opens the task/meeting dialog prefilled for then (a whole day:
  a meeting at 9:00 for 30 minutes, a task due at 18:00) with a Meeting / Task switch that
  keeps what was typed; a prefilled form closes without asking. An entry opens its activity.
  It reads the list API per type (`type`, `date_from`/`date_to` = the days on screen,
  `cancelled=false`, `ordering=scheduled`, `page_size=100`, `owner` for My calendar),
  following `next` with the same parameters, at most 5 pages per type; beyond that it says
  the period is cut short and suggests a narrower one. Its query key is under
  `["activities", "calendar", <workspace>]`, so every activity write marks it stale. The
  view and the calendar's position are remembered per workspace in memory, like the
  filters; a summary shortcut or a dashboard figure shows the list.
- **+ Task / + Meeting** open a dialog (from the Activities page or the calendar: an
  opportunity picker limited to this workspace, required: recent open opportunities, or any
  found by typing its title or customer, through global search; from an opportunity: that
  one, fixed. Then due date and time or start and end in India time, priority,
  location, https link, description/agenda). Errors sit under their fields and focus moves
  to the first; an identical retry reuses its idempotency key; an edit conflict keeps the
  typing and offers "Apply my changes to the latest version" or "Discard"; a refused create
  shows the server's reason. Moving a meeting's start moves its end (the length is kept).
  Escape, a click outside, Close or Cancel with unsaved typing asks "Discard what you
  typed?" first.
- **Opportunity page**: *Open work* (open tasks and scheduled meetings with Complete, + Task,
  + Meeting), its *Timeline* (History tab) and its notes (Notes tab). Activity rows, pages and
  the dashboard name the customer as text (there is no customer page to link to).
- **Activity page**: details, record (created by, completed/cancelled by and when), actions
  (complete, cancel, reopen, edit, archive/restore); meeting links open in a new tab with
  `rel="noopener noreferrer"`; a note is edited in place by its author only, and a conflict
  keeps the text on screen with the other version underneath (or, if the note was archived
  meanwhile, says so and disables saving). Note boxes are read-only while a save runs. A
  refetch refused with 403 removes the activity from the screen.
- Accessibility: labelled controls, errors associated with inputs, focus on the first
  invalid field, dialogs trap focus and return it, Escape closes, the timeline is an ordered
  list with `<time>` elements, nothing relies on colour alone. When an action removes the
  focused control, focus moves to what replaced it: the message saying what happened
  (Complete, Reopen), "Edit note" after saving a note, the first entry "Show older" added.
  Controls repeated per row are told apart ("Actions for note: Prefers morning calls";
  "New task", "New meeting").
- After *Change owner* hands a deal out of the user's workspace being viewed, the deal page
  marks that workspace's data stale without refetching what it shows (a 404 that flashed up
  as an error in the Phase 6 live walkthrough of lead reassignment) and goes to its Pipeline.

## Performance

Query counts per request are pinned (identical at 10 and 100 rows,
[`test_query_counts.py`](../backend/arkray/activities/tests/test_query_counts.py)):

| Endpoint | Queries |
|---|---|
| list (own / organisation), any filter or sort, following a cursor (both directions) | 3: session, user, activities (lead, opportunity, owner, author joined, text cut to a preview in SQL) |
| list in a user's workspace | 4 (+ the workspace check) |
| detail | 3 |
| summary | 4 (two aggregates since Phase 5: open tasks; meetings from today on) |
| lead timeline | 5: session, user, lead visibility, entries (actor, activity, opportunity joined), the people named in the page (one query for all; 4 when nobody is named) |
| opportunity timeline | 4 |
| create a task | 10, whatever else exists |
| complete a task | 11; a meeting 14 (+ the last-contact update and its audit) |
| lead reassignment moving 1 or 25 tasks and 1 or 25 notes | 12 either way |

Benchmark ([`tests/performance/bench_activities.py`](../backend/tests/performance/bench_activities.py)):
EXPLAIN ANALYZE of the exact SQL the selectors run, best of three, PostgreSQL 16 in Docker on
a laptop. **403k**: 403,000 activities (180,000 tasks, 120,000 meetings, 103,000 notes) over
100,000 leads and 60 owners (the heaviest with 29,800), 50,000 opportunities, 733,000
timeline entries, one lead with 3,000 notes. **2M**: the same plus four older 60-day periods
of closed history (1,998,067 activities; the heaviest owner 136,696), the performance
reviewer's method for testing growth.

| Query | Heaviest owner 403k / 2M | Typical owner 403k / 2M | Organisation 403k / 2M |
|---|---|---|---|
| all, newest / oldest first | 0.17–0.21 / 0.20–0.22 ms | 0.18–0.21 / 0.23–0.24 ms | 0.28–0.31 / 0.26–0.27 ms |
| all, by due/start; one week by due/start | 0.15–0.20 / 0.11–0.26 ms | 0.13–0.14 / 0.15–0.23 ms | 0.23 / 0.35–0.36 ms |
| open tasks, soonest / latest due; overdue; scheduled meetings | 0.21–0.28 / 0.14–0.20 ms | 0.15–0.20 / 0.15–0.21 ms | 0.28–0.40 / 0.30–0.57 ms |
| one type of any status by due/start | 0.34–0.35 / 0.20–0.30 ms | 0.19 / 0.21–0.23 ms | 0.34–0.35 / 0.34–0.49 ms |
| open and scheduled work; upcoming meetings list | 0.17–0.25 / 0.16–0.19 ms | 0.13–0.17 / 0.16 ms | 0.22–0.37 / 0.25–0.73 ms |
| completed tasks, notes, archived: newest first | 0.28–1.06 / 0.20–0.56 ms | 0.22–0.52 / 0.21–0.71 ms | 0.28–0.36 / 0.34–1.06 ms |
| rare or old matches, newest first (cancelled meetings, archived notes, a week 50 days ago) | 0.06–2.46 / 0.04–1.98 ms | 0.03–1.71 / 0.04–1.64 ms | 0.03–0.68 / 0.06–1.52 ms |
| an administrator's owner filter, newest / oldest | — | — | 0.15–0.27 / 0.29–0.39 ms |
| summary (one aggregate, Phase 4) | 4.1 / 12.1 ms | 0.9 / 4.0 ms | 25 / 31 ms |
| summary (two aggregates, Phase 5; 2M) | 0.8 ms | 0.03 ms | 12 ms |
| upcoming meetings (5) | 0.07 / 0.08 ms | 0.07 ms | 0.11–0.12 ms |
| deep cursor (3,000 rows in) | 0.40 / 0.49 ms | 0.21–0.23 ms | 2.4–2.5 ms |
| a lead's activities / its open work / an opportunity's | — | — | 0.04–0.08 / 0.05–0.13 ms |
| timeline: typical lead, 3,000-note lead (first page, page 125), opportunity | — | — | 0.08–0.11 / 0.09–0.15 ms |
| reassignment's lock query; the ownership key's check on a lead owner change | — | — | 0.02 / 0.04 ms; 0.43 / 0.48 ms |

What the benchmarks changed (each pinned by
[`test_activity_query_plans.py`](../backend/tests/performance/test_activity_query_plans.py),
which forbids sequential scans and sorts and names the index each shape must use):

- Lists by due/start without a status filter took 9–24 ms per owner and **261 ms**
  organisation-wide (bitmap or sequential scan plus a sort) before `activities_owner_when_idx`
  and `activities_when_idx` (migration 0004).
- **Performance review, P1**: newest/oldest-first lists of one owner could be served by
  walking the organisation-wide created index and filtering: 48 ms at 403k, 380–500 ms at 2M
  for an owner whose matches were rare or old (cancelled meetings, archived notes, a past
  week). The organisation-wide orderings now sort by their own generated column
  (`created_sort`, index `activities_created_idx`), used only when the list is
  organisation-wide with no owner, lead or opportunity filter; every narrower list sorts by
  `created_at`, which only the owner, lead and opportunity indexes cover. The choice is
  deterministic, not left to statistics. Extended statistics on `(owner_id, type, status)`
  and `(type, status)` (migration 0006) fix the planner's estimates for rare combinations.
- The lead index became `(lead, schedule_sort, id)` (a lead's open work in order; 64 ms
  organisation-wide with the lead filter before); the organisation-wide created index is
  ascending (rows arrive newest last; a descending index split pages on every insert: 28 MB
  against 15 MB at 400k).
- **At 2M** (found re-benchmarking the review fixes): open-and-scheduled work and one type
  of any status by due/start walked the all-statuses schedule index past the closed history
  (124 ms and 38 ms for the heaviest owner, 0.5 s organisation-wide). Partial indexes on
  current work only (`activities_owner_current_idx`, `activities_current_idx`: open tasks
  and scheduled meetings, a set that doesn't grow with history) and `(owner, type,
  schedule_sort)` / `(type, schedule_sort)` indexes (migration 0007) bring them to 0.2–0.7 ms.
  The "current" filter is written as `status IN ('open', 'scheduled')`, exactly the indexes'
  condition (equivalent by `activities_activity_status_valid`).
- The summary only reads meetings from today's start on (a past meeting can't be "today" or
  "upcoming"): organisation-wide it was a sequential scan (34 ms at 403k); it now follows
  open work (25 ms at 403k, 31 ms at 2M, five times the rows). One owner's summary reads
  that owner's entries of `activities_owner_sched_idx` (4 ms / 12 ms for the heaviest).

- **Phase 5 performance review (P2):** the one aggregate over "open task OR meeting from
  today" still read the heaviest owner's whole history (13–25 ms) and, with 20,000 more
  open tasks and 20,000 stale scheduled meetings, flipped organisation-wide to a sequential
  scan (135 ms; 154–243 ms with JIT). It is now two aggregates, open tasks and meetings from
  today's start on, each an index-only range of the schedule index: 0.8 ms / 12 ms at 2M,
  4.8 ms / 70 ms on the grown copy, figures identical
  ([dashboard.md](dashboard.md#what-the-benchmark-and-the-review-changed)).

Risk R44 is resolved by that split. Index count: 12 on `activities_activity` besides the primary and
timeline keys, nine of them partial (archived rows left out; the current-work ones hold open
work only). Writes are human-paced, so the extra index maintenance per write is cheap.

## Reliability

Activities depend on PostgreSQL only. With Redis down (the cache) or the broker down, creating,
completing, listing, the summary and timelines keep working and nothing is queued (tested in
`tests/security/test_outages.py`); AI is not involved.

## Adding a type

Call, email or WhatsApp: an `ActivityType` value; its statuses and columns (for example
`direction`, `duration_seconds`) with NULL-safe CHECKs in one migration; a `TypeSpec` in
[`spec.py`](../backend/arkray/activities/spec.py) (fields, required fields, initial status,
timeline kinds); timeline kinds and renderers; whether completing it counts as contact.
No new tables and no API redesign.
