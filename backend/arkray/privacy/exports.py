"""Exporting a person's data on a verified access request (privacy remediation P2-5;
docs/privacy.md#access-requests).

An administrator who has verified the requester's identity (`reference`: the request's
ticket; `identity_verified`) asks for an export of one **customer** (a lead) or one **staff
member** (a user). A job builds a ZIP into private storage (STORAGES["exports"]):

- `data.json`, the machine-readable whole, and `csv/*.csv`, the same as tables (each cell
  that a spreadsheet would run as a formula is prefixed with a quote);
- `files/`: the customer's attached files that are stored and downloadable (not deleted,
  not blocked by the scan), up to EXPORT_MAX_BYTES in total; the rest are listed, with why
  they were left out;
- `README.txt`: what is in it and what isn't.

**A customer**: the lead (every field, archived or not), its deals in full (the customer
copy, amounts, prices and CPTs, custom values by field name, those of removed fields too,
stage and price history), its tasks, meetings and notes in full text (not the 240-character
previews of the screens), attachment details and files, its timeline, and the Ask Arkray
questions and answers that cite its records or name it (another record cited there is shown
as "[another record]"). Staff appear by role only ("an Arkray user"): the export is about the
customer, not about who worked on it. Free text staff wrote can still mention other people:
the administrator reviews the export before releasing it (a legal decision, not automated).

**A staff member**: their account (name, email, role, status, dates), their sign-ins and the
security events about them (with the client address while the audit detail keeps it), the
support sessions for them, their own Ask Arkray conversations (every record an answer cited
shown as "[another record]": the records, and the customers they name, are not their personal
data; deep security audit L-6), and counts of the CRM records they own (the records themselves
are the organisation's and its customers' data).

Rules: only an administrator (privacy.manage), never in a support session; the subject must
exist (an erased lead or a pseudonymised user has nothing left to export); at most
EXPORT_MAX_PENDING_PER_ADMIN queued and EXPORT_MAX_PER_HOUR per administrator; only the
administrator who asked can download it, until EXPORT_TTL_HOURS after it was built; every
request, download and expiry is audited (ids only). The file is deleted when it expires
(hourly) and the row kept.

**Deleting the file** (final audit PRIV-3, ARCH-4): an export that ends (expires, is
withdrawn because its subject was erased, or times out) is marked in the same transaction
(`purge_requested_at`; never downloadable again) and keeps its storage key until the object
is confirmed gone: every version and delete marker of it in a versioned bucket
(core.storage_purge), the file otherwise. `purge_file` claims the row with a short lease
(SKIP LOCKED: a duplicate task or a concurrent housekeeping run skips it; a crashed attempt's
lease runs out), deletes outside any transaction, and records the outcome: `purged_at` (the
key cleared), or the failure in `purge_error` with the next attempt backing off from 5
minutes to a day. A version the store's retention (S3 Object Lock retention or legal hold)
refuses is *retained*: audited once (`privacy.export_file_retained`), retried daily, never in
a loop. After PURGE_ALERT_AFTER failed attempts each further one logs at ERROR; the
`arkray_export_file_purges` gauge counts what is pending, retained and stuck
(docs/runbooks.md#export-file-cleanup).

`reconcile_orphans` (hourly, bounded) lists the exports store and deletes what no live export
explains: an object of an ended export (through its row, as above), or of no export at all
(a database restored to before it). Only names an export is written under, older than
ORPHAN_GRACE, and never when the exports store overlaps the attachments store.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import io
import json
import logging
import re
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import IO, Any
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.core.files import File
from django.core.files.storage import storages
from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from arkray.activities import attachments, storage
from arkray.activities.models import Activity, Attachment, TimelineEntry
from arkray.ai import subjects
from arkray.ai.formatting import user_text
from arkray.ai.models import Conversation, Question
from arkray.audit import services as audit
from arkray.audit.models import AuditDetail, AuditEvent
from arkray.core import outbox, storage_purge
from arkray.core.errors import BusinessRuleViolation, NotFoundError, RateLimitedError
from arkray.identity.models import SupportSession, User
from arkray.leads.models import Lead
from arkray.pipeline.models import CustomField, NegotiationPrice, Opportunity, StageHistory

from . import services, staff
from .models import DataExport, ExportStatus, SubjectType

logger = logging.getLogger(__name__)

TOPIC_BUILD = "privacy.build_export"
AUDIT_REQUESTED = "privacy.export_requested"
AUDIT_DOWNLOADED = "privacy.export_downloaded"
AUDIT_EXPIRED = "privacy.export_expired"
AUDIT_RETAINED = "privacy.export_file_retained"
STAFF = "an Arkray user"
OTHER_RECORD = "[another record]"
NOTHING_LEFT = "This person's data was erased: there is nothing left to export."
TOO_MANY_PENDING = "You have exports still being built. Wait for them to finish."
TOO_MANY = "Too many exports in the last hour. Try again later."
UNAVAILABLE = "This export isn't available (still being built, failed or expired)."
MAX_EVENTS = 20_000  # a staff member's audit events in one export
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# Deleting an ended export's file (module docstring).
PURGE_LEASE = timedelta(minutes=15)  # a claimed attempt; longer than the store's timeouts
PURGE_RETRY_FIRST = timedelta(minutes=5)
PURGE_RETRY_MAX = timedelta(days=1)
PURGE_ALERT_AFTER = 6  # failed attempts before each further one logs at ERROR ("stuck")
PURGE_BATCH = 200  # attempts per housekeeping run
RETAINED = "retained"
# Reconciliation: objects listed per run (resumed from a cursor), the age below which an
# object may still be being written, and the names an export is written under (storage_key,
# or the storage's alternative name when that key was taken).
RECONCILE_KEYS = 1000
ORPHAN_GRACE = timedelta(hours=2)
RECONCILE_CURSOR = "privacy:export_reconcile_after"
_EXPORT_NAME = re.compile(r"^\d{4}/\d{2}/(?P<id>[0-9a-f]{32})(?:_[A-Za-z0-9]{7})?\.zip$")


# --- requesting ----------------------------------------------------------------------------------
def _subject_exists(subject_type: str, subject_id: UUID) -> bool:
    if subject_type == SubjectType.LEAD:
        lead = Lead.objects.filter(pk=subject_id).first()
        return lead is not None and not services._already_erased(lead)
    user = User.objects.filter(pk=subject_id).first()
    return user is not None and not staff.pseudonymised(user)


def request(*, actor: User, subject_type: str, subject_id: UUID, reference: str) -> DataExport:
    if (
        not Lead.objects.filter(pk=subject_id).exists()
        and not User.objects.filter(pk=subject_id).exists()
    ):
        raise NotFoundError()
    if not _subject_exists(subject_type, subject_id):
        if (
            (Lead if subject_type == SubjectType.LEAD else User)
            .objects.filter(pk=subject_id)
            .exists()
        ):
            raise BusinessRuleViolation(NOTHING_LEFT)
        raise NotFoundError()
    now = timezone.now()
    with transaction.atomic():
        # Serialise this administrator's requests on their own row, which exists even before
        # their first export (backend review P3: locking their exports locked nothing then).
        list(User.objects.select_for_update().filter(pk=actor.pk).values_list("pk", flat=True))
        mine = DataExport.objects.filter(requested_by=actor)
        if mine.filter(status=ExportStatus.QUEUED).count() >= settings.EXPORT_MAX_PENDING_PER_ADMIN:
            raise RateLimitedError(TOO_MANY_PENDING, retry_after=60)
        if (
            mine.filter(created_at__gte=now - timedelta(hours=1)).count()
            >= settings.EXPORT_MAX_PER_HOUR
        ):
            raise RateLimitedError(TOO_MANY, retry_after=3600)
        export = DataExport.objects.create(
            subject_type=subject_type,
            subject_id=subject_id,
            requested_by=actor,
            reference=reference,
        )
        audit.record(
            AUDIT_REQUESTED,
            actor_id=actor.pk,
            target_type=subject_type,
            target_id=subject_id,
            subject_user_id=subject_id if subject_type == SubjectType.USER else None,
            metadata={
                "export_id": str(export.pk),
                "reference": reference,
                "identity_verified": True,
            },
        )
        outbox.enqueue(TOPIC_BUILD, {"export_id": str(export.pk)})
    return export


def for_download(*, actor: User, export_id: UUID) -> tuple[DataExport, IO[bytes]]:
    """The export's file, for the administrator who asked, while it is ready."""
    export = DataExport.objects.filter(pk=export_id, requested_by=actor).first()
    if export is None:
        raise NotFoundError()
    now = timezone.now()
    downloadable = Q(status=ExportStatus.READY, expires_at__gt=now, purge_requested_at__isnull=True)
    if not DataExport.objects.filter(downloadable, pk=export.pk).exists():
        raise BusinessRuleViolation(UNAVAILABLE)
    file = storages["exports"].open(export.storage_key, "rb")
    with transaction.atomic():
        # Counted only while still downloadable: an erasure that ended it meanwhile holds
        # the row until it commits, and then this matches nothing (PRIV-3).
        if not DataExport.objects.filter(downloadable, pk=export.pk).update(
            downloads=F("downloads") + 1
        ):
            file.close()
            raise BusinessRuleViolation(UNAVAILABLE)
        audit.record(
            AUDIT_DOWNLOADED,
            actor_id=actor.pk,
            target_type=export.subject_type,
            target_id=export.subject_id,
            metadata={"export_id": str(export.pk)},
        )
    return export, file


