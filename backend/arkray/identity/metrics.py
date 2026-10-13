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
- `arkray_administrator_promotions_last_hour`: accounts made administrators in the last hour
  (`user.role_changed` to an administrator's role; adversarial review of 73ff1e3, L3). Raises
  ArkrayAdministratorPromoted: promoting an account an administrator took over put it out of
  every other administrator's reach (R100), so each promotion is worth a look.
- `arkray_sign_in_failures_last_hour`: failed sign-ins of existing accounts (wrong passwords
  and codes, inactive accounts; `auth.login_failed` audit events) in the last hour (L1).
  Raises ArkraySignInFailureStorm well above anything people typing produce: guessing or
  spraying against the staff's real addresses is going on. (Unknown emails aren't audited,
  so aren't counted: guessing at them takes over nothing, and the throttle bounds it.)
- `arkray_sign_in_locks_last_hour`: accounts whose daily failure cap locked their sign-in
  from new browsers in the last hour (`auth.sign_in_locked`, written with the owner's notice
  seconds later; L1). Raises ArkraySignInLocked: someone guessed that account's password
  about 50 times.

"Administrator" means either kind (ADR-0035): a super administrator (role `admin`) or a
team administrator (`team_admin`), as `identity.policy.administrator_roles` says. A team
administrator's deactivation or a promotion to one is worth the same look: they manage
accounts too.

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

from .authentication import AUDIT_LOGIN_FAILED
from .mfa_services import AUDIT_LOCKED
from .models import MfaLock
from .policy import administrator_roles
from .services import AUDIT_ROLE_CHANGED, AUDIT_SIGN_IN_LOCKED, AUDIT_USER_DEACTIVATED


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
    promoted = Gauge(
        "arkray_administrator_promotions_last_hour",
        "Accounts made administrators in the last hour (user.role_changed audit events).",
    )
    failures = Gauge(
        "arkray_sign_in_failures_last_hour",
        "Failed sign-ins of existing accounts in the last hour (auth.login_failed audit events).",
    )
    sign_in_locks = Gauge(
        "arkray_sign_in_locks_last_hour",
        "Accounts locked from new browsers by the daily failed sign-in cap in the last hour"
        " (auth.sign_in_locked audit events).",
    )
    since = timezone.now() - timedelta(hours=1)
    administrators = sorted(administrator_roles())
    counts = AuditEvent.objects.filter(
        action__in=[
            AUDIT_USER_DEACTIVATED,
            AUDIT_LOCKED,
            AUDIT_ROLE_CHANGED,
            AUDIT_LOGIN_FAILED,
            AUDIT_SIGN_IN_LOCKED,
        ],
        occurred_at__gte=since,
    ).aggregate(
        deactivated=Count(
            "pk",
            filter=Q(action=AUDIT_USER_DEACTIVATED, metadata__role__in=administrators),
        ),
        promoted=Count("pk", filter=Q(action=AUDIT_ROLE_CHANGED, metadata__to__in=administrators)),
        locked=Count("pk", filter=Q(action=AUDIT_LOCKED)),
        until_reset=Count(
            "pk", filter=Q(action=AUDIT_LOCKED, metadata__until=MfaLock.MFA_RESET.value)
        ),
        failures=Count("pk", filter=Q(action=AUDIT_LOGIN_FAILED)),
        sign_in_locks=Count("pk", filter=Q(action=AUDIT_SIGN_IN_LOCKED)),
    )
    deactivated.add(counts["deactivated"])
    locked.add(counts["locked"])
    until_reset.add(counts["until_reset"])
    promoted.add(counts["promoted"])
    failures.add(counts["failures"])
    sign_in_locks.add(counts["sign_in_locks"])
    return [deactivated, locked, until_reset, promoted, failures, sign_in_locks]
