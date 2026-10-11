"""Identity gauges for the metrics endpoint (ADR-0033, R126; docs/observability.md#metrics).

- `arkray_administrator_deactivations_last_hour`: administrators deactivated in the last hour
  (the `user.deactivated` audit events that record an administrator's role), from PostgreSQL
  at scrape time, one count over the `audit_action_idx` index. A database-wide figure every
  pod reports alike. Raises ArkrayAdministratorDeactivated: one administrator can deactivate
  the others (only the last active one is protected), so each time is worth a look.

No cache read (a stalled cache must not slow the scrape). Labels: none.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.metrics import Gauge

from .policy import Capability, roles_with
from .services import AUDIT_USER_DEACTIVATED


def gauges() -> list[Gauge]:
    """A DatabaseError propagates: the endpoint handles it."""
    deactivated = Gauge(
        "arkray_administrator_deactivations_last_hour",
        "Administrator accounts deactivated in the last hour (user.deactivated audit events).",
    )
    since = timezone.now() - timedelta(hours=1)
    deactivated.add(
        AuditEvent.objects.filter(
            action=AUDIT_USER_DEACTIVATED,
            occurred_at__gte=since,
            metadata__role__in=sorted(roles_with(Capability.USERS_MANAGE)),
        ).count()
    )
    return [deactivated]
