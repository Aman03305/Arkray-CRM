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

**Serialised with placing a hold**: an edit and the placing of a hold on the same subject
take one transaction-scoped advisory lock ("ARKH", per subject): the edit SHARED (edits
never wait for each other), then reads the holds; `place` EXCLUSIVE, then inserts. A hold
being placed therefore waits for every edit already past its check to commit, and an edit
arriving meanwhile waits for the hold and then sees it (READ COMMITTED: the check is a new
statement, after the lock). Placing holds no row lock and edits take this one after their
row locks, so the lock can't be part of a cycle. Bulk jobs (a removed custom field's values,
pipeline.field_values) don't take it per lead: they read the holds again for every batch,
so a hold placed while one runs takes effect from its next batch.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from uuid import UUID

from django.db import connection

from .errors import ConflictError
from .models import HoldSubject, LegalHold

_LOCK_NAMESPACE = 0x41524B48  # "ARKH": one subject's holds and the edits of its data


def active() -> dict[str, set[UUID]]:
    """The held subjects by type, in one query: {"lead": {...}, "user": {...}}."""
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


def _lock(subject_type: HoldSubject, subject_id: UUID, *, shared: bool) -> None:
    if not connection.in_atomic_block:  # a transaction-scoped lock outside one ends at once
        raise RuntimeError("Legal-hold checks and placements run inside a transaction.")
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {function}(%s, hashtext(%s))",
            [_LOCK_NAMESPACE, f"{HoldSubject(subject_type).value}:{subject_id}"],
        )


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
    """Place a hold in the caller's transaction, after every edit of the subject already
    past its check has committed (module docstring). IntegrityError if one is active."""
    _lock(subject_type, subject_id, shared=False)
    return LegalHold.objects.create(
        subject_type=subject_type, subject_id=subject_id, reference=reference, placed_by=placed_by
    )
