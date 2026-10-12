"""Re-applying the erasure ledger after a restore (docs/runbooks.md#restore-from-backup,
privacy remediation P2-8).

A restored database is behind the ledger (core.ledger): the people erased after the backup
are back, and the API stays closed (core.ledger_gate). `manage.py replay_erasures`, run with
the schema owner's credentials before traffic resumes:

1. reads and verifies the whole ledger (every MAC and link): a ledger that can't be read or
   was tampered with stops here, and the API stays closed. So does a ledger that is behind
   the database or disagrees with it (the database has applied entries this ledger doesn't
   have, or with other MACs: an empty or wrong ledger, as a container without the ledger
   volume sees): replaying it would report success while erasures may be missing (final
   production audit ARCH-6);
2. applies every entry, in order, idempotently, each in its own transaction:
   - `lead_erased`: erased again if the lead is back (`privacy.services.erase`, which
     deletes its Ask Arkray conversations and index chunks and its files too); if it is still
     erased, any index chunk or unpurged file that a restored bucket or a re-index brought
     back is removed;
   - `user_pseudonymised`: pseudonymised again (`privacy.staff`), deactivated with it if the
     backup had the account active;
   - `custom_values_deleted`: the field's values deleted again (`pipeline.field_values`),
     on the deals of the entry's `owner` only when it names one;
   - `attachment_deleted`: the file deleted again, its object purged and its name forgotten;
   - `key_rotated`: nothing to apply (the key change is checked by the verification);
3. reads the ledger again and applies what was added meanwhile (re-erasing a lead queues
   its files' purges, and each purge appends an entry), until it has reached the head; then,
   holding the append lock, records how far the database now matches the ledger
   (LedgerState, never moved backwards): the gates reopen within
   ERASURE_LEDGER_CHECK_INTERVAL_S. A failure leaves the state behind, so the API stays
   closed until a rerun succeeds;
4. returns a reconciliation report (ids, outcomes and counts, never personal data), which the
   command writes to a file as the restore's evidence, and audits `privacy.erasures_replayed`.

`dry_run` checks every entry against the database and reports what would be done, changing
nothing (and leaving the API closed). Without an operator (`operator=None`) the work is
recorded as the system's: the automatic reconciliation (privacy.reconcile). That one leaves
a held subject to an operator: an entry whose lead or user, or whose file's lead, is under a
legal hold fails (`UnderLegalHold`, the API stays closed), checked in the transaction that
applies it under the hold locks (core.holds), so a hold placed after the reconciliation
looked still counts.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from arkray.activities import attachments
from arkray.activities.models import Activity, ActivityType, Attachment
from arkray.ai.models import KnowledgeChunk
from arkray.audit import services as audit
from arkray.core import holds, ledger
from arkray.core.models import HoldSubject
from arkray.identity.models import User
from arkray.leads.models import Lead
from arkray.pipeline import field_values
from arkray.pipeline.models import CustomField, Opportunity

from . import services, staff

AUDIT_REPLAYED = "privacy.erasures_replayed"
REAPPLIED = "reapplied"
ALREADY = "already_applied"
CLEANED = "leftovers_removed"
MISSING = "subject_missing"
FAILED = "failed"
WOULD = "would_reapply"


@dataclass
class ReplayReport:
    started_at: str
    ledger_entries: int
    ledger_head_seq: int
    applied_before: int
    applied_after: int = 0
    dry_run: bool = False
    complete: bool = False
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    finished_at: str = ""

    def counts(self) -> dict[str, int]:
        return dict(Counter(item["outcome"] for item in self.outcomes))

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "complete": self.complete,
            "ledger_entries": self.ledger_entries,
            "ledger_head_seq": self.ledger_head_seq,
            "applied_before": self.applied_before,
            "applied_after": self.applied_after,
            "counts": self.counts(),
            "entries": self.outcomes,
        }


def _lead_records(lead_id: UUID) -> set[UUID]:
    opportunity_ids = set(Opportunity.objects.filter(lead_id=lead_id).values_list("pk", flat=True))
    activity_ids = set(
        Activity.objects.filter(
            Q(lead_id=lead_id) | Q(opportunity_id__in=opportunity_ids)
        ).values_list("pk", flat=True)
    )
    return {lead_id, *opportunity_ids, *activity_ids}


class LedgerMismatch(ledger.LedgerTampered):
    """The ledger doesn't hold what this database has applied: never replayed."""


