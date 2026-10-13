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
  replay_erasures` re-applies the rest; a small gap that continues what the database
  applied (a failed commit) is re-applied automatically (privacy.reconcile);
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

**Bounded** (security review M3, review round 1): a request, a readiness probe or a task
waits at most CHECK_WAIT_S (`is_open`) whatever the ledger's size or its store's speed, and
the gate still closes on a store slower than that:

- the database's position is read first. Entries this process has verified stay in the
  ledger (it only grows), so a database whose position is below them is **behind** at once,
  with no read of the ledger at all (unless an erasure is appending: "settling");
- otherwise the ledger is read on a thread of its own (the store only, no database
  connection), recording the database's position taken just before the read began. The
  check waits for it at most CHECK_WAIT_S, never twice for the same read; past that the
  read goes on in the background, the check answers from the state this process last
  verified (open within ERASURE_LEDGER_MAX_STALE_S of a good comparison, closed in a process
  that never had one) and is made again PENDING_RECHECK_S later, which uses the read once
  it has finished (a read is used for as long as the check interval from when it ended,
  however long it took);
- a finished read decides on its own: below the database's position taken before it began
  means **ahead** (every entry the database applied was written before its transaction
  committed); a database that moved on since the read began needs a newer read. "Behind" is
  confirmed by the database's position alone (an erasure may have committed meanwhile),
  never by a second read;
- a read finding fewer entries than this process verified (a ledger rolled back) doesn't
  verify the shorter chain again: the comparison closes the gate, and a different entry
  under a number already verified is tampering.

`GATE.status()` called directly (the management commands) waits for the read.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse, JsonResponse

from . import ledger
from .errors import error_body

logger = logging.getLogger(__name__)

OPEN = frozenset({"ok", "disabled", "settling"})
# What a read decided, which only another read's decision changes.
DECIDED_CLOSED = frozenset({"behind", "ahead", "diverged", "tampered"})
RETRY_AFTER_S = 60
MESSAGE = (
    "Arkray CRM is being reconciled after a restore and will be back shortly. Try again in a "
    "minute."
)
# The longest a request, a readiness probe or a task waits for this process's read of the
# ledger (module docstring); the readiness probe's own budget is a few seconds.
CHECK_WAIT_S = 2.0
# A check answered while its read was still running is made again this soon (at most every
# ERASURE_LEDGER_CHECK_INTERVAL_S), and uses the read once it has finished.
PENDING_RECHECK_S = 5.0
# The longest gunicorn's master spends bringing its verified chain up to date before a fork
# (`refresh`); what is left is read by the worker, in the background of its checks.
REFRESH_BUDGET_S = 5.0

_bounded = threading.local()


class _StillReading(ledger.LedgerUnavailable):
    """The read of the ledger didn't finish within the wait: answered as unavailable now,
    and checked again soon (PENDING_RECHECK_S)."""


@dataclass
class _Read:
    """One read of the ledger, on a thread of its own: its head, and the chain verified up
    to it (or the error), with the database's position taken just before it began."""

    started: float
    pid: int
    applied: tuple[int, str]
    # Whether it verifies the whole chain from scratch (no state to continue from).
    whole: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    head: ledger.Entry | None = None
    verified: ledger.Checked | None = None
    error: Exception | None = None
    finished: float = 0.0


def _read_ledger(job: _Read, known: ledger.Checked | None) -> None:
    """The head (from the entry last verified, or the database's position), then only the
    entries added since `known`, a chunk at a time (the whole chain from scratch in a process
    that never verified it, or since it found it tampered with). Far behind (a gunicorn
    worker recycled long after the master verified it; review round 2, LEDGER2-3), from the
    newest signed checkpoint past `known` instead. The store only: no database."""
    try:
        last = ledger.head(known.seq if known is not None else job.applied[0])
        verified = known
        if last is not None:
            if known is None:
                verified = ledger.verified_state()
            elif last.seq > known.seq:
                start = known
                if last.seq - known.seq > ledger.CHECKPOINT_EVERY:
                    newest = ledger.checkpoint(last.seq)
                    if newest is not None and newest.seq > known.seq:
                        start = newest
                verified = ledger.verified_state(after=start)  # only what was added since
            elif last.seq == known.seq and last.mac != known.mac:
                raise ledger.LedgerTampered(
                    f"Ledger entry {last.seq} changed after it was verified."
                )
            # Fewer entries than verified (a ledger rolled back or replaced): nothing is
            # verified again; the comparison closes the gate (module docstring).
        job.head, job.verified = last, verified
    except ledger.LedgerError as exc:  # handed to the check, which maps it
        job.error = exc
    except Exception as exc:  # noqa: BLE001 — anything else: the ledger couldn't be read
        logger.error("erasure_ledger_read_failed", extra={"error": type(exc).__name__})
        job.error = ledger.LedgerUnavailable("The ledger couldn't be read.")
    finally:
        job.finished = time.monotonic()
        job.done.set()


def _identity() -> tuple[str, tuple[str, ...]]:
    """What a verified chain belongs to: the ledger and the keys it was checked with."""
    return settings.ERASURE_LEDGER_URL, tuple(ledger._keys())


def _decide(applied: tuple[int, str], job: _Read) -> str:
    """The database's position against a finished read (module docstring); "moved" when
    the database applied entries after the read began (a newer read decides)."""
    if job.error is not None:
        raise job.error
    head_seq, head_mac = (job.head.seq, job.head.mac) if job.head else (0, "")
    if applied[0] == head_seq:
        return "ok" if applied[1] == head_mac else "diverged"
    if applied[0] < head_seq:
        return "behind"
    return "ahead" if job.applied[0] > head_seq else "moved"


class Gate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._valid_for = 0.0
        self._status = "unknown"
        self._last_ok_at: float | None = None
        # How far this process has verified the chain: only entries added since are read
        # when the head moves (erasures are rare), else just the head.
        self._verified: ledger.Checked | None = None
        self._identity: tuple[str, tuple[str, ...]] | None = None
        self._job: _Read | None = None
        # Sticky (review round 2, LEDGER2-1): once a check found the chain tampered with, it
        # answers "tampered" (a slow or failing read included) until a verification of the
        # whole chain from scratch succeeds; its verified state is dropped meanwhile.
        self._tampered = False

    # --- the ledger, read in the background ------------------------------------------------
    def _follow_configuration(self) -> None:
        """A verified chain is this ledger's, checked with these keys (tests change both)."""
        current = _identity()
        if self._identity != current:
            self._verified, self._job, self._identity = None, None, current
            self._tampered = False

    def _start_read(self, applied: tuple[int, str]) -> _Read:
        job = _Read(
            started=time.monotonic(),
            pid=os.getpid(),
            applied=applied,
            whole=self._verified is None,
        )
        thread = threading.Thread(
            target=_read_ledger, args=(job, self._verified), name="ledger-read", daemon=True
        )
        try:
            thread.start()
        except RuntimeError as exc:  # no thread to be had: unavailable, try again later
            raise ledger.LedgerUnavailable("The ledger can't be read right now.") from exc
        self._job = job
        return job

    def _finish(self, job: _Read) -> None:
        """Keep a finished read's progress (the chain only ever grows), then forget it. A
        verification of the whole chain from scratch that succeeded ends a tampered verdict;
        nothing else does (review round 2, LEDGER2-1)."""
        self._job = None
        if job.error is None and job.verified is not None:
            self._verified = job.verified
            if job.whole:
                self._tampered = False

    def _read(self, applied: tuple[int, str], wait: float | None, *, renew: bool = False) -> _Read:
        """A finished read: the one that ended since the last check if it ended within the
        interval (and isn't to be renewed), else a new one, waited for at most `wait` (None:
        until it ends). _StillReading past it; never waits twice for the same read."""
        job = self._job
        if job is not None and job.pid != os.getpid():
            job = self._job = None  # a read of the parent's, before this process forked
        if job is not None and not job.done.is_set():
            if wait is not None:
                raise _StillReading("The ledger is still being read.")
            job.done.wait()
        if job is not None:
            self._finish(job)
            interval = settings.ERASURE_LEDGER_CHECK_INTERVAL_S
            if not renew and time.monotonic() - job.finished <= interval:
                return job
        deadline = None if wait is None else time.monotonic() + wait
        job = self._start_read(applied)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not job.done.wait(remaining):
            raise _StillReading("The ledger is slow to answer.")
        self._finish(job)
        return job

    # --- the check ----------------------------------------------------------------------------
    def _behind(self) -> tuple[int, str] | None:
        """None when an erasure is appending (between its entry and its commit), else the
        database's position read again (one may have committed meanwhile)."""
        if ledger.appending():
            return None
        return ledger.applied()

    def _compare(self, wait: float | None) -> str:
        applied = ledger.applied()  # first (module docstring)
        known = self._verified
        if known is not None and applied[0] < known.seq:
            again = self._behind()
            if again is None:
                return "settling"
            applied = again
            if applied[0] < known.seq:
                return "behind"  # below entries verified here: no read needed
        if known is not None and applied[0] == known.seq and applied[1] != known.mac:
            return "diverged"
        job = self._read(applied, wait)
        found = _decide(applied, job)
        if found == "moved":
            job = self._read(applied, wait, renew=True)
            found = _decide(applied, job)
        if found == "behind":
            again = self._behind()
            if again is None:
                return "settling"
            found = _decide(again, job)
        if found == "moved":
            raise _StillReading("The database moved on during the read.")
        return found

    def _check(self, wait: float | None) -> tuple[str, bool]:
        """The status, and whether it waits on a read still running."""
        if not ledger.enabled():
            return "disabled", False
        self._follow_configuration()
        try:
            found = self._compare(wait)
        except _StillReading:
            return ("tampered" if self._tampered else "unavailable"), True
        except ledger.LedgerTampered as exc:
            if not ledger.lasting(exc, self._verified):
                # A body that isn't JSON beyond what this process verified: this read only
                # (review round 3, I-5); read again next.
                return "tampered", False
            # From scratch until it verifies: no state, and no earlier read, to continue from
            # (a verified entry no longer readable included: review round 4, LEDGER4-1).
            self._tampered, self._verified, self._job = True, None, None
            return "tampered", False
        except (ledger.LedgerUnavailable, DatabaseError):
            return ("tampered" if self._tampered else "unavailable"), False
        return ("tampered" if self._tampered else found), False

    def status(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        wait: float | None = getattr(_bounded, "wait", None)
        with self._lock:
            if self._checked_at is not None and now - self._checked_at < self._valid_for:
                return self._status
            found, pending = self._check(wait)
            if found == "settling":
                return found  # not remembered: the next request checks again
            if found == "ok":
                self._last_ok_at = now
            elif found in DECIDED_CLOSED:
                # A decided verdict: no stale window reopens the gate after it (review round
                # 3, LEDGER3-1: a rolled-back ledger reopened at each later check whose read
                # was still running: ok, ahead, ok, ahead...).
                self._last_ok_at = None
            elif found == "unavailable" and self._status in DECIDED_CLOSED:
                # The last decided verdict stands until a read decides otherwise: closed, and
                # under the same status (the alert's `for: 2m` keeps counting).
                found = self._status
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
            interval = settings.ERASURE_LEDGER_CHECK_INTERVAL_S
            self._valid_for = min(PENDING_RECHECK_S, interval) if pending else interval
            self._status, self._checked_at = found, now
            return found

    def prime(self) -> None:
        """Verify the whole chain now, outside any request: in a parent process before it
        forks, each child inherits the verified prefix and its first check reads only the
        entries added since (`_compare`). Without it every new gunicorn worker (recycled
        every ~2,000 requests) read the whole ledger inside its first request: one GET per
        entry on S3, which can outlast the 30 s worker timeout as the ledger grows
        (production readiness review). The status isn't set: the first check still compares
        with the database. A failure leaves the full check to that first request.

        From the newest signed checkpoint (core.ledger, review round 1: 33 s at 10,000
        entries on a 25 ms store), else from the start, a chunk at a time (bounded memory)."""
        with self._lock:
            self._follow_configuration()
            try:
                self._verified = ledger.verified_state(after=ledger.checkpoint())
            except ledger.LedgerTampered as exc:
                if ledger.lasting(exc, self._verified):
                    self._found_tampering()  # review round 4, LEDGER4-1: was a WARNING
                else:
                    logger.warning(
                        "erasure_ledger_prime_failed", extra={"error": type(exc).__name__}
                    )
            except Exception as exc:  # noqa: BLE001 — logged; the first check reads it all
                logger.warning("erasure_ledger_prime_failed", extra={"error": type(exc).__name__})

    def refresh(self, budget_s: float | None = None) -> None:
        """Bring the verified chain up to date, for at most REFRESH_BUDGET_S, outside any
        request: gunicorn's master before each fork (`pre_fork`), so a worker recycled long
        after the start inherits a recent state (review round 2, LEDGER2-3). From the newest
        checkpoint when far behind; progress is kept even when the budget runs out. A failure
        changes nothing (logged): the workers' own checks decide."""
        budget = REFRESH_BUDGET_S if budget_s is None else budget_s
        with self._lock:
            self._follow_configuration()
            if self._tampered:
                return
            known = self._verified
            try:
                # Every store call under one deadline, with short timeouts: the whole costs
                # the budget and one short call at most (review round 3, LEDGER3-2: the head,
                # the checkpoint and the first check ran before the budget, 15.5 s at 500 ms).
                with ledger.bounded_calls(budget):
                    last = ledger.head(known.seq if known is not None else 0)
                    if last is None or (known is not None and last.seq <= known.seq):
                        return
                    start = known
                    if known is None or last.seq - known.seq > ledger.CHECKPOINT_EVERY:
                        newest = ledger.checkpoint(last.seq)
                        if newest is not None and (known is None or newest.seq > known.seq):
                            start = newest
                    # Small reads (four rounds of parallel GETs, each GET refused once the
                    # deadline has passed): progress is kept a read at a time.
                    self._verified = ledger.verified_for(
                        start, budget, chunk=4 * ledger.READ_THREADS
                    )
            except ledger.LedgerTampered as exc:
                if ledger.lasting(exc, known):
                    # The workers it forks verify from scratch, as a gate that found it does;
                    # and it is a security event, not a warning (review round 3, I-1).
                    self._found_tampering()
                else:  # a body that isn't JSON beyond what it verified: a passing fault
                    logger.warning(
                        "erasure_ledger_refresh_failed", extra={"error": type(exc).__name__}
                    )
            except Exception as exc:  # noqa: BLE001 — logged; the workers' checks decide
                logger.warning("erasure_ledger_refresh_failed", extra={"error": type(exc).__name__})

    def _found_tampering(self) -> None:
        """Outside a request (prime, refresh; the lock held): from scratch from here on, in
        this process and those it forks, and the security event."""
        self._tampered, self._verified, self._job = True, None, None
        logger.error("erasure_ledger_tampered", extra={"status": "tampered", "security": True})

    def resume(self, verified: ledger.Checked) -> None:
        """Continue from a chain verified earlier, as a forked child does from its parent's
        (manage.py ledger_status, between its short processes): the next check reads the
        head from there and only the entries added since. The status isn't set."""
        with self._lock:
            self._follow_configuration()
            self._verified, self._job = verified, None

    def verified_state(self) -> ledger.Checked | None:
        """How far this process has verified the chain."""
        with self._lock:
            return self._verified

    def found_tampering(self) -> bool:
        """Whether this process found the chain tampered with (a mismatch, not a body it
        couldn't parse): sticky until a verification from scratch succeeds."""
        with self._lock:
            return self._tampered

    def reset(self) -> None:
        with self._lock:
            self._checked_at, self._status, self._last_ok_at = None, "unknown", None
            self._verified, self._job, self._identity = None, None, None
            self._valid_for, self._tampered = 0.0, False


GATE = Gate()


def prime() -> None:
    """GATE.prime(), when a ledger is configured (gunicorn's master and the Celery worker's
    parent call it before forking)."""
    if ledger.enabled():
        GATE.prime()


def refresh() -> None:
    """GATE.refresh(), when a ledger is configured (gunicorn's master, before each fork)."""
    if ledger.enabled():
        GATE.refresh()


@contextmanager
def bounded(wait: float = CHECK_WAIT_S) -> Iterator[None]:
    """GATE.status() inside waits at most `wait` for the ledger (module docstring)."""
    previous = getattr(_bounded, "wait", None)
    _bounded.wait = wait
    try:
        yield
    finally:
        _bounded.wait = previous


def bounded_status() -> str:
    """The gate's status, within CHECK_WAIT_S (requests, readiness, tasks, metrics)."""
    with bounded():
        return GATE.status()


def is_open() -> bool:
    return bounded_status() in OPEN


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
