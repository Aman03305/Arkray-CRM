"""Data-subject export requests (privacy.exports; docs/privacy.md#access-requests)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone

from arkray.core.models import UUIDPrimaryKeyModel
from arkray.identity.models import User


class SubjectType(models.TextChoices):
    LEAD = "lead", "Customer (lead)"
    USER = "user", "Staff member (user)"


class ExportStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    READY = "ready", "Ready to download"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"


class DataExport(UUIDPrimaryKeyModel):
    """One export of a person's data, requested by an administrator after verifying the
    person's identity (`reference`: the request's ticket). Built by a job into private
    storage (STORAGES["exports"]), downloadable only by the administrator who asked, until
    `expires_at`; then the file is deleted and the row kept (who exported whose data, when)."""

    subject_type = models.CharField(max_length=8, choices=SubjectType.choices)
    subject_id = models.UUIDField()
    requested_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="+", db_index=False
    )
    reference = models.CharField(max_length=64)
    status = models.CharField(
        max_length=8, choices=ExportStatus.choices, default=ExportStatus.QUEUED
    )
    created_at = models.DateTimeField(default=timezone.now)
    ready_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    storage_key = models.CharField(max_length=200, blank=True, default="")
    size = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    files_included = models.PositiveIntegerField(default=0)
    files_omitted = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=32, blank=True, default="")
    downloads = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "privacy_data_export"
        indexes = [
            models.Index(
                fields=["requested_by", "-created_at"], name="privacy_export_requester_idx"
            ),
            models.Index(
                fields=["expires_at"],
                name="privacy_export_expiry_idx",
                condition=Q(status="ready"),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(subject_type__in=SubjectType.values), name="privacy_export_subject"
            ),
            models.CheckConstraint(
                condition=Q(status__in=ExportStatus.values), name="privacy_export_status"
            ),
            models.CheckConstraint(
                condition=~Q(reference=""), name="privacy_export_reference_present"
            ),
            models.CheckConstraint(
                condition=~Q(status="ready") | (Q(expires_at__isnull=False) & ~Q(storage_key="")),
                name="privacy_export_ready_complete",
            ),
        ]

    def __str__(self) -> str:
        return f"DataExport({self.pk})"