def _operator_id(operator: User | None) -> Any:
    return operator.pk if operator is not None else None


def _lead(entry: ledger.Entry, operator: User | None, dry_run: bool) -> str:
    lead_id = UUID(entry.subject)
    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        return MISSING
    # Without an operator (the automatic reconciliation) a held subject is an operator's
    # decision (privacy.reconcile): checked again in the transaction that applies the entry,
    # serialised with placing a hold, so one placed since the reconciliation looked counts.
    automatic = operator is None
    if not services._already_erased(lead):
        if dry_run:
            return WOULD
        services.erase(
            lead_id, operator_id=_operator_id(operator), replay=True, unless_held=automatic
        )
        return REAPPLIED
    # Still erased: nothing a re-index or a restored bucket brought back may stay.
    records = _lead_records(lead_id)
    chunks = KnowledgeChunk.objects.filter(Q(lead_id=lead_id) | Q(source_id__in=records))
    note_ids = list(
        Activity.objects.filter(pk__in=records, type=ActivityType.NOTE).values_list("pk", flat=True)
    )
    files = Attachment.objects.filter(note_id__in=note_ids).exclude(
        purged_at__isnull=False, sha256=attachments.ERASED_SHA256
    )
    if not chunks.exists() and not files.exists():
        return ALREADY
    if dry_run:
        return WOULD
    with transaction.atomic():
        if automatic:
            holds.barrier()
            if holds.is_held(HoldSubject.LEAD, lead_id):
                raise holds.UnderLegalHold(HoldSubject.LEAD.value, lead_id)
        chunks.delete()
        attachments.erase_for_notes(note_ids, timezone.now())
    return CLEANED


def _user(entry: ledger.Entry, operator: User | None, dry_run: bool) -> str:
    user = User.objects.filter(pk=UUID(entry.subject)).first()
    if user is None:
        return MISSING
    if staff.pseudonymised(user):
        return ALREADY
    if dry_run:
        return WOULD
    staff.pseudonymise(
        user.pk, operator_id=_operator_id(operator), replay=True, unless_held=operator is None
    )
    return REAPPLIED


def _custom_values(entry: ledger.Entry, operator: User | None, dry_run: bool) -> str:
    found = CustomField.objects.filter(pk=UUID(entry.subject)).first()
    if found is None:
        return MISSING
    owner_id = UUID(entry.owner) if entry.owner else None
    if not field_values.holding(found, owner_id).exists():
        return ALREADY
    if dry_run:
        return WOULD
    if found.is_active:  # the backup predates the removal: remove it again first
        CustomField.objects.filter(pk=found.pk).update(is_active=False)
    field_values.purge(found.pk, owner_id=owner_id, replay=True)
    return REAPPLIED


def _key_rotated(entry: ledger.Entry, operator: User | None, dry_run: bool) -> str:
    return ALREADY  # verify() checked it; nothing in the database to change


def _attachment(entry: ledger.Entry, operator: User | None, dry_run: bool) -> str:
    row = Attachment.objects.filter(pk=UUID(entry.subject)).first()
    if row is None:
        return MISSING
    done = (
        row.deleted_at is not None
        and row.purged_at is not None
        and row.sha256 == attachments.ERASED_SHA256
    )
    if done:
        return ALREADY
    if dry_run:
        return WOULD
    # Without an operator, a file of a held lead (its name and hash are the lead's data too)
    # is an operator's decision, as for `_lead` (review P3-5): checked under the hold barrier
    # before the row changes, and again by the purge under the lead's hold lock.
    automatic = operator is None
    with transaction.atomic():
        if automatic:
            holds.barrier()
            lead_id = Activity.objects.filter(pk=row.note_id).values_list("lead_id", flat=True)[0]
            if holds.is_held(HoldSubject.LEAD, lead_id):
                raise holds.UnderLegalHold(HoldSubject.LEAD.value, lead_id)
        Attachment.objects.filter(pk=row.pk).update(
            deleted_at=row.deleted_at or timezone.now(), purged_at=None
        )
    # The object (probably already gone from storage) and the name, as the purge does.
    attachments.purge(row.pk, replaying=True, unless_held=automatic)
    return REAPPLIED


