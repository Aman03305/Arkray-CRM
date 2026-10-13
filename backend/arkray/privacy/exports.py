"""Exporting a person's data on a verified access request (privacy remediation P2-5;
docs/privacy.md#access-requests).

An administrator who has verified the requester's identity (`reference`: the request's
ticket; `identity_verified`) asks for an export of one **customer** (a lead) or one **staff
member** (a user). A job builds a ZIP into private storage (STORAGES["exports"]):

- `data.json`, the machine-readable whole, and `csv/*.csv`, the same as tables (each cell
  that a spreadsheet would run as a formula is prefixed with a quote);
- `files/`: the customer's attached files that are stored and downloadable (not deleted,
  not blocked by the scan), while the archive stays within EXPORT_MAX_BYTES; the rest are
  listed, with why they were left out;
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

**Bounded memory** (review of 73ff1e3, M6): the build loaded every record of the customer,
full text included, then made the whole JSON and every CSV as strings before writing the ZIP
(a customer with 1,500 long notes peaked at 82 MB for 12 MB of text; tens of thousands, which
nothing capped, could exhaust the worker). Now the ids are read first, then the records in
batches (EXPORT_BATCH; SMALL_BATCH for short rows such as the stage history), written straight
into the ZIP in the worker's temporary directory: `data.json` first, then each table's CSV from
a second pass over the same records. Both passes read one snapshot (a REPEATABLE READ, READ
ONLY transaction), so the JSON and the CSVs agree, and nothing else is spooled: the archive is
the only temporary file.

**The size cap** (review round 1, QUEUES-1, QUEUES-5) bounds the archive as it is stored
(compressed): what the worker's temporary space holds while it is built (beside one file being
copied in), what the store keeps and what the edge spools for its download, all three sized
from EXPORT_MAX_BYTES (docs/reliability.md#export-sizing). Counting the uncompressed JSON and
CSV instead refused customers well within the budgets: 5,000 notes of 10,000 characters came
to 104.5 MB, Hindi ones from about 1,929 notes, and one deal moved between stages for six
hours did it too. The structured data is written first and must fit (else the export fails,
`too_large`); files are added while they fit; README.txt names those left out (at most
README_OMITTED_MAX of them, then a count). A customer holds at most ACTIVITY_MAX_PER_CUSTOMER
activities (activities.services).
"""

from __future__ import annotations

import codecs
import contextlib
import csv
import functools
import hashlib
import io
import json
import logging
import re
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import IO, Any
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.core.files import File
from django.core.files.storage import storages
from django.db import connection, transaction
from django.db.models import F, Q, QuerySet
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
from arkray.pipeline.models import (
    CustomField,
    NegotiationPrice,
    Opportunity,
    OpportunityCorrection,
    StageHistory,
)

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
# Records read at a time (M6): one batch of them in memory, however many the customer has.
EXPORT_BATCH = 100  # records with long text (activities, deals, questions)
SMALL_BATCH = 1000  # short rows (stage history, prices, corrections, timeline, files' rows)
COPY_CHUNK = 256 * 1024  # bytes copied at a time: the files, the digest
# Room kept at the end of the archive for README.txt and the ZIP's central directory (review
# round 1): a tenth of the cap for a small one. Each included file also takes ENTRY_OVERHEAD
# (its local header and central directory record) and deflate's worst case (incompressible
# data grows by about 0.03 %).
TAIL_RESERVE = 128 * 1024
ENTRY_OVERHEAD = 1024
README_OMITTED_MAX = 1000  # files named one by one in README.txt; the rest counted
# How long a build may run (review round 1): longer than the workers' default 100 s, within
# the outbox's 300 s running lease. A customer at the budgets builds in about 30 s (5,000
# notes of 10,000 Hindi characters, measured at a tenth and scaled); short history rows go at
# about 11,000 a second (docs/reliability.md#export-sizing).
EXPORT_TIME_LIMIT_S = 240
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# Skipped before the first character a spreadsheet reads (review round 1, QUEUES-6): blanks,
# control and format characters (zero-width space, byte order mark, word joiner, direction
# marks, soft hyphen, ...) and combining marks; and what reads as a formula's start once
# folded: compatibility forms (full-width and small = + - @, by NFKC) and the minus sign.
_SKIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp", "Mn", "Me"})
_MINUS_SIGN = chr(0x2212)  # no compatibility mapping to '-'
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
        # NO KEY: user rows are never locked FOR UPDATE (ADR-0035 review: a deadlock with the
        # foreign-key checks of a team administrator's writes; identity.services).
        list(
            User.objects.select_for_update(no_key=True)
            .filter(pk=actor.pk)
            .values_list("pk", flat=True)
        )
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


