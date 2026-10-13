"""Pseudonymising a former staff member (docs/privacy.md#staff, privacy remediation P2-7).

Users are never deleted: their id attributes the CRM history they made (who owned, moved,
priced and closed each deal), and financial and negotiated-price history is kept as it is.
When an organisation no longer needs to know *who* a former colleague was, an administrator
replaces their identity with a pseudonym, here (`manage.py pseudonymise_user`, or the admin
API), on a deactivated account only:

- name -> "Former user <8 hex>", email -> former-<id>@pseudonymised.invalid, password
  unusable, last sign-in forgotten; their sessions end (session epoch), pending invitation and
  reset links are revoked, and live support sessions for or by them end;
- what only identifies them goes: their Ask Arkray conversations, and those about them (asked
  in their workspace, or naming them: deep security audit L-5), the personal details of
  audit events about them (client addresses, old and new emails, support reasons: the
  events themselves stay, with their id), the reasons typed for support sessions involving
  them, failed-sign-in evidence under their email, queued email payloads naming them, and
  their workspace-access bookkeeping;
- what stays: the id, role, status and dates; every record's ownership and attribution;
  the append-only audit trail, stage and price history (ids, amounts, never names).

A conversation a legal hold keeps (another held user's, or one touching a held customer:
ai.subjects.held_conversations) is kept, as housekeeping keeps it; those holds are read under
the hold barrier, the transaction's first lock (core.holds: one being placed at the same
moment has committed before, or waits; R134). Refused while a legal hold
covers them, while they still own work someone must take over
(unarchived leads, open deals, open tasks, scheduled meetings: reassign first), for an active
or invited account, and for the operator themselves. A dry run (`preview`) shows the counts.
Idempotent: an account already pseudonymised is reported, never changed again. Audited
(`user.pseudonymised`, no values) and recorded in the erasure ledger (core.ledger), so a
restored backup is pseudonymised again before it is served.

Final (adversarial review of 73ff1e3, L4): it was an email-domain test that user
administration never asked, so an administrator could rename the account back, or reactivate
it (an invitation), point it at their own mailbox and accept: the former employee's id was a
live login again. `User.pseudonymised_at` now marks it; the database holds a marked account
to its pseudonym (deactivated, no usable password, this name and email: CHECK
`identity_user_pseudonym_final`; a trigger keeps the mark), and identity.services refuses it
every change with 422 `account_pseudonymised`.

An administrator's account (its *role* is administrator, whatever its status: a deactivated
former administrator too) is changed only by that administrator (ADR-0030), and nobody
pseudonymises themselves, so no administrator pseudonymises another through the API: 403,
nothing changed (audit SEC-2: it replaced the other's email and password). The one way is
the higher-trust operator path, `manage.py pseudonymise_user --allow-administrator` (shell
and database access), audited with `administrator_target`. The rule is checked here, not
only in the view, after taking the user-administration lock and the target's row lock, on
the locked row: a promotion committed a moment earlier is seen, one that waits sees the
account changed. The operator is re-read there too (privacy.manage). A replay
(privacy.replay) re-applies a pseudonymisation that was authorised when it happened, so it
is never refused for the role.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import UUID

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from arkray.activities.models import Activity, ActivityStatus
from arkray.ai import subjects
from arkray.ai.models import Conversation
from arkray.audit import services as audit
from arkray.audit.models import AuditDetail
from arkray.core import holds, ledger
from arkray.core.errors import BusinessRuleViolation, NotFoundError, PermissionDeniedError
from arkray.core.models import HoldSubject, OutboxEvent, OutboxStatus
from arkray.identity import throttling
from arkray.identity.models import (
    PSEUDONYM_FIRST_NAME,
    AccountToken,
    AuthThrottleEvent,
    LoginLock,
    MfaDevice,
    MfaRecoveryCode,
    SupportEnd,
    SupportSession,
    TokenStatus,
    User,
    UserStatus,
    WorkspaceAccessWindow,
    pseudonym_email,
    pseudonym_last_name,
)
from arkray.identity.policy import Capability, has_capability
from arkray.identity.services import is_administrator, serialise_user_administration
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity, StageCategory

# Matching their name reads every stored answer: more than a web request's 10 s, but within
# the request's own budget (gunicorn ends it at 30 s, nginx at 35 s) while this holds its
# locks (review P3-3; the API calls this too, not only the command).
STATEMENT_TIMEOUT = "20s"
AUDIT_PSEUDONYMISED = "user.pseudonymised"
NOT_DEACTIVATED = "Deactivate the account first: only former staff are pseudonymised."
OWNS_WORK = (
    "This user still owns work someone must take over: {leads} leads, {deals} open deals and "
    "{work} open tasks or scheduled meetings. Reassign them first."
)
NOT_YOURSELF = "You can't pseudonymise your own account."
ADMINISTRATOR_REFUSED = (
    "An administrator's account can't be pseudonymised by another administrator. If it must"
    " be, an operator does it on the server (manage.py pseudonymise_user --allow-administrator)."
)


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
    """Marked by pseudonymise() (L4: the email domain alone was the test before)."""
    return user.is_pseudonymised


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


def _naming_terms(user: User) -> list[str]:
    """What names them in a question or an answer: their full name and email, as whole words
    (ai.subjects.naming_conversations; shorter ones would match unrelated conversations)."""
    terms = {user.full_name.strip(), user.email.strip()}
    return sorted(term for term in terms if len(term) >= subjects.MIN_TERM_LENGTH)


def _conversations(user: User) -> list[UUID]:
    """The Ask Arkray conversations that go: their own, those an administrator asked in their
    workspace, and every other one whose question or answer names them (deep security audit
    L-5: administrators' conversations about them kept their real name for up to 30 days),
    except those a legal hold keeps."""
    about = set(
        Conversation.objects.filter(Q(actor_id=user.pk) | Q(subject_id=user.pk)).values_list(
            "pk", flat=True
        )
    )
    about.update(subjects.naming_conversations(_naming_terms(user)))
    return sorted(about - subjects.held_conversations(among=about))


def _require_operator(operator_id: UUID) -> None:
    """The operator, re-read (under the lock when pseudonymising): one who lost privacy.manage
    a moment ago, or a sales user calling the service, is refused."""
    operator = User.objects.filter(pk=operator_id).first()
    if operator is None or not has_capability(operator, Capability.PRIVACY_MANAGE):
        raise PermissionDeniedError()


def _check(user: User, operator_id: UUID, *, allow_administrator: bool) -> None:
    if user.pk == operator_id:
        raise BusinessRuleViolation(NOT_YOURSELF)  # even with allow_administrator
    if is_administrator(user) and not allow_administrator:
        raise PermissionDeniedError(ADMINISTRATOR_REFUSED)
    if pseudonymised(user):
        raise AlreadyPseudonymised(user.pk)
    if user.status != UserStatus.DEACTIVATED:
        raise BusinessRuleViolation(NOT_DEACTIVATED)
    if connection.in_atomic_block:  # pseudonymise: serialised with placing a hold
        holds.refuse_if_held(HoldSubject.USER, user.pk)
    elif holds.is_held(HoldSubject.USER, user.pk):  # preview: a read, nothing to serialise
        raise holds.UnderLegalHold("user", user.pk)
    leads, deals, work = _owned_work(user)
    if leads or deals or work:
        raise BusinessRuleViolation(OWNS_WORK.format(leads=leads, deals=deals, work=work))


def preview(user_id: UUID, *, operator_id: UUID, allow_administrator: bool = False) -> StaffErasure:
    _require_operator(operator_id)
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise NotFoundError()
    _check(user, operator_id, allow_administrator=allow_administrator)
    sessions = SupportSession.objects.filter(Q(admin=user) | Q(target=user))
    return StaffErasure(
        user_id=user.pk,
        conversations=len(_conversations(user)),
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
def pseudonymise(
    user_id: UUID,
    *,
    operator_id: UUID | None,
    replay: bool = False,
    allow_administrator: bool = False,
    unless_held: bool = False,
) -> StaffErasure:
    """`replay`: re-applying a ledger entry after a restore (privacy.replay). The erasure was
    authorised and checked when it first happened, so only "already done" stops it (an
    administrator's too); an account the backup has active again is deactivated with it.
    `operator_id` None is the system (the automatic ledger reconciliation): only with
    `replay`, audited with no actor. `unless_held` (with `replay`): the automatic
    reconciliation leaves a held account to an operator (privacy.reconcile), so a hold on it
    stops the replay, under the same lock as a first pseudonymisation.
    `allow_administrator`: only the operator's command passes it (never the API), to
    pseudonymise an administrator's account (ADR-0030, SEC-2)."""
    if operator_id is None and not replay:
        raise ValueError("Only a replay is made without an operator.")
    # The first lock: serialised with placing any hold (core.holds.barrier). The holds read
    # below (theirs, and those keeping conversations that name them) are then final until
    # this commits: one being placed has committed, or waits (R134).
    holds.barrier()
    # The user-administration lock, then the account's row (identity's order): a promotion or
    # demotion is either committed before the check reads the role, or waits for this one.
    serialise_user_administration()
    user = User.objects.select_for_update().filter(pk=user_id).first()
    if user is None:
        raise NotFoundError()
    administrator = is_administrator(user)
    if replay:
        if pseudonymised(user):
            raise AlreadyPseudonymised(user.pk)
        if unless_held:
            holds.refuse_if_held(HoldSubject.USER, user.pk)
    else:
        assert operator_id is not None  # noqa: S101 — refused above
        _require_operator(operator_id)
        _check(user, operator_id, allow_administrator=allow_administrator)
    now = timezone.now()
    original_email = user.email
    with connection.cursor() as cursor:  # matching their name reads every stored answer
        cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
    # What only identifies them, first (their name and email still find it). The
    # conversations are locked before the delete collects their questions, as an erasure
    # locks them (privacy.services): one being added meanwhile is collected too.
    conversation_ids = list(
        Conversation.objects.select_for_update()
        .filter(pk__in=_conversations(user))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    _, deleted = Conversation.objects.filter(pk__in=conversation_ids).delete()
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
    identifiers = throttling.login_identifiers(original_email)
    throttle, _ = AuthThrottleEvent.objects.filter(identifier_hash__in=identifiers).delete()
    # A sign-in lock under their email (review of 73ff1e3, L1) is evidence about them too.
    LoginLock.objects.filter(identifier_hash__in=identifiers).delete()
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
    # Their second factor (ADR-0033): the encrypted secret and the recovery codes' hashes
    # are theirs alone and unusable on a pseudonymised account.
    MfaRecoveryCode.objects.filter(user=user).delete()
    MfaDevice.objects.filter(user=user).delete()

    user.first_name = PSEUDONYM_FIRST_NAME
    user.last_name = pseudonym_last_name(user.pk)
    user.email = pseudonym_email(user.pk)
    user.pseudonymised_at = now  # for good (CHECK and trigger; L4)
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
            **({"administrator_target": True} if administrator else {}),
            **({"replay": True} if replay else {}),
        },
    )
    if not replay:
        ledger.append("user_pseudonymised", user.pk, at=now)  # last: all the above succeeded
    return result