def storage_key(export: DataExport) -> str:
    """Where an export's ZIP is written: the same key every time the job runs for it, so a
    retried job replaces its earlier attempt instead of leaving it behind."""
    return f"{export.created_at:%Y/%m}/{export.pk.hex}.zip"


# --- deleting the file (PRIV-3, ARCH-4; module docstring) ---------------------------------------
def _purge_due(now: datetime) -> Q:
    return Q(purge_requested_at__isnull=False, purged_at__isnull=True, purge_next_at__lte=now)


def _ended(now: datetime) -> dict[str, Any]:
    """The fields that end an export's file: never downloadable again, deleted next."""
    return {"purge_requested_at": now, "purge_next_at": now}


def _keys(export: DataExport) -> list[str]:
    """Everything this export may have written: the key it was saved under (the storage's
    alternative name when the usual one was taken) and the usual one (a build that died)."""
    return sorted({export.storage_key, storage_key(export)} - {""})


def _retry_delay(attempts: int) -> timedelta:
    return min(PURGE_RETRY_FIRST * (1 << min(attempts - 1, 16)), PURGE_RETRY_MAX)


def purge_file(export_id: UUID, now: datetime | None = None) -> str:
    """Delete an ended export's file for good, if it is due: "purged", "retained" (the
    store's retention refused a version), "failed" (try again later), or "skipped" (not due,
    done already, or another worker holds it). Idempotent; never raises for storage."""
    now = now or timezone.now()
    with transaction.atomic():
        export = (
            DataExport.objects.select_for_update(skip_locked=True)
            .filter(_purge_due(now), pk=export_id)
            .first()
        )
        if export is None:
            return "skipped"
        # Claimed: a duplicate task sees it not due; a crashed attempt's lease runs out.
        DataExport.objects.filter(pk=export.pk).update(purge_next_at=now + PURGE_LEASE)
    result = storage_purge.PurgeResult()
    error = ""
    try:
        for key in _keys(export):
            result += storage_purge.purge(storages["exports"], key)
    except storage_purge.PurgeFailed as exc:
        error = exc.error_class
    else:
        error = RETAINED if result.retained else "partial" if result.failed else ""
    # Log fields from core.logging's allowlist: `count` removed, `held` retained.
    found = {
        "export_id": str(export.pk),
        "count": result.removed,
        "held": result.retained,
        "kind": "versions" if result.versioned else "object",
    }
    if not error:
        DataExport.objects.filter(pk=export.pk, purged_at__isnull=True).update(
            purged_at=timezone.now(), storage_key="", purge_error="", purge_next_at=None
        )
        logger.info("export_file_purged", extra=found)
        return "purged"
    attempts = export.purge_attempts + 1
    with transaction.atomic():
        DataExport.objects.filter(pk=export.pk, purged_at__isnull=True).update(
            purge_attempts=F("purge_attempts") + 1,
            purge_error=error,
            purge_next_at=now + _retry_delay(attempts),
        )
        if error == RETAINED and export.purge_error != RETAINED:
            # Personal data kept past its time by the store, not by us: on the record once.
            audit.record(
                AUDIT_RETAINED,
                actor_id=None,
                target_type=export.subject_type,
                target_id=export.subject_id,
                metadata={"export_id": str(export.pk), "count": result.retained},
            )
    logger.log(
        logging.ERROR if attempts >= PURGE_ALERT_AFTER else logging.WARNING,
        "export_file_purge_failed",
        extra={**found, "reason": error, "attempt": attempts},
    )
    return RETAINED if error == RETAINED else "failed"


