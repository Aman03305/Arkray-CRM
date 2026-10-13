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

A row is "stored" only once storage.save() returned, which reads the object's size back
from the store. A stored file whose object is gone is answered truthfully: 410
`attachment_unavailable`, never the outage's 503 (the download path writes nothing; the
daily reconciliation flags the row, and `reconcile_attachments --repair` marks it failed).
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import IO, Any
from uuid import UUID

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import Q, QuerySet, Sum
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import holds, ledger, outbox
from arkray.core.access import AccessScope
from arkray.core.errors import (
    BusinessRuleViolation,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
)
from arkray.core.models import HoldSubject
from arkray.identity.models import User
from arkray.identity.workspaces import authorize_write

from . import selectors, storage, telemetry
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
# Every deleted file still waiting for its purge, in batches (`purge_deleted`).
TOPIC_PURGE_DELETED = "activities.purge_deleted_attachments"
AUDIT_UPLOADED = "attachment.uploaded"
AUDIT_DELETED = "attachment.deleted"
AUDIT_REJECTED = "attachment.rejected"
AUDIT_UNAVAILABLE = "attachment.marked_unavailable"
AUDIT_RECONCILED = "attachment.reconciled"

NOTES_ONLY = "Files can be attached to notes."
ARCHIVED_NOTE = "This note is archived. Restore it to change its files."
TOO_MANY = "A note can have at most {limit} files."
OVER_QUOTA = (
    "Your files can take up at most {limit} in all, and this one would go over it. Delete files"
    " you no longer need, or ask an administrator."
)
EDIT_ONLY = "Only the note's author (or an administrator) can change its files."
STORAGE_DOWN = "File storage is unavailable right now. Try again in a few minutes."
SCANNING = "This file is still being checked for viruses. Try again in a minute."
BLOCKED = "This file was blocked by the virus scan and can't be downloaded."
NOT_PREVIEWABLE = "Only images can be previewed."
GONE = "This file is no longer available. Ask an administrator."
# How long an upload may stay unfinished before housekeeping gives up on it.
ABANDONED_AFTER = timedelta(hours=1)
ERASED_SHA256 = "0" * 64
# What a file removed for good is called afterwards (privacy remediation P2-9): its name
# and its content's fingerprint go with its object; the row keeps what isn't personal (type,
# size bucket's size, who uploaded and deleted it, when) as the audit trail's evidence.
REMOVED_NAME = "removed"
AUDIT_METADATA_REMOVED = "attachment.metadata_removed"
# Files stored while no scanner was configured, queued for scanning per housekeeping run once
# one is (until then they are not downloadable: only clean files are, with a scanner).
SCAN_BACKLOG_BATCH = 500
# A purge takes the other deleted files waiting for theirs with it, recorded in one ledger
# entry (`purge`; security review M3): at most this many files in one transaction...
PURGE_BATCH = ledger.MAX_SUBJECTS
# ...and no further file once its storage calls have taken this long (each file's lead hold
# lock is held until the transaction commits, so a hold being placed on it waits that long).
PURGE_BATCH_BUDGET_S = 2.0
# A paged purge job (`purge_deleted`) runs this long at most, then queues its continuation
# (inside Celery's 100 s soft limit).
PURGE_JOB_BUDGET_S = 60.0
# A file whose object the store wouldn't delete is tried again after PURGE_RETRY_FIRST,
# doubling to PURGE_RETRY_MAX (`_purge_failed`, kept on its row); from PURGE_ALERT_AFTER
# failed attempts each further one logs at ERROR; a run stops after PURGE_FAILURES_IN_A_ROW
# different files failed in a row (the store is down, not one file).
PURGE_RETRY_FIRST = timedelta(minutes=5)
PURGE_RETRY_MAX = timedelta(hours=24)
PURGE_ALERT_AFTER = 6
PURGE_FAILURES_IN_A_ROW = 3


class FileTooLarge(DomainError):
    code = "file_too_large"
    http_status = 413
    default_message = "This file is too large."


class StorageDown(ServiceUnavailableError):
    """503 with Retry-After (the API views add the header)."""

    code = "storage_unavailable"
    default_message = STORAGE_DOWN
    retry_after = storage.STORAGE_RETRY_AFTER_S

    def __init__(self, message: str | None = None, *, error_class: str = "other") -> None:
        super().__init__(message)
        self.error_class = error_class  # for logs and metrics only


