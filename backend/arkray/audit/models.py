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
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

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
