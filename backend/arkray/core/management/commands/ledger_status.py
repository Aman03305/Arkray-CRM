"""This process's view of the erasure ledger gate (core.ledger_gate), for container health
checks (final audit ARCH-1: workers that couldn't see the ledger looked healthy while they
skipped every task).

    manage.py ledger_status                       the gate's status; exit 1 unless open
    manage.py ledger_status --ping-worker ai      and the Celery worker ai@<this host> answers

The gate is compared afresh (the ledger read and verified, the database's applied position
queried): one short process, so probe it every minute or so, not every few seconds. With
`--ping-worker` the worker named `<name>@<hostname>` (Celery's `--hostname name@%h`) must
answer a ping through the broker within 5 s: a hung or dead worker turns unhealthy, and so
does every worker while the broker is down (none of them can work then). Prints the status
only, never a path or a key.

**At any ledger size** (security review M3, review round 1): each probe verified the whole
chain from scratch, one GET per entry on S3, every minute in every Celery container, until it
outlasted its 30 s timeout (33 s at 10,000 entries on a 25 ms store; a killed probe kept
nothing, so a restarted worker never turned healthy). Now every probe is bounded:

- a probe keeps the chain it verified in a file of the container's temporary directory (the
  verification state only, never an entry or a key; HMAC-signed with ERASURE_LEDGER_KEY,
  named after the ledger and its keys, written under a random temporary name), and the next
  probe starts from there like a forked worker from its parent: the head, and only the
  entries added since;
- a container's first verification starts from the newest signed checkpoint (core.ledger)
  and is spread over probes, AUDIT_ENTRIES_PER_PROBE entries each, toward the head as it was
  when it began (the progress is kept): until it ends the probe answers
  `erasure_ledger_gate=verifying` (exit 1; health checks allow for it in their start period);
- the whole chain is verified again from scratch: once FULL_CHECK_EVERY_S has passed since
  the last complete pass (at once after a start from a checkpoint), each probe verifies the
  next AUDIT_ENTRIES_PER_PROBE entries from the first, toward the entry the kept state ended
  with when the pass began; the pass completes there with the same MAC. An entry altered or
  removed after it was verified fails it: "tampered", and the file is removed, so every later
  probe starts over. Entries signed with a key retired since are carried to the rotation that
  vouches for them, never read at once.

A file that is missing, unreadable, from another ledger or key set, or whose MAC doesn't match
counts for nothing (a first verification, as above).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.core import ledger, ledger_gate

logger = logging.getLogger("arkray.core.ledger_gate")

PING_TIMEOUT_S = 5.0
# The whole chain is verified again at least this often (an hour)...
FULL_CHECK_EVERY_S = 60 * 60
# ...this many entries per probe at most...
AUDIT_ENTRIES_PER_PROBE = 5_000
# ...and no further slice once a probe has spent this long, well inside the health check's
# 30 s timeout (review round 2, LEDGER2-4)...
PROBE_BUDGET_S = 15.0
# ...a slice being this many entries, each kept as it is verified.
_SLICE = 500
VERIFYING = "verifying"
_STATE_LABEL = b"arkray-ledger-status-state"


class Command(BaseCommand):
    help = "Print the erasure ledger gate's status; exit 1 unless it is open."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--ping-worker",
            metavar="NAME",
            help="also require the Celery worker NAME@<this host> to answer a ping",
        )
        parser.add_argument(
            "--from-scratch",
            action="store_true",
            help="verify the whole chain from its first entry now, trusting no checkpoint and"
            " nothing kept (an operator's check, e.g. after a restart; may take long)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not ledger.enabled():
            status = ledger_gate.GATE.status()
        elif options["from_scratch"]:
            status = _from_scratch()
        else:
            status = _probe()
        self.stdout.write(f"erasure_ledger_gate={status}")
        if status not in ledger_gate.OPEN:
            raise CommandError(f"The erasure ledger gate is closed ({status}).")
        if options["ping_worker"]:
            from config.celery import app

            node = f"{options['ping_worker']}@{socket.gethostname()}"
            replies = app.control.ping(destination=[node], timeout=PING_TIMEOUT_S) or []
            if not any(node in reply for reply in replies):
                raise CommandError(f"The Celery worker {node} didn't answer a ping.")
            self.stdout.write(f"celery_worker={node} ok")


def _probe() -> str:
    kept = _load() or _EMPTY
    deadline = time.monotonic() + PROBE_BUDGET_S
    try:
        if kept["tampered"]:
            return _verify_again(kept, deadline)
        if kept["verified"] is None:
            return _first_verification(kept, deadline)
        ledger_gate.GATE.resume(kept["verified"])
        status = _gate_status()
        return _audit(kept, status, deadline) if status in ledger_gate.OPEN else status
    except ledger.LedgerTampered as exc:
        return _found(exc, kept)
    except ledger.LedgerUnavailable:
        return "unavailable"


def _found(exc: ledger.LedgerTampered, kept: dict[str, Any]) -> str:
    """Sticky (review round 2, LEDGER2-1: every later probe says so too, while a pass from
    scratch, never from a checkpoint, runs until one succeeds), with the security event at
    every probe: any mismatch; a body that isn't an entry where this container verified one
    before (its kept chain, or the checkpoint its first verification began at); and anything
    once a tampered verdict is kept (review round 4, LEDGER4-1: an old entry replaced by an
    error page was answered as a passing fault, with a WARNING). A body that isn't JSON
    beyond anything verified (a corrupted new entry, a proxy's error page) is refused for this
    probe only and read again by the next (review round 3, I-5)."""
    if not kept["tampered"] and not ledger.lasting(exc, _verified_before(kept)):
        logger.warning("erasure_ledger_malformed", extra={"status": "tampered"})
        return "tampered"
    _save(None, None, None, 0, 0.0, tampered=True)
    return _tampered()


def _verified_before(kept: dict[str, Any]) -> int:
    verified = kept["verified"]
    return max(verified.seq if verified is not None else 0, int(kept["base"]))


def _from_scratch() -> str:
    """`--from-scratch` (review round 3, I-2): the whole chain from its first entry, at once,
    whatever is kept and whatever checkpoints say; the verdict is kept like a probe's (a
    tampered one sticky, a good one clearing it)."""
    kept = _load() or _EMPTY
    try:
        return _verify_again(
            {**_EMPTY, "tampered": True}, float("inf"), entries=sys.maxsize, operator=True
        )
    except ledger.LedgerTampered as exc:
        return _found(exc, kept)
    except ledger.LedgerUnavailable:
        return "unavailable"


def _gate_status() -> str:
    status = ledger_gate.GATE.status()
    if status == "tampered":
        if ledger_gate.GATE.found_tampering():
            raise ledger.LedgerTampered("The gate found the ledger tampered with.")
        # A body it couldn't parse: this probe only (review round 3, I-5).
        raise ledger.LedgerMalformed("The gate couldn't parse a ledger entry.")
    return status


def _tampered() -> str:
    """Every probe that answers "tampered" says so in the log (an ERROR, a security event:
    what the log platform alerts on, docs/observability.md#alerts), not only the first."""
    logger.error("erasure_ledger_tampered", extra={"status": "tampered", "security": True})
    return "tampered"


def _advance(
    progress: ledger.Checked,
    target: dict[str, Any],
    deadline: float,
    keep: Callable[[ledger.Checked], object],
    entries: int = 0,
) -> ledger.Checked:
    """The verification carried on toward `target` a slice of _SLICE entries at a time, each
    kept as it is reached (a probe killed by its health check keeps what it did), until
    the target, AUDIT_ENTRIES_PER_PROBE entries, or the probe's deadline (review round 2,
    LEDGER2-4: a slice bounded in entries only took 32.4 s on a 50 ms store)."""
    limit = entries or AUDIT_ENTRIES_PER_PROBE
    spent = 0
    while progress.seq < target["seq"] and spent < limit:
        step = min(_SLICE, target["seq"] - progress.seq, limit - spent)
        progress = ledger.verified_until(progress, target["seq"], step)
        spent += step
        keep(progress)
        if time.monotonic() >= deadline:
            break
    return progress


def _head_target() -> dict[str, Any]:
    last = ledger.head()
    return {"seq": last.seq if last else 0, "mac": last.mac if last else ""}


def _verify_again(
    kept: dict[str, Any],
    deadline: float,
    *,
    entries: int = 0,
    operator: bool = False,
) -> str:
    """After a tampered verdict: a pass over the whole chain from scratch, a slice per probe,
    answering "tampered" until it succeeds (at once for `--from-scratch`)."""
    progress, target = kept["audit"], kept["target"]
    if progress is None or target is None:
        progress, target = ledger.Checked(), _head_target()

    def keep(at: ledger.Checked) -> None:
        # An operator's pass runs at once: nothing to carry on, and the probes meanwhile
        # keep answering from what they kept (no "tampered" before anything is found).
        if not operator:
            _save(None, at, target, 0, 0.0, tampered=True)

    progress = _advance(progress, target, deadline, keep, entries)
    if progress.seq < target["seq"]:
        return _tampered()
    if progress.mac != target["mac"]:
        raise ledger.LedgerTampered(f"Ledger entry {target['seq']} changed.")
    ledger_gate.GATE.resume(progress)
    status = _gate_status()
    verified = ledger_gate.GATE.verified_state()
    if status in ledger_gate.OPEN and verified is not None:
        _save(verified, None, None, 0, _now(), clear=True)  # the whole chain verifies again
    return status


def _first_verification(kept: dict[str, Any], deadline: float) -> str:
    """A slice of this container's first verification (module docstring), from the newest
    checkpoint or the start toward the head as it was when it began."""
    progress, target, base = kept["audit"], kept["target"], kept["base"]
    if progress is None or target is None:
        # A checkpoint only where the pass from scratch it leads to can be kept going: with
        # nothing kept (an unwritable temporary directory) every probe would start from the
        # checkpoint again and the pass would never run (review round 3).
        keepable = _save(None, None, None, 0, 0.0)
        progress = (ledger.checkpoint() if keepable else None) or ledger.Checked()
        last = ledger.head(progress.seq)
        target = {"seq": last.seq if last else 0, "mac": last.mac if last else ""}
        base = progress.seq
    progress = _advance(progress, target, deadline, lambda at: _save(None, at, target, base, 0.0))
    if progress.seq < target["seq"]:
        return VERIFYING
    if progress.mac != target["mac"]:
        raise ledger.LedgerTampered(f"Ledger entry {target['seq']} changed.")
    ledger_gate.GATE.resume(progress)
    status = _gate_status()
    verified = ledger_gate.GATE.verified_state()
    if status in ledger_gate.OPEN and verified is not None:
        # From the start: the whole chain is verified. From a checkpoint: not yet by this
        # container, so the pass from scratch is due at once.
        _save(verified, None, None, 0, _now() if base == 0 else 0.0)
    return status


def _audit(kept: dict[str, Any], status: str, deadline: float) -> str:
    """Keep what this probe verified, and carry the pass over the whole chain further when
    one is due (module docstring)."""
    verified = ledger_gate.GATE.verified_state()
    if verified is None:
        return status
    audit, target, full_at = kept["audit"], kept["target"], kept["full_at"]
    if _now() - full_at >= FULL_CHECK_EVERY_S:
        if audit is None or target is None:  # a pass begins: toward where the kept state ends
            audit = ledger.Checked()
            target = {"seq": kept["verified"].seq, "mac": kept["verified"].mac}
        audit = _advance(
            audit, target, deadline, lambda at: _save(verified, at, target, 0, full_at)
        )
        if audit.seq >= target["seq"]:
            if audit.mac != target["mac"]:
                raise ledger.LedgerTampered(f"Ledger entry {target['seq']} changed.")
            audit, target, full_at = None, None, _now()
    _save(verified, audit, target, 0, full_at)
    return status


# --- the state kept between probes ----------------------------------------------------------
def _now() -> float:
    return time.time()


def _path() -> Path:
    """Named after the ledger and every configured key (by id): another ledger, or a key
    added or removed, starts from the whole chain again."""
    keys = ",".join(ledger.key_id(key) for key in ledger._keys())
    name = hashlib.sha256(f"{settings.ERASURE_LEDGER_URL}\n{keys}".encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"arkray-ledger-status-{name}.json"


def _as_dict(state: ledger.Checked | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "seq": state.seq,
        "mac": state.mac,
        "digest": state.digest,
        "retired": sorted(state.retired),
        "signers": sorted(state.signers),
        "key": state.key,
        "pending": state.pending,
    }


def _as_checked(data: dict[str, Any] | None) -> ledger.Checked | None:
    if data is None:
        return None
    return ledger.Checked(
        seq=int(data["seq"]),
        mac=str(data["mac"]),
        digest=str(data["digest"]),
        retired=frozenset(str(k) for k in data["retired"]),
        signers=frozenset(str(k) for k in data["signers"]),
        key=str(data["key"]),
        pending=int(data.get("pending", 0)),
    )


def _mac(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    secret = settings.ERASURE_LEDGER_KEY.encode()
    return hmac.new(secret, _STATE_LABEL + b"\n" + canonical, hashlib.sha256).hexdigest()


_FIELDS = ("verified", "audit", "target", "base", "full_at", "tampered")
_EMPTY: dict[str, Any] = {
    "verified": None,
    "audit": None,
    "target": None,
    "base": 0,
    "full_at": 0.0,
    "tampered": False,
}


def _load() -> dict[str, Any] | None:
    """The state the last probe kept, if it can be trusted (module docstring), else None."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        body = {k: data[k] for k in _FIELDS}
        if not hmac.compare_digest(str(data["mac"]).encode(), _mac(body).encode()):
            return None
        verified = _as_checked(body["verified"])
        if verified is not None and verified.pending:
            return None  # never a verified chain
        target = body["target"]
        return {
            "verified": verified,
            "audit": _as_checked(body["audit"]),
            "target": None
            if target is None
            else {"seq": int(target["seq"]), "mac": str(target["mac"])},
            "base": int(body["base"]),
            "full_at": float(body["full_at"]),
            "tampered": bool(body["tampered"]),
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _save(
    verified: ledger.Checked | None,
    audit: ledger.Checked | None,
    target: dict[str, Any] | None,
    base: int,
    full_at: float,
    *,
    tampered: bool = False,
    clear: bool = False,
) -> bool:
    """Kept for the next probe. Never clears a tampered verdict another probe kept meanwhile
    (the file is read again first), unless `clear`: a pass from scratch that succeeded
    (review round 3, I-3). False when nothing could be written (logged)."""
    if not tampered and not clear:
        current = _load()
        if current is not None and current["tampered"]:
            return True  # that verdict stands
    body = {
        "verified": _as_dict(verified),
        "audit": _as_dict(audit),
        "target": target,
        "base": base,
        "full_at": full_at,
        "tampered": tampered,
    }
    path = _path()
    # A random name created exclusively (review round 1): nothing planted at a predictable
    # name is ever opened, truncated or written through.
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump({**body, "mac": _mac(body)}, file)
        os.replace(temporary, path)
    except OSError as exc:
        # Nothing kept: the next probe starts over (from scratch: `_first_verification`).
        logger.warning("erasure_ledger_status_unsaved", extra={"error": type(exc).__name__})
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        return False
    return True


def _forget() -> None:
    with contextlib.suppress(OSError):
        _path().unlink()