def _purge_after_commit(export_id: UUID) -> None:
    purge_file(export_id)  # its failure is on the row: housekeeping tries again


def purge_due(now: datetime | None = None) -> dict[str, int]:
    """Housekeeping: every ended export's file whose deletion is due (bounded)."""
    now = now or timezone.now()
    outcomes = {"purged": 0, RETAINED: 0, "failed": 0, "skipped": 0}
    due = (
        DataExport.objects.filter(_purge_due(now))
        .order_by("purge_next_at")
        .values_list("pk", flat=True)[:PURGE_BATCH]
    )
    for export_id in list(due):
        outcomes[purge_file(export_id, now)] += 1
    return outcomes


def withdraw_for(subject_type: str, subject_id: UUID) -> int:
    """The subject has been erased or pseudonymised: their exports, queued or ready, end now
    (inside the caller's transaction; the files go once it commits, or housekeeping deletes
    them later). A job building one finds it no longer queued and has its file deleted the
    same way (backend review P2)."""
    found = list(
        DataExport.objects.select_for_update().filter(
            subject_type=subject_type,
            subject_id=subject_id,
            status__in=[ExportStatus.QUEUED, ExportStatus.READY],
        )
    )
    if not found:
        return 0
    DataExport.objects.filter(pk__in=[e.pk for e in found]).update(
        status=ExportStatus.EXPIRED, error_code="subject_erased", **_ended(timezone.now())
    )
    for export in found:
        audit.record(
            AUDIT_EXPIRED,
            actor_id=None,
            target_type=export.subject_type,
            target_id=export.subject_id,
            metadata={"export_id": str(export.pk), "reason": "subject_erased"},
        )
        transaction.on_commit(functools.partial(_purge_after_commit, export.pk), robust=True)
    return len(found)