class NoteFull(BusinessRuleViolation):
    """The note already has ATTACHMENT_MAX_PER_NOTE files."""


class QuotaExceeded(BusinessRuleViolation):
    """The uploader's files would take more than ATTACHMENT_MAX_BYTES_PER_USER (deep security
    audit L-7)."""

    code = "attachment_quota_exceeded"


# Advisory-lock namespace ("ARKQ"): one person's uploads are checked against their quota one
# at a time (core.idempotency's and identity.throttling's two-key form).
_QUOTA_LOCK = 0x41524B51
# ("ARKP"): one purge of a file at a time (purge, a try-lock).
_PURGE_LOCK = 0x41524B50


class AttachmentUnavailable(DomainError):
    """The row says the file is stored, and the store says its object is gone: lost data
    to investigate (docs/runbooks.md#attachment-object-missing), not an outage to wait out."""

    code = "attachment_unavailable"
    http_status = 410
    default_message = GONE


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
        attachment = _upload(actor, scope, note_id, filename, stream, declared_length)
    except (NotFoundError, PermissionDeniedError):
        raise  # the note went away or out of reach meanwhile: not an upload outcome
    except Exception as exc:
        telemetry.upload_failed(_upload_failure(exc))
        raise
    telemetry.upload_stored()
    return attachment


def _upload_failure(exc: Exception) -> str:
    """The metric's reason for a failed upload (telemetry.UPLOAD_REASONS)."""
    if isinstance(exc, FileTooLarge):
        return "too_large"
    if isinstance(exc, InvalidInputError | BusinessRuleViolation):
        return "quota" if isinstance(exc, NoteFull | QuotaExceeded) else "rejected"
    if isinstance(exc, StorageDown):
        return "timeout" if exc.error_class == "timeout" else "storage_error"
    return "other"


def _upload(
    actor: User,
    scope: AccessScope,
    note_id: UUID,
    filename: str,
    stream: storage.Readable,
    declared_length: int | None,
) -> Attachment:
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
        except storage.StorageUnavailable as exc:
            # Never stored: the row can't be downloaded, and housekeeping removes whatever
            # part of the object may have been written.
            Attachment.objects.filter(pk=attachment.pk, state=AttachmentState.UPLOADING).update(
                state=AttachmentState.FAILED
            )
            logger.warning(
                "attachment_upload_failed",
                extra={"reason": "storage", "error_class": exc.error_class},
            )
            raise StorageDown(error_class=exc.error_class) from None
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
    """Step 2: under the uploader's quota lock, then the note's lock (lead first), check
    the quota, the note and its limit, write the row."""
    from .services import lock_note  # the activities lock order lives there

    with transaction.atomic():
        _charge_quota(actor, received.size)
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
            raise NoteFull(TOO_MANY.format(limit=settings.ATTACHMENT_MAX_PER_NOTE))
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


def _charge_quota(actor: User, size: int) -> None:
    """Refuse a file that would take the uploader past ATTACHMENT_MAX_BYTES_PER_USER (0: no
    quota; deep security audit L-7: without one, an account could fill the volume or the
    bucket at about 300 MB a minute). What counts is every file of theirs still in storage:
    stored, being uploaded, and deleted or failed until its object is purged. The uploader is
    who pays, also for a file added in someone else's workspace. Checked under a per-person
    advisory lock held until the row is written, so two uploads at once can't both fit in
    the same room. Taken first, before the lead and the note: only uploads take it, and each
    takes it before any other lock, so no cycle is possible, and the sum (one person's rows)
    is never computed while the lead's and the note's rows are locked."""
    limit = settings.ATTACHMENT_MAX_BYTES_PER_USER
    if limit <= 0:
        return
    digest = int.from_bytes(hashlib.sha256(str(actor.pk).encode()).digest()[:4], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_QUOTA_LOCK, digest])
    if stored_bytes(actor.pk) + size > limit:
        raise QuotaExceeded(OVER_QUOTA.format(limit=_human_size(limit)))


def stored_bytes(user_id: UUID) -> int:
    """How much storage a person's files take now (the quota's measure)."""
    total = Attachment.objects.filter(uploaded_by_id=user_id, purged_at__isnull=True).aggregate(
        total=Sum("size")
    )["total"]
    return int(total or 0)


