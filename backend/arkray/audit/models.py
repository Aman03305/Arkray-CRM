from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone

from arkray.core.models import AppendOnlyModel


class ActorType(models.TextChoices):
    USER = "user", "User"
    SYSTEM = "system", "System"


class AuditEvent(AppendOnlyModel):
    """One security- or business-sensitive action. Never updated, never deleted.

    `subject_user_id` records whose CRM workspace the action concerned when it differs from
    the actor (e.g. an admin viewing or editing a sales user's records).

    Personal details that only matter for a while (the client address of a security event,
    an old and new sign-in email, a password-reset requester's address, a support session's
    reason) are not stored here but in its AuditDetail, which expires (privacy remediation
    P2-4, docs/privacy.md#audit-trail). `detail_digest` seals them: the SHA-256 of the
    detail's values and a random salt kept with them, so a detail altered while it exists no
    longer matches, and once it has expired (salt and values gone together) the digest
    can't be matched against guessed values.
    """

    id = models.BigAutoField(primary_key=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    actor_id = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=50, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    subject_user_id = models.UUIDField(null=True, blank=True)
    # The administrator's support session (identity.SupportSession) the action was taken in,
    # if any: the actor is still the administrator, the subject the user. A column, not
    # metadata (whose keys mentioning a session are redacted).
    support_session_id = models.UUIDField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True, default="")
    # Legacy: events written before AuditDetail held the client address here (moved by the
    # owner's `audit_minimise_legacy`). New events never set it.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    # db_default: the previous release (which never names the column) can still insert.
    detail_digest = models.CharField(max_length=64, blank=True, default="", db_default="")

    class Meta:
        db_table = "audit_event"
        indexes = [
            models.Index(fields=["-occurred_at"], name="audit_occurred_idx"),
            models.Index(fields=["actor_id", "-occurred_at"], name="audit_actor_idx"),
            models.Index(fields=["subject_user_id", "-occurred_at"], name="audit_subject_idx"),
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
            models.Index(fields=["action", "-occurred_at"], name="audit_action_idx"),
            # What an administrator did in one support session.
            models.Index(
                fields=["support_session_id", "occurred_at"],
                name="audit_support_idx",
                condition=Q(support_session_id__isnull=False),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(actor_type__in=ActorType.values), name="audit_actor_type_valid"
            ),
            models.CheckConstraint(
                condition=Q(actor_type=ActorType.SYSTEM) | Q(actor_id__isnull=False),
                name="audit_user_actor_has_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.occurred_at:%Y-%m-%d %H:%M:%S} {self.action} by {self.actor_id or 'system'}"


class AuditDetail(models.Model):
    """An audit event's expiring personal details (AuditEvent docstring): kept for
    AUDIT_DETAIL_RETENTION_DAYS (90 by default, a starting policy, not a legal duration),
    then deleted by the daily `audit.housekeeping`, except while a legal hold covers the
    event's actor, subject or target. The event itself (who, what, when, which record)
    stays."""

    event = models.OneToOneField(
        AuditEvent, primary_key=True, on_delete=models.CASCADE, related_name="detail"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    values = models.JSONField(default=dict, blank=True)
    salt = models.CharField(max_length=32)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "audit_event_detail"
        indexes = [models.Index(fields=["expires_at"], name="audit_detail_expires_idx")]

    def __str__(self) -> str:
        return f"AuditDetail({self.event_id})"  # never the values: this can reach logs