def expire(now: datetime | None = None) -> int:
    """End the exports past their expiry and those queued too long, and delete the files due
    (hourly; idempotent; rows another worker holds are left for the next run)."""
    now = now or timezone.now()
    with transaction.atomic():
        due = list(
            DataExport.objects.select_for_update(skip_locked=True).filter(
                status=ExportStatus.READY, expires_at__lte=now
            )[:500]
        )
        DataExport.objects.filter(pk__in=[e.pk for e in due]).update(
            status=ExportStatus.EXPIRED, **_ended(now)
        )
        for export in due:
            audit.record(
                AUDIT_EXPIRED,
                actor_id=None,
                target_type=export.subject_type,
                target_id=export.subject_id,
                metadata={"export_id": str(export.pk)},
            )
    # Queued too long (the job died): failed, so the administrator can ask again, and
    # whatever a crashed attempt wrote is deleted (backend review P2: orphaned files).
    stale = now - timedelta(hours=settings.EXPORT_TTL_HOURS)
    with transaction.atomic():
        timed_out = list(
            DataExport.objects.select_for_update(skip_locked=True)
            .filter(status=ExportStatus.QUEUED, created_at__lt=stale)
            .values_list("pk", flat=True)[:500]
        )
        DataExport.objects.filter(pk__in=timed_out).update(
            status=ExportStatus.FAILED, error_code="timed_out", **_ended(now)
        )
    outcomes = purge_due(now)
    if any(outcomes[name] for name in ("purged", RETAINED, "failed")):
        logger.info(
            "export_files_purged",
            extra={
                "purged": outcomes["purged"],
                "held": outcomes[RETAINED],
                "failed": outcomes["failed"],
            },
        )
    return len(due)


def reconcile_orphans(now: datetime | None = None) -> dict[str, int]:
    """Hourly, bounded (RECONCILE_KEYS objects a run, resumed from a cursor): delete the
    objects in the exports store that no live export explains. Through the row when there is
    one (re-armed: deleted, retried and recorded as above); directly when no export has that
    id (a database restored to before it), found again next run if that fails. Left alone: a
    queued export's (being built), a ready export's own file, one whose row's deletion is
    pending, anything newer than ORPHAN_GRACE, and any name an export is never written
    under. Refused outright if the exports store overlaps the attachments store."""
    now = now or timezone.now()
    report = {"listed": 0, "orphans": 0, "removed": 0, "failed": 0, "unknown": 0, "refused": 0}
    store = storages["exports"]
    if storage_purge.overlaps(store, storages["attachments"]):
        logger.error("export_reconcile_refused", extra={"reason": "overlapping_store"})
        return _reconciled({**report, "refused": 1})
    try:
        after = str(cache.get(RECONCILE_CURSOR) or "")
    except Exception:  # noqa: BLE001 — the cache is an optimisation: start over
        after = ""
    try:
        found, done = storage_purge.objects(store, after=after, limit=RECONCILE_KEYS)
    except storage_purge.PurgeFailed as exc:
        logger.warning("export_reconcile_failed", extra={"reason": exc.error_class})
        return _reconciled({**report, "failed": 1})
    _remember(RECONCILE_CURSOR, "" if done or not found else found[-1].key)
    report["listed"] = len(found)
    named: dict[str, UUID] = {}
    for item in found:
        match = _EXPORT_NAME.match(item.key)
        if match is None:
            report["unknown"] += 1  # not an export's name: reported, never deleted
        elif item.modified <= now - ORPHAN_GRACE:
            named[item.key] = UUID(match["id"])
    rows = DataExport.objects.in_bulk(set(named.values()))
    for key, export_id in named.items():
        export = rows.get(export_id)
        if export is not None and _explains(export, key):
            continue
        report["orphans"] += 1
        if (
            export is not None
            and export.status not in (ExportStatus.QUEUED, ExportStatus.READY)
            and (export.purged_at is not None or export.purge_requested_at is None)
        ):
            # An ended export's file, deleted once already or before deletion was recorded
            # (a restored bucket, an older release): its row's deletion again.
            DataExport.objects.filter(pk=export.pk).exclude(status=ExportStatus.READY).update(
                storage_key=key,
                purged_at=None,
                purge_requested_at=Coalesce(F("purge_requested_at"), now),
                purge_next_at=now,
            )
            outcome = purge_file(export.pk, now)
            report["removed" if outcome == "purged" else "failed"] += 1
            continue
        try:
            result = storage_purge.purge(store, key)
        except storage_purge.PurgeFailed:
            report["failed"] += 1
            continue
        report["removed" if result.complete else "failed"] += 1
    if report["orphans"] or report["failed"] or report["unknown"]:
        logger.warning(
            "export_orphans_reconciled",
            extra={
                "listed": report["listed"],
                "orphaned": report["orphans"],
                "purged": report["removed"],
                "failed": report["failed"],
                "problems": report["unknown"],  # names no export is written under
            },
        )
    return _reconciled(report)


