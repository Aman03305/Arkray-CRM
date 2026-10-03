"""Idempotent creates: replaying a request that carries the same `Idempotency-Key`.

Flow for a create endpoint (see arkray.leads.services.create_lead):

    1. replayed_resource(): a record for (actor, operation, key)?  -> return that resource
       (same request digest) or refuse (a different request reused the key: 422).
    2. In the creating transaction, remember() inserts the record. Two concurrent requests
       with one key serialise on the unique index: the loser's transaction rolls back
       entirely (IntegrityError, see is_duplicate_key) and it replays the winner's result.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import IntegrityError
from django.utils import timezone

from .errors import BusinessRuleViolation
from .models import IdempotencyRecord

RETENTION = timedelta(hours=24)
_UNIQUE_CONSTRAINT = "core_idempotency_unique_key"


class IdempotencyKeyReused(BusinessRuleViolation):
    code = "idempotency_key_reused"
    default_message = (
        "This request key was already used for a different request. Reload the page and try again."
    )


def request_digest(*parts: Any) -> str:
    """A stable digest of the validated request (never stored in readable form)."""
    canonical = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def replayed_resource(actor_id: UUID, operation: str, key: UUID, digest: str) -> UUID | None:
    record = (
        IdempotencyRecord.objects.filter(
            actor_id=actor_id,
            operation=operation,
            key=key,
            created_at__gte=timezone.now() - RETENTION,
        )
        .only("request_hash", "resource_id")
        .first()
    )
    if record is None:
        return None
    if record.request_hash != digest:
        raise IdempotencyKeyReused()
    return record.resource_id


def remember(actor_id: UUID, operation: str, key: UUID, digest: str, resource_id: UUID) -> None:
    """Record the created resource. Call inside the creating transaction."""
    IdempotencyRecord.objects.filter(
        actor_id=actor_id, created_at__lt=timezone.now() - RETENTION
    ).delete()  # lazy, bounded per-actor housekeeping
    IdempotencyRecord.objects.create(
        actor_id=actor_id,
        operation=operation,
        key=key,
        request_hash=digest,
        resource_id=resource_id,
    )


def purge_expired() -> int:
    """Housekeeping (hourly): forget records past the retention, for every actor."""
    deleted, _ = IdempotencyRecord.objects.filter(
        created_at__lt=timezone.now() - RETENTION
    ).delete()
    return deleted


def is_duplicate_key(error: IntegrityError) -> bool:
    diag = getattr(error.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == _UNIQUE_CONSTRAINT
