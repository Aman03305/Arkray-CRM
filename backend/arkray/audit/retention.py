"""Retention of the audit trail's personal details (docs/privacy.md#audit-trail).

The audit trail is append-only and kept: who did what to which record, and when. The
personal details some events need for a while (a security event's client address, an old
and new sign-in email, a reset requester's address, a support session's reason) live in
AuditDetail, never in the event row, and are deleted AUDIT_DETAIL_RETENTION_DAYS after the
event (90 by default: a starting policy, configurable, not a legal duration):

- `expire_details()` (the hourly `audit.housekeeping`): deletes expired details in batches,
  except those of events whose actor, subject or target is under a legal hold; idempotent,
  and records one `audit.details_expired` event per run that removed any (counts only);
- `verify()`: whether a detail still matches the digest its event sealed;
- `minimise_legacy()` (`manage.py audit_minimise_legacy`): events written before this
  existed kept these details in the append-only row itself. The schema owner moves them into
  expiring details once, in one transaction, with the trigger disabled for each statement
  only, and records `audit.legacy_minimised` with a digest of every value it moved: a
  documented, authorised and audited operation, never an application path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from arkray.core import holds

from . import services
from .models import AuditDetail, AuditEvent

BATCH = 5000
MAX_BATCHES = 100  # per run: at most 500,000 details an hour
AUDIT_EXPIRED = "audit.details_expired"
AUDIT_LEGACY_MINIMISED = "audit.legacy_minimised"
# What events written before AuditDetail kept in their metadata, by action.
LEGACY_SENSITIVE_KEYS: dict[str, tuple[str, ...]] = {
    "user.email_changed": ("from", "to"),
    "auth.password_reset_issued": ("requested_from",),
    "auth.password_reset_suppressed": ("requested_from",),
    "support_session.started": ("reason",),
}


def _held() -> Q | None:
    """Details of events covered by an active legal hold (their actor, subject or target);
    None when nothing is held."""
    held = holds.active()
    users, leads = held["user"], held["lead"]
    if not (users or leads):
        return None
    condition = Q(pk__in=[])
    if users:
        condition |= (
            Q(event__actor_id__in=users)
            | Q(event__subject_user_id__in=users)
            | Q(event__target_type="user", event__target_id__in=[str(u) for u in users])
        )
    if leads:
        condition |= Q(
            event__target_type="lead", event__target_id__in=[str(lead) for lead in leads]
        )
    return condition


def expire_details(now: datetime | None = None) -> dict[str, int]:
    now = now or timezone.now()
    expired = AuditDetail.objects.filter(expires_at__lte=now)
    held = _held()
    due = expired if held is None else expired.exclude(held)
    removed = 0
    for _ in range(MAX_BATCHES):
        with transaction.atomic():
            batch = list(due.order_by("expires_at").values_list("pk", flat=True)[:BATCH])
            if not batch:
                break
            removed += AuditDetail.objects.filter(pk__in=batch).delete()[0]
    kept = 0 if held is None else expired.filter(held).count()
    if removed:
        services.record(
            AUDIT_EXPIRED,
            actor_id=None,
            target_type="audit",
            metadata={
                "count": removed,
                "held": kept,
                "retention_days": settings.AUDIT_DETAIL_RETENTION_DAYS,
                "before": now.isoformat(),
            },
        )
    return {"details_expired": removed, "held": kept}


def verify(event: AuditEvent) -> bool | None:
    """True: the event's detail matches its seal; False: it was altered; None: there is
    no detail (expired, or none was written)."""
    detail = AuditDetail.objects.filter(pk=event.pk).first()
    if detail is None:
        return None
    return services.seal(detail.ip_address, detail.values, detail.salt) == event.detail_digest


# --- events written before AuditDetail ---------------------------------------------------------
class OwnerRequired(Exception):
    """Only the schema owner may move values out of the append-only table."""


@dataclass(frozen=True, slots=True)
class LegacyReport:
    events: int
    addresses: int
    values: int
    digest: str


def _legacy() -> Any:
    sensitive = Q()
    for action, keys in LEGACY_SENSITIVE_KEYS.items():
        for key in keys:
            sensitive |= Q(action=action, metadata__has_key=key)
    return (
        AuditEvent.objects.filter(Q(ip_address__isnull=False) | sensitive)
        .filter(detail__isnull=True)
        .order_by("pk")
    )


def _owns_audit_table() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_has_role(current_user, relowner, 'MEMBER') FROM pg_class"
            " WHERE oid = 'audit_event'::regclass"
        )
        return bool(cursor.fetchone()[0])


def _plan(
    events: list[AuditEvent],
) -> tuple[list[tuple[AuditEvent, dict[str, Any], str | None]], str]:
    planned = []
    before = hashlib.sha256()
    for event in events:
        keys = LEGACY_SENSITIVE_KEYS.get(event.action, ())
        values = {k: event.metadata[k] for k in keys if k in (event.metadata or {})}
        # The address only where new events keep it (security events); elsewhere it goes.
        ip = event.ip_address if event.action.startswith(services.IP_ACTIONS) else None
        before.update(
            json.dumps([event.pk, event.ip_address, values], sort_keys=True, default=str).encode()
        )
        planned.append((event, values, ip))
    return planned, before.hexdigest()


def preview_legacy() -> LegacyReport:
    events = list(_legacy())
    planned, digest = _plan(events)
    return LegacyReport(
        events=len(planned),
        addresses=sum(1 for event, _, _ in planned if event.ip_address),
        values=sum(len(values) for _, values, _ in planned),
        digest=digest,
    )


@transaction.atomic
def minimise_legacy(*, operator_id: UUID) -> LegacyReport:
    """Move legacy details into expiring AuditDetails (module docstring). Idempotent: a
    second run finds nothing left to move."""
    if not _owns_audit_table():
        raise OwnerRequired(
            "Moving values out of the append-only audit trail needs the schema owner's"
            " database credentials (the migrate job's)."
        )
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '600s'")
    events = list(_legacy().select_for_update(of=("self",)))
    planned, digest = _plan(events)
    retention = timedelta(days=settings.AUDIT_DETAIL_RETENTION_DAYS)
    rows = []
    details = []
    for event, values, ip in planned:
        salt = secrets.token_hex(16)
        details.append(
            AuditDetail(
                event=event,
                ip_address=ip,
                values=values,
                salt=salt,
                expires_at=event.occurred_at + retention,
            )
        )
        rows.append((event.pk, list(values), services.seal(ip, values, salt)))
    if rows:
        AuditDetail.objects.bulk_create(details)
        with connection.cursor() as cursor:
            # Deferred constraint checks would block ALTER TABLE: run them now.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("ALTER TABLE audit_event DISABLE TRIGGER audit_event_append_only")
            cursor.executemany(
                "UPDATE audit_event SET ip_address = NULL,"
                " metadata = metadata - %s::text[], detail_digest = %s WHERE id = %s",
                [(keys, seal, pk) for pk, keys, seal in rows],
            )
            cursor.execute("ALTER TABLE audit_event ENABLE TRIGGER audit_event_append_only")
    report = LegacyReport(
        events=len(planned),
        addresses=sum(1 for event, _, _ in planned if event.ip_address),
        values=sum(len(values) for _, values, _ in planned),
        digest=digest,
    )
    services.record(
        AUDIT_LEGACY_MINIMISED,
        actor_id=operator_id,
        target_type="audit",
        metadata={
            "events": report.events,
            "addresses": report.addresses,
            "values": report.values,
            # SHA-256 of every (id, address, values) moved: whoever holds an older backup can
            # check exactly what this changed; it reveals nothing itself.
            "before_digest": report.digest,
            "first_id": planned[0][0].pk if planned else None,
            "last_id": planned[-1][0].pk if planned else None,
        },
    )
    return report