def _explains(export: DataExport, key: str) -> bool:
    """Whether the export accounts for this object, so it must stay (for now)."""
    if export.status == ExportStatus.QUEUED:
        return True  # being built; ended later, its deletion covers what it wrote
    if export.status == ExportStatus.READY:
        return key == export.storage_key
    pending = export.purge_requested_at is not None and export.purged_at is None
    return pending and key in _keys(export)


def _remember(name: str, value: Any) -> None:
    try:
        cache.set(name, value, timeout=7 * 24 * 3600)
    except Exception:  # noqa: BLE001 — the cache is an optimisation
        logger.warning("export_reconcile_cache_failed")


def _reconciled(report: dict[str, int]) -> dict[str, int]:
    return report  # its outcomes are logged above


# --- shaping values ------------------------------------------------------------------------------
def _value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    return value


def _row(obj: Any, fields: Sequence[str]) -> dict[str, Any]:
    return {field: _value(getattr(obj, field)) for field in fields}


def _cell(value: Any) -> str:
    """A CSV cell a spreadsheet never runs: formulas are quoted (CSV injection)."""
    text = (
        "" if value is None else json.dumps(value) if isinstance(value, dict | list) else str(value)
    )
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def _csv(rows: Iterable[Mapping[str, Any]], columns: Sequence[str]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in columns])
    return out.getvalue().encode("utf-8-sig")


# --- a customer ----------------------------------------------------------------------------------
LEAD_FIELDS = (
    "first_name",
    "last_name",
    "organization_name",
    "job_title",
    "email",
    "phone",
    "mobile",
    "alternate_phone",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "postal_code",
    "country",
    "description",
    "rating",
    "last_contacted_at",
    "created_at",
    "updated_at",
    "archived_at",
)
OPPORTUNITY_FIELDS = (
    "id",
    "title",
    "account_name",
    "customer_name",
    "contact_phone",
    "contact_email",
    "address",
    "instrument_name",
    "work_load",
    "expected_cpt",
    "value",
    "probability",
    "status",
    "opportunity_date",
    "expected_close_date",
    "closed_at",
    "description",
    "lost_reason",
    "negotiated_price",
    "negotiated_at",
    "created_at",
    "archived_at",
)
ACTIVITY_FIELDS = (
    "id",
    "type",
    "title",
    "description",
    "status",
    "priority",
    "due_at",
    "starts_at",
    "ends_at",
    "location",
    "meeting_url",
    "completed_at",
    "cancelled_at",
    "created_at",
    "edited_at",
    "archived_at",
)


def _custom_values(
    opportunity: Opportunity, names: Mapping[str, tuple[str, bool]]
) -> list[dict[str, Any]]:
    values = []
    for key, value in (opportunity.custom_fields or {}).items():
        name, active = names.get(key, ("(unknown field)", False))
        values.append({"field": name, "value": value, "removed_field": not active})
    return values


def _answer_text(answer: dict[str, Any], own_refs: set[str]) -> str:
    """An answer's text with every record that isn't the subject's own shown as
    OTHER_RECORD."""
    sources = [
        {**source, "label": source["label"] if source.get("ref") in own_refs else OTHER_RECORD}
        for source in answer.get("sources", [])
    ]
    labels = {source["ref"]: source["label"] for source in sources}
    lines = []
    for block in answer.get("blocks", []):
        lines.append(
            "".join(
                labels.get(part["ref"], OTHER_RECORD)
                if "ref" in part
                else str(part.get("text", ""))
                for part in block.get("parts", [])
            )
        )
    return "\n".join(lines)


