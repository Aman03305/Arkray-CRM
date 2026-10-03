# Leads

**Built in Phase 2.** Code: [`backend/arkray/leads/`](../backend/arkray/leads/) and
[`frontend/src/features/leads/`](../frontend/src/features/leads/). Decisions:
[ADR-0015](adr/0015-leads-domain-model.md) (model, configurable statuses and sources),
[ADR-0016](adr/0016-composite-keyset-pagination.md) (pagination),
[ADR-0017](adr/0017-in-transaction-domain-events.md) (domain events).

## What a lead is

A lead is **the** sales-prospect record of Arkray CRM: a person, an organisation, or a
person at an organisation that a salesperson is working with. There is deliberately **no
Company, Account, Contact or Product entity**. `organization_name` is a plain text
attribute of the lead. It has nothing to do with Arkray's own users, roles or tenancy
(the "organisation-wide" workspace means *all of Arkray's users*, not a customer company).

Opportunities (Phase 3, [pipeline.md](pipeline.md)) hang off leads: a lead has zero, one
or many; activities (Phase 4) will too.

## Fields

| Field | Rules |
|---|---|
| `first_name`, `last_name` | optional each, up to 100 characters; see [Names](#names) |
| `organization_name` | optional, up to 200 |
| `job_title` | optional, up to 100 |
| `email` | optional; contact data, not a sign-in identity: syntax-checked, stored as typed (trimmed), compared case-insensitively, international domains accepted, never unique |
| `phone`, `mobile`, `alternate_phone` | optional, up to 40 characters; see [Phone numbers](#phone-numbers) |
| `address_line_1`, `address_line_2`, `city`, `state` | optional, one line each |
| `postal_code` | optional; letters, digits, spaces and hyphens (400001, SW1A 1AA, 12345-6789) |
| `country` | optional ISO 3166-1 alpha-2 code (`IN`); names are rendered by the browser |
| `status` | required; a status key (default `new`); changed only by the status action |
| `source` | optional source key |
| `rating` | optional `hot`, `warm` or `cold`: the salesperson's own judgement, **not** a computed or AI score (none exists in Phase 2) |
| `owner` | required; the one ownership concept ("owner" = "assigned to"); changed only by reassignment |
| `created_by` | set once; provenance (for example the admin who created a lead for a salesperson) |
| `last_contacted_at` | optional timestamp entered by the user; not in the future (5 min skew allowed), not before 2000; Phase 4 activities will also advance it |
| `description` | optional multi-line text, up to 5,000 characters |
| `archived_at` | set by archive, cleared by restore |
| `version` | optimistic-concurrency counter |
| `created_at`, `updated_at` | timestamps (UTC; shown in Asia/Kolkata) |
| `display_name` | **derived by PostgreSQL** (generated column): "first last", or the organisation for an organisation-only lead. Never writable, so it can't disagree with its parts |

### Deviations from the suggested field list

- `full_name` is not stored as an editable field; `display_name` is a generated column
  (needed for sorting by name and cursors), so there is one source of truth.
- `status` and `source` reference small configuration tables by immutable key instead of
  being free text or code enums ([Statuses](#statuses-sources-and-ratings)).
- `converted_at` (in the Phase 0 draft) is not stored: conversion is defined by
  opportunities (Phase 3, [ADR-0019](adr/0019-lead-conversion.md)), and the audit trail
  records every conversion and status change with its time.
- **Notes and the lead timeline** (planned for Phase 2 in the Phase 0 draft) move to
  Phase 4, where notes are activities and the timeline is built with them. Until then the
  lead page reserves the space ("Activities will appear here.") without inventing entries,
  and the audit log keeps the history (created, edited, status, reassignment, archive).
- CSV import/export (listed for Phase 2 in the Phase 0 security draft) is not part of this
  phase's brief and is deferred; its controls stay designed in [security.md](security.md).

## Names

People's names don't fit a Western first/last pattern. Rules:

- Both name fields are optional; **a lead needs a person's name, an organisation, or both**
  (a database CHECK plus a field error on `first_name`). A mononymous person uses either
  field; family-name-first names are entered as the person writes them.
- Any script is accepted (Devanagari, Tamil, Chinese, Arabic, accented Latin, ...).
  Input is normalised to Unicode NFC, runs of whitespace are collapsed and trimmed.
- Refused, not silently dropped, and checked before anything is collapsed: control
  characters other than tab/newline; format characters (bidirectional embeddings, overrides
  and isolates that make text display differently from how it is stored, zero-width spaces,
  soft hyphens, the invisible Unicode "tag" block used to smuggle hidden instructions into
  AI prompts, the BOM); line/paragraph separators; private-use characters, lone surrogates
  and noncharacters. Zero-width joiner/non-joiner are **kept**: Malayalam, Devanagari and
  other Indic scripts need them.
- A value with nothing visible in it (only joiners or combining marks) counts as empty, so
  an invisible "name" can't satisfy the name rule.

## Statuses, sources and ratings

Statuses and sources are rows (`leads_lead_status`, `leads_lead_source`) seeded by a
migration and referenced from leads by their **immutable key**. The API speaks keys
(`"status": "qualified"`); names are display labels. Lead forms and filters load them
from `GET /api/v1/config/lead-options`, so the UI hard-codes no list.

| Key | Name | Category |
|---|---|---|
| `new` (default) | New | open |
| `contacted` | Contacted | open |
| `qualified` | Qualified | qualified |
| `unqualified` | Unqualified | unqualified |
| `converted` | Converted | converted |

Sources: Website, Referral, Campaign, Cold Call, Email, Event, Partner, Other.

- **Business rules and reports use the `category`, never the name**, so a future
  "Nurturing" status (category *open*) or a renamed status needs no code change.
- A retired status or source (`is_active = false`) stays valid on leads that already use
  it but can't be chosen for new changes. Rows in use can't be deleted (FK `PROTECT`).
- Adding or retiring values is a data change today; an admin screen (`config.manage`) is
  a later, additive feature.
- **Transitions:** a lead may move between any active statuses (qualification is the
  salesperson's judgement and mistakes must be fixable), with one rule since Phase 3:
  **a lead becomes Converted only with an opportunity**. The **Convert** operation
  (`POST …/leads/{id}/convert`) creates the opportunity and sets the status in one
  transaction; the plain status change into a *converted* status (and creating a lead as
  Converted) is refused with a 422 unless the lead already has an opportunity. The
  pipeline module enforces this from its `LeadStatusChanged`/`LeadCreated` subscribers,
  so `leads` still doesn't import it ([ADR-0019](adr/0019-lead-conversion.md),
  [pipeline.md](pipeline.md#conversion)). Leaving Converted stays allowed. Conversion
  never creates a Company, Account or Contact: none exist.

## Ownership and workspaces

Every lead has exactly one owner. The same endpoints serve every workspace; the
workspace in the URL decides whose leads exist
([authorization.md](authorization.md#admin--user-workspace-built-resolution-banner-endpoint-module-views-from-phase-2)):

| Workspace | Who | Reads | New lead's owner | Writes need |
|---|---|---|---|---|
| `me` | everyone | own leads | always the actor (any other `owner` is a 400) | `crm.access_own` |
| `{user_id}` | admins (`workspace.view_any`) | that user's leads | always that user (must be active) | `crm.manage_any` + `crm.assign_any` to create |
| `all` | admins (`crm.view_all`) | every lead | required `owner`: an active user who works in a CRM workspace | `crm.manage_any` + `crm.assign_any` to create |

- Viewing a workspace never implies changing it: delegated writes need the separate
  `crm.manage_any` capability (`identity.workspaces.authorize_write`).
- A sales user can't give a lead away, even their own: reassignment needs `crm.assign_any`.
- Records an admin creates in Rahul's workspace are **owned by Rahul**, `created_by` is
  the admin, and the audit actor is the admin with Rahul as the subject. No impersonation.

## Reassignment

`POST …/leads/{id}/assign {owner, version}` is the only way ownership changes: never
through PATCH, never as a side effect.

```
Reassign lead (crm.assign_any; lead inside the workspace; current version; not archived)
  → lock the lead row; share-lock the new owner's user row (must be active and assignable)
  → owner changes, version + 1
  → audit lead.reassigned {from_owner_id, to_owner_id}
  → publish LeadReassigned (in the same transaction)
        Phase 3 (built): the lead's OPEN opportunities (archived ones too) move to the
        new owner, one opportunity.owner_changed audit event each; won and lost ones
        keep the owner who closed them. The database refuses to commit an open
        opportunity whose owner isn't its lead's owner (docs/pipeline.md#ownership).
        Phase 4: current activities follow the same way.
  → commit (all or nothing)
```

- Historical attribution never changes: `created_by`, audit actors and (from Phase 4)
  who completed an activity stay as they were.
- Reassigning to the current owner is a no-op success. After a reassignment out of the
  user workspace an admin is viewing, the UI returns to that workspace's list, where the
  lead no longer appears.
- The share lock on the new owner means a concurrent deactivation waits for the
  reassignment (or is seen by it): a lead is never handed to someone already deactivated.

## Archive

Leads are never deleted (no DELETE endpoint; FKs are `PROTECT`).

- `POST …/archive {version}` hides a lead from the default list; `…/restore` brings it
  back. Both are audited and idempotent.
- The list shows active leads by default; `archived=true` (the "Archived" view) shows only
  archived ones. The detail page still opens an archived lead, flagged as archived.
- An archived lead is read-only (edit, status and reassignment answer 422) until restored.
- Nothing about its relationships changes; retention/purging is a later, explicit process.

## Phone numbers

Arkray's customers aren't only in India, so no country is assumed.

- **Stored as typed** (whitespace tidied): `+91 98765 43210`, `(022) 2345 6789`,
  `+1 555 010 9999 ext. 12`. The leading `+` and extensions are never lost.
- Accepted: optional leading `+`, ASCII digits and `space ( ) . - /`, optional extension
  (`x`, `ext`, `ext.`, `extension`, `#`, `;ext=` + 1–7 ASCII digits); 5–17 digits in the
  number; up to 40 characters. Full-width digits and symbols (common in Japanese input) are
  converted to ASCII first; other scripts' digits are refused, so every number is dialable.
- On the lead page a number links to a dialable `tel:` URI with the extension as `;ext=`.
- **Canonical key** for matching (`phone_keys`, kept in sync by the model on every ORM path:
  `save()` and `bulk_create` recompute it, and bulk updates of numbers are refused): `+` and digits
  for international numbers (a leading `00` counts as `+`, and a `(0)` trunk marker after
  the country code is dropped, so `+44 (0)20 7946 0958` matches `+44 20 7946 0958`), digits
  only for national ones, then `x` + extension. `+91 98765 43210` and `+91-9876-543210`
  match; a national spelling
  (`098765 43210`) deliberately does not match the international one, because telling them
  apart needs the country's numbering plan. **Extension point:** region-aware parsing
  (e.g. libphonenumber with the lead's country as the default region) can later add an
  E.164 key without changing what is stored.

## Duplicate assistance

`GET …/leads/duplicates?email=&phone=&phone=&exclude=` returns up to 5 leads **in the same
workspace** with the same email (case-insensitive, folded by PostgreSQL on both sides, like
its index) or the same canonical phone number, including archived ones, with what matched.
At most 50 matches are considered (the query is unordered so it can combine the email and
phone indexes); with more than 50 (a shared switchboard number) the 5 shown are recent but
not guaranteed to be the very newest. The create and edit forms show them as an
advisory notice with links. Nothing is ever blocked or merged: people share switchboards
and generic mailboxes, so no fuzzy rule is a uniqueness constraint. Because it looks only
inside the caller's workspace, it can't reveal other users' leads.

## Search, filters, sorting

`GET /api/v1/workspaces/{workspace}/leads` (allowlisted; anything else is a 400):

| Parameter | Meaning |
|---|---|
| `q` | 2–100 characters, up to 5 whitespace-separated words, **every** word must occur (case-insensitive substring) in first name, last name, organisation, email or any phone number's digits. Words shorter than 2 characters are ignored ("Rahul S" searches "Rahul"; only short words is a 400). A phone-like word (`98765-43210`, `1-800`) matches phone digits **or** the text as typed ("1-800 Flowers"). LIKE wildcards are literal. Case folding is PostgreSQL's `upper()`: locale-specific letters (Turkish İ, German ß) match only as typed. Description, city and other fields are not searched (global search is Phase 7). |
| `status`, `source`, `rating` | exact key |
| `owner` | user id; **organisation-wide workspace only** (400 elsewhere); only narrows the scope |
| `created_from`, `created_to` | inclusive dates (2000–2999), interpreted as business days in Asia/Kolkata |
| `archived` | `false` (default) or `true` |
| `ordering` | `-created_at` (default, newest), `created_at`, `name` (display name, A–Z), `-updated_at`, `-last_contacted_at` (never-contacted last), `last_contacted_at` (never-contacted first) |
| `cursor`, `page_size` | keyset pagination, 1–100 (default 25) |

Search runs in PostgreSQL against one generated, upper-cased `search_text` column with a
trigram index, and is always combined with the workspace scope. Nothing is ever loaded into
Python to be searched. Responses carry no counts or totals.

## Pagination

Composite keyset pagination ([ADR-0016](adr/0016-composite-keyset-pagination.md)): the
cursor holds the full sort key of the boundary row (e.g. `display_name, id`), signed and
bound to its ordering. Pages never overlap or skip rows, whatever the ties or NULLs, in
both directions, and a lead inserted meanwhile appears once in its sorted place (tested
against PostgreSQL's own ordering for every sort and several page sizes). A malformed,
forged or other-ordering cursor is a 400, never a 500. A cursor carries sort values only:
the workspace always comes from the URL, so a cursor can't widen what anyone sees.

- **Names never travel in cursors** (they end up in URLs and possibly proxy logs): the name
  sort is a *private* key, so its cursor holds the boundary lead's id, and the name is re-read
  from that lead when the next page is requested. Cursors stay short whatever the script.
- The last-contact sorts use `last_contacted_sort` (a generated, NOT NULL copy with "never"
  as the oldest date), so a cursor bounds the index scan and a page 250,000 rows deep costs
  the same as the first.
- A lead whose sort value changes while someone is paging (it is edited, contacted or
  renamed) moves to its new place: it can then appear on a later page again or be passed.
  That is inherent to cursor pagination; inserts and archiving never cause it.

## Concurrency

Every write locks the lead row and requires the `version` the client last saw.

| Race | Outcome |
|---|---|
| two admins reassign one lead from the same version | one succeeds, the other gets 409; one audit event |
| both reassign to the same person | both succeed; the second is a no-op |
| an admin and the owner edit at once | one succeeds, the other gets 409 (no lost update); the UI offers to re-apply the loser's changes on top of the winner's |
| archive while someone edits | one wins; the other gets 409 |
| status change racing reassignment | one wins; the other gets 409 (or 404 if the lead has just left the previous owner's workspace) |
| reassignment racing the new owner's deactivation | serialised by the share lock; never assigned to a deactivated user |
| the same create submitted twice (double click, retry after a timeout) | with the same `Idempotency-Key`: one lead, the retry replays it (`Idempotent-Replayed: true`); if the lead has meanwhile been reassigned out of the workspace, the retry says so (409) instead of a misleading 404 |

Idempotency keys are per user and per operation, kept 24 h (purged hourly by
`core.housekeeping`), and a reused key with a different body is refused (422
`idempotency_key_reused`). The frontend derives the key from the exact request body, so it
reuses a key only for an identical retry. The UI never navigates or shows "saved" for a
save that finishes after the user has left the form.

## Audit and domain events

| Operation | Audit action | Metadata (never contact data) | Domain event |
|---|---|---|---|
| create | `lead.created` | workspace, owner_id, status | `LeadCreated` |
| edit | `lead.updated` | workspace, changed field **names** | `LeadUpdated` |
| status change | `lead.status_changed` | workspace, from, to (keys) | `LeadStatusChanged` |
| reassignment | `lead.reassigned` | workspace, from_owner_id, to_owner_id | `LeadReassigned` |
| archive / restore | `lead.archived` / `lead.restored` | workspace | `LeadArchived` / `LeadRestored` |

The actor is whoever acted; `subject_user_id` is the lead's owner when that is someone
else (the previous owner for a reassignment). No-op and refused requests write nothing.
Domain events run in the same transaction ([ADR-0017](adr/0017-in-transaction-domain-events.md)).
Since Phase 3 the pipeline module subscribes (`LeadReassigned`: open opportunities follow;
`LeadStatusChanged`/`LeadCreated`: the conversion rule); still **no** outbox work is queued.
A conversion also writes `lead.converted` (`opportunity_id`, from/to status). Ask Arkray re-indexing (Phase 8),
analytics and automation subscribe later and enqueue outbox work from their subscribers.

## Frontend

| Route | Workspace |
|---|---|
| `/leads`, `/leads/new`, `/leads/{id}`, `/leads/{id}/edit` | own (sales users); organisation-wide (admins) |
| `/admin/users/{id}/leads`, `…/new`, `…/{leadId}`, `…/{leadId}/edit` | that user's, under the "Viewing CRM for" banner |

- One implementation: the route files only render the shared views; the workspace comes
  from the URL. Views are keyed by workspace and every query key starts with the workspace
  segment, so switching from Rahul's leads to Priya's never shows Rahul's rows, filters or
  cached pages (tested).
- List filters, search and page are remembered per workspace **in memory** for the page
  load (Back from a lead restores the list), never in the URL or browser storage, so
  names, emails and phone numbers don't end up in history, proxy logs or storage that
  outlives sign-out.
- Table on tablets and desktops (columns appear as space allows), cards on phones. Status
  and rating are always written out, colour only reinforces them.
- The create/edit form has five sections (Basic, Contact, Sales, Address, Additional;
  the last two collapse and open themselves when they hold an error), field errors tied
  to their inputs, focus on the first invalid field, duplicate assistance, an
  unsaved-changes warning, and double-submit protection (idempotency key + busy button).
- Edit conflicts (409) never lose work: "Apply my changes to the latest version" re-applies
  the user's edits onto what the other person saved and flags fields both changed;
  "Discard my changes" loads the latest.

## Performance

Measured, not assumed. Query counts per request are constant (pinned in
`arkray/leads/tests/test_query_counts.py`, identical for 10 and 100 leads): own list 3
(session, user, leads with owner/status/source joined), a user's workspace 4 (+ the
workspace check), organisation-wide 3, detail 3, duplicate check 3, options 4, assignees 3.
Following a name-sorted cursor adds one indexed lookup (the boundary lead's name).

Timings from a 300,000-lead benchmark (60 owners, one with 20,000 leads; PostgreSQL 16,
EXPLAIN ANALYZE of the exact SQL the API runs, after the review fixes); the index behind
each is in [database.md](database.md#leads_lead):

| Query | Time |
|---|---|
| any per-owner list page, every sort, first or deep page | 0.1–0.9 ms |
| per-owner name sort with a selective filter or search (archived, rare status, no-match combination, search) | 4–8 ms (was 136–368 ms before the review added `leads_owner_name_idx`) |
| per-owner search (name, phone digits, 2-character term) | 0.1–0.7 ms |
| organisation-wide list page, every sort, first or deep page | 0.1–1 ms |
| organisation-wide page 150,000–250,000 rows deep (name, last-contact sorts) | 0.1–0.2 ms (last contact was 35–145 ms before the NOT NULL sort column) |
| organisation-wide status / owner / date-range filter | 0.1–0.2 ms |
| organisation-wide search (common term, rare term, phone digits, exact email) | 0.06–12 ms |
| organisation-wide 2-character rare term (can't use trigrams) | 32 ms |
| duplicate check (organisation-wide / one owner) | 0.05 ms / 0.9 ms |
| **worst cases found**: organisation-wide filter combination with no matches (rating + source) | 127 ms |
| organisation-wide search combined with name sort on correlated data | 197 ms |

The two worst cases are admin-only, bounded by the 10 s statement timeout, and recorded as
risk R33 in the [risk register](risk-register.md). The query-plan regression suite
(`tests/performance/test_lead_query_plans.py`) pins which index serves each query shape.
