"""Reconciling the database with the erasure ledger without an operator, when that is safe
(final production audit ARCH-3; docs/runbooks.md#erasure-ledger-behind).

An erasure appends its ledger entry last, just before its transaction commits (core.ledger).
When the commit then fails (a worker killed by its time limit or a deploy, a web worker
killed while a slow S3 put returns, a constraint checked at COMMIT), the ledger is ahead of
the database: every process reads "behind" and the API closes, exactly as after a restore,
although nothing was restored. The erasure was asked for, so the repair is the replay's:
apply the entry again (privacy.replay, the safe direction).

`reconcile()` (the beat task `privacy.reconcile_ledger`, every minute; the one task allowed
to run while the gate is closed) does that without an operator, but only when the gap looks
like that and nothing else:

- the database's applied state is a prefix of the ledger: the entry it last applied is in
  the ledger with the same MAC (`replay.require_consistent`). A wrong, empty or rolled-back
  ledger is never "repaired": the API stays closed for an operator;
- the database is behind by at most ERASURE_LEDGER_AUTO_RECONCILE_MAX entries (default 10;
  0 turns this off). A restore from an older backup is an operator's job, with its dry run
  and its report (`manage.py replay_erasures`);
- no erasure is appending right now (the gap may be one about to commit);
- no missing erasure's lead or user has been put under a legal hold since: replay applies
  an erasure whatever holds say (it was authorised when asked for), so that decision is an
  operator's.

It applies only the entries the database hasn't, as the system (no operator), and moves
the applied state only once they all succeeded: the gates reopen within
ERASURE_LEDGER_CHECK_INTERVAL_S, never before the database matches the ledger. An entry it
can't apply (a lead whose lost reasons only the schema owner may redact) leaves the API
closed, and the error log names the command to run. One run at a time per cache.
"""

from __future__ import annotations

import logging
from uuid import UUID

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError

from arkray.core import holds, ledger
from arkray.core.models import HoldSubject
from config.celery import GatedTask

from . import replay

logger = logging.getLogger(__name__)

_RUNNING_KEY = "privacy:reconcile-ledger:running"
_RUNNING_TTL_S = 10 * 60
OPERATOR = "an operator must run manage.py replay_erasures --by <admin email> --report <file>"
HELD = f"a missing erasure's subject is under a legal hold: {OPERATOR}"


def reconcile() -> str:
    """One reconciliation pass; returns what it found or did (logged when it matters)."""
    if not ledger.enabled():
        return "disabled"
    try:
        # The database's position first, then the head from there (one small read at any
        # ledger size, security review M3): every entry it applied was written before its
        # transaction committed, so a head read after it holds them all.
        applied_seq, applied_mac = ledger.applied()
        last = ledger.head(applied_seq)
    except (ledger.LedgerError, DatabaseError):
        return "unavailable"  # the gate reports it; nothing to repair from here
    head_seq = last.seq if last else 0
    if applied_seq == head_seq:
        if last is not None and applied_mac != last.mac:
            logger.error("erasure_ledger_reconciliation_refused", extra={"status": "diverged"})
            return "diverged"
        return "ok"
    if applied_seq > head_seq:
        logger.error("erasure_ledger_reconciliation_refused", extra={"status": "ahead"})
        return "ahead"
    if ledger.appending():
        return "settling"
    behind, limit = head_seq - applied_seq, settings.ERASURE_LEDGER_AUTO_RECONCILE_MAX
    if behind > limit:
        logger.error(
            "erasure_ledger_reconciliation_required",
            extra={"count": behind, "reason": f"more than {limit} entries behind: {OPERATOR}"},
        )
        return "operator"
    try:
        held = _held(applied_seq)
    except ledger.LedgerError as exc:
        logger.error(
            "erasure_ledger_reconciliation_refused",
            extra={"status": type(exc).__name__, "reason": OPERATOR},
        )
        return "refused"
    if held:
        logger.error(
            "erasure_ledger_reconciliation_required", extra={"count": behind, "reason": HELD}
        )
        return "operator"
    if not _claim():
        return "busy"
    try:
        report = replay.replay(operator=None, missing_only=True)
    except ledger.LedgerError as exc:
        logger.error(
            "erasure_ledger_reconciliation_refused",
            extra={"status": type(exc).__name__, "reason": OPERATOR},
        )
        return "refused"
    finally:
        cache.delete(_RUNNING_KEY)
    if not report.complete:
        logger.error(
            "erasure_ledger_reconciliation_failed",
            extra={"failures": report.counts().get(replay.FAILED, 0), "reason": OPERATOR},
        )
        return "failed"
    logger.warning(
        "erasure_ledger_reconciled",
        extra={"entries": behind, "ledger_seq": report.ledger_head_seq},
    )
    return "reconciled"


def _held(applied_seq: int) -> bool:
    """Whether a missing erasure's subject is under a legal hold now."""
    subjects = {"lead_erased": HoldSubject.LEAD, "user_pseudonymised": HoldSubject.USER}
    for entry in ledger.verify()[applied_seq:]:
        if entry.kind in subjects and holds.is_held(subjects[entry.kind], UUID(entry.subject)):
            return True
    return False


def _claim() -> bool:
    """One pass at a time (replay is idempotent, so a cache outage only costs work)."""
    try:
        return bool(cache.add(_RUNNING_KEY, "1", _RUNNING_TTL_S))
    except Exception:  # noqa: BLE001 — the cache is an optimisation here
        return True


# Gated like every task, but exempt: it is what reopens the gate (config.celery.GatedTask).
@shared_task(
    name="privacy.reconcile_ledger", ignore_result=True, base=GatedTask, ledger_gate_exempt=True
)
def reconcile_ledger() -> str:
    return reconcile()
