"""Attachment rows against the objects in storage (`manage.py reconcile_attachments`, and the
daily read-only check; docs/runbooks.md#attachment-reconciliation).

Two streams, both in key order and both a page at a time: the rows, by keyset on the unique
`storage_key` index (no OFFSET; each query an index range scan), and the store's listing
(S3 `list_objects_v2` pages, or a sorted walk of the directory). A merge pairs them, so the
cost is one listing call per page of objects plus one query per batch of rows, whatever the
number of files, and memory holds a page of each. Every difference the merge finds is only
a *candidate* until confirmed on its own: an object without a row by a lookup of its key
(a row created meanwhile, or the database ordering a key differently, is not an orphan),
a row without an object by asking the store for that object (only a definitive "no such
object" makes it missing). A store that fails or times out is an error, never a missing
file: after `max_errors` failures in a row the pass stops, so an outage can't be reported as
mass data loss.

A check (the default) changes nothing anywhere. A repair applies only the safe fixes, each
to a row that still looks as it was seen, under a brief SKIP LOCKED lock
(attachments.mark_unavailable and friends): never a deleted row, never a deleted object
that no row names (orphans are reported; removing them is a manual runbook step).
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from arkray.audit import services as audit

from . import attachments, storage, telemetry
from .models import Attachment, AttachmentState, ScanStatus

logger = logging.getLogger(__name__)

# Mismatches, by kind: a stored file whose object is gone; an object no row names; an upload
# that never finished; a file whose malware scan is overdue; the object of a deleted, failed
# or blocked file still in storage; an object that came back for a file reconciliation had
# marked unavailable (a bucket restore: an administrator decides); an object of the wrong
# size or content.
KINDS = (
    "missing",
    "orphaned",
    "pending",
    "pending_scan",
    "failed",
    "restorable",
    "size_mismatch",
    "hash_mismatch",
)
SAMPLE_IDS = 20
MAX_KEYS_SHOWN = 1000
FIELDS = (
    "id",
    "storage_key",
    "size",
    "sha256",
    "state",
    "scan_status",
    "deleted_at",
    "purged_at",
    "created_at",
    "stored_at",
)


@dataclass(frozen=True)
class Options:
    repair: bool = False
    # Younger rows are in progress, not stale: an upload being written, a purge job queued.
    # The housekeeping's own grace (it gives up on uploads after an hour).
    stale_after: timedelta = attachments.ABANDONED_AFTER
    scan_stale_after: timedelta = timedelta(hours=1)
    limit: int | None = None  # rows; a limited pass is partial (no orphans beyond it)
    batch_size: int = 500
    rate: float = 50.0  # storage calls per second
    verify_hash: bool = False
    hash_limit: int = 100
    hash_sample: float = 0.01
    max_errors: int = 5  # in a row, then the pass stops
    retry_pause_s: float = 2.0
    # More missing objects than this in one run is more likely a wrong bucket, an unmounted
    # volume or a restore in progress than lost files: reported, but not repaired.
    max_repair_missing: int = 50
    max_seconds: float | None = None
    show_keys: bool = False


@dataclass
class Report:
    mode: str
    checked: int = 0
    listed: int = 0
    healthy: int = 0
    missing: int = 0
    orphaned: int = 0
    pending: int = 0
    pending_scan: int = 0
    failed: int = 0
    restorable: int = 0
    size_mismatch: int = 0
    hash_mismatch: int = 0
    repaired: int = 0
    errors: int = 0
    complete: bool = False
    aborted: str = ""
    samples: dict[str, list[str]] = field(default_factory=lambda: {kind: [] for kind in KINDS})
    orphan_keys: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def mismatches(self) -> int:
        return sum(getattr(self, kind) for kind in KINDS)

    def exit_code(self) -> int:
        """0 healthy, 1 mismatches found, 2 errors (or the pass couldn't finish)."""
        if self.errors or self.aborted:
            return 2
        return 1 if self.mismatches() else 0

    def counts(self) -> dict[str, int]:
        names = ("checked", "listed", "healthy", *KINDS, "repaired", "errors")
        return {name: int(getattr(self, name)) for name in names}

    def as_dict(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "mode": self.mode,
            "complete": self.complete,
            "aborted": self.aborted,
            "exit_code": self.exit_code(),
            **self.counts(),
            "samples": {kind: ids for kind, ids in self.samples.items() if ids},
            "notes": self.notes,
        }
        if self.orphan_keys:
            found["orphan_keys"] = self.orphan_keys
        return found


class _Abort(Exception):
    pass


class _Rate:
    """At most `per_second` storage calls a second (a pause before each)."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.next = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if self.next > now:
            time.sleep(self.next - now)
        self.next = max(now, self.next) + self.interval


class _Pass:
    def __init__(
        self, options: Options, report: Report, progress: Callable[[str], None] | None
    ) -> None:
        self.options = options
        self.report = report
        self.progress = progress
        self.now = timezone.now()
        self.stale_before = self.now - options.stale_after
        self.scan_stale_before = self.now - options.scan_stale_after
        self.rate = _Rate(options.rate)
        self.in_a_row = 0
        self.hashed = 0
        self.missing_repairs = 0
        self.started = time.monotonic()
        self.pages = storage.ObjectPages(options.batch_size)
        self.buffer: deque[storage.StoredObject] = deque()

    # --- the two streams ------------------------------------------------------------------
    def rows(self) -> Iterator[list[dict[str, Any]]]:
        after: str | None = None
        remaining = self.options.limit
        while remaining is None or remaining > 0:
            size = (
                self.options.batch_size
                if remaining is None
                else min(self.options.batch_size, remaining)
            )
            query = Attachment.objects.order_by("storage_key").values(*FIELDS)
            if after is not None:
                query = query.filter(storage_key__gt=after)
            batch = list(query[:size])
            if not batch:
                return
            yield batch
            after = batch[-1]["storage_key"]
            if remaining is not None:
                remaining -= len(batch)

    def peek(self) -> storage.StoredObject | None:
        while not self.buffer and not self.pages.done:
            self.rate.wait()
            try:
                page = self.pages.next_page()
            except storage.StorageUnavailable as exc:
                self.failed(exc)
                time.sleep(self.options.retry_pause_s)
                continue
            self.answered()
            self.report.listed += len(page)
            self.buffer.extend(page)
        return self.buffer[0] if self.buffer else None

    # --- errors -----------------------------------------------------------------------------
    def failed(self, exc: storage.StorageUnavailable) -> None:
        self.report.errors += 1
        self.in_a_row += 1
        if self.in_a_row >= self.options.max_errors:
            raise _Abort(
                f"storage unavailable ({exc.error_class}): {self.in_a_row} failures in a row"
            )

    def answered(self) -> None:
        self.in_a_row = 0

    def note(self, kind: str, attachment_id: object) -> None:
        setattr(self.report, kind, getattr(self.report, kind) + 1)
        if len(self.report.samples[kind]) < SAMPLE_IDS:
            self.report.samples[kind].append(str(attachment_id))

    # --- the merge --------------------------------------------------------------------------
    def run(self) -> None:
        for batch in self.rows():
            for row in batch:
                key = row["storage_key"]
                while (found := self.peek()) is not None and found.key < key:
                    self.orphan_candidate(self.buffer.popleft())
                found = self.peek()
                if found is not None and found.key == key:
                    self.buffer.popleft()
                    self.present(row, found.size)
                else:
                    self.absent_candidate(row)
                self.report.checked += 1
            self.batch_done()
        if self.options.limit is None:
            # Objects after the last row's key: candidates too.
            drained = 0
            while self.peek() is not None:
                self.orphan_candidate(self.buffer.popleft())
                drained += 1
                if drained % self.options.batch_size == 0:
                    self.batch_done()
            self.report.complete = True

    def batch_done(self) -> None:
        report = self.report
        if self.progress is not None:
            found = ", ".join(f"{kind} {getattr(report, kind)}" for kind in KINDS)
            self.progress(
                f"checked {report.checked} rows, {report.listed} objects: healthy"
                f" {report.healthy}, {found}, repaired {report.repaired}, errors {report.errors}"
            )
        limit = self.options.max_seconds
        if limit is not None and time.monotonic() - self.started > limit:
            raise _Abort(f"time budget of {limit:.0f} s used up")

    def orphan_candidate(self, found: storage.StoredObject) -> None:
        # A row created after its batch was read, or ordered elsewhere by the database: not
        # an orphan (the row stream classifies it, or it is newer than this pass).
        if Attachment.objects.filter(storage_key=found.key).exists():
            return
        self.report.orphaned += 1
        if self.options.show_keys and len(self.report.orphan_keys) < MAX_KEYS_SHOWN:
            self.report.orphan_keys.append(found.key)

    def absent_candidate(self, row: dict[str, Any]) -> None:
        gone = self.gone(row)
        if gone and row["purged_at"] is not None:
            self.report.healthy += 1  # removed, as it should be
            return
        if row["state"] == AttachmentState.UPLOADING and row["created_at"] >= self.stale_before:
            self.report.healthy += 1  # being written
            return
        self.rate.wait()
        try:
            size = storage.size(row["storage_key"])
        except storage.ObjectMissing:
            self.answered()
            self.absent(row)
            return
        except storage.StorageUnavailable as exc:
            self.failed(exc)
            return
        self.answered()
        self.present(row, size)

    # --- classification -----------------------------------------------------------------------
    def gone(self, row: dict[str, Any]) -> bool:
        return (
            row["deleted_at"] is not None
            or row["state"] == AttachmentState.FAILED
            or row["scan_status"] == ScanStatus.REJECTED
        )

    def stale(self, row: dict[str, Any]) -> bool:
        since = max(row["created_at"], row["deleted_at"] or row["created_at"])
        return bool(since < self.stale_before)

    def absent(self, row: dict[str, Any]) -> None:
        """No object, confirmed by the store."""
        pk, seen = row["id"], _seen(row)
        if self.gone(row):
            if not self.stale(row):
                self.report.healthy += 1  # its purge job hasn't run yet
                return
            self.note("failed", pk)  # its purge never finished: only the bookkeeping is left
            self.repair(lambda: attachments.purge_again(pk, seen))
        elif row["state"] == AttachmentState.UPLOADING:
            self.note("pending", pk)
            self.repair(lambda: attachments.fail_stale_upload(pk, seen))
        else:
            self.note("missing", pk)
            if not self.options.repair:
                return
            if self.missing_repairs >= self.options.max_repair_missing:
                message = (
                    f"more than {self.options.max_repair_missing} missing objects: not marked"
                    " unavailable (check the bucket or volume, or a restore, first)"
                )
                if message not in self.report.notes:
                    self.report.notes.append(message)
                return
            self.missing_repairs += 1
            self.repair(lambda: attachments.mark_unavailable(pk, seen))

    def present(self, row: dict[str, Any], size: int) -> None:
        """An object exists under the row's key, of `size` bytes."""
        pk, seen = row["id"], _seen(row)
        if self.gone(row):
            if row["purged_at"] is None and not self.stale(row):
                self.report.healthy += 1  # its purge job hasn't run yet
                return
            erased = row["sha256"] == attachments.ERASED_SHA256
            if (
                row["purged_at"] is not None
                and not erased
                and attachments.was_marked_unavailable(pk)
            ):
                self.note("restorable", pk)  # see docs/runbooks.md#attachment-object-missing
                return
            self.note("failed", pk)
            self.repair(lambda: self.purge(pk, seen))
            return
        if row["state"] == AttachmentState.UPLOADING:
            if row["created_at"] >= self.stale_before:
                self.report.healthy += 1  # being written
                return
            self.note("pending", pk)
            if size == row["size"] and self.content_matches(row) is not False:
                self.repair(lambda: attachments.finish_stale_upload(pk, seen))
            else:
                self.repair(lambda: attachments.fail_stale_upload(pk, seen))
            return
        if size != row["size"]:
            self.note("size_mismatch", pk)
            return
        if self.sampled() and self.content_matches(row) is False:
            self.note("hash_mismatch", pk)
            return
        waiting_since = row["stored_at"] or row["created_at"]
        if row["scan_status"] == ScanStatus.PENDING and waiting_since < self.scan_stale_before:
            self.note("pending_scan", pk)
            return
        self.report.healthy += 1

    def sampled(self) -> bool:
        options = self.options
        if not options.verify_hash or self.hashed >= options.hash_limit:
            return False
        return random.random() < options.hash_sample  # noqa: S311 — sampling, not secrecy

    def content_matches(self, row: dict[str, Any]) -> bool | None:
        """The object's SHA-256 against the row's, when hashes are being verified (None:
        not checked)."""
        if not self.options.verify_hash or self.hashed >= self.options.hash_limit:
            return None
        self.hashed += 1
        self.rate.wait()
        digest = hashlib.sha256()
        try:
            file = storage.open_file(row["storage_key"])
        except storage.StorageUnavailable as exc:
            self.failed(exc)
            return None
        self.answered()
        for chunk in storage.iter_file(file):
            digest.update(chunk)
        return bool(digest.hexdigest() == row["sha256"])

    # --- repairs ------------------------------------------------------------------------------
    def purge(self, pk: Any, seen: attachments.Seen) -> bool:
        self.rate.wait()
        return attachments.purge_again(pk, seen)

    def repair(self, fix: Callable[[], bool]) -> None:
        if not self.options.repair:
            return
        try:
            if fix():
                self.report.repaired += 1
        except storage.StorageUnavailable as exc:
            self.failed(exc)


def _seen(row: dict[str, Any]) -> attachments.Seen:
    return attachments.Seen(row["state"], row["deleted_at"], row["purged_at"])


def run(options: Options, *, progress: Callable[[str], None] | None = None) -> Report:
    report = Report(mode="repair" if options.repair else "check")
    work = _Pass(options, report, progress)
    try:
        work.run()
    except _Abort as abort:
        report.aborted = str(abort)
        report.complete = False
    _finish(report, options, started=work.now)
    return report


def _finish(report: Report, options: Options, *, started: datetime) -> None:
    counts = report.counts()
    logger.info(
        "attachment_reconcile_finished",
        extra={
            "mode": report.mode,
            "complete": report.complete,
            "aborted": bool(report.aborted),
            "exit_code": report.exit_code(),
            "backend": storage.backend_name(),
            **counts,
        },
    )
    # A pass over part of the rows (--limit) says nothing about the rest: only whole passes,
    # or ones that couldn't finish (an error is worth seeing), become "the last" one.
    if report.complete or report.aborted:
        published = telemetry.publish_reconcile(
            {"mode": report.mode, "outcome": report.exit_code(), **counts}
        )
        if not published:
            report.notes.append("the result could not be published to the metrics (cache down)")
    if options.repair:
        audit.record(
            attachments.AUDIT_RECONCILED,
            actor_id=None,
            target_type="attachment",
            metadata={
                "complete": report.complete,
                "aborted": bool(report.aborted),
                "started_at": started.isoformat(),
                **counts,
            },
        )
