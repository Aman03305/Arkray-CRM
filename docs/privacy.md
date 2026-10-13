# Privacy: personal data, retention and requests

Arkray CRM holds personal data about two groups: **customers** (leads: the people and
organisations the sales team works with) and **staff** (the users who sign in). This document
says what is held, where, who it goes to, for how long, and how a request to see, correct or
erase a person's data is met. It describes what the code does (each statement is backed by a
test named in [the remediation report's evidence](adr/0032-privacy-remediation.md)); the
legal basis, the retention periods the organisation chooses, the review of an export before
release and the reply to the person are the organisation's decisions, listed under
[Decisions that aren't the system's](#decisions-that-arent-the-systems). Nothing here is a
claim of compliance with any law. Security controls are in [security.md](security.md).

## What is held, and where

| Data | Personal data in it | Where |
|---|---|---|
| Leads (the canonical customer record) | names, organisation, job title, email, three phone numbers, address, free-text description | `leads_lead` (with its generated search text and phone keys) |
| Opportunities | the deal's copy of its customer (customer and account names, contact phone and email, address), its name (derived from the customer and instrument), description, lost reason, work load, Expected CPT; amounts and prices (commercial, not personal) | `pipeline_opportunity`; lost reasons also in the append-only `pipeline_stage_history`, agreed prices and CPTs in the append-only `pipeline_negotiation_price`, a closed deal's corrected figures and the administrator's reason in the append-only `pipeline_opportunity_correction` ([pipeline.md](pipeline.md#closed-deals)) |
| Custom field values | whatever a pipeline's fields collect (free text included); values of **removed** fields stay, hidden, until deleted on purpose ([below](#custom-fields)) | `pipeline_opportunity.custom_fields` |
| Activities | notes (up to 10,000 characters), task and meeting titles and descriptions, meeting places and links | `activities_activity` |
| Attachments | the file itself (any content), its name and content hash, type, size, who uploaded and deleted it | bytes in private object storage (an S3 bucket or a private volume); details in `activities_attachment` |
| Timeline | user ids, kinds, dates, statuses, stage names; text is read live from the activity | `activities_timeline_entry` |
| Ask Arkray | questions as typed, stored answers (which quote CRM text), conversations (asker, workspace), including those their asker deleted while a legal hold keeps them (hidden, [below](#legal-holds)) | `ai_question`, `ai_conversation` (`forgotten_at`); index chunks hold vectors, hashes and offsets, no text, but vectors are **derived** from personal text (`ai_knowledge_chunk`) |
| Staff | name, email, role, status, sign-in and password-change times, an Argon2id password hash | `identity_user`; sessions in `django_session` |
| Support sessions | which administrator helped which user, when, and the reason typed (blanked after the audit-detail retention) | `identity_support_session` |
| Audit trail | who did what to which record, when: ids, field **names**, statuses, counts; never a customer's field values | `audit_event` (append-only) |
| Audit details | for a while only: the client address of security events, old and new sign-in emails, a reset requester's address, a support reason | `audit_event_detail` ([below](#audit-trail)) |
| Security evidence | client addresses and keyed hashes of submitted emails (never the email), device ids; an account's daily sign-in lock, under the same keyed hash | `identity_auth_throttle_event`, `identity_login_lock` |
| Account links | a hash of each invitation or reset secret, never the secret | `identity_account_token` |
| Background work | ids; the account-email events' payloads hold an email address and, for a reset request, the requester's address (blanked 24 h after they finish); error text with email addresses replaced | `core_outbox_event` |
| Idempotency records | a SHA-256 of a create request (24 h) | `core_idempotency_record` |
| Data exports | who exported whose data and when (the export file itself lives 24 h) | `privacy_data_export`; files in private storage (`STORAGES["exports"]`) |
| Legal holds | the held subject's id and a case reference | `core_legal_hold` |
| Erasure ledger | ids of erased leads, pseudonymised users, deleted custom fields and files, and signed checkpoints of the verified chain (numbers, MACs, digests, key ids); no personal data | outside the database ([below](#restore-safe-erasure)) |
| Redis | the cache's rate-limit keys contain a client address (about a minute); the broker's messages carry ids only | the cache instance (never persisted in the production stack) and the broker |
| Application logs | per request: the client address, the user id, the method and path (never the query string), status and timing; elsewhere ids, codes and counts only ([observability.md](observability.md#what-a-log-line-may-hold)) | stdout, then the log platform |
| Edge logs | the client address, the path (no query string, one-time links redacted), status, timings; no user agent, referrer or cookie | nginx stdout, then the log platform |
| Backups | every table above except sessions (the attachment files and the ledger are not in database backups) | the backup store, encrypted |
| Browser | nothing personal is stored: the query cache lives in memory and is dropped at sign-out; one localStorage key holds a panel's open/closed state | the browser |

## Who receives what

Verified against the code; contracts and regions are the operator's to put in place
([decisions](#decisions-that-arent-the-systems)).

| Recipient | When | What it receives | Transport | Retention there |
|---|---|---|---|---|
| **Anthropic** (the language model) | only with `AI_LLM_PROVIDER=anthropic` (the default is `none`; production runs `none` until the conditions in [rag-architecture.md](rag-architecture.md#privacy-and-data-minimisation) are met) | the question and up to four earlier turns, with links, email addresses, phone numbers, passcodes and other credentials, and PAN, Aadhaar and card numbers replaced by markers (whatever their digits' script or encoding; previews are masked before they are cut: adversarial review of 73ff1e3, M5); tool results: lead name, organisation, job title, status and dates; deal names, account, customer, lost reason, description, work load and CPTs (only where the asker may see the customer: L6), amounts and prices; pipeline and stage names, activity titles and note passages (masked the same way). Never: contact fields, addresses, meeting links or places, custom values, audit data | HTTPS only (`AI_LLM_BASE_URL` must be https; SDK variables refused) | the provider's, under the organisation's agreement (not verified here) |
| **SMTP provider** | always (account emails) | recipient address, first name, the inviter's name, a one-time link (invitation or reset), the old address and a masked new one (email change) | STARTTLS or TLS with certificate verification, enforced in production | the provider's |
| **Object storage (S3-compatible)** | with `ATTACHMENT_STORAGE=s3` | attachment bytes under a random key (never the file name); export ZIPs (24 h) | HTTPS with certificate verification, enforced; objects private, SSE-AES256 | until purged; a versioned bucket keeps earlier versions until its lifecycle rule or `ATTACHMENT_S3_PURGE_VERSIONS` removes them |
| **ClamAV** (clamd) | with `ATTACHMENT_SCANNER` | attachment bytes, to scan | plain TCP: must be loopback or a listed private container name (enforced) | none (scans in memory) |
| **PostgreSQL** (managed or self-hosted) | always | everything in the first table | `sslmode=verify-full` with a CA, enforced for any remote host | the operator's (point-in-time recovery window) |
| **Redis** | always | broker: ids; cache: rate-limit keys with a client address | `rediss://` with certificate and host-name checks, enforced for any remote host | broker until consumed; cache about a minute, not persisted in the production stack |
| **Log platform** | always | the log lines described above | the platform's | 30 days recommended; the platform's setting (container logs on the host rotate at 5 x 10 MB) |
| **Backup store** | the operator's schedule | encrypted dumps (OpenPGP, public-key) | the operator's | 30 days by policy (`backup.sh --prune`) |
| **Erasure ledger store** | every erasure | ids and times | a volume of its own, or S3 over HTTPS | indefinitely (holds no personal data) |
| Analytics, error monitoring, fonts, CDNs | never | nothing: the CSP allows only the app's own origin, Next.js telemetry is off, no such SDK is installed | — | — |
| GitHub (CI) | builds | test fixtures only | — | — |

## Retention

| Data | Kept | Mechanism |
|---|---|---|
| Leads, opportunities, activities | until erased on request or under the organisation's retention policy (archiving hides, it doesn't delete) | `manage.py erase_lead` ([below](#erasure)); no bulk retention job yet (a decision) |
| Timeline entries, stage history, negotiated prices | the life of the record | append-only; lost reasons and agreed CPTs redacted on erasure |
| Custom values of a removed field | until deleted on purpose | [custom fields](#custom-fields) |
| Audit trail (the events) | indefinitely: a security record without personal values | append-only; a DBA can purge old rows as the schema owner ([runbooks.md](runbooks.md#purge-old-audit-events)) |
| Audit details (client addresses, old/new emails, reset requesters' addresses, support reasons) | **`AUDIT_DETAIL_RETENTION_DAYS`, 90 by default** (a starting policy, configurable; not a legal duration) | `audit.housekeeping`, hourly; suspended by a legal hold |
| Support-session reasons on the session row | the same 90 days after the session started | `identity.housekeeping` blanks them; the session (who, when) stays |
| Sessions | at most 12 hours (2 hours idle); the cookie ends with the browser | expired rows deleted hourly; never in backups |
| Login and reset throttle evidence | 24 hours | hourly housekeeping |
| Daily sign-in locks | until the owner's unlock link, a reset, an administrator, or 30 days (`LOGIN_LOCK_MAX_AGE_S`) | hourly housekeeping (`identity.housekeeping`) |
| Account-email payloads (an email address, a requester's address) | 24 hours after the email is sent | hourly housekeeping blanks them |
| Finished background work | 7 days (`OUTBOX_DONE_RETENTION_DAYS`) | hourly housekeeping; dead events until an operator resolves them |
| Idempotency records, workspace access windows | 24 hours / 30 minutes | hourly housekeeping |
| Ask Arkray questions and answers | 30 days after each answer (`AI_CONVERSATION_RETENTION_DAYS`), whatever happens in the conversation since | hourly housekeeping; a user can delete a conversation at any time (gone for them at once; under a legal hold it is kept, hidden, and deleted by the first housekeeping after the release); retention suspended by a legal hold ([below](#legal-holds)) |
| Ask Arkray index chunks | as long as the text they were computed from | deleted with it and by an erasure |
| Attachment files of deleted notes' files | removed by a job after deletion (with their name and content hash) | [attachments](#attachments) |
| Data exports | 24 hours after being built (`EXPORT_TTL_HOURS`); the record of the export stays | `privacy.housekeeping`, hourly |
| Application and edge logs | 30 days recommended at the log platform (a decision); on the host, 5 x 10 MB per container | the platform's setting; Compose `logging` rotation |
| Redis | cache keys about a minute; the production stack's cache writes nothing to disk; the broker's append-only file holds ids | TTL; `--save '' --appendonly no` for the cache |
| Backups | 30 days by policy, at least the 7 newest kept, never while `LEGAL_HOLD` exists in the backup directory | `scripts/backup.sh --prune` (opt-in) or the store's lifecycle rule |

### Legal holds

A legal hold (`manage.py legal_hold place --lead|--user <id> --reference CASE-123 --by
<admin>`; `release`; `list`) stops everything that would remove data about that person while
it is active: lead erasure, staff pseudonymisation, audit-detail expiry for events about them,
attachment purges on their notes, the deletion of their deals' custom values, the
support-reason blanking, and the deletion of Ask Arkray conversations about them (below). The reference names the matter, never describes it. Placing and
releasing are audited (`privacy.legal_hold_placed` / `_released`). A backup directory is held
by creating a file named `LEGAL_HOLD` in it.

**Edits of a held lead** (audit PRIV-2; `core/holds.py`). The hold preserves the record as
evidence, so what an erasure would clear can't change through the application either, by
anyone, through any path (the services check it, not the forms):

| Frozen while held (409 `legal_hold`, nothing written, version unchanged) | Still allowed |
|---|---|
| the lead's name, organisation, job title, email, phones, address and description (an edit or a [correction](#correction)) | status, owner, source, rating, last contact; archiving and restoring (archiving hides, deletes nothing) |
| its deals' customer, account, contact phone and email, address, instrument, work load, Expected CPT, description, lost reason and custom values (and so their derived name) | new deals; value, probability, dates; stage moves and agreed prices (the history is append-only), except a move that sets or clears the lost reason (a move to a lost stage with a reason, or reopening a lost deal that has one), which is an edit of it (deep security audit L-9 and its review: a move that leaves the reason as it is goes on) |
| its activities' subject and text, meeting place and link | new notes, tasks, meetings and files; times and priority; completing, cancelling, reopening, archiving |
| deleting a file of its notes | uploading one |

A won or lost deal's value and dates change only through an administrator's correction
([pipeline.md](pipeline.md#closed-deals)), which goes on under a hold: it adds a row to an
append-only history and overwrites none of the held text (adversarial review of 73ff1e3, M2).

"Frozen" means no change at all, filling an empty field included: the record stays as it
was when the hold was placed. A request changing a frozen field and an allowed one is
refused whole. Releasing the hold re-allows every edit. Placing a hold is serialised with
edits in flight (`holds.place`): an edit already past its check commits first and the hold
waits for it; an edit arriving while a hold is being placed waits and is then refused. Holds
on a user (staff) stop their pseudonymisation and the expiry of details about them; they
don't freeze CRM records.

**Placing a hold is serialised with whatever acts on the holds** (risk register R134;
`core/holds.py`). A hold placed at the same moment as a deletion, purge, erasure or overwrite
either committed before that operation read the holds (the data is kept) or waits until the
operation has committed; it is never committed between the operation's check and its
deletion. Two transaction-scoped locks do it:

- an operation on one subject's data takes that subject's hold lock, shared: every edit
  above, and the purge of a note's file (the lead's lock is held until the file's object is
  gone and its row says so, so a hold on that lead waits at most for that file's storage
  calls; no row is locked meanwhile, so an erasure of the lead never waits for them, and a
  second purge of the same file skips it);
- an operation that reads every hold, or holds it can't name before reading them, takes the
  **hold barrier**, shared, as the first lock of its transaction: an erasure (the
  conversations a hold on someone else keeps), a staff pseudonymisation, deleting an Ask
  Arkray conversation, the hourly conversation housekeeping and the deletion of a removed
  custom field's values (each batch takes it and reads the holds again, so a hold placed
  while either runs counts from the next batch, and one placed during a batch waits for it),
  forgetting purged files' names, the audit-detail expiry (per batch), the support-reason
  blanking, and a ledger replay without an operator (the automatic reconciliation, which
  leaves a held lead or user, or a held lead's deleted file, to an operator).

`legal_hold place` takes the barrier exclusively, then the subject's lock, then inserts, so it
waits for the operations already past their check (each is short; an erasure is the longest,
seconds) and holds new ones back until it commits. Should it wait longer than the
database's lock timeout (5 s by default) it stops with nothing placed and says to run it
again. Rules for code (`core/holds.py`, enforced there): the barrier is the first lock of its
transaction, before any row lock, write or other advisory lock, so whoever waits for it holds
nothing and it can't close a deadlock; a subject's lock comes after the operation's row
locks.

**Ask Arkray conversations** (deep security audit M-5; `ai/subjects.py: held_conversations`).
A hold keeps every conversation that concerns its subject, whole (every question and answer
in it), until it is released:

- a **user** hold: every conversation they asked, and every conversation an administrator
  asked in their workspace. An Admin's team conversations (kind `team`, ADR-0035) have no
  subject, like the organisation's: a hold on a team member doesn't keep them (one on the
  Admin does: they asked them), and a lead hold keeps those that touch its customer, below;
- a **lead** hold: every conversation one of whose questions or answers cites the customer's
  lead, deals or activities, or names them (name, email, phone numbers), matched exactly as
  an [erasure](#erasure) of that customer matches them.

While a hold keeps it, the 30-day housekeeping skips the conversation (questions older than
the retention in it too), staff pseudonymisation leaves it in place, and so does the erasure
of another customer it touches ([erasure](#erasure)). **Deleting it is never refused**
(adversarial review of 73ff1e3, M1, R148): a refusal (409 `legal_hold`, as before) told the
asker whether any name in their question belonged to a held customer, anyone's, and a held
user that they were held. Deleting one's own conversation always answers 204 and the
conversation is gone for them at once, everywhere (the list, the detail, its questions,
continuing it and a second deletion answer 404); a conversation no hold keeps is deleted then
and there, one a hold keeps is kept as a tombstone (`ai_conversation.forgotten_at`; its
unanswered questions are failed `forgotten` and never answered). Tombstones stay the hold's
evidence: a subject-access export of the held customer still contains them, and an erasure
still matches them. Both paths do the same work and are audited the same way
(`ai.conversation_deleted`: the conversation's id, a count of its questions and `retained`,
whether a hold kept it; never text, never which hold). Once the hold is released, the next
hourly housekeeping deletes what is older than the retention, and every tombstone no hold
keeps any more, whatever its age (the user's deletion completes then). Hourly housekeeping excludes a held user's conversations
without reading them, matches the rest against the held customers in batches that each
commit, and remembers a conversation found held while the same holds are active, so long
holds don't slow the deletion of everyone else's history. Which customers a conversation
touches is known only by matching it, so deleting one, the housekeeping, an erasure and a
pseudonymisation read every hold under the hold barrier (above): a lead or user hold placed
at the same moment either keeps the conversation or is placed once it is gone, never in
between.

## Audit trail

The audit trail is append-only (a database trigger refuses UPDATE and DELETE to the
application's role). It keeps who did what to which record, and when, for as long as the
organisation keeps it. The personal details a few events need for a while are kept apart, in
`audit_event_detail`, and expire:

- the **client address**, for security events only (`auth.*`, `user.*`, `support_session.*`,
  `workspace.*`, `privacy.*`, `lead.erased`, `audit.*`); everyday CRM edits keep no address
  (their request id ties them to the access log while that is kept);
- the old and new address of a **sign-in email change**, a **reset requester's** address, a
  **support session's reason**.

Each event seals its detail with a salted SHA-256 (`detail_digest`): a detail changed while it
exists no longer matches (`audit.retention.verify`), and once it has expired (salt and values
deleted together) the digest can't be matched against guessed values. The administrators'
security-events page shows a support reason while it is kept.

Events written before this existed kept these details in the row itself. The schema owner
moves them into expiring details once, with `manage.py audit_minimise_legacy --by <admin>
--yes` (a dry run without `--yes`): one transaction, the trigger disabled for each statement
only, and an `audit.legacy_minimised` event recording the counts and a SHA-256 of every value
moved ([runbooks.md](runbooks.md#minimise-legacy-audit-details)). This is the one documented,
authorised and audited operation that changes old audit rows; the application never does.

## Requests

### Access requests

Privacy requests are organisation-wide and need `privacy.manage`, which only super admins
hold: an Admin (team administrator, [ADR-0035](adr/0035-super-administrators-and-teams.md))
can't request exports or pseudonymise anyone, even for their own team.
A super admin who has verified the requester's identity exports their data from the
customer's (lead's) page or the user's details (**Export data**; the **Data requests** page
under Admin lists the administrator's own exports and their downloads): the request names a reference (the ticket),
confirms the identity check, and is audited (`privacy.export_requested`). A job builds a ZIP:

- `data.json` (machine-readable) and `csv/*.csv` (the same as tables; a value a spreadsheet
  would run as a formula, also behind leading blanks, is prefixed with a quote);
- for a **customer**: the lead (every field, archived or not), its deals in full (the customer
  copy, amounts, prices, CPTs, custom values by field name, removed fields' values too, stage
  and price history, and the corrections of its closed deals: the figures before and after,
  the reason and the time, table `deal_corrections`), every task, meeting and note in full text (not the screens'
  240-character previews), attachment details and the stored, downloadable files up to
  `EXPORT_MAX_BYTES` (100 MB; the rest are listed with the reason), the timeline, and the
  Ask Arkray questions and answers that cite the customer's records or name them, question by
  question (never the rest of a conversation that once touched them; another record cited
  in an answer shows as "[another record]"). Staff appear by role only;
- for a **staff member**: their account, their sign-ins and the security events about them
  (with their own client address while the audit detail keeps it), the support sessions for
  them, their own Ask Arkray questions and the answers' text (every record an answer cited
  shown as "[another record]", and email addresses, phone numbers and links in the text
  masked: the customers their answers named are not their personal data; deep security audit
  L-6), and counts of the records they own. An answer's narrative written by the model can
  still mention a person in words: review before releasing, as for free text. Their own
  questions include conversations they deleted that a legal hold keeps (tombstones,
  [legal holds](#legal-holds)): the data is still held, but the export then tells the person,
  through the administrator handing it over, that a hold kept them (a decision for the
  organisation: [below](#decisions-that-arent-the-systems)).

The customer's export is built in bounded memory (adversarial review of 73ff1e3, M6): the
records are read 100 at a time and written straight into the ZIP on disk, so its memory stays
flat however much the customer holds (about 4-5 MB up to 5,000 long notes, where it peaked at
82 MB for 1,500 before). Since its review round 1 (QUEUES-1, QUEUES-5) the CSVs are a second
pass over the same database snapshot instead of temporary files, `EXPORT_MAX_BYTES` (100 MB)
bounds the archive as stored (compressed), so every customer within the activity budgets
fits (natural text in any script: about 39-76 MB measured at the budget), and the build has
240 s; text deliberately made incompressible (random CJK or emoji) at the full budget can
still exceed the cap and fail as too large ([reliability.md](reliability.md#export-sizing)).
The `stage_history` rows also carry each transition's expected close date and opportunity
date. A staff member's export is still built in memory (its audit events are capped at
20,000).

Only the administrator who asked can download it (`privacy.export_downloaded`), for 24 hours;
then the file is deleted (`privacy.export_expired`) and the record kept. At most 3 exports may
be waiting and 20 requested per hour per administrator. Exports are refused inside a support
session and for an erased or pseudonymised subject; erasing or pseudonymising a subject ends
their exports at once, queued or ready (the file is deleted, the record kept, the erasure's
audit counts them). A build that finds its export ended meanwhile deletes what it wrote, a
retried build replaces its earlier attempt, and a build that never finished has its file
deleted when it is timed out: no export file outlives its record's 24 hours. **Before
releasing an export, review it**: free text staff wrote may mention other people, which the
system can't tell apart.

Deleting an export's file is durable (final audit PRIV-3, ARCH-4). An export that ends is
no longer downloadable at once, and its record keeps the file's key until the object is
confirmed gone (`purged_at`); a failed deletion is retried by the hourly
`privacy.housekeeping`, backing off from 5 minutes to a day, with the last failure on the
record and in the `arkray_export_file_purges` gauge. In S3 every version and delete marker
of the key is deleted (a versioned bucket otherwise keeps the ZIP), whatever
`ATTACHMENT_S3_PURGE_VERSIONS` says, so the credentials need `s3:ListBucketVersions` and
`s3:DeleteObjectVersion`. A version that S3 Object Lock (a retention period or a legal hold)
protects is *retained*: audited once (`privacy.export_file_retained`) and retried daily
until the lock ends. Each hour a bounded reconciliation also deletes objects in the exports
store that no live export explains (an ended export's, or one a restored database no longer
knows), never names an export isn't written under, and never anything while the exports
store overlaps the attachments store ([runbooks.md](runbooks.md#export-file-cleanup)).

### Correction

A customer's identity and contact details (name, organisation, job title, email, phones,
address) are corrected on the lead's page (**Correct details**), by whoever may edit in the
workspace that sees the lead: one transaction, the lead's version checked (409 if someone
changed it), audited by field names (`lead.corrected`). Every deal whose copy of the customer
still holds the **old** value follows (`opportunity.customer_corrected`; its derived name
too); a copy someone deliberately made different, the deal's amounts, prices and history are
left as they are. The answer counts only the deals the caller can see; copies on other deals
(a previous owner's closed deals) follow too and are audited as the system's (`cause:
lead.corrected`), so a correction reveals nothing about deals out of the caller's sight
(adversarial review of 73ff1e3, L5, R158). Search, the dashboard and Ask Arkray (re-indexed)
show the corrected details. An erased lead can't be corrected. A staff member's name and email are corrected by
an administrator on the Users page (a super admin for anyone, an Admin for their own team's
users; another administrator's email only by that administrator, R100); the change is audited, an email change with its old and new address in
the expiring audit detail.

### Erasure

On a verified request an administrator runs:

```
manage.py erase_lead <lead id> --by admin@example.com          # what would be erased
manage.py erase_lead <lead id> --by admin@example.com --yes    # erase it
```

with the schema owner's database credentials (the migrate job's) when the lead's
opportunities were lost with a reason; otherwise the application's are enough (the command
says which). Refused while a legal hold covers the lead. In one transaction it:

- deletes every Ask Arkray conversation that touches the person: any question or answer
  citing one of their records, or naming them (their name, email or a phone number; the
  organisation's name only for a lead that is just an organisation, since for a person it
  names their colleagues too), in a question's text or an answer. Whole conversations,
  because a follow-up answer can be written from the earlier turns alone. Answers are
  serialised with the erasure: one being stored finishes before its sweep, and one composed
  from data read before the erasure committed is discarded instead of stored. A legal hold
  on someone else wins (deep security audit M-5): a conversation that touches the erased
  person but was asked by (or in the workspace of) a held user, or that also touches another
  held customer, is kept (`ai.subjects.held_conversations`, the one hold rule for Ask
  history), and `lead.erased` counts them (`conversations_held`). A hold on the erased lead
  itself never keeps them: the erasure refuses a held lead, and a ledger replay re-applies an
  erasure that was authorised (a hold restored with an older backup predates it), so a replay
  counts only the holds on other subjects. A kept conversation is deleted by the 30-day
  housekeeping once its hold is released (the erasure isn't re-run for it);
- clears every personal field of the lead and names it `[erased]` (archived, with a
  `lead.archived` timeline entry like any archive), and redacts the text of its activities
  and opportunities;
- **marks the lead as erased** (`erased_at`, the time of the first erasure; never cleared).
  From then on the erasure can't be undone through the CRM (deep security audit M-1): the
  lead can't be restored, edited or moved to another status; no deal, activity or file can be
  added for it; and the personal text of its deals and activities can't be written again: the
  deal's customer copy (customer, account, contact phone and email, address), instrument, work
  load, Expected CPT, description, lost reason (also through a move to a lost stage) and
  custom values, a new agreed CPT (a move into negotiation or a revised agreed price), and the
  title, description, place and link of every task, meeting and note. Each is refused with
  422 `lead_erased` and nothing is written. What isn't the person's data still changes: the
  deal's value, probability, dates, stage moves without a lost reason, task priority and due
  time, completing work, archiving. Sending a field with the value it already holds (an edit
  form) is not a change. Customer corrections of an erased lead are refused the same way
  (`lead_erased`), and so is an administrator's correction of its closed deals' figures (its
  reason would be new free text about the person; adversarial review of 73ff1e3, M2);
- blanks the opportunities' account, customer, contact and address fields, custom values,
  work load and Expected CPT and their derived names; a free-text instrument typed before the
  list existed is blanked too (one of the fixed list stays);
- deletes the files of the lead's notes (names and content hashes blanked, objects removed by
  a job) and refuses new files on them;
- deletes the Ask Arkray index chunks of those records and queues their re-indexing from the
  redacted text;
- records `lead.erased` (the administrator, the lead's id and counts, never a value);
- redacts the free text in the append-only tables, if there is any (the stage history's lost
  reasons, the agreed prices' CPTs and the reasons of its closed deals' corrections; their
  figures stay);
- last, appends `lead_erased` to the [erasure ledger](#restore-safe-erasure): a ledger that
  can't be written stops the erasure, with nothing changed.

Ids, dates, statuses, stage names and amounts stay, so the pipeline's figures and history keep
their shape without identifying anyone. A customer holds at most 5,000 activities
(`ACTIVITY_MAX_PER_CUSTOMER`, archived ones included; adversarial review of 73ff1e3, M6), and
the erasure redacts and deletes them in statements of at most 1,000 rows, still in one
transaction (about 2 s at that size); the automatic replay's worker job runs each statement
under 60 s, an operator's command under 120 s. **What it can't find:** the person named in other
leads' notes, tasks or meetings (search for the name across the organisation and edit those by
hand), and a conversation naming only the company of a person lead.

**Erasing again.** The erasure tool runs again whenever anything of the person is still
there: the lead's own fields, its deals' and activities' text, free text in the append-only
history (lost reasons, agreed CPTs) or a file of its notes. Text typed back in before the
marker existed (or by the previous release during a rolling deploy) is removed that way. It
answers "already erased" only when the lead is marked and nothing is left. A ledger replay
(`replay_erasures`) uses the same rule. Leads erased before this release were marked by the
migration (`leads.0007`) with the time of their first `lead.erased` audit event, or failing that from the
placeholder the erasure leaves. Run `erase_lead` with the new release's image during a rolling
deploy; an erasure by the previous release isn't marked until the tool is run again (which
marks it).

### Staff

Users are never deleted: their id attributes the history they made. When the organisation no
longer needs to know who a former colleague was, a super admin pseudonymises them
(`manage.py pseudonymise_user <id> --by <admin> [--yes]`, or the user's page; `privacy.manage`,
which Admins don't hold): only a
**deactivated** account that owns no current work (reassign it first), never under a legal
hold, never one's own. Their name becomes "Former user <8 hex>", their email
`former-<id>@pseudonymised.invalid`, their password unusable and their last sign-in
forgotten; sessions, pending links and support sessions end; their Ask Arkray conversations,
the conversations administrators asked in their workspace and every other conversation whose
question or answer names them (full name or email, as whole words, ignoring case: "Anna
Lee" doesn't match "Hanna Leeds"; deep security audit L-5), except those a
legal hold keeps ([legal holds](#legal-holds)), the audit details about them (addresses, emails, reasons), the reasons of support sessions
involving them, failed-sign-in evidence, queued email payloads naming them and their second factor (the encrypted TOTP secret and recovery-code hashes, ADR-0033) are deleted.
An Admin's team conversations (kind `team`, ADR-0035) have no subject, like the
organisation's: they go when the Admin is the one pseudonymised (their own) or when a question
or answer names the person, never merely because the person was in the team.
Their id, role, dates, ownership, the append-only trail and every amount and negotiated
price stay. Audited (`user.pseudonymised`, counts only), recorded in the erasure ledger,
irreversible, idempotent. This is not lead erasure: their customers are untouched. The
matching reads every stored answer once, so the operation lifts the statement timeout to
20 s (it runs inside a web request from the user's page: gunicorn allows 30 s, nginx 35 s;
an erasure, which has no API path, allows 120 s).

**Pseudonymisation is final** (adversarial review of 73ff1e3, L4, R157). It used to be
inferred from the email's domain, which no user-administration action asked, so an
administrator could type the old name back, or reactivate the account, point it at their own
mailbox and accept the invitation: the former employee's id was a live login again. Now
`pseudonymised_at` marks the account; the database holds a marked account to its pseudonym
(deactivated, no usable password, that name and email: CHECK `identity_user_pseudonym_final`)
and keeps the mark (trigger `identity_user_pseudonym_kept`, which the application's role
can't disable); user administration refuses every change to it (422 `account_pseudonymised`:
name, role, email, password, reactivation, invitation, two-step reset, clearing a sign-in lock,
a support session). Accounts pseudonymised before this release were marked by migration
`identity.0008` (renamed or revived ones put back under their pseudonym, their pending links
revoked and their sessions ended), and only those a `user.pseudonymised` audit event names, or
that are still under their own pseudonym with no usable password and not active: a live
account an administrator merely gave an address at `pseudonymised.invalid` is never
pseudonymised by it, it is listed in the migrate job's log
(`pseudonym_domain_accounts_not_pseudonymised`, ids only) to be given a real address, and one
holding a former user's pseudonym is moved aside to `reserved-<its own hex>@pseudonymised.invalid`
(review round 1, IDENTITY-4 and IDENTITY-5). That domain is reserved: no invitation, email
change or new account may use it. Someone who returns is invited as a new user.

**Never another administrator's account through the API** (audit SEC-2,
[ADR-0030](adr/0030-administrator-accounts-change-only-by-their-owner.md)): pseudonymising
replaces the account's email and password, which only the administrator themselves may
change, and nobody pseudonymises their own account. So for an account whose **role** is
administrator of either kind (a Super admin or an Admin, ADR-0035), whatever its status (a
deactivated former administrator included), the API
answers 403 `permission_denied` and changes nothing, and the user's page doesn't offer it. The
service (`privacy.staff`) enforces it, not only the view: it takes the user-administration
lock and the account's row lock and reads the role from the locked row, so a promotion racing
it is either seen or finds the account changed (`tests/security/test_staff_pseudonymisation_administrators.py`,
with real races). The one way is the higher-trust operator path, with shell and database
access: `manage.py pseudonymise_user <id> --by <admin> --allow-administrator [--yes]`; it keeps
every other rule (deactivated, no current work, no hold, not oneself) and the audit event says
`administrator_target`. Restoring a backup re-applies an administrator's pseudonymisation like
any other (it was authorised when it happened).

### Custom fields

Removing a pipeline's custom field keeps its values, hidden (removal is reversible). To delete
them, whoever may configure the pipeline either ticks **Also delete these fields' stored
values** when removing them, or deletes an already removed field's values by confirming its
name. The request is audited (`pipeline.field_values_deletion_requested`); a job, once the
request has committed, removes the field's key from every deal of the pipeline in batches, row by row atomically (a
concurrent edit of the deal's other values is kept), leaving deals under a legal hold
(counted), records the deletion in the erasure ledger in the same transaction as its first
removal, and audits the result (`pipeline.field_values_deleted`). Irreversible. Asking again
is harmless (adversarial review of 73ff1e3, M3, R150): a request that finds no deal of its
workspace still holding a value queues nothing, requests made before the job runs share it,
and the job records the deletion in the ledger only in a transaction that removes values,
once, so repeating the request can't grow the ledger. The answer is the same (202) either way.
One request is one job and one ledger entry, however many fields it names (removing 30 fields
with their values had written 30 entries; review round 1, LEDGER-2), and a person may ask for
the deletion of at most 100 fields' values in any 24 hours
(`MAX_VALUE_DELETIONS_PER_USER_PER_DAY`; 429 `rate_limited` with `Retry-After`, nothing
changed; a field removal asking for deletion is refused as a whole): each one is written to the
ledger for good.
Configuring a pipeline is not authority over every deal in it (final production audit
SEC-1): a deal reassigned with its lead stays in its old owner's personal pipeline, out of
their sight. So outside the organization-wide workspace the deletion is refused as a whole
(403, nothing removed, archived or queued) while any deal of another user holds a value, and
the job and its ledger entry are limited to the workspace owner's deals: a deal reassigned
between the request and the job keeps its value, after a restore too. Removal hides the
values from every deal of the pipeline, so it follows the same rule. Removing a field, or deleting its values, is refused (403) outside the organisation-wide
workspace while a deal outside the caller's scope (one reassigned with its lead, still in
the caller's pipeline), or a deal of theirs whose customer they no longer see (a closed deal
kept after its lead was reassigned), holds a value of it (deep security audit L-4 and its
review; final production audit SEC-1 already covered the deletion). The deletion job of a
workspace touches only deals whose owner and whose lead's owner are that workspace's owner,
so a deal whose customer moved elsewhere keeps its value. An administrator removes such a
field, or deletes every deal's values, from the organisation-wide workspace.

### Attachments

Deleting a file hides it at once; a job removes its object from storage and then its name and
content hash (the row keeps type, size, uploader, deleter and times as the audit trail's
evidence, and the file name becomes "removed"). The same happens to an upload that never
finished and to a file the virus scan blocked. A stored file that reconciliation found
missing keeps its hash (how a bucket restore proves the object it brings back). A legal hold
on the lead defers the purge entirely. Storage being down forgets nothing: the job retries.
With a versioned bucket, set `ATTACHMENT_S3_PURGE_VERSIONS=true` (and grant
`s3:ListBucketVersions` and `s3:DeleteObjectVersion`) to remove every version at once;
otherwise the bucket's lifecycle rule expires them. Files deleted before this release keep
their names until an administrator runs `manage.py forget_attachment_names --by <admin>
--yes` (audited; [runbooks.md](runbooks.md#forget-names-of-deleted-files)).

### Historical deals

When a lead is reassigned, its open deals follow it, but a closed deal stays with the
salesperson who worked it (their sales history). That person no longer looks after the
customer, so they see the deal without the customer's identity and contact details: the
customer name, phone, email and address are blank, the deal is named by its organisation and
instrument ("Customer restricted" when the organisation is the person's own name), free-text
custom values are left out, and the page says why. Global search finds such a deal only by
its organisation and instrument. The same rule applies to the pipeline's lists and boards,
activity and timeline links and Ask Arkray's tools (its search names the deal the same way,
in the answer and its citations, and its retrieval never returns such a deal's text); an
administrator's support session sees
what the user sees. Writes follow reads: what they can't see they can't write or probe
either (403 `customer_restricted` whatever the value sent; deep security audit M-2). The new
owner and an administrator organisation-wide see everything
([authorization.md](authorization.md#historical-deals)). Conservative by default: whether a
previous owner should keep a customer's contact details is a product decision (R118), and so
is the deal's own free text. **One rule since the adversarial review of 73ff1e3 (L6):** the
API showed the description, lost reason, work load and CPTs verbatim while Ask Arkray withheld
some of them; now every surface (the deal's page, lists, board, its stage history, agreed
prices and corrections, and Ask Arkray's tools) withholds the description, lost reason, work
load, Expected CPT and agreed CPTs, the histories' lost reasons, agreed CPTs and correction
reasons, and free-text custom values, from the one definition in `pipeline/customer.py`. The
organisation, the instrument and every figure stay. The deal is read-only to them: its text
because it may describe the customer (403 `customer_restricted`, so a change can't reveal an
erasure or a hold of that customer), its figures because the deal is closed (422
`deal_closed`, [pipeline.md](pipeline.md#closed-deals)).

## Restore-safe erasure

A database restore (a dump or point-in-time recovery) returns every row to its state at the
backup, the people erased since included. So every erasure-like operation (a lead erased, a
user pseudonymised, a custom field's values deleted, a file purged after deletion) also
appends an entry to the **erasure ledger**, kept where database backups never go (a volume of
its own, or an S3 bucket with versioning and Object Lock), and the database records how far it
has applied it (`core_erasure_ledger_state`):

- entries hold ids and times only; each carries an HMAC (`ERASURE_LEDGER_KEY`, a key of its
  own) over its content and the previous entry's HMAC, and is written once (written in full
  under a temporary name and then linked to its own, or `If-None-Match: *`): an entry
  changed, removed or reordered breaks the chain, and a full disk leaves no partial entry;
- an erasure appends its entry last, just before its transaction commits; a deletion carried
  out by a job (a removed field's values, a deleted file's object) is appended by the job,
  after the request committed, in the transaction that removes the data and only when it
  removes some, so a request that fails leaves no entry behind and a repeated one adds none
  (adversarial review of 73ff1e3, M3). Files purged together (deleted within moments of each
  other, an erasure's files, an hourly clean-up) share one `attachment_deleted` entry naming
  up to 100 of them; a replay purges each again. An erasure queues one paged job for all its
  files (a minute of purging a run, then a continuation), so it costs the same statements
  however many files the customer had (review round 1, QUEUES-2). A commit
  that fails after the entry was written leaves the ledger ahead and the API closed; the
  **automatic reconciliation** (`privacy.reconcile_ledger`, every minute, the one task that
  runs while the gate is closed) then carries out the erasure that was asked for and the
  API reopens once the database matches the ledger, never before (final production audit
  ARCH-3). It acts only on a gap of at most `ERASURE_LEDGER_AUTO_RECONCILE_MAX` entries
  (10) whose earlier entries are exactly those the database applied, with no erasure in
  progress; a larger gap (a restore), a different ledger, or an entry it can't apply waits
  for an operator ([runbooks.md](runbooks.md#erasure-ledger-behind));
- the key can be rotated without losing or re-signing anything
  (`ERASURE_LEDGER_VERIFY_KEYS`, `manage.py rotate_erasure_ledger_key`): a `key_rotated`
  entry signed with the new key names it by an id (never the key) and anchors a SHA-256
  digest of every entry before it, so the old key can then be removed and can never add
  entries again ([runbooks.md](runbooks.md#rotate-the-erasure-ledger-key), ARCH-5);
- the ledger costs the same at any size (adversarial review of 73ff1e3, M3, R150): the last
  entry is found from the one a process last verified (one small LIST and one GET on S3, a
  name check on a volume), entries are read only after the last verified one, which must
  still be there unchanged (an entry naming another number than its own, or the last verified
  entry changed or removed, is tampering), and a check never holds a request, a task or the
  readiness probe more than 2 s: past that the gate answers from its last verified state
  while the read finishes. A database position below the chain a process verified closes the
  gate at once, without reading the ledger, and a ledger read decides "ahead" on its own, so a
  slow store can't keep a restored database's gate open (review round 1, LEDGER-1). Every
  verification leaves **signed checkpoints** in the ledger's own store, beside the entries
  (`<prefix>.checkpoints/` in a bucket, a `checkpoints/` directory on a volume; one per 1,000
  entries, an HMAC with the ledger key, no personal data); a process starts from the newest one
  that verifies and whose entry is still there, reading only the entries after it (1,000 in
  memory at a time; LEDGER-2, LEDGER-3; a store that refuses checkpoints only makes starts read
  the whole chain), and each worker container's health check (`manage.py ledger_status`) still
  re-verifies the whole chain from scratch at least hourly, a slice per probe, as do
  `replay_erasures`, the key rotation and preflight. A verdict that the chain was tampered with
  holds, in the health check and in the process that found it, until a verification of the
  whole chain from scratch succeeds, and is logged as a security event at every probe (review
  round 2, LEDGER2-1); `manage.py ledger_status --from-scratch` runs that verification at once.
  A decided closed verdict (behind, ahead, tampered) stands until another read decides, and an
  erasure appends only while the database's last applied entry is in the ledger unchanged
  (otherwise 503, nothing written), so it never regrows a rolled-back ledger onto the
  database's position (review round 3, LEDGER3-1). An entry verified before that is replaced by
  anything that isn't an entry is tampering too; only a body beyond everything verified that
  isn't JSON (a torn write, a proxy's error page) is a passing fault (review round 4,
  LEDGER4-1). A restore is detected exactly as before (the database's position
  against the ledger's head);
- every process compares the database with the ledger at most once a minute, reading only
  the entries added since its last check (the whole chain at its first). A check that
  falls between an erasure's entry and its commit sees the eraser holding the append lock
  and looks again at the next request, never closing anything. While the
  database is **behind** (a restored backup), **ahead** (the ledger lost or swapped), or the
  ledger is **tampered with** or **unreadable** (beyond an hour after a verified state, or at
  once in a process that never verified it, as after a restore), the API answers 503 and the
  readiness probe fails: a restored database is never served before its erasures are
  re-applied. The background workers wait too (every task, the outbox included, is deferred
  while the gate is closed): a restored backup's queued work (an export of someone erased
  since, an email to a pseudonymised address, re-indexing erased text) never runs first;
- `manage.py replay_erasures --by <admin> --report <file>` (the owner's credentials) verifies
  the whole ledger, re-applies every entry idempotently (erases the lead again, deletes
  its index chunks and files, pseudonymises the user again, deletes the custom values again,
  purges the file again), then applies whatever was appended meanwhile until it reaches the
  ledger's head, records the database as current while holding the append lock (never
  moving it backwards), and writes a reconciliation report (ids, outcomes and counts). The
  API and the workers resume within a minute. A ledger that doesn't hold what the database
  has applied (fewer entries, or other ones: an empty directory where the volume wasn't
  mounted, another environment's ledger) is refused before anything changes, never
  "replayed" as complete (final production audit ARCH-6).

Tested in `tests/security/test_erasure_ledger.py` and drilled for real with
`tests/drills/restore_drill.py` (an encrypted `backup.sh` dump, erasures after it,
`restore.sh` into an empty database, the API refused while behind and while the ledger is
unavailable, the replay, every check passing): [runbooks.md](runbooks.md#restore-drill).
Production refuses to start without a ledger.

## Backups

`scripts/backup.sh` writes only encrypted dumps (OpenPGP, to the backup public key in
`BACKUP_GPG_RECIPIENT_FILE`; the plaintext exists only in a private temporary directory for
the length of the read-back check). The private key is kept offline by two named people and
brought in only to restore ([runbooks.md](runbooks.md#backup-encryption-keys)). Dumps leave
out sessions. Attachment files and the ledger are not in them. Retention: 30 days by policy
(`--prune`, opt-in, honouring `LEGAL_HOLD`). An erasure reaches a backup only as it expires,
and the ledger re-applies it if that backup is ever restored.

## Decisions that aren't the system's

| Decision | Default in the code | Owner |
|---|---|---|
| Legal basis for each processing, privacy notices, records of processing | none (not a software matter) | the organisation (privacy/legal) |
| How long customers' records are kept (a bulk retention job) | until erased on request | the organisation |
| How long audit details are kept | 90 days (`AUDIT_DETAIL_RETENTION_DAYS`) | the organisation, with security |
| How long logs are kept at the log platform | 30 days recommended | operations, with privacy |
| Backup retention | 30 days | operations |
| Whether a previous owner keeps a closed deal's customer contact details, and the deal's own free text | both hidden (conservative; R118, extended to the free text by the adversarial review of 73ff1e3, L6) | product owner |
| Whether a staff member's subject-access export includes the Ask Arkray conversations they deleted that a legal hold keeps | included (the data is still held; the export then tells them a hold kept them) | legal / privacy |
| Whether a commercial document must be kept for a minimum period despite a deletion request | not enforced (a legal hold is the mechanism) | legal |
| Whether to enable the Anthropic model (data-processing agreement, region, retention, a real-model evaluation) | off (`AI_LLM_PROVIDER=none`) | the organisation (R68) |
| Contracts and regions for SMTP, object storage, database, log platform, backup store | not verifiable in code | the organisation |
| The review of an export before releasing it (third parties named in free text) | manual | the administrator handling the request |

## Breaches

Follow the incident runbook ([runbooks.md](runbooks.md#security-incident)): contain, rotate
the affected secrets, read the audit trail (`auth.*`, `workspace.accessed`, `lead.erased`,
`privacy.*` and the rest) for what was accessed, and notify as the law and the organisation's
policy require.
