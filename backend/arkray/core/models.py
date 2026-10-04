"""Base model building blocks and the transactional outbox table."""

from __future__ import annotations

import uuid
from typing import Any, NoReturn

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .errors import AppendOnlyViolation


class UUIDPrimaryKeyModel(models.Model):
    """Business entities use random UUIDs: safe to expose in URLs, not enumerable."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AppendOnlyQuerySet(models.QuerySet[Any]):
    """Blocks bulk mutation of append-only tables at the ORM level.

    This is the first line of defence; a PostgreSQL trigger (arkray.core.db) is the second
    and cannot be bypassed by application code.
    """

    def update(self, **kwargs: Any) -> NoReturn:
        raise AppendOnlyViolation(f"{self.model.__name__} records are append-only.")

    def delete(self) -> NoReturn:
        raise AppendOnlyViolation(f"{self.model.__name__} records are append-only.")

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AppendOnlyViolation(f"{self.model.__name__} records are append-only.")


class AppendOnlyModel(models.Model):
    """Audit and history records: insert once, never update, never delete."""

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyViolation(f"{type(self).__name__} records are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AppendOnlyViolation(f"{type(self).__name__} records are append-only.")


class OutboxStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    IN_FLIGHT = "in_flight", "In flight"
    DONE = "done", "Done"
    DEAD = "dead", "Dead (needs attention)"


class OutboxEvent(models.Model):
    """A unit of background work, written in the same transaction as the business change.

    Lifecycle: pending -> in_flight -> done | pending (retry with backoff) | dead.
    See arkray.core.outbox for the processing rules.
    """

    id = models.BigAutoField(primary_key=True)
    topic = models.CharField(max_length=100)
    queue = models.CharField(max_length=32)
    payload = models.JSONField(default=dict)
    # Best-effort coalescing: new work with the same key merges into a *never-attempted*
    # pending event (five quick edits of one lead -> one re-index). Deliberately not a unique
    # constraint: duplicates are harmless (handlers are idempotent) and a constraint would
    # block rows returning to pending on retry or lease recovery.
    dedupe_key = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=OutboxStatus.choices, default=OutboxStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=8)
    available_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    # Identifies one claim (dispatch). Every state transition must present the current token,
    # so stale or duplicated broker messages are no-ops.
    claim_token = models.UUIDField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_outbox_event"
        indexes = [
            models.Index(
                fields=["queue", "available_at"],
                condition=Q(status=OutboxStatus.PENDING),
                name="outbox_pending_due_idx",
            ),
            models.Index(
                fields=["queue", "locked_until"],
                condition=Q(status=OutboxStatus.IN_FLIGHT),
                name="outbox_in_flight_idx",
            ),
            # The metrics endpoint counts dead events per queue on every scrape (Phase 10).
            models.Index(
                fields=["queue"], condition=Q(status=OutboxStatus.DEAD), name="outbox_dead_idx"
            ),
            models.Index(
                fields=["topic", "dedupe_key"],
                condition=Q(status=OutboxStatus.PENDING, attempts=0) & ~Q(dedupe_key=""),
                name="outbox_coalesce_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=OutboxStatus.values), name="outbox_status_valid"
            ),
            models.CheckConstraint(
                condition=~Q(status=OutboxStatus.IN_FLIGHT) | Q(locked_until__isnull=False),
                name="outbox_in_flight_has_lease",
            ),
            models.CheckConstraint(
                condition=~Q(status=OutboxStatus.IN_FLIGHT) | Q(claim_token__isnull=False),
                name="outbox_in_flight_has_claim",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status__in=[OutboxStatus.DONE, OutboxStatus.DEAD], finished_at__isnull=False)
                    | Q(status__in=[OutboxStatus.PENDING, OutboxStatus.IN_FLIGHT], finished_at=None)
                ),
                name="outbox_finished_at_matches_status",
            ),
        ]

    def __str__(self) -> str:
        return f"OutboxEvent({self.pk}, {self.topic}, {self.status})"


class IdempotencyRecord(models.Model):
    """The outcome of a create request sent with an `Idempotency-Key` header.

    A retry of the same request (after a timeout, a double submit, a flaky connection)
    returns the record created the first time instead of a duplicate. Only identifiers and a
    digest of the request are stored, never the request body or the response. Keys are
    per actor and per operation, so one user's key can never replay another user's result.
    Records expire after 24 hours (arkray.core.idempotency).
    """

    id = models.BigAutoField(primary_key=True)
    actor_id = models.UUIDField()
    operation = models.CharField(max_length=64)
    key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    resource_id = models.UUIDField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "core_idempotency_record"
        constraints = [
            # Also serves the per-actor purge of expired records (leading actor_id).
            models.UniqueConstraint(
                fields=["actor_id", "operation", "key"], name="core_idempotency_unique_key"
            ),
            models.CheckConstraint(
                condition=Q(request_hash__regex=r"^[0-9a-f]{64}$"),
                name="core_idempotency_hash_format",
            ),
        ]

    def __str__(self) -> str:
        return f"IdempotencyRecord({self.operation}, {self.key})"
