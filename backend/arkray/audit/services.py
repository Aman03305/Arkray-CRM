"""Recording audit events.

Call `record()` inside the same transaction as the change it describes, so the audit
trail and the data can never disagree. Metadata is for *safe* context (IDs, old/new status
values); secrets are redacted defensively and oversized payloads are truncated.

Personal details that matter only for a while go in `sensitive` instead (an old and new
email, a reset requester's address, a support session's reason), and the client address is
kept only for security events (`IP_ACTIONS`): both are stored in the event's AuditDetail,
which expires after AUDIT_DETAIL_RETENTION_DAYS (audit.retention), sealed by the event's
`detail_digest` (privacy remediation P2-4, docs/privacy.md#audit-trail).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.utils import timezone

from arkray.core.context import current_support_session_id, get_context
from arkray.core.text import without_refused

from .models import ActorType, AuditDetail, AuditEvent

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
# Events whose client address is kept (in the expiring detail): sign-in and account
# security, support sessions, workspace access, privacy operations. Not every CRM edit: the
# request id ties those to the access log while the log is kept.
IP_ACTIONS = (
    "auth.",
    "user.",
    "support_session.",
    "workspace.",
    "privacy.",
    "lead.erased",
    "audit.",
)


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
    # Expiring personal details (module docstring).
    sensitive: dict[str, Any] | None = None


def seal(ip_address: str | None, values: dict[str, Any], salt: str) -> str:
    """The digest an event keeps of its detail (AuditEvent docstring)."""
    canonical = json.dumps(
        {"ip": ip_address, "values": values}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(f"{salt}:{canonical}".encode()).hexdigest()


def _detail(entry: Entry) -> AuditDetail | None:
    context = get_context()
    ip = context.client_ip if context and entry.action.startswith(IP_ACTIONS) else None
    values = sanitize_metadata(entry.sensitive) if entry.sensitive else {}
    if ip is None and not values:
        return None
    return AuditDetail(
        ip_address=ip,
        values=values,
        salt=secrets.token_hex(16),
        expires_at=timezone.now() + timedelta(days=settings.AUDIT_DETAIL_RETENTION_DAYS),
    )


def _event(entry: Entry, detail: AuditDetail | None) -> AuditEvent:
    context = get_context()
    return AuditEvent(
        actor_type=ActorType.USER if entry.actor_id else ActorType.SYSTEM,
        actor_id=entry.actor_id,
        action=entry.action,
        target_type=entry.target_type,
        target_id=str(entry.target_id),
        subject_user_id=entry.subject_user_id,
        request_id=context.correlation_id[:64] if context else "",
        metadata=sanitize_metadata(entry.metadata),
        support_session_id=entry.support_session_id or current_support_session_id(),
        detail_digest=seal(detail.ip_address, detail.values, detail.salt) if detail else "",
    )


def record_many(entries: Sequence[Entry]) -> list[AuditEvent]:
    """Append several audit events in one INSERT (e.g. every opportunity a lead
    reassignment moved), sanitised and with the request context, exactly like record()."""
    if not entries:
        return []
    details = [_detail(entry) for entry in entries]
    events = AuditEvent.objects.bulk_create(
        [_event(entry, detail) for entry, detail in zip(entries, details, strict=True)]
    )
    kept = []
    for event, detail in zip(events, details, strict=True):
        if detail is not None:
            detail.event = event
            kept.append(detail)
    if kept:
        AuditDetail.objects.bulk_create(kept)
    return events


def record(
    action: str,
    *,
    actor_id: UUID | None,
    target_type: str = "",
    target_id: str | UUID = "",
    subject_user_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
    support_session_id: UUID | None = None,
    sensitive: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event. `actor_id=None` means the system performed the action.
    `sensitive`: personal details kept only for the detail retention (module docstring)."""
    entry = Entry(
        action,
        actor_id,
        target_type,
        target_id,
        subject_user_id,
        metadata,
        support_session_id,
        sensitive,
    )
    detail = _detail(entry)
    event = _event(entry, detail)
    event.save()
    if detail is not None:
        detail.event = event
        detail.save()
    return event


def count(action: str) -> int:
    """How many events of one kind have been recorded (and committed, as the caller's
    transaction sees them). An index-only count (`audit_action_idx`)."""
    return AuditEvent.objects.filter(action=action).count()
