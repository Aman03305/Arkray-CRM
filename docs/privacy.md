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
| Opportunities | the deal's copy of its customer (customer and account names, contact phone and email, address), its name (derived from the customer and instrument), description, lost reason, work load, Expected CPT; amounts and prices (commercial, not personal) | `pipeline_opportunity`; lost reasons also in the append-only `pipeline_stage_history`, agreed prices and CPTs in the append-only `pipeline_negotiation_price` |
| Custom field values | whatever a pipeline's fields collect (free text included); values of **removed** fields stay, hidden, until deleted on purpose ([below](#custom-fields)) | `pipeline_opportunity.custom_fields` |
| Activities | notes (up to 10,000 characters), task and meeting titles and descriptions, meeting places and links | `activities_activity` |
| Attachments | the file itself (any content), its name and content hash, type, size, who uploaded and deleted it | bytes in private object storage (an S3 bucket or a private volume); details in `activities_attachment` |
| Timeline | user ids, kinds, dates, statuses, stage names; text is read live from the activity | `activities_timeline_entry` |
| Ask Arkray | questions as typed, stored answers (which quote CRM text), conversations (asker, workspace) | `ai_question`, `ai_conversation`; index chunks hold vectors, hashes and offsets, no text, but vectors are **derived** from personal text (`ai_knowledge_chunk`) |
| Staff | name, email, role, status, sign-in and password-change times, an Argon2id password hash | `identity_user`; sessions in `django_session` |
| Support sessions | which administrator helped which user, when, and the reason typed (blanked after the audit-detail retention) | `identity_support_session` |
| Audit trail | who did what to which record, when: ids, field **names**, statuses, counts; never a customer's field values | `audit_event` (append-only) |
| Audit details | for a while only: the client address of security events, old and new sign-in emails, a reset requester's address, a support reason | `audit_event_detail` ([below](#audit-trail)) |
| Security evidence | client addresses and keyed hashes of submitted emails (never the email), device ids | `identity_auth_throttle_event` |
| Account links | a hash of each invitation or reset secret, never the secret | `identity_account_token` |
| Background work | ids; the account-email events' payloads hold an email address and, for a reset request, the requester's address (blanked 24 h after they finish); error text with email addresses replaced | `core_outbox_event` |
| Idempotency records | a SHA-256 of a create request (24 h) | `core_idempotency_record` |
| Data exports | who exported whose data and when (the export file itself lives 24 h) | `privacy_data_export`; files in private storage (`STORAGES["exports"]`) |
| Legal holds | the held subject's id and a case reference | `core_legal_hold` |
| Erasure ledger | ids of erased leads, pseudonymised users, deleted custom fields and files; no personal data | outside the database ([below](#restore-safe-erasure)) |
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
| **Anthropic** (the language model) | only with `AI_LLM_PROVIDER=anthropic` (the default is `none`; production runs `none` until the conditions in [rag-architecture.md](rag-architecture.md#privacy-and-data-minimisation) are met) | the question and up to four earlier turns, with links, email addresses and phone numbers replaced by markers; tool results: lead name, organisation, job title, status and dates; deal names, account, customer (only where the asker may see the customer), amounts, prices, CPTs, work load, lost reason, description; activity titles and note passages (masked the same way). Never: contact fields, addresses, meeting links or places, custom values, audit data | HTTPS only (`AI_LLM_BASE_URL` must be https; SDK variables refused) | the provider's, under the organisation's agreement (not verified here) |
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
| Account-email payloads (an email address, a requester's address) | 24 hours after the email is sent | hourly housekeeping blanks them |
| Finished background work | 7 days (`OUTBOX_DONE_RETENTION_DAYS`) | hourly housekeeping; dead events until an operator resolves them |
| Idempotency records, workspace access windows | 24 hours / 30 minutes | hourly housekeeping |
| Ask Arkray questions and answers | 30 days after each answer (`AI_CONVERSATION_RETENTION_DAYS`), whatever happens in the conversation since | hourly housekeeping; a user can delete a conversation at any time |
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
attachment purges on their notes, the deletion of their deals' custom values and the
support-reason blanking. The reference names the matter, never describes it. Placing and
releasing are audited (`privacy.legal_hold_placed` / `_released`). A backup directory is held
by creating a file named `LEGAL_HOLD` in it.

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

An administrator who has verified the requester's identity exports their data from the
customer's (lead's) page or the user's details (**Export data**; the **Data requests** page
under Admin lists the administrator's own exports and their downloads): the request names a reference (the ticket),
confirms the identity check, and is audited (`privacy.export_requested`). A job builds a ZIP:

- `data.json` (machine-readable) and `csv/*.csv` (the same as tables; a value a spreadsheet
  would run as a formula is prefixed with a quote);
- for a **customer**: the lead (every field, archived or not), its deals in full (the customer
  copy, amounts, prices, CPTs, custom values by field name, removed fields' values too, stage
  and price history), every task, meeting and note in full text (not the screens'
  240-character previews), attachment details and the stored, downloadable files up to
  `EXPORT_MAX_BYTES` (100 MB; the rest are listed with the reason), the timeline, and the
  Ask Arkray questions and answers that cite the customer's records or name them, question by
  question (never the rest of a conversation that once touched them; another record cited
  in an answer shows as "[another record]"). Staff appear by role only;
- for a **staff member**: their account, their sign-ins and the security events about them
  (with their own client address while the audit detail keeps it), the support sessions for
  them, their own Ask Arkray conversations, and counts of the records they own.

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

### Correction

A customer's identity and contact details (name, organisation, job title, email, phones,
address) are corrected on the lead's page (**Correct details**), by whoever may edit in the
workspace that sees the lead: one transaction, the lead's version checked (409 if someone
changed it), audited by field names (`lead.corrected`). Every deal whose copy of the customer
still holds the **old** value follows (`opportunity.customer_corrected`; its derived name
too); a copy someone deliberately made different, the deal's amounts, prices and history are
left as they are. Search, the dashboard and Ask Arkray (re-indexed) show the corrected
details. An erased lead can't be corrected. A staff member's name and email are corrected by
an administrator on the Users page (another administrator's email only by that
administrator, R100); the change is audited, an email change with its old and new address in
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
  from data read before the erasure committed is discarded instead of stored;
- clears every personal field of the lead and names it `[erased]` (archived, with a
  `lead.archived` timeline entry like any archive), and redacts the text of its activities
  and opportunities;
- blanks the opportunities' account, customer, contact and address fields, custom values,
  work load and Expected CPT and their derived names; a free-text instrument typed before the
  list existed is blanked too (one of the fixed list stays);
- deletes the files of the lead's notes (names and content hashes blanked, objects removed by
  a job) and refuses new files on them;
- deletes the Ask Arkray index chunks of those records and queues their re-indexing from the
  redacted text;
- records `lead.erased` (the administrator, the lead's id and counts, never a value);
- redacts the free text in the append-only tables, if there is any (lost reasons, agreed
  CPTs);
- last, appends `lead_erased` to the [erasure ledger](#restore-safe-erasure): a ledger that
  can't be written stops the erasure, with nothing changed.

Ids, dates, statuses, stage names and amounts stay, so the pipeline's figures and history keep
their shape without identifying anyone. **What it can't find:** the person named in other
leads' notes, tasks or meetings (search for the name across the organisation and edit those by
hand), and a conversation naming only the company of a person lead.

### Staff

Users are never deleted: their id attributes the history they made. When the organisation no
longer needs to know who a former colleague was, an administrator pseudonymises them
(`manage.py pseudonymise_user <id> --by <admin> [--yes]`, or the user's page): only a
**deactivated** account that owns no current work (reassign it first), never under a legal
hold, never one's own. Their name becomes "Former user <8 hex>", their email
`former-<id>@pseudonymised.invalid`, their password unusable and their last sign-in
forgotten; sessions, pending links and support sessions end; their Ask Arkray conversations,
the audit details about them (addresses, emails, reasons), the reasons of support sessions
involving them, failed-sign-in evidence and queued email payloads naming them are deleted.
Their id, role, dates, ownership, the append-only trail and every amount and negotiated
price stay. Audited (`user.pseudonymised`, counts only), recorded in the erasure ledger,
irreversible, idempotent. This is not lead erasure: their customers are untouched.

### Custom fields

Removing a pipeline's custom field keeps its values, hidden (removal is reversible). To delete
them, whoever may configure the pipeline either ticks **Also delete these fields' stored
values** when removing them, or deletes an already removed field's values by confirming its
name. The request is audited (`pipeline.field_values_deletion_requested`); a job, once the
request has committed, records it in the erasure ledger and removes the field's key from every deal of the pipeline in batches, row by row atomically (a
concurrent edit of the deal's other values is kept), leaving deals under a legal hold
(counted), and audits the result (`pipeline.field_values_deleted`). Irreversible.

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
what the user sees; the new owner and an administrator organisation-wide see everything
([authorization.md](authorization.md#historical-deals)). Conservative by default: whether a
previous owner should keep a customer's contact details is a product decision, and so is the
deal's own description and lost reason, which stay visible to them (R122).

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
  after the request committed, so a request that fails leaves no entry behind. (A commit
  that fails after the entry was written leaves the ledger ahead: the API closes until the
  replay carries out the erasure that was asked for, R122);
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
  API and the workers resume within a minute.

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
| Whether a previous owner keeps a closed deal's customer contact details | hidden (conservative) | product owner |
| Whether a commercial document must be kept for a minimum period despite a deletion request | not enforced (a legal hold is the mechanism) | legal |
| Whether to enable the Anthropic model (data-processing agreement, region, retention, a real-model evaluation) | off (`AI_LLM_PROVIDER=none`) | the organisation (R68) |
| Contracts and regions for SMTP, object storage, database, log platform, backup store | not verifiable in code | the organisation |
| The review of an export before releasing it (third parties named in free text) | manual | the administrator handling the request |

## Breaches

Follow the incident runbook ([runbooks.md](runbooks.md#security-incident)): contain, rotate
the affected secrets, read the audit trail (`auth.*`, `workspace.accessed`, `lead.erased`,
`privacy.*` and the rest) for what was accessed, and notify as the law and the organisation's
policy require.
