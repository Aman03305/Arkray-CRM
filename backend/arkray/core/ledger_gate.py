"""Closing the API while the database doesn't match the erasure ledger
(arkray.core.ledger; docs/runbooks.md#restore-from-backup).

Each process compares the ledger's last entry with the database's LedgerState at most every
ERASURE_LEDGER_CHECK_INTERVAL_S (one small query and one read of the ledger: nothing on the
request path in between), verifying the whole chain at its first check and, after that,
only the entries added since (the store is write-once). An erasure writes its entry just
before its transaction commits: a check that falls in between sees the ledger one entry
ahead while the eraser still holds the append lock, and is repeated at the next request
instead of closing anything ("settling"). The API answers 503
`erasure_reconciliation_required` while:

- **behind**: the database hasn't applied every entry (a restored backup, or an erasure that
  failed after its entry was written): someone erased may be back. `manage.py
  replay_erasures` re-applies the rest;
- **ahead / diverged**: the database records entries the ledger doesn't have (the ledger was
  rolled back, replaced or pointed elsewhere): erasures may be lost. An operator must
  restore the right ledger (never edit the database's state to match);
- **tampered**: an entry's MAC or link is wrong;
- **unavailable**: the ledger can't be read, and this process hasn't seen a good state for
  ERASURE_LEDGER_MAX_STALE_S (a process that never has, like every process after a restore,
  stays closed until it can read it). A brief outage of the ledger's store doesn't take the
  CRM down; a restore with no ledger never serves its data.

The health readiness check reports the same (core.health), so a load balancer stops sending
traffic too. With no ledger configured (development, tests) the gate is open; production
refuses to start without one.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse, JsonResponse

from . import ledger
from .errors import error_body

logger = logging.getLogger(__name__)

OPEN = frozenset({"ok", "disabled", "settling"})
RETRY_AFTER_S = 60
MESSAGE = (
    "Arkray CRM is being reconciled after a restore and will be back shortly. Try again in a "
    "minute."
)


class Gate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._status = "unknown"
        self._last_ok_at: float | None = None
        # The ledger head whose whole chain this process has verified: the full chain is
        # read again only when the head moves (erasures are rare), else just the head.
        self._verified: tuple[int, str] | None = None

    def _compare(self) -> str:
        last = ledger.head()
        if last is not None and self._verified != (last.seq, last.mac):
            known = self._verified
            if known is not None and last.seq > known[0]:
                ledger.verify(after=known)  # only what was added since
            else:
                ledger.verify()
            self._verified = (last.seq, last.mac)
        applied_seq, applied_mac = ledger.applied()
        head_seq, head_mac = (last.seq, last.mac) if last else (0, "")
        if applied_seq == head_seq:
            return "ok" if applied_mac == head_mac else "diverged"
        return "behind" if applied_seq < head_seq else "ahead"

    def _check(self) -> str:
        if not ledger.enabled():
            return "disabled"
        try:
            found = self._compare()
            if found == "behind":
                if ledger.appending():
                    return "settling"  # an erasure between its entry and its commit
                found = self._compare()  # one may have committed between the two reads
        except ledger.LedgerTampered:
            return "tampered"
        except (ledger.LedgerUnavailable, DatabaseError):
            return "unavailable"
        return found

    def status(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        with self._lock:
            interval = settings.ERASURE_LEDGER_CHECK_INTERVAL_S
            if self._checked_at is not None and now - self._checked_at < interval:
                return self._status
            found = self._check()
            if found == "settling":
                return found  # not remembered: the next request checks again
            if found == "ok":
                self._last_ok_at = now
            elif (
                found == "unavailable"
                and self._last_ok_at is not None
                and now - self._last_ok_at < settings.ERASURE_LEDGER_MAX_STALE_S
            ):
                found = "ok"  # a brief outage after a verified state: stay open
            if found != self._status and found not in OPEN:
                logger.error("erasure_ledger_gate_closed", extra={"status": found})
            elif found != self._status and self._status not in ("unknown", *OPEN):
                logger.warning("erasure_ledger_gate_opened", extra={"status": found})
            self._status, self._checked_at = found, now
            return found

    def reset(self) -> None:
        with self._lock:
            self._checked_at, self._status, self._last_ok_at = None, "unknown", None
            self._verified = None


GATE = Gate()


def is_open() -> bool:
    return GATE.status() in OPEN


class LedgerGateMiddleware:
    """503 for API requests while the gate is closed (module docstring). After the request
    context middleware, so the refusal is logged and carries a request id."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith("/api/") and not is_open():
            response: Any = JsonResponse(
                error_body("erasure_reconciliation_required", MESSAGE), status=503
            )
            response["Retry-After"] = str(RETRY_AFTER_S)
            response["Cache-Control"] = "no-store"
            return response  # type: ignore[no-any-return]
        return self.get_response(request)
