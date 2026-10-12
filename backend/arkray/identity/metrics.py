"""Identity gauges for the metrics endpoint (ADR-0033, R126; docs/observability.md#metrics).

- `arkray_administrator_deactivations_last_hour`: administrators deactivated in the last hour
  (the `user.deactivated` audit events that record an administrator's role). Raises
  ArkrayAdministratorDeactivated: one administrator can deactivate the others (only the last
  active one is protected), so each time is worth a look.
- `arkray_mfa_lockouts_last_hour`: second factors locked by too many wrong codes in a row in
  the last hour (`mfa.locked` audit events; deep security audit M-4). Raises
  ArkrayMfaLocked: codes are asked for only after the right password, so a lockout means
  someone else very probably has that user's password.
- `arkray_mfa_locked_until_reset_last_hour`: those of them past the cap a new password
  doesn't clear (`mfa.locked` with `until: mfa_reset`; R133). Raises
  ArkrayMfaLockedUntilReset: it took the password and new ones chosen through the emailed
  link (the mailbox, very probably), and only an MFA reset unlocks it.

All from PostgreSQL at scrape time, in one count over the `audit_action_idx` index: a
database-wide figure every pod reports alike. No cache read (a stalled cache must not slow
the scrape). Labels: none.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.metrics import Gauge

from .mfa_services import AUDIT_LOCKED
from .models import MfaLock
from .policy import Capability, roles_with
from .services import AUDIT_USER_DEACTIVATED


def gauges() -> list[Gauge]:
    """A DatabaseError propagates: the endpoint handles it."""
    deactivated = Gauge(
        "arkray_administrator_deactivations_last_hour",
        "Administrator accounts deactivated in the last hour (user.deactivated audit events).",
    )
    locked = Gauge(
        "arkray_mfa_lockouts_last_hour",
        "Second factors locked by too many wrong codes in the last hour (mfa.locked audit events).",
    )
    until_reset = Gauge(
        "arkray_mfa_locked_until_reset_last_hour",
        "Second factors locked until an MFA reset in the last hour (mfa.locked, until mfa_reset).",
    )
    since = timezone.now() - timedelta(hours=1)
    counts = AuditEvent.objects.filter(
        action__in=[AUDIT_USER_DEACTIVATED, AUDIT_LOCKED], occurred_at__gte=since
    ).aggregate(
        deactivated=Count(
            "pk",
            filter=Q(
                action=AUDIT_USER_DEACTIVATED,
                metadata__role__in=sorted(roles_with(Capability.USERS_MANAGE)),
            ),
        ),
        locked=Count("pk", filter=Q(action=AUDIT_LOCKED)),
        until_reset=Count(
            "pk", filter=Q(action=AUDIT_LOCKED, metadata__until=MfaLock.MFA_RESET.value)
        ),
    )
    deactivated.add(counts["deactivated"])
    locked.add(counts["locked"])
    until_reset.add(counts["until_reset"])
    return [deactivated, locked, until_reset]
