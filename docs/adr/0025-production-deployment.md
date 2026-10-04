# 0025. Production deployment: restricted database role enforced at start, the edge proxy's contract, erasure on request

Status: Accepted
Date: 2026-10-04

## Context
Phase 11 took the application from "deployable" to "deployed the way production must be",
and closed what earlier phases had deferred to it:

1. **The runtime database role** (R74). The append-only trigger binds only a role that
   neither owns the audit and history tables nor is a superuser; the development stack ran
   everything as one superuser, and nothing stopped a deployment doing the same.
2. **The edge proxy** carried rules the application depends on (overwriting
   `X-Forwarded-For`, no query strings or one-time links in its log, one origin for pages and
   API, R25, R31, R61) but existed only as prose.
3. **Personal data** (R35): leads were archived, never deleted, with no way to honour an
   erasure request.
4. **Recovery**: no backup or restore procedure had been exercised.

## Decision
- **Two database roles, and the processes refuse the wrong one.** `infrastructure/postgres/roles.sql`
  creates `arkray_owner` (schema, migrations) and `arkray_app` (everything else); after every
  migrate the owner runs `manage.py grant_app_privileges arkray_app`, which grants
  read and write on ordinary tables and SELECT and INSERT only on the append-only tables,
  found by their trigger rather than a list. Gunicorn's master (before forking) and every
  Celery worker check their own role at start (`arkray.core.privileges`) and refuse to run as
  a superuser, as a role that owns or belongs to the owner of an append-only table, as a role
  that may UPDATE, DELETE or TRUNCATE one, or as one that can create objects. The refusal is
  on in production (`DB_REQUIRE_RESTRICTED_ROLE`) and opted out explicitly by the development
  stack. An unreachable database doesn't stop a start (readiness reports it). The same check
  is a deploy check (`check --deploy --database default`) for the release pipeline: not a
  plain database check, because `migrate` runs those, as the owner, on purpose.
- **The edge proxy is a reference configuration** (`infrastructure/nginx/arkray.conf`), verified with
  the application in a production-shaped stack (`infrastructure/compose.production.yml`): TLS and
  HSTS, overwritten forwarding headers, a minted request id the application trusts, a log
  format with the path only and redacted one-time links, `/api` straight to Django,
  `/health` not routed, and upstream names re-resolved while running.
- **Erasure is a command, not a feature of the UI.** `manage.py erase_lead <id> --by
  <admin>` clears the person's fields and the text of everything linked to them, deletes
  what was derived from that text (index chunks, stored answers) and records counts in the
  audit trail, in one transaction. Ids, dates, statuses, stage names and amounts stay, so
  figures and history keep their shape. The one append-only column holding free text
  (stage history's lost reason) is redacted by disabling the trigger for that statement
  only, inside the transaction, which needs the owner's credentials; the command says so
  when they're needed.
- **Backups are drilled.** `scripts/backup.sh` and `scripts/restore.sh` (an empty target
  only, checksum verified, `VACUUM (ANALYZE)` after, more memory for the index builds),
  measured on the million-lead copy.

## Consequences
- A deployment that connects the application as the owner or a superuser fails to start,
  loudly, instead of running with an audit trail any compromised process could rewrite.
- The release pipeline gains two steps (the grant after migrate, the deploy check), and an
  administrator runs `roles.sql` once per environment.
- Erasure is an operator's act with an audit record, not something a sales user can do by
  mistake; it reaches backups only as they expire (R79).
- The proxy configuration is part of the release: a change to the forwarding or logging
  rules is a change to the system's security, reviewed like code.

## Alternatives considered
- **Checking the role only in the release pipeline**: a deployment that skipped the step,
  or a worker started by hand, would run privileged. The process itself checks.
- **Row-level security or a SECURITY DEFINER function for erasure's history redaction**:
  more machinery than one owner-only statement in a transaction that already holds the lock.
- **Deleting erased leads outright**: breaks the pipeline's history and figures, and the
  foreign keys that keep the append-only records consistent.