# Rounds of "read what was added meanwhile, apply it" before giving up (the API stays closed;
# run it again).
ROUNDS = 10

_APPLY = {
    "lead_erased": _lead,
    "user_pseudonymised": _user,
    "custom_values_deleted": _custom_values,
    "attachment_deleted": _attachment,
    ledger.KEY_ROTATED: _key_rotated,
}


def _apply(
    entries: list[ledger.Entry], operator: User | None, dry_run: bool, report: ReplayReport
) -> bool:
    """Apply each entry in order, recording its outcome; False if any failed."""
    ok = True
    for entry in entries:
        item = {"seq": entry.seq, "kind": entry.kind, "subject": entry.subject}
        try:
            item["outcome"] = _APPLY[entry.kind](entry, operator, dry_run)
        except Exception as exc:  # noqa: BLE001 — reported per entry; the state stays behind
            item |= {"outcome": FAILED, "error": type(exc).__name__}
            ok = False
        report.outcomes.append(item)
    return ok


def require_consistent(entries: list[ledger.Entry]) -> None:
    """The database's applied state must be a prefix of `entries` (LedgerMismatch)."""
    applied_seq, applied_mac = ledger.applied()
    if applied_seq > len(entries):
        raise LedgerMismatch(
            f"The database has applied {applied_seq} ledger entries but this ledger holds"
            f" {len(entries)}: it is the wrong ledger, or was rolled back. Restore the right"
            " one; nothing was changed."
        )
    if applied_seq and entries[applied_seq - 1].mac != applied_mac:
        raise LedgerMismatch(
            f"Ledger entry {applied_seq} isn't the one this database applied: it is the wrong"
            " ledger. Restore the right one; nothing was changed."
        )


def replay(
    *, operator: User | None, dry_run: bool = False, missing_only: bool = False
) -> ReplayReport:
    """Re-apply every ledger entry (module docstring), or with `missing_only` just those
    after the database's applied state (the automatic reconciliation: they were never
    committed; a restore re-checks them all). Raises ledger.LedgerTampered (or
    LedgerMismatch) or LedgerUnavailable before changing anything."""
    entries, verified = ledger.verified()
    require_consistent(entries)
    applied_before, _ = ledger.applied()
    report = ReplayReport(
        started_at=timezone.now().isoformat(),
        ledger_entries=len(entries),
        ledger_head_seq=entries[-1].seq if entries else 0,
        applied_before=applied_before,
        dry_run=dry_run,
    )
    failed = not _apply(
        entries[applied_before:] if missing_only else entries, operator, dry_run, report
    )
    last = entries[-1] if entries else None
    caught_up = dry_run
    for _ in range(ROUNDS):
        if failed or dry_run:
            break
        with transaction.atomic():
            # Holding the append lock: no erasure can add an entry until this commits, so
            # the head read here is the head when the state is recorded.
            ledger.lock()
            head = ledger.head()
            if head is None or (last is not None and head.seq == last.seq):
                if head is not None and last is not None and head.mac != last.mac:
                    raise ledger.LedgerTampered("The ledger changed under the replay.")
                applied_seq, _ = ledger.applied()
                if last is None or applied_seq < last.seq:  # never backwards
                    ledger.mark_applied(last)
                audit.record(
                    AUDIT_REPLAYED,
                    actor_id=_operator_id(operator),
                    target_type="ledger",
                    metadata={"head_seq": last.seq if last else 0, **report.counts()},
                )
                caught_up = True
                break
        # Entries added while replaying (the purges that re-erasing queued): verified as
        # continuing the chain already checked, then applied the same way.
        added, verified = ledger.verified(after=verified)
        failed = not _apply(added, operator, dry_run, report)
        last = added[-1] if added else last
    report.ledger_head_seq = last.seq if last else 0
    report.ledger_entries = report.ledger_head_seq
    report.complete = not failed and caught_up
    report.applied_after, _ = ledger.applied()
    report.finished_at = timezone.now().isoformat()
    return report
