"""Pseudonymising a former staff member (docs/privacy.md#staff, privacy remediation P2-7).

Users are never deleted: their id attributes the CRM history they made (who owned, moved,
priced and closed each deal), and financial and negotiated-price history is kept as it is.
When an organisation no longer needs to know *who* a former colleague was, an administrator
replaces their identity with a pseudonym, here (`manage.py pseudonymise_user`, or the admin
API), on a deactivated account only:

- name -> "Former user <8 hex>", email -> former-<id>@pseudonymised.invalid, password
  unusable, last sign-in forgotten; their sessions end (session epoch), pending invitation and
  reset links are revoked, and live support sessions for or by them end;
- what only identifies them goes: their Ask Arkray conversations, the personal details of
  audit events about them (client addresses, old and new emails, support reasons: the
  events themselves stay, with their id), the reasons typed for support sessions involving
  them, failed-sign-in evidence under their email, queued email payloads naming them, and
  their workspace-access bookkeeping;
- what stays: the id, role, status and dates; every record's ownership and attribution;
  the append-only audit trail, stage and price history (ids, amounts, never names).

Refused while a legal hold covers them, while they still own work someone must take over
(unarchived leads, open deals, open tasks, scheduled meetings: reassign first), for an active
or invited account, and for the operator themselves. A dry run (`preview`) shows the counts.
Idempotent: an account already pseudonymised is reported, never changed again. Audited
(`user.pseudonymised`, no values) and recorded in the erasure ledger (core.ledger), so a
restored backup is pseudonymised again before it is served.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from arkray.activities.models import Activity, ActivityStatus
from arkray.ai.models import Conversation
from arkray.audit import services as audit
from arkray.audit.models import AuditDetail
from arkray.core import holds, ledger
from arkray.core.errors import BusinessRuleViolation, NotFoundError
from arkray.core.models import HoldSubject, OutboxEvent, OutboxStatus
from arkray.identity import throttling
from arkray.identity.models import (
    AccountToken,
    AuthThrottleEvent,
    SupportEnd,
    SupportSession,
    TokenStatus,
    User,
    UserStatus,
    WorkspaceAccessWindow,
)
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity, StageCategory

PSEUDONYM_DOMAIN = "pseudonymised.invalid"
AUDIT_PSEUDONYMISED = "user.pseudonymised"
NOT_DEACTIVATED = "Deactivate the account first: only former staff are pseudonymised."
OWNS_WORK = (
    "This user still owns work someone must take over: {leads} leads, {deals} open deals and "
    "{work} open tasks or scheduled meetings. Reassign them first."
)
NOT_YOURSELF = "You can't pseudonymise your own account."


class AlreadyPseudonymised(Exception):
    def __init__(self, user_id: UUID) -> None:
        super().__init__(f"User {user_id} has already been pseudonymised.")


@dataclass(frozen=True, slots=True)
class StaffErasure:
    user_id: UUID
    conversations: int
    audit_details: int
    support_reasons: int
    tokens_revoked: int
    support_sessions_ended: int
    throttle_events: int
    queued_payloads: int
    access_windows: int


def pseudonymised(user: User) -> bool:
    return user.email.endswith(f"@{PSEUDONYM_DOMAIN}")


def _owned_work(user: User) -> tuple[int, int, int]:
    leads = Lead.objects.filter(owner=user, archived_at__isnull=True).count()
    deals = Opportunity.objects.filter(
        owner=user, status=StageCategory.OPEN, archived_at__isnull=True
    ).count()
    work = Activity.objects.filter(
        owner=user,
        archived_at__isnull=True,
        status__in=[ActivityStatus.OPEN, ActivityStatus.SCHEDULED],
    ).count()
    return leads, deals, work


def _details_about(user_id: UUID) -> Q:
    return (
        Q(event__actor_id=user_id)
        | Q(event__subject_user_id=user_id)
        | Q(event__target_type="user", event__target_id=str(user_id))
    )


def _queued_payloads(user: User) -> Q:
    return Q(payload__user_id=str(user.pk)) | Q(payload__email=user.email)


def _check(user: User, operator_id: UUID) -> None:
    if pseudonymised(user):
        raise AlreadyPseudonymised(user.pk)
    if user.pk == operator_id:
        raise BusinessRuleViolation(NOT_YOURSELF)
    if user.status != UserStatus.DEACTIVATED:
        raise BusinessRuleViolation(NOT_DEACTIVATED)
    if holds.is_held(HoldSubject.USER, user.pk):
        raise holds.UnderLegalHold("user", user.pk)
    leads, deals, work = _owned_work(user)
    if leads or deals or work:
        raise BusinessRuleViolation(OWNS_WORK.format(leads=leads, deals=deals, work=work))


def preview(user_id: UUID, *, operator_id: UUID) -> StaffErasure:
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise NotFoundError()
    _check(user, operator_id)
    sessions = SupportSession.objects.filter(Q(admin=user) | Q(target=user))
    return StaffErasure(
        user_id=user.pk,
        conversations=Conversation.objects.filter(actor_id=user.pk).count(),
        audit_details=AuditDetail.objects.filter(_details_about(user.pk)).count(),
        support_reasons=sessions.exclude(reason="").count(),
        tokens_revoked=AccountToken.objects.filter(user=user, status=TokenStatus.PENDING).count(),
        support_sessions_ended=sessions.filter(ended_at__isnull=True).count(),
        throttle_events=AuthThrottleEvent.objects.filter(
            identifier_hash__in=throttling.login_identifiers(user.email)
        ).count(),
        queued_payloads=OutboxEvent.objects.filter(_queued_payloads(user)).count(),
        access_windows=WorkspaceAccessWindow.objects.filter(actor_id=user.pk).count(),
    )


@transaction.atomic
def pseudonymise(user_id: UUID, *, operator_id: UUID, replay: bool = False) -> StaffErasure:
    """`replay`: re-applying a ledger entry after a restore (privacy.replay). The erasure was
    authorised and checked when it first happened, so only "already done" stops it; an
    account the backup has active again is deactivated with it."""
    user = User.objects.select_for_update().filter(pk=user_id).first()
    if user is None:
        raise NotFoundError()
    if replay:
        if pseudonymised(user):
            raise AlreadyPseudonymised(user.pk)
    else:
        _check(user, operator_id)
    now = timezone.now()
    original_email = user.email
    # What only identifies them, first (their email still finds it).
    _, deleted = Conversation.objects.filter(actor_id=user.pk).delete()
    conversations = deleted.get(Conversation._meta.label, 0)
    details, _ = AuditDetail.objects.filter(_details_about(user.pk)).delete()
    sessions = SupportSession.objects.filter(Q(admin=user) | Q(target=user))
    ended = sessions.filter(ended_at__isnull=True).update(
        ended_at=now, end_reason=SupportEnd.NOT_ALLOWED
    )
    reasons = sessions.exclude(reason="").update(reason="")
    tokens = AccountToken.objects.filter(user=user, status=TokenStatus.PENDING).update(
        status=TokenStatus.REVOKED, revoked_at=now
    )
    throttle, _ = AuthThrottleEvent.objects.filter(
        identifier_hash__in=throttling.login_identifiers(original_email)
    ).delete()
    queued = OutboxEvent.objects.filter(_queued_payloads(user))
    # Unfinished work about them (an email to their address) has nothing left to do: done,
    # emptied, rather than failing into dead events (backend review P3); finished events
    # just lose their payload.
    payloads = queued.filter(status__in=[OutboxStatus.PENDING, OutboxStatus.IN_FLIGHT]).update(
        payload={},
        status=OutboxStatus.DONE,
        finished_at=now,
        locked_until=None,
        claim_token=None,
        last_error="",
    ) + queued.exclude(status__in=[OutboxStatus.PENDING, OutboxStatus.IN_FLIGHT]).update(payload={})
    windows, _ = WorkspaceAccessWindow.objects.filter(actor_id=user.pk).delete()

    user.first_name = "Former user"
    user.last_name = user.pk.hex[:8]
    user.email = f"former-{user.pk.hex}@{PSEUDONYM_DOMAIN}"
    user.set_unusable_password()
    user.last_login = None
    user.password_change_required = False
    user.password_changed_at = None
    user.session_epoch += 1  # any session it still had ends
    user.version += 1
    if user.status != UserStatus.DEACTIVATED:  # only when replaying a restored backup
        user.status, user.is_active, user.deactivated_at = UserStatus.DEACTIVATED, False, now
    user.save()

    result = StaffErasure(
        user_id=user.pk,
        conversations=conversations,
        audit_details=details,
        support_reasons=reasons,
        tokens_revoked=tokens,
        support_sessions_ended=ended,
        throttle_events=throttle,
        queued_payloads=payloads,
        access_windows=windows,
    )
    counts = {key: value for key, value in asdict(result).items() if key != "user_id"}
    from .exports import withdraw_for  # exports reads this module

    # Their exports, queued or ready, end with their name (backend review P2).
    withdrawn = withdraw_for("user", user.pk)
    audit.record(
        AUDIT_PSEUDONYMISED,
        actor_id=operator_id,
        target_type="user",
        target_id=user.pk,
        subject_user_id=user.pk,
        metadata={
            **counts,
            **({"exports_withdrawn": withdrawn} if withdrawn else {}),
            **({"replay": True} if replay else {}),
        },
    )
    if not replay:
        ledger.append("user_pseudonymised", user.pk, at=now)  # last: all the above succeeded
    return result