def _starts_a_formula(text: str) -> bool:
    """Whether a spreadsheet could read `text` as a formula: it starts with one of
    _FORMULA_START, or does once what a spreadsheet may skip or fold is (whitespace,
    invisible format characters, full-width and other compatibility forms, the minus sign)."""
    if text.startswith(_FORMULA_START):
        return True
    for character in text:
        if character.isspace() or unicodedata.category(character) in _SKIPPED_CATEGORIES:
            continue
        folded = unicodedata.normalize("NFKC", character)
        return folded.startswith(_FORMULA_START) or character == _MINUS_SIGN
    return False


def _cell(value: Any) -> str:
    """A CSV cell a spreadsheet never runs: formulas are quoted (CSV injection), also behind
    whitespace a spreadsheet may skip ("  =HYPERLINK(...)", a no-break space; review of
    73ff1e3, Low), invisible characters and look-alikes of = + - @ (review round 1)."""
    text = (
        "" if value is None else json.dumps(value) if isinstance(value, dict | list) else str(value)
    )
    return f"'{text}" if _starts_a_formula(text) else text


class ExportTooLarge(Exception):
    pass


class _Budget:
    """The archive as stored (compressed) against EXPORT_MAX_BYTES, read from the spool as it
    is written (review round 1, QUEUES-1): the structured data must leave room for the
    README and the central directory (`tail`); a file goes in only if it fits whole."""

    def __init__(self, spool: IO[bytes], cap: int) -> None:
        self.spool = spool
        self.cap = cap
        self.tail = min(TAIL_RESERVE, cap // 10)

    def check(self) -> None:
        """Past the cap: the build stops at once (deflate holds back at most a few hundred
        kilobytes, so the spool may lag that much behind what was handed to it)."""
        if self.spool.tell() > self.cap - self.tail:
            raise ExportTooLarge()

    def fits(self, size: int) -> bool:
        """Whether a file of `size` bytes fits whole, at deflate's worst."""
        needed = size + size // 1000 + ENTRY_OVERHEAD
        return self.spool.tell() + needed <= self.cap - self.tail


def _in_batches(
    queryset: QuerySet[Any], ids: Sequence[Any], batch: int = EXPORT_BATCH
) -> Iterator[Any]:
    """The rows of `queryset` with these ids, in the ids' order, `batch` at a time: memory
    holds one batch however many there are, with or without server-side cursors (a transaction
    pooler turns them off, and psycopg then reads a whole result into memory). A row gone
    meanwhile is left out."""
    for start in range(0, len(ids), batch):
        chunk = list(ids[start : start + batch])
        found = queryset.order_by().in_bulk(chunk)
        yield from (found[pk] for pk in chunk if pk in found)


def _stream(queryset: QuerySet[Any], *order: str, batch: int = EXPORT_BATCH) -> Iterator[Any]:
    """Every row of `queryset` in `order`: the ids when first asked for (small), then the rows
    in batches (_in_batches)."""
    ids = list(queryset.order_by(*order).values_list("pk", flat=True))
    yield from _in_batches(queryset, ids, batch)


def _stream_values(
    queryset: QuerySet[Any], order: Sequence[str], fields: Sequence[str]
) -> Iterator[dict[str, Any]]:
    """The rows' `fields` as dicts, in `order`, SMALL_BATCH at a time: for the short rows a
    user can multiply without a budget (stage moves, price revisions, the timeline), read
    without building a model instance each (about twice as fast)."""
    ids = list(queryset.order_by(*order).values_list("pk", flat=True))
    for start in range(0, len(ids), SMALL_BATCH):
        chunk = ids[start : start + SMALL_BATCH]
        found = {
            row["pk"]: row for row in queryset.order_by().filter(pk__in=chunk).values("pk", *fields)
        }
        yield from (found[pk] for pk in chunk if pk in found)


Rows = Callable[[], Iterator[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class Document:
    """What an export holds, in data.json's order. Each section is an object, or `Rows`: a
    function returning its rows afresh, read (in batches) only as they are written, once for
    data.json and once more for the section's CSV. `tables`: the sections that also get a
    CSV (its name and columns), in the ZIP's order; `files`: a customer's attached files."""

    sections: list[tuple[str, Any]]
    tables: dict[str, tuple[str, Sequence[str]]]
    files: Callable[[], Iterator[Attachment]] | None = None


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
# The figures each correction of a closed deal records before and after (pipeline.models.
# OpportunityCorrection): "value_before", "value_after", ...
CORRECTION_FIGURES = ("value", "expected_close_date", "opportunity_date")
CORRECTION_COLUMNS = [
    "opportunity_id",
    "fields",
    *(f"{field}_{when}" for field in CORRECTION_FIGURES for when in ("before", "after")),
    "reason",
    "occurred_at",
]
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


HISTORY_VALUES = (
    "opportunity_id",
    "from_stage_name",
    "to_stage_name",
    "value",
    "expected_close_date",
    "opportunity_date",
    "lost_reason",
    "occurred_at",
)
PRICE_VALUES = ("opportunity_id", "price", "currency", "agreed_cpt", "stage_name", "occurred_at")
LEAD_TABLES: dict[str, tuple[str, Sequence[str]]] = {
    "lead": ("lead", ("id", *LEAD_FIELDS, "status", "source", "owner")),
    "opportunities": ("opportunities", (*OPPORTUNITY_FIELDS, "pipeline", "stage", "custom_fields")),
    "stage_history": (
        "stage_history",
        (
            "opportunity_id",
            "from_stage",
            "to_stage",
            "value",
            "expected_close_date",
            "opportunity_date",
            "lost_reason",
            "occurred_at",
        ),
    ),
    "negotiated_prices": (
        "negotiated_prices",
        ("opportunity_id", "price", "currency", "agreed_cpt", "stage", "occurred_at"),
    ),
    "deal_corrections": ("deal_corrections", CORRECTION_COLUMNS),
    "activities": ("activities", (*ACTIVITY_FIELDS, "opportunity_id")),
    "attachments": (
        "attachments",
        (
            "id",
            "note_id",
            "name",
            "content_type",
            "size",
            "uploaded_at",
            "deleted_at",
            "state",
            "scan_status",
        ),
    ),
    "ask_arkray": ("ask_arkray", ("asked_at", "question", "answer")),
}


def lead_document(lead_id: UUID) -> tuple[dict[str, Any], list[Attachment]]:
    """A customer's export as one document and its files, all in memory: for a caller that
    looks at it whole (tests). The build streams it instead (lead_sections)."""
    document = lead_sections(lead_id)
    whole = {name: list(value()) if _is_rows(value) else value for name, value in document.sections}
    return whole, list(document.files()) if document.files is not None else []


def lead_sections(lead_id: UUID) -> Document:
    """A customer's export. Only ids (and an activity's type) are read up front: what the Ask
    Arkray conversations about the customer are matched by. The records are read in batches
    as they are written (build), once for data.json and once for the CSVs."""
    lead = Lead.objects.select_related("status", "source").get(pk=lead_id)
    opportunities = Opportunity.objects.filter(lead_id=lead_id).select_related("stage", "pipeline")
    opportunity_ids = list(opportunities.order_by("created_at", "pk").values_list("pk", flat=True))
    activities = Activity.objects.filter(Q(lead_id=lead_id) | Q(opportunity_id__in=opportunity_ids))
    activity_refs = list(activities.order_by("created_at", "pk").values_list("pk", "type"))
    activity_ids = [pk for pk, _ in activity_refs]
    files = Attachment.objects.filter(note_id__in=activity_ids)
    names = {
        str(f.pk): (f.name, f.is_active)
        for f in CustomField.objects.filter(
            pipeline_id__in=set(opportunities.values_list("pipeline_id", flat=True))
        )
    }
    history = StageHistory.objects.filter(opportunity_id__in=opportunity_ids)
    prices = NegotiationPrice.objects.filter(opportunity_id__in=opportunity_ids)
    # Corrections of its closed deals' figures, with their reasons (review of 73ff1e3, M2).
    deal_corrections = OpportunityCorrection.objects.filter(opportunity_id__in=opportunity_ids)
    records = {lead_id, *opportunity_ids, *activity_ids}
    refs = {f"lead:{lead_id}", *(f"opportunity:{pk}" for pk in opportunity_ids)} | {
        f"{kind}:{pk}" for pk, kind in activity_refs
    }
    terms = subjects.identifying_terms(lead)
    conversation_ids = subjects.touching_conversations(records, terms)
    # Only the questions about this person, not every question of a conversation that once
    # touched them (backend review P2: a sibling question named other customers).
    questions = Question.objects.filter(conversation_id__in=conversation_ids, status="answered")
    sections: list[tuple[str, Any]] = [
        (
            "lead",
            {
                "id": str(lead.pk),
                **_row(lead, LEAD_FIELDS),
                "status": lead.status.name,
                "source": lead.source.name if lead.source else None,
                "owner": STAFF,
            },
        ),
        (
            "opportunities",
            lambda: (
                {
                    **_row(o, OPPORTUNITY_FIELDS),
                    "pipeline": o.pipeline.name,
                    "stage": o.stage.name,
                    "custom_fields": _custom_values(o, names),
                    "owner": STAFF,
                }
                for o in _in_batches(opportunities, opportunity_ids)
            ),
        ),
        (
            "stage_history",
            lambda: (
                {
                    "opportunity_id": str(h["opportunity_id"]),
                    "from_stage": h["from_stage_name"],
                    "to_stage": h["to_stage_name"],
                    "value": _value(h["value"]),
                    # What the deal's dates were at that move (pipeline.0012); null before.
                    "expected_close_date": _value(h["expected_close_date"]),
                    "opportunity_date": _value(h["opportunity_date"]),
                    "lost_reason": h["lost_reason"],
                    "occurred_at": _value(h["occurred_at"]),
                }
                for h in _stream_values(history, ("occurred_at", "pk"), HISTORY_VALUES)
            ),
        ),
        (
            "negotiated_prices",
            lambda: (
                {
                    "opportunity_id": str(p["opportunity_id"]),
                    "price": _value(p["price"]),
                    "currency": p["currency"],
                    "agreed_cpt": p["agreed_cpt"],
                    "stage": p["stage_name"],
                    "occurred_at": _value(p["occurred_at"]),
                }
                for p in _stream_values(prices, ("occurred_at", "pk"), PRICE_VALUES)
            ),
        ),
        (
            "deal_corrections",
            lambda: (
                {
                    "opportunity_id": str(c.opportunity_id),
                    "fields": list(c.fields),
                    **{
                        f"{field}_{when}": _value(getattr(c, f"{field}_{when}"))
                        for field in CORRECTION_FIGURES
                        for when in ("before", "after")
                    },
                    "reason": c.reason,
                    "occurred_at": _value(c.occurred_at),
                }
                for c in _stream(deal_corrections, "occurred_at", "id", batch=SMALL_BATCH)
            ),
        ),
        (
            "activities",
            lambda: (
                {
                    **_row(a, ACTIVITY_FIELDS),
                    "opportunity_id": _value(a.opportunity_id),
                    "owner": STAFF,
                }
                for a in _in_batches(activities, activity_ids)
            ),
        ),
        (
            "attachments",
            lambda: (
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
                for f in _stream(files, "created_at", "pk", batch=SMALL_BATCH)
            ),
        ),
        (
            "timeline",
            lambda: (
                {"kind": e["kind"], "occurred_at": _value(e["occurred_at"])}
                for e in _stream_values(
                    TimelineEntry.objects.filter(lead_id=lead_id),
                    ("occurred_at", "pk"),
                    ("kind", "occurred_at"),
                )
            ),
        ),
        (
            "ask_arkray",
            lambda: (
                {
                    "asked_at": _value(q.created_at),
                    "asked_by": STAFF,
                    "question": q.text,
                    "answer": _answer_text(q.answer or {}, refs),
                }
                for q in _stream(questions, "created_at", "pk")
                if _about(q, refs, terms)
            ),
        ),
    ]
    return Document(sections, LEAD_TABLES, files=lambda: _stream(files, "created_at", "pk"))


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


USER_TABLES: dict[str, tuple[str, Sequence[str]]] = {
    "user": ("account", ("id", *USER_FIELDS)),
    "audit_events": (
        "audit_events",
        ("action", "occurred_at", "by_them", "about_them", "target_type", "client_address"),
    ),
    "support_sessions_for_them": ("support_sessions", ("started_at", "ended_at", "reason")),
    "ask_arkray": ("ask_arkray", ("asked_at", "question", "answer")),
}


# --- building ---------------------------------------------------------------------------------
README = """Arkray CRM: data export {export_id}
Generated: {generated_at}
Subject: {subject_type} {subject_id}
Request reference: {reference}

data.json      everything, machine-readable
csv/           the same as tables (UTF-8; values starting with = + - @ are prefixed with ')
files/         attached files (customers only), while the archive is within its size limit
{omitted}
Staff are shown by role only. Free text written by staff may mention other people: review
before releasing this export (docs/privacy.md#access-requests).
"""


def _indented(value: Any, level: int) -> str:
    """`value` as json.dumps(indent=2) writes it `level` levels deep (a JSON string never
    holds a raw line break, so every line break is the layout's)."""
    text = json.dumps(value, indent=2, ensure_ascii=False, default=_value)
    return text.replace("\n", "\n" + "  " * level)


def _is_rows(value: Any) -> bool:
    return callable(value)


def _write_document(target: IO[bytes], sections: list[tuple[str, Any]], budget: _Budget) -> None:
    """data.json, section by section, byte for byte what json.dumps(indent=2) made of the
    whole document."""

    def write(text: str) -> None:
        target.write(text.encode())
        budget.check()

    write("{")
    for index, (name, value) in enumerate(sections):
        write(("," if index else "") + "\n  " + json.dumps(name) + ": ")
        if not _is_rows(value):
            write(_indented(value, 1))
            continue
        count = 0
        for row in value():
            write(("," if count else "[") + "\n    " + _indented(row, 2))
            count += 1
        write("\n  ]" if count else "[]")
    write("\n}")


def _write_table(
    target: IO[bytes], columns: Sequence[str], rows: Iterable[Mapping[str, Any]], budget: _Budget
) -> None:
    """One table's CSV, UTF-8 with a byte order mark (spreadsheets need it), row by row."""
    line = io.StringIO()
    writer = csv.writer(line)

    def emit(cells: Sequence[str]) -> None:
        line.seek(0)
        line.truncate()
        writer.writerow(cells)
        target.write(line.getvalue().encode())
        budget.check()

    target.write(codecs.BOM_UTF8)
    emit(columns)
    for row in rows:
        emit([_cell(row.get(column)) for column in columns])


@contextlib.contextmanager
def _snapshot() -> Iterator[None]:
    """One view of the subject for both passes (data.json, then the CSVs), so they agree
    however the records change meanwhile: a REPEATABLE READ, READ ONLY transaction of its own.
    Inside a transaction already (a test's), that one's view."""
    if connection.in_atomic_block:
        yield
        return
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield


def _document(export: DataExport) -> Document:
    if export.subject_type == SubjectType.LEAD:
        return lead_sections(export.subject_id)
    sections: list[tuple[str, Any]] = [
        (name, (lambda rows=value: iter(rows)) if isinstance(value, list) else value)
        for name, value in user_document(export.subject_id).items()
    ]
    return Document(sections, USER_TABLES)


def _readme(export: DataExport, generated_at: datetime, omitted: list[str]) -> str:
    listed = omitted[:README_OMITTED_MAX]
    if len(omitted) > len(listed):
        listed.append(f"  ... and {len(omitted) - len(listed)} more")
    return README.format(
        export_id=export.pk,
        generated_at=generated_at.isoformat(),
        subject_type=export.subject_type,
        subject_id=export.subject_id,
        reference=export.reference,
        omitted=("Files left out:\n" + "\n".join(listed) + "\n") if listed else "",
    )


def build(export: DataExport) -> tuple[str, int, str, int, int]:
    """Write the export's ZIP to private storage. Returns (key, size, sha256, files
    included, files omitted). In bounded memory and within EXPORT_MAX_BYTES as stored (module
    docstring): the records are read in batches as they are written, into the archive only."""
    generated_at = timezone.now()
    head = {
        "id": str(export.pk),
        "generated_at": generated_at.isoformat(),
        "subject_type": export.subject_type,
        "subject_id": str(export.subject_id),
        "reference": export.reference,
    }
    included = 0
    omitted: list[str] = []
    with tempfile.TemporaryFile() as spool:
        budget = _Budget(spool, settings.EXPORT_MAX_BYTES)
        with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            with _snapshot():
                document = _document(export)
                with archive.open("data.json", "w") as target:
                    _write_document(target, [("export", head), *document.sections], budget)
                sections = dict(document.sections)
                for section, (name, columns) in document.tables.items():
                    value = sections[section]
                    rows = value() if _is_rows(value) else [value]
                    with archive.open(f"csv/{name}.csv", "w") as target:
                        _write_table(target, columns, rows, budget)
            for file in document.files() if document.files is not None else ():
                usable = (
                    file.deleted_at is None
                    and file.state == "stored"
                    and attachments.downloadable(file)
                )
                if not usable:
                    continue
                if not budget.fits(file.size):
                    omitted.append(f"  {file.pk}: over the export's size limit")
                    continue
                try:
                    stored = storage.open_file(file.storage_key)
                except storage.StorageUnavailable:
                    omitted.append(f"  {file.pk}: storage unavailable or object missing")
                    continue
                with stored:
                    safe = storage.ascii_fallback(file.original_name)
                    with archive.open(f"files/{file.pk}-{safe}", "w") as target:
                        while chunk := stored.read(COPY_CHUNK):
                            target.write(chunk)
                included += 1
            archive.writestr("README.txt", _readme(export, generated_at, omitted))
        size = spool.tell()
        spool.seek(0)
        digest = hashlib.sha256()
        while chunk := spool.read(COPY_CHUNK):
            digest.update(chunk)
        spool.seek(0)
        key = storage_key(export)
        storages["exports"].delete(key)  # an earlier attempt's, if a job died after saving
        stored_as = storages["exports"].save(key, File(spool))
    return stored_as, size, digest.hexdigest(), included, len(omitted)


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


@outbox.handler(TOPIC_BUILD, queue="default", max_attempts=3, time_limit=EXPORT_TIME_LIMIT_S)
def build_export(payload: dict[str, Any]) -> None:
    run(UUID(str(payload["export_id"])))