def _human_size(size: int) -> str:
    """A limit as people read it: "2 GB", "500 MB" (binary units, as ATTACHMENT_MAX_BYTES is
    shown)."""
    for unit, label in ((1024**3, "GB"), (1024**2, "MB"), (1024, "KB")):
        if size >= unit:
            return f"{size / unit:.1f}".rstrip("0").rstrip(".") + f" {label}"
    return f"{size} bytes"


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
    except storage.ObjectMissing:
        _object_missing(attachment, "download")
        telemetry.download_failed("missing")
        raise AttachmentUnavailable() from None
    except storage.StorageUnavailable as exc:
        telemetry.download_failed("storage_unavailable")
        raise StorageDown(error_class=exc.error_class) from None


def _object_missing(attachment: Attachment, operation: str) -> None:
    """A stored file whose object the store says is gone: lost data, worth a security
    review (deleted outside the application? a bucket restored from an older backup?).
    Logged with the file's id only, never its key or name; counted for the metrics. Nothing
    is written here (the download path never writes): reconciliation flags the row."""
    telemetry.object_missing()
    logger.warning(
        "attachment_object_missing",
        extra={
            "attachment_id": str(attachment.pk),
            "operation": operation,
            "backend": storage.backend_name(),
            "security": True,
        },
    )


# --- delete ---------------------------------------------------------------------------------------
def delete(*, actor: User, scope: AccessScope, attachment_id: UUID) -> None:
    """Hide the file at once and remove its object by a job. Deleting a deleted file is a
    no-op (a retry). Refused (409 `legal_hold`) while a legal hold covers the note's lead."""
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
        # A held lead's files stay where they are, visible, until the hold is released
        # (core.holds; the purge's deferral also covers a file deleted before the hold).
        holds.refuse_if_held(HoldSubject.LEAD, note.lead_id)
        attachment.deleted_at = timezone.now()
        attachment.deleted_by = actor
        attachment.save(update_fields=["deleted_at", "deleted_by"])
        _audit(AUDIT_DELETED, actor.pk, attachment, note, workspace=scope.kind.value)
        outbox.enqueue(TOPIC_PURGE, {"attachment_id": str(attachment.pk)})


# --- background work (outbox handlers, housekeeping, erasure) ----------------------------------
def removed_for_good() -> Q:
    """Files whose name and fingerprint go with their object: deleted (by a person or an
    erasure), blocked by the scan, or an upload that never finished. Not a stored file that
    reconciliation found missing (state failed, but it had been stored): its hash is how a
    bucket restore proves the object it brings back (docs/runbooks.md#attachment-object-missing)."""
    return (
        Q(deleted_at__isnull=False)
        | Q(scan_status=ScanStatus.REJECTED)
        | Q(state=AttachmentState.FAILED, stored_at__isnull=True)
    )


def _held_notes() -> Q | None:
    """Notes whose lead is under a legal hold: their files are never removed meanwhile. Read
    under core.holds.barrier() by a job that acts on it (forget_purged), or only to sift
    (housekeeping: purge decides for each file under its lead's hold lock)."""
    leads = holds.held_leads()
    return Q(note__lead_id__in=leads) if leads else None  # every activity has its lead


def _purgeable(attachment: Attachment | None) -> bool:
    """Not purged yet, and deleted, failed or rejected. Not an upload still being written:
    its object may not exist yet. _finish (or housekeeping, giving up on it) queues the
    purge again once it does."""
    if attachment is None:
        return False
    gone = (
        attachment.deleted_at is not None
        or attachment.state == AttachmentState.FAILED
        or attachment.scan_status == ScanStatus.REJECTED
    )
    return gone and attachment.state != AttachmentState.UPLOADING


