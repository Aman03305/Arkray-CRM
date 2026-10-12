"""Legal holds (core.models.LegalHold): what every retention, erasure and purge job asks
before it removes anything about a person, and what every edit asks before it overwrites
or removes a held person's data (docs/privacy.md#legal-holds).

**What a hold on a lead freezes** (PRIV-2): exactly what an erasure would clear or redact,
because that is the person's data and the matter's evidence. Each module names its own
frozen fields next to the operation that writes them (`HELD_FIELDS` in leads.services,
pipeline.services and activities.services): the lead's identity, contact and address
fields and its description; its deals' customer copy (customer, account, contact, address),
name, description, lost reason, instrument, work load, Expected CPT and custom values; the
title and text, place and link of its activities. While the hold is active none of them may
change at all (filled, overwritten or cleared: the record stays as it was when the hold was
placed, as a correction does), and no file of its notes may be deleted. Everything that
adds or only moves the record on stays allowed: new notes, tasks, meetings and files, new
deals, stage moves and agreed prices (the history is append-only), status, owner, rating,
dates, completing work, archiving and restoring (archiving hides, it deletes nothing).

**Serialised with placing a hold**: whatever reads the holds and then removes, purges,
erases or overwrites data on that basis is serialised with `place`, so a hold placed at the
same moment either committed before the read (which sees it and keeps the data) or waits
until the reader's transaction has committed (risk register R134). Two transaction-scoped
advisory locks do it:

- per subject ("ARKH"): an edit of one subject's data (`refuse_if_held`,
  `refuse_frozen_changes`) and a job that acts on one subject's data (a note's file being
  purged: activities.attachments.purge) take it SHARED (they never wait for each other),
  then read that subject's holds;
- the **hold barrier** ("ARKB", one for everything): a reader that acts on every hold, or
  on holds it can't name before reading them, takes it SHARED with `barrier()`, then reads
  them (readers never wait for each other): an erasure (the conversations a hold on someone
  else keeps), a staff pseudonymisation, deleting an Ask Arkray conversation, the hourly
  conversation housekeeping, the custom-value deletion job and the audit-detail expiry (once
  per batch, each batch its own transaction), forgetting purged files' names, blanking old
  support reasons (identity.support), and a replay without an operator (privacy.replay).

`place` takes the barrier EXCLUSIVE, then the subject's lock EXCLUSIVE, then inserts. So a
hold being placed waits for every reader and every edit of its subject already past its
check to commit, and one arriving meanwhile waits for the hold and then sees it (READ
COMMITTED: the read is a new statement, after the lock). Releasing a hold takes neither: a
reader that still sees it only keeps more.

**Lock order** (no new cycle; docs/privacy.md#legal-holds):

1. The barrier is the **first lock of its transaction**, in `place` and in every reader: no
   row lock, no write, no other advisory lock before it (`barrier()` and `place()` refuse
   otherwise with LockOrderError, in a transaction of their own; inside a caller's, as in a
   test or the legal_hold command, that is the caller's to keep). So whoever waits for the
   barrier holds nothing, nobody waits for it, and it can't be part of a cycle: in
   particular a transaction holding a subject's lock SHARED never then waits for the
   barrier while `place` holds the barrier and waits for that subject's lock (that
   interleaving deadlocked: tests/security/test_hold_races.py, TestLockOrder).
2. A subject's lock comes after the row locks of the operation that takes it (edits lock
   their records first) and before its writes. The file purge locks no row before it, nor
   while it waits for storage: its one row lock is its last write (review P3-4). A reader
   holding the barrier never waits for a subject's lock (no placement can be past its
   barrier meanwhile).
3. Everything else a reader takes, it takes after the barrier, in the order it always has:
   the erasure lock (ai.service.hold_erasure_lock), the user-administration lock, row
   locks, the erasure ledger's append lock (core.ledger, last). None of their holders waits
   for the barrier (rule 1), and `place` takes nothing after the barrier but the subject's
   lock and its own insert, so the barrier adds no edge to any wait between them.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from uuid import UUID

from django.db import connection

from .errors import ConflictError
from .models import HoldSubject, LegalHold

_LOCK_NAMESPACE = 0x41524B48  # "ARKH": one subject's holds and the edits of its data
_BARRIER = (0x41524B42, 0)  # "ARKB": placing any hold, against the readers of every hold

# What this transaction holds already (pg_locks: an advisory lock on two int4 keys has them as
# classid and objid, objsubid 2): the barrier (and whether exclusively), and whether it wrote
# or locked a row (it has a transaction id) or holds any other advisory lock.
_HELD_BY_THIS_TRANSACTION = (
    "WITH mine AS (SELECT classid = %s AND objid = %s AND objsubid = 2 AS barrier, mode"
    " FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory' AND granted)"
    " SELECT EXISTS (SELECT 1 FROM mine WHERE barrier),"
    " EXISTS (SELECT 1 FROM mine WHERE barrier AND mode = 'ExclusiveLock'),"
    " pg_current_xact_id_if_assigned() IS NOT NULL OR EXISTS (SELECT 1 FROM mine WHERE NOT barrier)"
)


def active() -> dict[str, set[UUID]]:
    """The held subjects by type, in one query: {"lead": {...}, "user": {...}}. A reader that
    removes or changes data on the strength of it takes `barrier()` first."""
    held: dict[str, set[UUID]] = {subject: set() for subject in HoldSubject.values}
    for subject_type, subject_id in LegalHold.objects.filter(released_at__isnull=True).values_list(
        "subject_type", "subject_id"
    ):
        held[subject_type].add(subject_id)
    return held


def held_users() -> set[UUID]:
    return active()[HoldSubject.USER]


def held_leads() -> set[UUID]:
    return active()[HoldSubject.LEAD]


def is_held(subject_type: HoldSubject, subject_id: UUID) -> bool:
    return LegalHold.objects.filter(
        subject_type=subject_type, subject_id=subject_id, released_at__isnull=True
    ).exists()


class UnderLegalHold(ConflictError):
    """The operation would remove or change data under an active legal hold (409 through
    the API)."""

    code = "legal_hold"

    def __init__(
        self, subject_type: str, subject_id: UUID, *, fields: Collection[str] = ()
    ) -> None:
        if fields:
            super().__init__(
                "This customer's records are under a legal hold: these details can't be"
                f" changed or removed until an administrator releases it ({', '.join(fields)}).",
                details={"fields": list(fields)},
            )
            return
        super().__init__(
            f"The {subject_type} {subject_id} is under a legal hold: release it first (an"
            " administrator, with the matter's reference)."
        )


def _require_transaction() -> None:
    if not connection.in_atomic_block:  # a transaction-scoped lock outside one ends at once
        raise RuntimeError("Legal-hold checks and placements run inside a transaction.")


def _lock(subject_type: HoldSubject, subject_id: UUID, *, shared: bool) -> None:
    _require_transaction()
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {function}(%s, hashtext(%s))",
            [_LOCK_NAMESPACE, f"{HoldSubject(subject_type).value}:{subject_id}"],
        )


class LockOrderError(RuntimeError):
    """The hold barrier asked for after another lock in the same transaction (module
    docstring, rule 1): a programming error, which could deadlock with `place`."""


def _barrier(*, exclusive: bool) -> None:
    """Take the barrier, refusing an order that could deadlock (module docstring, rule 1).
    Checked in a transaction of the caller's own (not inside an enclosing one, whose
    earlier work isn't this operation's: a test's, the legal_hold command's)."""
    _require_transaction()
    with connection.cursor() as cursor:
        if not connection.savepoint_ids:
            cursor.execute(_HELD_BY_THIS_TRANSACTION, list(_BARRIER))
            held, held_exclusively, other_locks = cursor.fetchone()
            if held_exclusively or (held and not exclusive):
                return  # already ours, in a mode that covers this one
            if held:  # SHARED, now wanted EXCLUSIVE: two upgrading at once would deadlock
                raise LockOrderError("A hold can't be placed by a reader of the holds.")
            if other_locks:
                raise LockOrderError(
                    "The legal-hold barrier must be the first lock of its transaction."
                )
        function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        cursor.execute(f"SELECT {function}(%s, %s)", list(_BARRIER))


def barrier() -> None:
    """For a reader that removes, purges, erases or overwrites data on the strength of the
    holds it reads (all of them, or ones it can't name beforehand): the hold barrier,
    SHARED, until the transaction ends. Called first in that transaction, before reading
    the holds (module docstring): a hold being placed has committed before this returns,
    and none is placed until the transaction ends. A batched job calls it in each batch's
    transaction, and reads the holds again there."""
    _barrier(exclusive=False)


def refuse_if_held(
    subject_type: HoldSubject, subject_id: UUID, *, fields: Collection[str] = ()
) -> None:
    """UnderLegalHold if the subject is held. Called inside the transaction about to change
    its data, before writing anything (so a refusal writes nothing), once the record's own
    row lock is held; a hold placed concurrently is serialised with it (module docstring)."""
    _lock(subject_type, subject_id, shared=True)
    if is_held(subject_type, subject_id):
        raise UnderLegalHold(HoldSubject(subject_type).value, subject_id, fields=fields)


def refuse_frozen_changes(
    subject_id: UUID, changed: Iterable[str], frozen: Collection[str]
) -> None:
    """For an edit of a lead's records: refused while the lead is held if any of `changed`
    is one of the module's frozen fields (`frozen`); other changes need no check."""
    touched = sorted(set(changed) & set(frozen))
    if touched:
        refuse_if_held(HoldSubject.LEAD, subject_id, fields=touched)


def place(
    subject_type: HoldSubject, subject_id: UUID, *, reference: str, placed_by: UUID
) -> LegalHold:
    """Place a hold in the caller's transaction, after every reader of the holds and every
    edit of the subject already past its check has committed (module docstring): first in
    the transaction. IntegrityError if one is active."""
    _barrier(exclusive=True)
    _lock(subject_type, subject_id, shared=False)
    return LegalHold.objects.create(
        subject_type=subject_type, subject_id=subject_id, reference=reference, placed_by=placed_by
    )
