# 0026. User-defined pipelines, negotiated prices, support sessions without impersonation, admin-set passwords, note attachments

Status: Accepted
Date: 2026-10-05

## Context
The product enhancement phase asked for: pipelines every user can create, with stages they
choose and edit; a negotiation stage that captures the agreed price; richer opportunity data
and custom fields; "Admin can log in as any user"; an administrator creating users with a
password; "if a user updates their password, the admin gets the updated password"; editable
deal notes with files. Two of these, read literally, contradict the security architecture
(ADR-0003, ADR-0005, ADR-0013): an administrator logging in as a user (impersonation), and an
administrator receiving a user's plaintext password.

## Decision
- **Pipelines have an owner.** `owner` NULL is an *organisation* (shared) pipeline, configured
  by administrators (`config.manage`) from the organisation-wide workspace; the seeded
  "Sales Pipeline" stays one, and the default. A *personal* pipeline belongs to one user and is
  configured by whoever may write in that user's workspace (the user; an administrator with
  `crm.manage_any`, recorded as the actor with the user as the subject: no impersonation). A
  workspace sees shared pipelines, its owner's own, and **any pipeline holding one of its
  deals** (a deal follows its lead on reassignment and must never vanish). Every configuration
  change is versioned (409 on a stale version).
- **Stages are replaced as a whole list** (`PUT …/stages`): add, rename, retype, re-probability,
  reorder and remove in one versioned change. Behaviour follows a stage's **type** (open,
  negotiation, won, lost: the existing `category` plus `is_negotiation`), never its name. A
  stage holding current (non-archived) opportunities can't be removed (422: move them first);
  a used but empty stage is archived (history keeps its name); an unused one is deleted.
  Concurrency: a stage edit takes the pipeline FOR UPDATE, then FOR UPDATE on the stages it
  removes, retypes or moves; every move and creation takes KEY SHARE (creations SHARE) on the
  pipeline and KEY SHARE on its target stage *in the statement that reads it*, so moves
  queue at the pipeline before any stage lock and a removal and a move serialise;
  configuration changes never lock leads or opportunities. (The first version took the
  pipeline FOR NO KEY UPDATE, which doesn't conflict with KEY SHARE: a reorder deadlocked
  with a forward move. Found by the adversarial review, fixed, race-tested.)
- **Negotiated prices are an append-only history** (`pipeline_negotiation_price`, trigger like
  stage history): price, currency, stage (and its name then), actor, subject, support session,
  opportunity version, time. Entering a negotiation stage requires a price on every path (one
  move service; no API bypass); re-entering asks again; revisions while negotiating append.
  The opportunity keeps a copy of the latest price; `value` ("Installation price") stays the
  amount pipeline totals use (a negotiated price never silently rewrites it).
- **Custom fields are per pipeline, values in one JSONB column.** Definitions are rows (type
  fixed after creation, bounded counts and sizes, plain-text names); values are validated
  against the definitions under a SHARE lock on the pipeline, stored canonically (strings for
  numbers, money and dates; option ids for choices), audited by field id only. No DDL, ever.
- **"Log in as a user" is a support session, not impersonation.** The administrator stays
  signed in as themselves; a time-limited (30 min), audited session, bound to their browser
  session and to one active non-administrator user, lets them open only that user's CRM.
  Every write keeps the administrator as the actor and the user as the subject, and carries
  the session id (a new `support_session_id` audit column and history column). Identity and
  security operations (user administration, passwords, security events, another session) are
  refused during it. It ends on exit, expiry, sign-out, a browser-session change, or when its
  conditions fail (checked on every request).
- **Administrators may set passwords, never learn them afterwards.** Creating a user with an
  initial password, or setting a new one, stores only the hash; the user must change it at
  first sign-in (a server-side gate) and an unchanged temporary password expires (72 h).
  Administrators can't set another administrator's password. **No administrator ever
  receives a plaintext password**: the "notification" is a security event (who changed their
  password, when), shown in an allowlisted security-events feed.
- **Attachments live in object storage, metadata in PostgreSQL.** Explicit type allowlist
  checked by content; generated keys; streamed bounded uploads; a row written before the
  object (housekeeping finds every object that might exist); downloads through the API only,
  re-authorised every time, `attachment` disposition, sandboxing CSP; images previewed inline
  only when validated; ClamAV boundary (no pretend scanner: unscanned files are labelled so).

## Consequences
- The pipeline module grew a configuration surface; the board, lists, totals, Ask Arkray and
  the dashboard work over the pipelines a workspace may see.
- A database backup no longer contains everything: attachment objects need their own backup
  (docs/deployment.md#attachments-storage).
- `django-storages[s3]` (boto3) is a new runtime dependency.
- Support sessions add one query per API request for an administrator in one.
- The business requirement "the admin gets the updated password" is deliberately not met;
  the report says so.

## Alternatives considered
- **Impersonation (a session as the user)**: rejected again (ADR-0005): it loses the real
  actor, mixes sessions and is an attack surface.
- **Per-user stage tables or DDL per custom field**: rejected; schema changes at runtime are
  unbounded and unauditable.
- **Overwriting the negotiated price**: rejected; the history is the requirement.
- **Files in PostgreSQL (bytea)**: rejected; it bloats backups and the WAL and couples the
  database to file traffic.
- **Reversible password storage or emailing passwords**: rejected outright.
