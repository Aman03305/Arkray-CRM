# Privacy: personal data, retention and requests

Arkray CRM holds personal data about two groups: **leads** (the people and organisations the
sales team works with) and **users** (the staff who sign in). This document says what is
held, where, for how long, and how a request to see or erase a person's data is met. The
legal basis, the retention periods the business chooses and the response to a data
principal are the organisation's decisions; the system provides the mechanisms. Security
controls are in [security.md](security.md#personal-data).

## What is held, and where

| Data | Personal data in it | Where |
|---|---|---|
| Leads | names, organisation, job title, email, phone numbers, address, free-text description | `leads_lead` |
| Activities | notes, task and meeting titles and descriptions, meeting places and links (often about a lead) | `activities_activity` |
| Opportunities | titles, descriptions and lost reasons (may mention a person) | `pipeline_opportunity`; lost reasons also in the append-only `pipeline_stage_history` |
| Timeline | none: kinds, dates, statuses, stage names and ids; text is read live from the activity | `activities_timeline_entry` |
| Users (staff) | name, email, role, sign-in times | `identity_user`, `django_session` |
| Security evidence | client addresses and keyed hashes of submitted emails (never the email) | `identity_auth_throttle_event`; `audit_event` (client address of some events) |
| Account links | a hash of each invitation or reset secret, never the secret | `identity_account_token` |
| Audit trail | who did what to which record: ids, field **names**, statuses, counts; no lead values. One exception, about staff: a user's email change records the old and new address (the account-takeover trail) | `audit_event` |
| Ask Arkray | questions as typed, stored answers (which quote CRM text) | `ai_question`; index chunks hold vectors, hashes and offsets, no text (`ai_knowledge_chunk`) |
| Background work | ids; an email address only in the payloads of account-email events (blanked 24 h after they finish); error text is stored with email addresses replaced (Phase 11 review: an SMTP rejection quotes the recipient) | `core_outbox_event` |
| Logs | none by design: ids, routes, statuses, timings; no names, emails, phone numbers, search terms or note text (tested) | stdout, the log platform |
| Backups | everything above except sessions | the backup store |

Sent elsewhere: emails go through the configured SMTP provider (the account emails only);
with `AI_LLM_PROVIDER=anthropic`, a non-routed question and the CRM context it needs go to
the model provider ([rag-architecture.md](rag-architecture.md#privacy-and-data-minimisation),
R68). With `AI_LLM_PROVIDER=none` no CRM text leaves the deployment; embeddings are always
computed locally.

## Retention

| Data | Kept | Mechanism |
|---|---|---|
| Leads, opportunities, activities | until erased on request or under the organisation's retention policy (archiving keeps them; it hides, it doesn't delete) | `manage.py erase_lead` ([below](#erasure)) |
| Timeline entries, stage history | the life of the record | append-only; lost reasons redacted on erasure |
| Audit trail | **indefinitely** (a security record; no field values) | append-only. Set a retention period with the business; deleting old rows is a DBA operation as the schema owner ([runbooks.md](runbooks.md#purge-old-audit-events)) |
| Sessions | at most 12 hours (2 hours idle) | expired sessions deleted hourly; never in backups |
| Login and reset throttle evidence | 24 hours | hourly housekeeping (`AUTH_THROTTLE_RETENTION_S`) |
| Account-email payloads (an email address) | 24 hours after the email is sent | hourly housekeeping blanks them |
| Finished background work | 7 days (`OUTBOX_DONE_RETENTION_DAYS`) | hourly housekeeping; dead events until an operator resolves them |
| Idempotency records, workspace access windows | hours | hourly housekeeping |
| Ask Arkray conversations and answers | 30 days after the last question (`AI_CONVERSATION_RETENTION_DAYS`) | hourly housekeeping; a user can delete a conversation at any time |
| Ask Arkray index chunks | as long as the text they were computed from | deleted with it (and by an erasure); rebuilt from the CRM on demand |
| Account-link records | the account's lifetime (no secret, no personal data beyond the user link) | — |
| Logs | the log platform's retention (no personal data by design) | platform setting |
| Backups | 30 days | the backup store's lifecycle rule; an erasure reaches backups only as they expire |

## Requests

**Access (a copy of a person's data).** An administrator opens the lead in the
organisation-wide workspace: the lead record, its timeline (every note, task, meeting and
opportunity with their text) and its opportunities show everything the CRM holds about the
person. Ask Arkray answers older than 30 days no longer exist; newer ones are the asking
users' and are not personal records of the lead.

**Correction.** Edit the lead or the activity; the audit trail records that a field changed,
not its old or new value.

### Erasure

On a verified request an administrator runs:

```
manage.py erase_lead <lead id> --by admin@example.com          # what would be erased
manage.py erase_lead <lead id> --by admin@example.com --yes    # erase it
```

with the schema owner's database credentials (the migrate job's) when the lead's
opportunities were lost with a reason; otherwise the application's are enough (the command
says which). In one transaction it:

- deletes every Ask Arkray conversation that touches the person: any question or answer
  citing one of their records, or naming them (their name, email or a phone number; the
  organisation's name only for a lead that is just an organisation, since for a person it
  names their colleagues too), in a question's text or an answer. Whole conversations,
  because a follow-up answer can be written from the earlier turns alone. Answers are
  serialised with the erasure: one being stored finishes before its sweep, and one composed
  from data read before the erasure committed is discarded instead of stored (the question
  fails `ai_unavailable`; asked again, it is answered from what is left). A question asked
  while the erasure runs waits for it. Other users' questions are untouched;
- clears every personal field of the lead and names it `[erased]` (archived, with a
  `lead.archived` timeline entry like any archive), and redacts the text of its activities
  and opportunities;
- deletes the Ask Arkray index chunks of those records and queues their re-indexing from
  the redacted text (an indexing job that read the old text meanwhile is overwritten);
- records `lead.erased` (the administrator, the lead's id and counts, never a value);
- blanks the opportunities' account, customer, contact and address fields and their custom
  values (their names are derived from the customer since ADR-0028: the title is replaced
  too; the instrument, work load and Expected CPT describe the deal, not the person, and
  stay), deletes the files of the lead's notes (names and content hashes blanked, objects
  removed by a job; an upload in flight is removed when it finishes) and, the lead being
  archived, refuses new files and text on its notes (product enhancement phase);
- last, redacts the lost reasons in the stage history, if there are any.

Ids, dates, statuses, stage names and amounts stay, so the pipeline's figures and history
keep their shape without identifying anyone. An erased lead can't be erased again (nothing
of the person is left, and its placeholder name would match every other erased lead's
conversations). The operator is found as at sign-in (`--by` normalised like an email) and
must hold the CRM-management capability. **What it can't find:** the person named in
other leads' notes, tasks or meetings (search for the name across the organisation and
edit those by hand), and a conversation that names only the company of a person lead. Backups keep the old data until they expire (30 days), and versioned attachment buckets
keep earlier object versions until their lifecycle expires them (R93). Implemented in
`arkray/privacy` and tested (`arkray/privacy/tests/test_erasure.py`: every planted value of
the person gone from the lead, activities, opportunities, history, index, stored questions
and answers (follow-ups and failed questions included) and the audit event; a second lead
and its conversation untouched; an answer read before the erasure discarded, a waiting
question answered afterwards from the redacted text; the records re-indexed; the history
append-only again afterwards). The races (an answer, an indexing job and a follow-up
question during an erasure) run with real concurrent transactions in
`tests/integration/test_audit_races.py`, each mutation-checked. Verified live on the production-shaped stack, with
the application's credentials and with the owner's.

**Staff leaving.** Deactivate the user (Users page): they can't sign in and their sessions
end; their records are reassigned. Their name and email stay for the audit trail's
attribution.

## Breaches

Follow the incident runbook ([runbooks.md](runbooks.md#security-incident)): contain, rotate
the affected secrets, read the audit trail (`auth.*`, `workspace.accessed`, `lead.erased`
and the rest) for what was accessed, and notify as the law and the organisation's policy
require.