def _about(question: Question, own_refs: set[str], terms: list[str]) -> bool:
    """Whether a question is about the subject: its answer cites one of their records, or
    the question or its answer names them."""
    answer = json.dumps(question.answer or {}, ensure_ascii=False)
    if any(ref in answer for ref in own_refs):  # a source or citation of their records
        return True
    text = f"{question.text}\n{answer}".casefold()
    return any(term.casefold() in text for term in terms)


def lead_document(lead_id: UUID) -> tuple[dict[str, Any], list[Attachment]]:
    lead = Lead.objects.select_related("status", "source").get(pk=lead_id)
    opportunities = list(
        Opportunity.objects.filter(lead_id=lead_id)
        .select_related("stage", "pipeline")
        .order_by("created_at")
    )
    opportunity_ids = [o.pk for o in opportunities]
    activities = list(
        Activity.objects.filter(
            Q(lead_id=lead_id) | Q(opportunity_id__in=opportunity_ids)
        ).order_by("created_at")
    )
    files = list(
        Attachment.objects.filter(note_id__in=[a.pk for a in activities]).order_by("created_at")
    )
    names = {
        str(f.pk): (f.name, f.is_active)
        for f in CustomField.objects.filter(pipeline_id__in={o.pipeline_id for o in opportunities})
    }
    history = StageHistory.objects.filter(opportunity_id__in=opportunity_ids).order_by(
        "occurred_at"
    )
    prices = NegotiationPrice.objects.filter(opportunity_id__in=opportunity_ids).order_by(
        "occurred_at"
    )
    records = {lead_id, *opportunity_ids, *(a.pk for a in activities)}
    refs = {f"lead:{lead_id}", *(f"opportunity:{pk}" for pk in opportunity_ids)} | {
        f"{a.type}:{a.pk}" for a in activities
    }
    terms = subjects.identifying_terms(lead)
    conversation_ids = subjects.touching_conversations(records, terms)
    # Only the questions about this person, not every question of a conversation that once
    # touched them (backend review P2: a sibling question named other customers).
    questions = [
        q
        for q in Question.objects.filter(
            conversation_id__in=conversation_ids, status="answered"
        ).order_by("created_at")
        if _about(q, refs, terms)
    ]
    document: dict[str, Any] = {
        "lead": {
            "id": str(lead.pk),
            **_row(lead, LEAD_FIELDS),
            "status": lead.status.name,
            "source": lead.source.name if lead.source else None,
            "owner": STAFF,
        },
        "opportunities": [
            {
                **_row(o, OPPORTUNITY_FIELDS),
                "pipeline": o.pipeline.name,
                "stage": o.stage.name,
                "custom_fields": _custom_values(o, names),
                "owner": STAFF,
            }
            for o in opportunities
        ],
        "stage_history": [
            {
                "opportunity_id": str(h.opportunity_id),
                "from_stage": h.from_stage_name,
                "to_stage": h.to_stage_name,
                "value": _value(h.value),
                "lost_reason": h.lost_reason,
                "occurred_at": _value(h.occurred_at),
            }
            for h in history
        ],
        "negotiated_prices": [
            {
                "opportunity_id": str(p.opportunity_id),
                "price": _value(p.price),
                "currency": p.currency,
                "agreed_cpt": p.agreed_cpt,
                "stage": p.stage_name,
                "occurred_at": _value(p.occurred_at),
            }
            for p in prices
        ],
        "activities": [
            {
                **_row(a, ACTIVITY_FIELDS),
                "opportunity_id": _value(a.opportunity_id),
                "owner": STAFF,
            }
            for a in activities
        ],
        "attachments": [
            {
                "id": str(f.pk),
                "note_id": str(f.note_id),
                "name": f.original_name,
                "content_type": f.content_type,
                "size": f.size,
                "uploaded_at": _value(f.created_at),
                "deleted_at": _value(f.deleted_at),
                "state": f.state,
                "scan_status": f.scan_status,
            }
            for f in files
        ],
        "timeline": [
            {"kind": e.kind, "occurred_at": _value(e.occurred_at)}
            for e in TimelineEntry.objects.filter(lead_id=lead_id).order_by("occurred_at")
        ],
        "ask_arkray": [
            {
                "asked_at": _value(q.created_at),
                "asked_by": STAFF,
                "question": q.text,
                "answer": _answer_text(q.answer or {}, refs),
            }
            for q in questions
        ],
    }
    return document, files


