"""Note attachments: upload, list, download, delete (docs/activities.md#attachments).

Authorization is the note's, for every operation and every caller:
- a note (and so its files) is visible exactly when the note is, in the caller's scope:
  outside it, NotFoundError (404), indistinguishable from a file that doesn't exist; every
  download re-checks this, so knowing a file's id or URL grants nothing;
- adding or deleting a file changes the note: its author may, and an administrator working
  in someone's workspace (crm.manage_any; recorded as the actor, the owner as the subject);
  others get 403, like editing the note's text.

Upload (three steps, so storage and the database never disagree for long, and no lock is
held while bytes travel): the file is received, bounded and recognised first (no lock); then
the note is locked (lead -> note, the activities lock order), the per-note limit checked and
a row written in state "uploading" with a generated key; then the object is stored outside
any transaction; then the row is marked stored and audited. A storage failure marks the row
failed (503 to the client); a crash in between leaves an "uploading" row that the hourly
housekeeping resolves (object deleted if present, row failed). Deleting hides the file at
once and removes the object by a job (and, failing that, the housekeeping). All idempotent.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import IO
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import outbox
from arkray.core.access import AccessScope
from arkray.core.errors import (
    BusinessRuleViolation,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
)
from arkray.identity.models import User
from arkray.identity.workspaces import authorize_write

from . import selectors, storage
from .models import (
    Activity,
    ActivityType,
    Attachment,
    AttachmentState,
    ScanStatus,
)

logger = logging.getLogger(__name__)

TOPIC_SCAN = "activities.scan_attachment"
TOPIC_PURGE = "activities.purge_attachment"
AUDIT_UPLOADED = "attachment.uploaded"
AUDIT_DELETED = "attachment.deleted"
AUDIT_REJECTED = "attachment.rejected"

NOTES_ONLY = "Files can be attached to notes."
ARCHIVED_NOTE = "This note is archived. Restore it to change its files."
TOO_MANY = "A note can have at most {limit} files."
EDIT_ONLY = "Only the note's author (or an administrator) can change its files."
STORAGE_DOWN = "File storage is unavailable right now. Try again in a few minutes."
SCANNING = "This file is still being checked for viruses. Try again in a minute."
BLOCKED = "This file was blocked by the virus scan and can't be downloaded."
NOT_PREVIEWABLE = "Only images can be previewed."
# How long an upload may stay unfinished before housekeeping gives up on it.
ABANDONED_AFTER = timedelta(hours=1)
ERASED_SHA256 = "0" * 64
# Files stored while no scanner was configured, queued for scanning per housekeeping run once
# one is (until then they are not downloadable: only clean files are, with a scanner).
SCAN_BACKLOG_BATCH = 500


class FileTooLarge(DomainError):
    code = "file_too_large"
    http_status = 413
    default_message = "This file is too large."


class StorageDown(ServiceUnavailableError):
    code = "storage_unavailable"
    default_message = STORAGE_DOWN


def may_change_note(actor: User, scope: AccessScope, note: Activity) -> bool:
    """A note's text and files are its author's; an administrator working in someone's
    workspace (crm.manage_any, already required by authorize_write there) may change them
    too, recorded as themselves (docs/activities.md#notes)."""
    return note.created_by_id == actor.pk or scope.is_delegated


def _visible_note(scope: AccessScope, note_id: UUID) -> Activity:
    note = selectors.activity_detail(scope, note_id)
    if note.type != ActivityType.NOTE:
        raise NotFoundError()
    return note


def _audit(
    action: str, actor_id: UUID | None, attachment: Attachment, note: Activity, **meta: object
) -> None:
    audit.record(
        action,
        actor_id=actor_id,
        target_type="attachment",
        target_id=attachment.pk,
        subject_user_id=note.owner_id if note.owner_id != actor_id else None,
        metadata={"note_id": str(note.pk), **meta},
    )


def _size_bucket(size: int) -> str:
    """A coarse size for audit and logs (never the name or the content)."""
    for limit, label in (
        (100 * 1024, "<100KB"),
        (1024 * 1024, "<1MB"),
        (10 * 1024 * 1024, "<10MB"),
    ):
        if size < limit:
            return label
    return ">=10MB"


# --- upload ---------------------------------------------------------------------------------------
def upload(
    *,
    actor: User,
    scope: AccessScope,
    note_id: UUID,
    filename: str,
    stream: storage.Readable,
    declared_length: int | None,
) -> Attachment:
    authorize_write(actor, scope)
    note = _visible_note(scope, note_id)  # 404 before anything is read
    if not may_change_note(actor, scope, note):
        raise PermissionDeniedError(EDIT_ONLY)
    try:
        name, extension = storage.clean_filename(filename)
    except storage.RejectedFile as exc:
        raise InvalidInputError(details={"file": [str(exc)]}) from None
    limit = settings.ATTACHMENT_MAX_BYTES
    if declared_length is not None and declared_length > limit:
        raise FileTooLarge(f"Files can be at most {limit // (1024 * 1024)} MB.")
    try:
        received = storage.receive(stream, max_bytes=limit)
    except storage.RejectedFile as exc:
        if "at most" in str(exc):
            raise FileTooLarge(str(exc)) from None
        raise InvalidInputError(details={"file": [str(exc)]}) from None
    try:
        kind = storage.recognise(extension, received)
        attachment = _reserve(actor, scope, note_id, name, extension, kind, received)
        try:
            storage.save(attachment.storage_key, received.file)
        except storage.StorageUnavailable:
            Attachment.objects.filter(pk=attachment.pk, state=AttachmentState.UPLOADING).update(
                state=AttachmentState.FAILED
            )
            logger.warning("attachment_upload_failed", extra={"reason": "storage"})
            raise StorageDown() from None
        return _finish(actor, scope, attachment)
    except storage.RejectedFile as exc:
        logger.info("attachment_rejected", extra={"extension": extension, "reason": "content"})
        raise InvalidInputError(details={"file": [str(exc)]}) from None
    finally:
        received.file.close()


def _reserve(
    actor: User,
    scope: AccessScope,
    note_id: UUID,
    name: str,
    extension: str,
    kind: storage.FileKind,
    received: storage.Received,
) -> Attachment:
    """Step 2: under the note's lock (lead first), check it and the limit, write the row."""
    from .services import lock_note  # the activities lock order lives there

    with transaction.atomic():
        note = lock_note(scope, note_id, adding=True)
        if note.archived_at is not None:
            raise BusinessRuleViolation(ARCHIVED_NOTE)
        if not may_change_note(actor, scope, note):
            raise PermissionDeniedError(EDIT_ONLY)
        live = Attachment.objects.filter(
            note_id=note.pk,
            deleted_at__isnull=True,
            state__in=[AttachmentState.UPLOADING, AttachmentState.STORED],
        ).count()
        if live >= settings.ATTACHMENT_MAX_PER_NOTE:
            raise BusinessRuleViolation(TOO_MANY.format(limit=settings.ATTACHMENT_MAX_PER_NOTE))
        now = timezone.now()
        return Attachment.objects.create(
            note=note,
            original_name=name,
            extension=extension,
            content_type=kind.content_type,
            size=received.size,
            sha256=received.sha256,
            storage_key=f"{now:%Y/%m}/{uuid.uuid4().hex}",
            state=AttachmentState.UPLOADING,
            uploaded_by=actor,
            created_at=now,
        )


def _finish(actor: User, scope: AccessScope, attachment: Attachment) -> Attachment:
    """Step 3: the object is stored; mark the row and audit it, under the row's lock. If
    housekeeping gave up on the upload meanwhile (more than an hour later), or the file was
    deleted meanwhile (the lead's erasure; purge leaves uploading rows alone), the object is
    removed again: the purge is queued in the same transaction and the error raised after it
    commits (enhancement review: an erasure during an upload kept the file)."""
    scanning = storage.scanning_enabled()
    problem: Exception | None = None
    with transaction.atomic():
        row = Attachment.objects.select_for_update().filter(pk=attachment.pk).first()
        now = timezone.now()
        if row is None or row.state != AttachmentState.UPLOADING:
            problem = StorageDown()
        elif row.deleted_at is not None:
            # Its bytes exist now: stored and deleted, which the purge job removes.
            row.state = AttachmentState.STORED
            row.stored_at = now
            row.save(update_fields=["state", "stored_at"])
            problem = NotFoundError()
        else:
            row.state = AttachmentState.STORED
            row.stored_at = now
            row.scan_status = ScanStatus.PENDING if scanning else ScanStatus.NOT_SCANNED
            row.save(update_fields=["state", "stored_at", "scan_status"])
            note = Activity.objects.only("id", "owner_id").get(pk=row.note_id)
            _audit(
                AUDIT_UPLOADED,
                actor.pk,
                row,
                note,
                workspace=scope.kind.value,
                extension=row.extension,
                size=_size_bucket(row.size),
            )
            if scanning:
                outbox.enqueue(TOPIC_SCAN, {"attachment_id": str(row.pk)})
        if problem is not None:
            outbox.enqueue(TOPIC_PURGE, {"attachment_id": str(attachment.pk)})
    if problem is not None:
        if isinstance(problem, StorageDown):
            # Given up on by housekeeping, which may already have purged the row while the
            # bytes were still being written: remove them here (best effort; idempotent).
            try:
                storage.delete(attachment.storage_key)
            except storage.StorageUnavailable:
                logger.warning("attachment_storage_failed", extra={"operation": "delete"})
        raise problem
    return attachment_by_id(attachment.pk)


# --- read -----------------------------------------------------------------------------------------
def _listed() -> QuerySet[Attachment]:
    return Attachment.objects.filter(
        deleted_at__isnull=True, state=AttachmentState.STORED
    ).select_related("uploaded_by")


def attachment_by_id(attachment_id: UUID) -> Attachment:
    return _listed().get(pk=attachment_id)


def for_notes(note_ids: list[UUID]) -> dict[UUID, list[Attachment]]:
    """The live files of notes the caller already found in its scope, oldest first: one
    query for any number of notes (no binary data, metadata only)."""
    found: dict[UUID, list[Attachment]] = {note_id: [] for note_id in note_ids}
    if note_ids:
        for attachment in _listed().filter(note_id__in=note_ids).order_by("created_at", "id"):
            found[attachment.note_id].append(attachment)
    return found


def downloadable(attachment: Attachment) -> bool:
    if attachment.scan_status == ScanStatus.CLEAN:
        return True
    return attachment.scan_status == ScanStatus.NOT_SCANNED and not storage.scanning_enabled()


@dataclass(frozen=True, slots=True)
class Download:
    attachment: Attachment
    file: IO[bytes]


def open_for_download(
    *, scope: AccessScope, attachment_id: UUID, preview: bool = False
) -> Download:
    """The file, if its note is visible in `scope` now (404 otherwise) and the scan policy
    allows it. Images only, for a preview."""
    attachment = _listed().filter(pk=attachment_id).first()
    if attachment is None:
        raise NotFoundError()
    _visible_note(scope, attachment.note_id)
    if preview and not storage.CATALOG[attachment.extension].previewable:
        raise BusinessRuleViolation(NOT_PREVIEWABLE)
    if attachment.scan_status == ScanStatus.REJECTED:
        raise BusinessRuleViolation(BLOCKED)
    if not downloadable(attachment):
        raise BusinessRuleViolation(SCANNING)
    try:
        return Download(attachment, storage.open_file(attachment.storage_key))
    except storage.StorageUnavailable:
        raise StorageDown() from None


# --- delete ---------------------------------------------------------------------------------------
def delete(*, actor: User, scope: AccessScope, attachment_id: UUID) -> None:
    """Hide the file at once and remove its object by a job. Deleting a deleted file is a
    no-op (a retry)."""
    from .services import lock_note

    authorize_write(actor, scope)
    found = (
        Attachment.objects.filter(pk=attachment_id)
        .exclude(state=AttachmentState.FAILED)
        .values_list("note_id", "deleted_at")
        .first()
    )
    if found is None:
        raise NotFoundError()
    note_id, _ = found
    _visible_note(scope, note_id)
    with transaction.atomic():
        note = lock_note(scope, note_id)
        if not may_change_note(actor, scope, note):
            raise PermissionDeniedError(EDIT_ONLY)
        attachment = Attachment.objects.select_for_update().get(pk=attachment_id)
        if attachment.deleted_at is not None:
            return
        attachment.deleted_at = timezone.now()
        attachment.deleted_by = actor
        attachment.save(update_fields=["deleted_at", "deleted_by"])
        _audit(AUDIT_DELETED, actor.pk, attachment, note, workspace=scope.kind.value)
        outbox.enqueue(TOPIC_PURGE, {"attachment_id": str(attachment.pk)})


# --- background work (outbox handlers, housekeeping, erasure) ----------------------------------
def purge(attachment_id: UUID) -> bool:
    """Remove the object of a deleted, failed or rejected file from storage (idempotent:
    a missing object counts as removed). StorageUnavailable propagates: the job retries."""
    attachment = Attachment.objects.filter(pk=attachment_id, purged_at__isnull=True).first()
    if attachment is None:
        return False
    gone = (
        attachment.deleted_at is not None
        or attachment.state == AttachmentState.FAILED
        or attachment.scan_status == ScanStatus.REJECTED
    )
    if not gone or attachment.state == AttachmentState.UPLOADING:
        # An upload still being written: its object may not exist yet. _finish (or
        # housekeeping, giving up on it) queues the purge again once it does.
        return False
    storage.delete(attachment.storage_key)
    Attachment.objects.filter(pk=attachment.pk, purged_at__isnull=True).update(
        purged_at=timezone.now()
    )
    return True


def scan(attachment_id: UUID) -> str | None:
    """Scan a pending file (the outbox job). Clean files become downloadable; infected ones
    are blocked and their object removed. ScannerUnavailable / StorageUnavailable propagate,
    so the job retries with backoff and finally goes dead (an alert)."""
    attachment = Attachment.objects.filter(
        pk=attachment_id,
        scan_status=ScanStatus.PENDING,
        state=AttachmentState.STORED,
        deleted_at__isnull=True,  # deleted meanwhile: nothing to scan (its object is going)
    ).first()
    if attachment is None:
        return None
    file = storage.open_file(attachment.storage_key)
    try:
        clean = storage.scan(file)
    finally:
        file.close()
    verdict = ScanStatus.CLEAN if clean else ScanStatus.REJECTED
    with transaction.atomic():
        updated = Attachment.objects.filter(
            pk=attachment.pk, scan_status=ScanStatus.PENDING
        ).update(scan_status=verdict)
        if updated and verdict == ScanStatus.REJECTED:
            note = Activity.objects.only("id", "owner_id").get(pk=attachment.note_id)
            _audit(AUDIT_REJECTED, None, attachment, note, extension=attachment.extension)
            outbox.enqueue(TOPIC_PURGE, {"attachment_id": str(attachment.pk)})
            logger.warning("attachment_malware_blocked", extra={"extension": attachment.extension})
    return verdict.value


def housekeeping(now: datetime) -> dict[str, int]:
    """Hourly, idempotent: give up on abandoned uploads, and remove the objects of files
    deleted, failed or blocked whose purge job didn't finish. Storage being down leaves the
    rest for the next run."""
    abandoned = Attachment.objects.filter(
        state=AttachmentState.UPLOADING, created_at__lt=now - ABANDONED_AFTER
    ).update(state=AttachmentState.FAILED)
    purged = failed = 0
    leftovers = Attachment.objects.filter(purged_at__isnull=True).filter(
        Q(deleted_at__isnull=False)
        | Q(state=AttachmentState.FAILED)
        | Q(scan_status=ScanStatus.REJECTED)
    )
    for attachment_id in leftovers.order_by("created_at").values_list("pk", flat=True)[:1000]:
        try:
            purged += purge(attachment_id)
        except storage.StorageUnavailable:
            failed += 1
            break
    queued, unscannable = _follow_scanner_setting()
    pending_scans = Attachment.objects.filter(
        scan_status=ScanStatus.PENDING, state=AttachmentState.STORED
    ).count()
    return {
        "abandoned": abandoned,
        "purged": purged,
        "storage_failures": failed,
        "queued_for_scanning": queued,
        "no_longer_scanned": unscannable,
        "pending_scans": pending_scans,
    }


def _follow_scanner_setting() -> tuple[int, int]:
    """Files keep working when the scanner setting changes (enhancement review): once a
    scanner is configured, files stored without one are queued for scanning (a batch per
    run; downloadable once clean); once it is removed, files still waiting for a scan become
    "not scanned" (downloadable, labelled), since nothing would ever scan them."""
    if not storage.scanning_enabled():
        unscannable = Attachment.objects.filter(scan_status=ScanStatus.PENDING).update(
            scan_status=ScanStatus.NOT_SCANNED
        )
        return 0, unscannable
    with transaction.atomic():
        backlog = list(
            Attachment.objects.select_for_update(skip_locked=True)
            .filter(
                scan_status=ScanStatus.NOT_SCANNED,
                state=AttachmentState.STORED,
                deleted_at__isnull=True,
            )
            .order_by("created_at")
            .values_list("pk", flat=True)[:SCAN_BACKLOG_BATCH]
        )
        Attachment.objects.filter(pk__in=backlog).update(scan_status=ScanStatus.PENDING)
        for attachment_id in backlog:
            outbox.enqueue(TOPIC_SCAN, {"attachment_id": str(attachment_id)})
    return len(backlog), 0


def erase_for_notes(note_ids: list[UUID], now: datetime) -> int:
    """Personal-data erasure (arkray.privacy): every file of these notes is deleted (its
    name blanked to a placeholder) and its object queued for removal. Returns how many."""
    targets = list(
        Attachment.objects.filter(note_id__in=note_ids, purged_at__isnull=True).values_list(
            "pk", flat=True
        )
    )
    # The name and the content's fingerprint go too (a hash identifies a known document).
    erased = {"original_name": "erased", "sha256": ERASED_SHA256}
    Attachment.objects.filter(pk__in=targets).update(**erased, deleted_at=now)
    Attachment.objects.filter(note_id__in=note_ids, purged_at__isnull=False).update(**erased)
    for attachment_id in targets:
        outbox.enqueue(TOPIC_PURGE, {"attachment_id": str(attachment_id)})
    return len(targets)
