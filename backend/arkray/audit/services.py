"""Recording audit events.

Call `record()` inside the same transaction as the change it describes, so the audit
trail and the data can never disagree. Metadata is for *safe* context (IDs, old/new status
values); secrets are redacted defensively and oversized payloads are truncated.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from arkray.core.context import get_context

from .models import ActorType, AuditEvent

MAX_METADATA_BYTES = 4 * 1024
_SECRET_KEY_PATTERN = re.compile(
    r"pass(word)?|secret|token|api[_-]?key|authorization|cookie|session|credential", re.I
)
REDACTED = "[REDACTED]"


def sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Redact secret-looking keys (recursively) and cap the serialised size."""

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(k): REDACTED if _SECRET_KEY_PATTERN.search(str(k)) else scrub(v)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [scrub(v) for v in value]
        if value is None or isinstance(value, bool | int | float | str):
            return value
        return str(value)

    cleaned = scrub(metadata or {})
    if len(json.dumps(cleaned, default=str).encode()) > MAX_METADATA_BYTES:
        return {"_truncated": True, "keys": sorted(cleaned)[:50]}
    return cleaned  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class Entry:
    """One audit event for record_many()."""

    action: str
    actor_id: UUID | None
    target_type: str = ""
    target_id: str | UUID = ""
    subject_user_id: UUID | None = None
    metadata: dict[str, Any] | None = None


def _event(entry: Entry) -> AuditEvent:
    context = get_context()
    return AuditEvent(
        actor_type=ActorType.USER if entry.actor_id else ActorType.SYSTEM,
        actor_id=entry.actor_id,
        action=entry.action,
        target_type=entry.target_type,
        target_id=str(entry.target_id),
        subject_user_id=entry.subject_user_id,
        request_id=context.correlation_id[:64] if context else "",
        ip_address=context.client_ip if context else None,
        metadata=sanitize_metadata(entry.metadata),
    )


def record_many(entries: Sequence[Entry]) -> list[AuditEvent]:
    """Append several audit events in one INSERT (e.g. every opportunity a lead
    reassignment moved), sanitised and with the request context, exactly like record()."""
    return AuditEvent.objects.bulk_create([_event(e) for e in entries]) if entries else []


def record(
    action: str,
    *,
    actor_id: UUID | None,
    target_type: str = "",
    target_id: str | UUID = "",
    subject_user_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event. `actor_id=None` means the system performed the action."""
    event = _event(Entry(action, actor_id, target_type, target_id, subject_user_id, metadata))
    event.save()
    return event