# --- a staff member ---------------------------------------------------------------------------
USER_FIELDS = (
    "email",
    "first_name",
    "last_name",
    "role",
    "status",
    "created_at",
    "activated_at",
    "deactivated_at",
    "last_login",
    "password_changed_at",
)


def user_document(user_id: UUID) -> dict[str, Any]:
    user = User.objects.get(pk=user_id)
    about = (
        Q(actor_id=user_id)
        | Q(subject_user_id=user_id)
        | Q(target_type="user", target_id=str(user_id))
    )
    events = list(AuditEvent.objects.filter(about).order_by("occurred_at")[: MAX_EVENTS + 1])
    details = {
        d.pk: d for d in AuditDetail.objects.filter(pk__in=[e.pk for e in events[:MAX_EVENTS]])
    }

    def event_row(event: AuditEvent) -> dict[str, Any]:
        detail = details.get(event.pk)
        row = {
            "action": event.action,
            "occurred_at": _value(event.occurred_at),
            "by_them": event.actor_id == user_id,
            "about_them": event.actor_id != user_id,
            "target_type": event.target_type,
        }
        if detail is not None and event.actor_id == user_id and detail.ip_address:
            row["client_address"] = detail.ip_address  # their own address, while kept
        return row

    conversations = Conversation.objects.filter(actor_id=user_id)
    questions = Question.objects.filter(conversation__in=conversations, status="answered").order_by(
        "created_at"
    )
    return {
        "user": {"id": str(user.pk), **_row(user, USER_FIELDS)},
        "audit_events": [event_row(e) for e in events[:MAX_EVENTS]],
        "audit_events_truncated": len(events) > MAX_EVENTS,
        "support_sessions_for_them": [
            {
                "started_at": _value(s.started_at),
                "ended_at": _value(s.ended_at),
                "reason": s.reason,
                "by": STAFF,
            }
            for s in SupportSession.objects.filter(target_id=user_id).order_by("started_at")
        ],
        "ask_arkray": [
            {
                "asked_at": _value(q.created_at),
                "question": q.text,
                # Cited records by placeholder, and contact details in the text masked: the
                # customers their answers named are not theirs to receive (L-6).
                "answer": user_text(_answer_text(q.answer or {}, own_refs=set())),
            }
            for q in questions
        ],
        "records_owned": {
            "leads": Lead.objects.filter(owner_id=user_id).count(),
            "opportunities": Opportunity.objects.filter(owner_id=user_id).count(),
            "activities": Activity.objects.filter(owner_id=user_id).count(),
        },
    }


# --- building ---------------------------------------------------------------------------------
README = """Arkray CRM: data export {export_id}
Generated: {generated_at}
Subject: {subject_type} {subject_id}
Request reference: {reference}

data.json      everything, machine-readable
csv/           the same as tables (UTF-8; values starting with = + - @ are prefixed with ')
files/         attached files (customers only), up to the export's size limit
{omitted}
Staff are shown by role only. Free text written by staff may mention other people: review
before releasing this export (docs/privacy.md#access-requests).
"""


