# 0032. Privacy remediation: verified backend TLS, redacted logs, expiring audit details, data-subject rights, restore-safe erasure

Status: Accepted (engineering); the product and legal decisions it names stay open
Date: 2026-10-10

## Context
The privacy data-flow audit of 2026-10-10 found no exposure (0 P0, 0 P1) but 12 P2 gaps:
backend connections could run without verified TLS; Celery failure logs and other exception
messages skipped redaction; Ask Arkray replayed cited titles and questions to the model
unmasked; client addresses, old and new emails and support reasons were kept forever in the
append-only audit trail; logs held addresses with no rotation and the docs said they held no
personal data; there was no export, no correction path since ADR-0028, no staff erasure; a
restored backup brought erased people back; deleted files kept their names and hashes; a
closed deal's previous owner kept its customer's contact details after a reassignment; and
`docs/privacy.md` was out of date. Reproducing them found two more: `?sslmode=` in
`DATABASE_URL` was silently dropped (base settings replaced the URL's options), and kombu
connects to a `rediss://` broker without certificate checks unless `ssl_*` parameters are
given.

## Decision
1. **Backend TLS, fail closed** (`arkray.core.transport`). Production refuses: PostgreSQL
   without `sslmode=verify-full` and a CA (`sslrootcert=<file>` or `system`); Redis without
   `rediss://` and `ssl_cert_reqs=required` (and the broker gets explicit `broker_use_ssl`);
   SMTP without STARTTLS/TLS (`arkray.core.mail.VerifiedSMTPBackend`: system CAs or
   `EMAIL_TLS_CA_FILE`, TLS 1.2+); an `http://` S3 endpoint; clamd anywhere but loopback or a
   listed private name. Exceptions are explicit: loopback, `BACKEND_TLS_PRIVATE_HOSTS`
   (single-label container names only), or the local stack's opt-in, itself refused for any
   public host name. The URL's own database options are kept.
2. **Logs by allowlist** (`arkray.core.logging`). Extra fields are written only from
   `SAFE_FIELDS` (others listed by name under `withheld`); exception messages are withheld
   (types and frames kept; `LOG_EXCEPTION_MESSAGES` for local development only, refused in
   production); third-party message arguments are sanitised (Celery's failure line keeps task
   and id, gunicorn's loses query strings, and gunicorn's error log goes through the same
   formatter); the client address is on the access line only; `ai_tool_failed` logs a type.
   Container logs rotate (5 x 10 MB); the edge drops the user agent and redacts one-time
   links case-insensitively.
3. **AI payloads masked before sending**: cited labels in replayed answers, earlier questions
   and the question itself pass through `formatting.user_text`; stored text stays as typed.
   The provider stays `none` until a real-model evaluation and the contractual prerequisites
   (R68) are done.
4. **Expiring audit details** (`audit_event_detail`). Client addresses (security events only),
   old/new emails, reset requesters' addresses and support reasons live apart from the
   append-only event, sealed by a salted digest on the event, and expire after
   `AUDIT_DETAIL_RETENTION_DAYS` (90, a starting policy) unless a legal hold covers the event.
   The application never updates an audit row; legacy rows are moved once by the owner's
   audited `audit_minimise_legacy` (the documented exception, tamper-evident by a digest of
   what it moved).
5. **Legal holds** (`core_legal_hold`) stop every erasure, purge and expiry for the held person.
6. **Access**: an administrator-only, audited, asynchronous export (JSON + CSV + files),
   downloadable by the requester only for 24 hours, size-capped, rate-limited, refused in a
   support session; other people appear by role or as "[another record]"; release is
   reviewed by a person.
7. **Correction amends ADR-0028**: the lead page is no longer only read-only. A narrow
   correction of the lead's identity and contact fields updates every deal copy that still
   holds the old value; deliberately different copies and all commercial history are kept.
8. **Staff pseudonymisation** for deactivated users who own no current work: identity replaced,
   sessions, links and support sessions ended, personal remnants deleted, attribution and
   financial history kept. Not lead erasure.
9. **Restore-safe erasure**: an HMAC-chained, write-once erasure ledger outside the database;
   the API and readiness close while the database is behind or ahead of it, the ledger is
   tampered with, or unreadable without a recent verified state; `replay_erasures`
   re-applies it with a reconciliation report. Production refuses to start without one.
   Background workers defer every task while the gate is closed. An erasure appends its
   entry last in its transaction; a deletion a job carries out is appended by the job, after
   the request committed (the backend review's P1: a request that failed after appending
   left an entry that a replay would have acted on). The gate verifies only new entries and
   treats a check between an entry and its commit as settling, not closed; the replay
   applies entries added while it runs and records its progress under the append lock.
   Backups are encrypted (OpenPGP, public key on the backup host, private key offline).
10. **Attachments**: a purge removes the name and hash with the object; legal holds defer it;
    optional removal of every S3 version.
11. **Historical deals**: a viewer who can't see a closed deal's lead sees it without the
    customer's identity and contact details on every surface, Ask Arkray's search and
    retrieval included (conservative default; a product decision to confirm, as is whether
    the deal's own description and lost reason stay visible to them, which they do today).
12. **Custom values of removed fields**: deleted only on purpose (confirmed, audited,
    ledgered, legal holds respected), by an atomic per-row job.
13. **Sessions**: browser-session cookies (the server keeps the idle and absolute limits and
    the row's pinned expiry); the UI defaults to invitations; password mutations keep nothing
    in the query cache. The login-device cookie is **kept** at sign-out after review: it holds
    keyed hashes and a random id only, and clearing it would let an attacker's guesses lock a
    signed-out owner out (its anti-lockout purpose).
14. Ask Arkray retention is per answer (30 days), not per conversation; a user under a legal
    hold keeps theirs.
15. **Exports end with their subject**: erasing or pseudonymising someone withdraws their
    queued and ready exports and deletes the files; an export's file is written under one
    key per export (a retry replaces it) and deleted by any build that finds its export no
    longer queued, and by the time-out of a build that never finished.

## Consequences
- A misconfigured production deployment fails at start instead of running in clear; Supabase
  and other managed PostgreSQL need their CA file (or `system` for a publicly trusted one) in
  `sslrootcert`; Supavisor/PgBouncer accept TLS the same way.
- Logs are less verbose about exceptions; developers see full messages locally.
- Security investigations have client addresses for 90 days (configurable), not forever.
- Operations gain a mandatory replay step after any restore and must run the ledger's own
  storage; a ledger outage longer than an hour closes the API (fail closed by design).
- New schema: `audit_event_detail`, `audit_event.detail_digest`, `core_legal_hold`,
  `core_erasure_ledger_state`, `privacy_data_export`; every new migration refuses to be
  reversed (ADR on rollback: the previous release's backup is the way back).

## Alternatives considered
- **Regex masking of all log text**: misses what it doesn't recognise; an allowlist fails
  closed.
- **Nulling IPs in place with a privileged job**: defeats append-only integrity; rejected
  except for the one-off, audited legacy move.
- **Keeping the erasure list inside the database**: restored with it; useless.
- **Clearing the login-device cookie at sign-out**: privacy gain small (no readable data),
  security cost real (targeted lockouts); rejected.
- **Excluding historical deals from search entirely**: the owner couldn't find their own
  sales history; matching on organisation and instrument only is enough.
