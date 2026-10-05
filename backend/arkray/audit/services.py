"""Recording audit events.

Call `record()` inside the same transaction as the change it describes, so the audit
trail and the data can never disagree. Metadata is for *safe* context (IDs, old/new status
values); secrets are redacted defensively and oversized payloads are truncated.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from arkray.core.context import current_support_session_id, get_context
from arkray.core.text import without_refused

from .models import ActorType, AuditEvent

MAX_METADATA_BYTES = 4 * 1024
MAX_KEY_LENGTH = 64
MAX_DEPTH = 6
_SECRET_KEY_PATTERN = re.compile(
    r"pass(word)?|pwd|secret|token|api[_-]?key|private[_-]?key|authori[sz]ation|^auth$|bearer"
    r"|cookie|session|credential|otp|dsn|signature",
    re.I,
)
# "password=hunter2" or "token: abc" inside a value under a harmless key.
_SECRET_IN_VALUE = re.compile(
    r"(pass(?:word)?|pwd|secret|token|api[_-]?key|bearer)(\s*[=:]\s*|\s+)\S+", re.I
)
REDACTED = "[REDACTED]"


def sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Redact secret-looking keys and values (recursively), drop what PostgreSQL's JSON or a
    log viewer can't take safely (non-finite numbers, NUL, control and bidi characters),
    bound key length and depth, and cap the serialised size (Phase 9 review)."""

    def text(value: str) -> str:
        return _SECRET_IN_VALUE.sub(lambda m: f"{m.group(1)}={REDACTED}", without_refused(value))

    def scrub(value: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            return "[nested too deeply]"
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = text(str(raw_key))[:MAX_KEY_LENGTH]
                secret = _SECRET_KEY_PATTERN.search(key)
                cleaned[key] = REDACTED if secret else scrub(item, depth + 1)
            return cleaned
        if isinstance(value, list | tuple):
            return [scrub(v, depth + 1) for v in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if value is None or isinstance(value, bool | int | float):
            return value
        return text(str(value))

    cleaned = scrub(metadata or {}, 0)
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
    # The support session the event belongs to, when the caller knows it better than the
    # request context (starting and ending one); otherwise the context's.
    support_session_id: UUID | None = None


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
        support_session_id=entry.support_session_id or current_support_session_id(),
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
    support_session_id: UUID | None = None,
) -> AuditEvent:
    """Append an audit event. `actor_id=None` means the system performed the action."""
    event = _event(
        Entry(
            action, actor_id, target_type, target_id, subject_user_id, metadata, support_session_id
        )
    )
    event.save()
    return event


def count(action: str) -> int:
    """How many events of one kind have been recorded (and committed, as the caller's
    transaction sees them). An index-only count (`audit_action_idx`)."""
    return AuditEvent.objects.filter(action=action).count()