def _flatten(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def build(export: DataExport) -> tuple[str, int, str, int, int]:
    """Write the export's ZIP to private storage. Returns (key, size, sha256, files
    included, files omitted)."""
    generated_at = timezone.now()
    files: list[Attachment] = []
    if export.subject_type == SubjectType.LEAD:
        document, files = lead_document(export.subject_id)
        tables = {
            "lead": ([document["lead"]], list(document["lead"])),
            "opportunities": (
                document["opportunities"],
                [*OPPORTUNITY_FIELDS, "pipeline", "stage", "custom_fields"],
            ),
            "stage_history": (
                document["stage_history"],
                ["opportunity_id", "from_stage", "to_stage", "value", "lost_reason", "occurred_at"],
            ),
            "negotiated_prices": (
                document["negotiated_prices"],
                ["opportunity_id", "price", "currency", "agreed_cpt", "stage", "occurred_at"],
            ),
            "activities": (document["activities"], [*ACTIVITY_FIELDS, "opportunity_id"]),
            "attachments": (
                document["attachments"],
                [
                    "id",
                    "note_id",
                    "name",
                    "content_type",
                    "size",
                    "uploaded_at",
                    "deleted_at",
                    "state",
                    "scan_status",
                ],
            ),
            "ask_arkray": (document["ask_arkray"], ["asked_at", "question", "answer"]),
        }
    else:
        document = user_document(export.subject_id)
        tables = {
            "account": ([document["user"]], list(document["user"])),
            "audit_events": (
                document["audit_events"],
                ["action", "occurred_at", "by_them", "about_them", "target_type", "client_address"],
            ),
            "support_sessions": (
                document["support_sessions_for_them"],
                ["started_at", "ended_at", "reason"],
            ),
            "ask_arkray": (document["ask_arkray"], ["asked_at", "question", "answer"]),
        }
    payload = json.dumps(
        {
            "export": {
                "id": str(export.pk),
                "generated_at": generated_at.isoformat(),
                "subject_type": export.subject_type,
                "subject_id": str(export.subject_id),
                "reference": export.reference,
            },
            **document,
        },
        indent=2,
        ensure_ascii=False,
        default=_value,
    ).encode()
    csvs = {name: _csv(_flatten(rows), columns) for name, (rows, columns) in tables.items()}
    limit = settings.EXPORT_MAX_BYTES
    # Everything counts towards the cap: the JSON, its CSV copy and the files.
    budget = limit - len(payload) - sum(len(body) for body in csvs.values()) - len(README)
    if budget < 0:
        raise ExportTooLarge()
    included = omitted = 0
    omitted_reasons: list[str] = []
    with tempfile.TemporaryFile() as spool:
        with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data.json", payload)
            for name, body in csvs.items():
                archive.writestr(f"csv/{name}.csv", body)
            for file in files:
                usable = (
                    file.deleted_at is None
                    and file.state == "stored"
                    and attachments.downloadable(file)
                )
                if not usable:
                    continue
                if file.size > budget:
                    omitted += 1
                    omitted_reasons.append(f"  {file.pk}: over the export's size limit")
                    continue
                try:
                    stored = storage.open_file(file.storage_key)
                except storage.StorageUnavailable:
                    omitted += 1
                    omitted_reasons.append(f"  {file.pk}: storage unavailable or object missing")
                    continue
                with stored:
                    safe = storage.ascii_fallback(file.original_name)
                    with archive.open(f"files/{file.pk}-{safe}", "w") as target:
                        while chunk := stored.read(1024 * 1024):
                            target.write(chunk)
                budget -= file.size
                included += 1
            archive.writestr(
                "README.txt",
                README.format(
                    export_id=export.pk,
                    generated_at=generated_at.isoformat(),
                    subject_type=export.subject_type,
                    subject_id=export.subject_id,
                    reference=export.reference,
                    omitted=("Files left out:\n" + "\n".join(omitted_reasons) + "\n")
                    if omitted_reasons
                    else "",
                ),
            )
        size = spool.tell()
        spool.seek(0)
        digest = hashlib.sha256()
        while chunk := spool.read(1024 * 1024):
            digest.update(chunk)
        spool.seek(0)
        key = storage_key(export)
        storages["exports"].delete(key)  # an earlier attempt's, if a job died after saving
        stored_as = storages["exports"].save(key, File(spool))
    return stored_as, size, digest.hexdigest(), included, omitted


class ExportTooLarge(Exception):
    pass


def run(export_id: UUID) -> None:
    """The outbox job: build a queued export (idempotent: anything else is left alone)."""
    export = DataExport.objects.filter(pk=export_id, status=ExportStatus.QUEUED).first()
    if export is None:
        return
    if not _subject_exists(export.subject_type, export.subject_id):
        DataExport.objects.filter(pk=export.pk).update(
            status=ExportStatus.FAILED, error_code="nothing_left"
        )
        return
    try:
        key, size, digest, included, omitted = build(export)
    except ExportTooLarge:
        DataExport.objects.filter(pk=export.pk).update(
            status=ExportStatus.FAILED, error_code="too_large"
        )
        logger.warning("export_failed", extra={"export_id": str(export.pk), "reason": "too_large"})
        return
    now = timezone.now()
    finished = DataExport.objects.filter(pk=export.pk, status=ExportStatus.QUEUED).update(
        status=ExportStatus.READY,
        storage_key=key,
        size=size,
        sha256=digest,
        files_included=included,
        files_omitted=omitted,
        ready_at=now,
        expires_at=now + timedelta(hours=settings.EXPORT_TTL_HOURS),
    )
    if not finished:  # withdrawn or timed out meanwhile: nothing may stay in storage
        # Its deletion may have run already, before this file existed: due again, for the
        # key just written, durably (PRIV-3).
        DataExport.objects.filter(pk=export.pk).exclude(status=ExportStatus.READY).update(
            storage_key=key,
            purged_at=None,
            purge_requested_at=Coalesce(F("purge_requested_at"), now),
            purge_next_at=now,
        )
        purge_file(export.pk, now)
        return
    logger.info("export_ready", extra={"export_id": str(export.pk), "count": included})


@outbox.handler(TOPIC_BUILD, queue="default", max_attempts=3)
def build_export(payload: dict[str, Any]) -> None:
    run(UUID(str(payload["export_id"])))