def _claim_purge(attachment_id: UUID) -> bool:
    """The file's purge claim until the transaction ends ("ARKP": never waits, so it can't
    be part of a cycle); False while another purge of it holds the claim."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(%s, hashtext(%s))",
            [_PURGE_LOCK, str(attachment_id)],
        )
        return bool(cursor.fetchone()[0])


def purge(attachment_id: UUID, *, replaying: bool = False, unless_held: bool = False) -> bool:
    """Remove the object of a deleted, failed or rejected file from storage (idempotent:
    a missing object counts as removed), then its name and content hash (REMOVED_NAME):
    nothing left identifies it. Deferred while a legal hold covers the note's lead.
    StorageUnavailable propagates: the job retries, and nothing is forgotten before the
    object is really gone. `replaying`: the erasure ledger re-applied after a restore
    (privacy.replay): already decided, so neither deferred nor recorded again; with
    `unless_held` (the automatic reconciliation, which leaves a held lead to an operator)
    a hold on the lead raises UnderLegalHold instead.

    Serialised with placing a hold on the note's lead (core.holds; R134): the lead's hold
    lock (SHARED) is held from the check until the object is gone and the row says so, so a
    hold placed at the same moment has either committed before the check (the file stays)
    or waits for the purge to finish; never placed while the object is being removed. Only
    a hold on that lead waits for it (the storage calls are each bounded by
    ATTACHMENT_STORAGE_DEADLINE_S). No row is locked meanwhile (review P3-4): an erasure
    rewriting the lead's files never waits for a purge's storage calls, and the purge's one
    row lock is its last write. One purge of a file at a time: a second one (an outbox
    retry, housekeeping) finds the file claimed and skips it, without waiting.

    **One ledger entry per batch** (security review M3: the ledger is write-once, and each
    deleted file appended an entry of its own, so deleting files grew it without limit): a
    purge of a file someone deleted (or an erasure did) takes the other deleted files
    waiting for their purge with it, oldest first, each claimed and checked under its lead's
    hold lock as above, at most PURGE_BATCH of them and none more once the storage calls
    have taken PURGE_BATCH_BUDGET_S; their rows are marked at the end (rows locked in id
    order, after every storage call) and one `attachment_deleted` entry names them all,
    appended last, so the entry and the rows commit together (a failed append rolls them
    back: the objects are gone, which the retry counts as removed). A storage failure after
    the first file ends the batch with what is done; the rest wait for their own job or the
    housekeeping. Files deleted together (an erasure, a burst of deletions) share an entry;
    the replay and the automatic reconciliation purge one file at a time."""
    return attachment_id in _purge(attachment_id, replaying=replaying, unless_held=unless_held)


def _purge(attachment_id: UUID, *, replaying: bool = False, unless_held: bool = False) -> set[UUID]:
    """`purge`, returning every file it purged (the batch's, `attachment_id` first)."""
    found = Attachment.objects.filter(pk=attachment_id, purged_at__isnull=True).first()
    if found is None or not _purgeable(found):  # the common case, without a lock: nothing to do
        return set()
    checked = not replaying or unless_held
    with transaction.atomic():
        if not _remove_object(found, checked=checked, unless_held=unless_held):
            return set()  # being purged right now, or kept under a hold
        removed = [attachment_id]
        if not replaying and found.deleted_at is not None:
            started = time.monotonic()
            for other in _waiting(exclude={attachment_id}, limit=PURGE_BATCH - 1):
                if len(removed) >= PURGE_BATCH or time.monotonic() - started > PURGE_BATCH_BUDGET_S:
                    break
                # Each in a savepoint of its own (review round 1, LEDGER-5): a lock timeout on
                # one file's hold lock rolled back the rows of the files whose objects were
                # already gone. Any failure ends the batch with what is done, recorded.
                try:
                    with transaction.atomic():
                        taken = _remove_object(other, checked=True, unless_held=False)
                except (storage.StorageUnavailable, DatabaseError):
                    break  # the rest wait for their own job (or the housekeeping)
                if taken:
                    removed.append(other.pk)
        purged = _mark_purged(removed)
        recorded = [pk for pk, deleted in purged.items() if deleted]
        if recorded and not replaying:
            # Files someone deleted (or an erasure did): a restored backup must not show
            # them again, names and all (core.ledger; privacy.replay deletes them again).
            # Last: the ledger's append lock after every other (core.holds rule 3).
            ledger.append("attachment_deleted", ledger.joined(recorded))
    return set(purged)


def _due(now: datetime | None = None) -> Q:
    """Not backing off after a failed delete (`_purge_failed`): never failed, or due again."""
    return Q(purge_next_at__isnull=True) | Q(purge_next_at__lte=now or timezone.now())


def _waiting(*, exclude: Collection[UUID], limit: int) -> list[Attachment]:
    """Files deleted (by a person or an erasure) whose purge hasn't run, oldest first, but
    those in `exclude` and those backing off after a failed delete (`_purge_failed`, in the
    query: any number of them never hide the rest; review round 3, LEDGER3-3): a sift
    (`_remove_object` decides each under its lead's hold lock), read from the
    housekeeping's partial index."""
    rows = (
        Attachment.objects.filter(purged_at__isnull=True, deleted_at__isnull=False)
        .filter(_due())
        .exclude(state=AttachmentState.UPLOADING)
    )
    held = _held_notes()
    if held is not None:
        rows = rows.exclude(held)
    return list(rows.exclude(pk__in=list(exclude)).order_by("created_at")[:limit])


def _purge_failed(attachment_id: UUID, exc: storage.StorageUnavailable) -> None:
    """A file whose object the store wouldn't delete (an outage, or for good: Object Lock on
    a version, access denied) is left for later, PURGE_RETRY_FIRST doubling to
    PURGE_RETRY_MAX, so it never holds up the others (review round 2, LEDGER2-5). The count
    and the next try are kept on its row, in their own short transaction (review round 3,
    LEDGER3-3: in the cache, an outage of it lost them, and the count never reached the
    alert). Logged by id and reason, at ERROR from PURGE_ALERT_AFTER attempts on."""
    with transaction.atomic():
        found = (
            Attachment.objects.select_for_update()
            .filter(pk=attachment_id)
            .values_list("purge_attempts", flat=True)
            .first()
        )
        attempts = (found or 0) + 1
        delay = min(PURGE_RETRY_FIRST * (1 << min(attempts - 1, 16)), PURGE_RETRY_MAX)
        Attachment.objects.filter(pk=attachment_id).update(
            purge_attempts=attempts, purge_next_at=timezone.now() + delay
        )
    logger.log(
        logging.ERROR if attempts >= PURGE_ALERT_AFTER else logging.WARNING,
        "attachment_purge_failed",
        extra={"attachment_id": str(attachment_id), "reason": exc.error_class, "attempt": attempts},
    )


def purge_deleted(budget_s: float | None = None) -> int:
    """Every file deleted by a person or an erasure whose purge hasn't run, a batch at a time
    (`purge`: one transaction and one ledger entry each), oldest first, until none is left
    or `budget_s` (PURGE_JOB_BUDGET_S) is spent; then it queues its own continuation. The
    job an erasure queues for its files (`erase_for_notes`). Returns how many it purged.
    A file the store won't delete is skipped and backs off (`_purge_failed`); the job
    retries (StorageUnavailable) only when nothing was purged or PURGE_FAILURES_IN_A_ROW
    failed in a row (the store is down, not one file: review round 2, LEDGER2-5)."""
    budget = PURGE_JOB_BUDGET_S if budget_s is None else budget_s
    started, done, in_a_row = time.monotonic(), 0, 0
    skipped: set[UUID] = set()  # being purged elsewhere, kept by a hold, or failing
    failure: storage.StorageUnavailable | None = None
    while time.monotonic() - started < budget:
        waiting = _waiting(exclude=skipped, limit=1)
        if not waiting:
            break
        first = waiting[0].pk
        try:
            purged = _purge(first)
        except storage.StorageUnavailable as exc:
            _purge_failed(first, exc)
            skipped.add(first)  # a different file each time: `_waiting` excludes it
            failure, in_a_row = exc, in_a_row + 1
            if in_a_row >= PURGE_FAILURES_IN_A_ROW:
                raise
            continue
        in_a_row = 0
        if first not in purged:
            skipped.add(first)
        done += len(purged)
    else:
        with transaction.atomic():
            outbox.enqueue(TOPIC_PURGE_DELETED, {}, dedupe_key=TOPIC_PURGE_DELETED)
    if failure is not None and not done:
        raise failure
    return done


@outbox.handler(TOPIC_PURGE_DELETED, queue="default")
def purge_deleted_job(payload: dict[str, Any]) -> None:
    purge_deleted()


def _remove_object(found: Attachment, *, checked: bool, unless_held: bool) -> bool:
    """One file's object removed from storage, inside the purge's transaction (`purge`):
    its claim, then its lead's hold lock (SHARED, until the transaction commits) when
    `checked`, then the row read again, then the storage calls. False if another purge has
    it, it was purged meanwhile, or a hold keeps it (UnderLegalHold with `unless_held`)."""
    if not _claim_purge(found.pk):
        return False  # being purged right now
    if checked:
        lead_id = Activity.objects.filter(pk=found.note_id).values_list("lead_id", flat=True)[0]
        try:
            holds.refuse_if_held(HoldSubject.LEAD, lead_id)
        except holds.UnderLegalHold:
            if unless_held:
                raise
            return False  # kept, object and all, until the hold is released (housekeeping)
    # Read again after the locks: purged meanwhile (by the purge that held the claim).
    attachment = Attachment.objects.filter(pk=found.pk, purged_at__isnull=True).first()
    if attachment is None or not _purgeable(attachment):
        return False
    storage.delete(attachment.storage_key)
    if settings.ATTACHMENT_S3_PURGE_VERSIONS:
        storage.delete_versions(attachment.storage_key)
    return True


def _mark_purged(attachment_ids: list[UUID]) -> dict[UUID, bool]:
    """The rows of files whose objects are gone, marked purged (and their names and hashes
    forgotten when they were removed for good), each locked only now, in id order (no row
    lock is held across a storage call; review P3-4). Returns them, each with whether
    someone (or an erasure) deleted it, as the rows say at that point."""
    if not attachment_ids:
        return {}
    locked = dict(
        Attachment.objects.select_for_update()
        .filter(pk__in=attachment_ids, purged_at__isnull=True)
        .order_by("pk")
        .values_list("pk", "deleted_at")
    )
    forget = set(
        Attachment.objects.filter(removed_for_good(), pk__in=list(locked)).values_list(
            "pk", flat=True
        )
    )
    now = timezone.now()
    Attachment.objects.filter(pk__in=list(forget)).update(
        purged_at=now, original_name=REMOVED_NAME, sha256=ERASED_SHA256
    )
    Attachment.objects.filter(pk__in=[pk for pk in locked if pk not in forget]).update(
        purged_at=now
    )
    return {pk: deleted_at is not None for pk, deleted_at in locked.items()}


def forget_purged(*, actor_id: UUID | None, dry_run: bool = True) -> int:
    """Files removed for good before names and hashes went with their objects (`manage.py
    forget_attachment_names`): their name and hash removed now, except under a legal hold.
    Audited (a count; never a name). Idempotent."""
    rows = (
        Attachment.objects.filter(purged_at__isnull=False)
        .filter(removed_for_good())
        .exclude(sha256=ERASED_SHA256)
    )
    if dry_run:
        held = _held_notes()
        return (rows if held is None else rows.exclude(held)).count()
    with transaction.atomic():
        # First: serialised with placing a hold (core.holds.barrier; R134). The holds read
        # here stay the holds until the names are gone: one being placed has committed
        # before, or waits.
        holds.barrier()
        held = _held_notes()
        if held is not None:
            rows = rows.exclude(held)
        count = rows.update(original_name=REMOVED_NAME, sha256=ERASED_SHA256)
        if count:
            audit.record(
                AUDIT_METADATA_REMOVED,
                actor_id=actor_id,
                target_type="attachment",
                metadata={"count": count},
            )
    return count


def scan(attachment_id: UUID) -> str | None:
    """Scan a pending file (the outbox job, on a worker). Clean files become downloadable;
    infected ones are blocked and their object removed. ScannerUnavailable /
    StorageUnavailable propagate, so the job retries with backoff and finally goes dead (an
    alert): the file stays "being checked" (never downloadable) meanwhile. Logged and
    audited by id, outcome and reason only: never the file's name or content."""
    attachment = Attachment.objects.filter(
        pk=attachment_id,
        scan_status=ScanStatus.PENDING,
        state=AttachmentState.STORED,
        deleted_at__isnull=True,  # deleted meanwhile: nothing to scan (its object is going)
    ).first()
    if attachment is None:
        return None
    try:
        file = storage.open_file(attachment.storage_key)
    except storage.ObjectMissing:
        # Retrying can't bring it back: the job ends, the file stays "being checked" (never
        # downloadable) and reconciliation reports it missing.
        _object_missing(attachment, "scan")
        return None
    started = time.monotonic()
    try:
        found = storage.scan(file, expected_sha256=attachment.sha256)
    except storage.ScannerUnavailable as exc:
        logger.warning(
            "attachment_scan_failed",
            extra={
                "attachment_id": str(attachment.pk),
                "reason": exc.reason,
                "duration_ms": round((time.monotonic() - started) * 1000),
                # bytes in storage that aren't the uploaded file: worth a security review
                **({"security": True} if exc.reason == "integrity" else {}),
            },
        )
        raise
    finally:
        file.close()
    verdict = ScanStatus.CLEAN if found.clean else ScanStatus.REJECTED
    with transaction.atomic():
        updated = Attachment.objects.filter(
            pk=attachment.pk, scan_status=ScanStatus.PENDING
        ).update(scan_status=verdict)
        if updated and verdict == ScanStatus.REJECTED:
            note = Activity.objects.only("id", "owner_id").get(pk=attachment.note_id)
            _audit(
                AUDIT_REJECTED,
                None,
                attachment,
                note,
                extension=attachment.extension,
                detected=found.signature,  # "signature" is a redacted key name
            )
            outbox.enqueue(TOPIC_PURGE, {"attachment_id": str(attachment.pk)})
    took = round((time.monotonic() - started) * 1000)
    if verdict == ScanStatus.REJECTED:
        logger.warning(
            "attachment_malware_blocked",
            extra={
                "attachment_id": str(attachment.pk),
                "outcome": verdict.value,
                "extension": attachment.extension,
                "duration_ms": took,
                "security": True,
            },
        )
    else:
        logger.info(
            "attachment_scanned",
            extra={
                "attachment_id": str(attachment.pk),
                "outcome": verdict.value,
                "extension": attachment.extension,
                "duration_ms": took,
            },
        )
    return verdict.value


def housekeeping(now: datetime) -> dict[str, int]:
    """Hourly, idempotent: give up on abandoned uploads, and remove the objects of files
    deleted, failed or blocked whose purge job didn't finish. Storage being down leaves the
    rest for the next run."""
    abandoned = Attachment.objects.filter(
        state=AttachmentState.UPLOADING, created_at__lt=now - ABANDONED_AFTER
    ).update(state=AttachmentState.FAILED)
    failed = 0
    leftovers = (
        Attachment.objects.filter(purged_at__isnull=True)
        .filter(
            Q(deleted_at__isnull=False)
            | Q(state=AttachmentState.FAILED)
            | Q(scan_status=ScanStatus.REJECTED)
        )
        .filter(_due(now))  # backing off after a failed delete: later (`_purge_failed`)
    )
    held = _held_notes()  # a sift: purge() decides for each file, under its lead's hold lock
    if held is not None:
        leftovers = leftovers.exclude(held)
    done: set[UUID] = set()
    candidates = list(leftovers.order_by("created_at").values_list("pk", flat=True)[:1000])
    in_a_row = 0
    for attachment_id in candidates:
        if attachment_id in done:
            continue  # purged with an earlier file's batch
        try:
            done |= _purge(attachment_id)
            in_a_row = 0
        except storage.StorageUnavailable as exc:
            # One file the store won't delete is left for later, the rest go on (review
            # round 2, LEDGER2-5); several in a row: the store is down, next run.
            _purge_failed(attachment_id, exc)
            failed, in_a_row = failed + 1, in_a_row + 1
            if in_a_row >= PURGE_FAILURES_IN_A_ROW:
                break
    purged = len(done)
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
    name blanked to a placeholder) and their objects queued for removal by one paged job
    (`purge_deleted`). Returns how many. The same few statements at any number of files
    (QUEUES-2: a job per file, a savepoint, an insert and a release each, made an erasure
    grow with its files: 66.8 s at 5,000, beyond the automatic replay's time limit at the
    activity budget's 50,000)."""
    # The name and the content's fingerprint go too (a hash identifies a known document).
    erased = {"original_name": "erased", "sha256": ERASED_SHA256}
    count = Attachment.objects.filter(note_id__in=note_ids, purged_at__isnull=True).update(
        **erased, deleted_at=now
    )
    Attachment.objects.filter(note_id__in=note_ids, purged_at__isnull=False).update(**erased)
    if count:
        outbox.enqueue(TOPIC_PURGE_DELETED, {}, dedupe_key=TOPIC_PURGE_DELETED)
    return count


# --- reconciliation repairs (reconcile.py) --------------------------------------------------------
# Each takes the row as reconciliation saw it and applies only if the row still says so, under
# a brief row lock taken with SKIP LOCKED (a row an upload or a purge holds is left for the
# next run). Storage is never called under the lock: what the store holds was checked
# before. All idempotent.
@dataclass(frozen=True, slots=True)
class Seen:
    state: str
    deleted_at: datetime | None
    purged_at: datetime | None


def _locked_as_seen(attachment_id: UUID, seen: Seen) -> Attachment | None:
    row = Attachment.objects.select_for_update(skip_locked=True).filter(pk=attachment_id).first()
    if row is None or Seen(row.state, row.deleted_at, row.purged_at) != seen:
        return None
    return row


def mark_unavailable(attachment_id: UUID, seen: Seen) -> bool:
    """A stored file whose object the store says is gone: failed, so it stops answering as
    a healthy file (it leaves the note's list; the row, its audit trail and its note stay).
    Marked purged too: there is nothing to remove, and a bucket restored later brings the
    object back for an administrator to look at, not for the housekeeping to delete."""
    with transaction.atomic():
        row = _locked_as_seen(attachment_id, seen)
        if row is None or row.state != AttachmentState.STORED or row.deleted_at is not None:
            return False
        row.state = AttachmentState.FAILED
        row.purged_at = timezone.now()
        row.save(update_fields=["state", "purged_at"])
        note = Activity.objects.only("id", "owner_id").get(pk=row.note_id)
        _audit(AUDIT_UNAVAILABLE, None, row, note, reason="object_missing")
    logger.warning(
        "attachment_marked_unavailable",
        extra={"attachment_id": str(attachment_id), "security": True},
    )
    return True


def was_marked_unavailable(attachment_id: UUID) -> bool:
    """Whether reconciliation once marked this file unavailable (its audit event, by target:
    an indexed lookup)."""
    from arkray.audit.models import AuditEvent

    return AuditEvent.objects.filter(
        target_type="attachment", target_id=str(attachment_id), action=AUDIT_UNAVAILABLE
    ).exists()


def fail_stale_upload(attachment_id: UUID, seen: Seen) -> bool:
    """An upload that never finished and whose object is absent or incomplete: failed
    (never downloadable); whatever was written is removed by the purge job."""
    with transaction.atomic():
        row = _locked_as_seen(attachment_id, seen)
        if row is None or row.state != AttachmentState.UPLOADING:
            return False
        row.state = AttachmentState.FAILED
        row.save(update_fields=["state"])
        outbox.enqueue(TOPIC_PURGE, {"attachment_id": str(row.pk)})
    return True


def finish_stale_upload(attachment_id: UUID, seen: Seen) -> bool:
    """An upload whose request died after its object was written whole (checked against
    the row's size, and its hash when asked): finished as _finish would have, recorded as
    done by the system."""
    scanning = storage.scanning_enabled()
    with transaction.atomic():
        row = _locked_as_seen(attachment_id, seen)
        if row is None or row.state != AttachmentState.UPLOADING:
            return False
        row.state = AttachmentState.STORED
        row.stored_at = timezone.now()
        if row.deleted_at is None:
            row.scan_status = ScanStatus.PENDING if scanning else ScanStatus.NOT_SCANNED
        row.save(update_fields=["state", "stored_at", "scan_status"])
        note = Activity.objects.only("id", "owner_id").get(pk=row.note_id)
        _audit(
            AUDIT_UPLOADED,
            None,
            row,
            note,
            extension=row.extension,
            size=_size_bucket(row.size),
            repaired=True,
        )
        if row.deleted_at is not None:
            outbox.enqueue(TOPIC_PURGE, {"attachment_id": str(row.pk)})
        elif scanning:
            outbox.enqueue(TOPIC_SCAN, {"attachment_id": str(row.pk)})
    return True


def purge_again(attachment_id: UUID, seen: Seen) -> bool:
    """The object of a deleted, failed or blocked file is still (or again: a late write, a
    bucket restore) in storage: removed through the purge path."""
    with transaction.atomic():
        row = _locked_as_seen(attachment_id, seen)
        if row is None or row.state == AttachmentState.UPLOADING:
            return False
        if row.purged_at is not None:
            row.purged_at = None
            row.save(update_fields=["purged_at"])
    return purge(attachment_id)
