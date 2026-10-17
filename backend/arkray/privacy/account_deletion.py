"""Deleting an account from user administration (ADR-0036).

"Delete" never removes CRM business data. It composes the lifecycle the project already has:

1. **Decide, write nothing.** The actor (a Super admin, or an Admin for their own team's users)
   is re-read and the target found within their reach (404 outside it); who may be deleted is
   decided in one place, `identity.services.refuse_account_operation_unless_permitted`. A
   stale version (409), a wrong confirmation email (422) or a legal hold on the person (409,
   or a plain refusal for whoever doesn't handle privacy requests: R148) stops it.
2. **Hand the work over, lead by lead.** The target's leads that are not archived, and archived
   ones still carrying open work, go to `reassign_to` through `leads.services.reassign_lead`
   (audited; the open deals, tasks, meetings and notes follow the lead), each in a transaction
   of its own holding only that lead's locks, and the actor and target re-read before each. At
   most `MAX_LEADS_PER_REQUEST` leads or `TIME_BUDGET_S` seconds per request: a request that
   leaves leads behind answers "not completed" and the same request is repeated.
   **No lock is held across the hand-over.** The project's lock order is lead, then
   opportunities, activities and configuration, then user rows FOR SHARE as leaf locks; a
   deletion that held the target's row (or the user-administration lock and the legal-hold
   barrier) while it waited for those would deadlock with any CRM write on the target's leads
   and starve every other user-administration write for the length of a batch (adversarial
   review of ADR-0036, M1 and M2).
3. **Archive their personal pipelines** that hold no open deal (kept, never deleted).
4. **Finish in one short transaction**, in privacy.staff's lock order: the hold barrier, the
   user-administration lock, the target's row. Everything is decided again on the locked rows
   and the remaining work counted again; then the account is deactivated (sessions end, an
   Admin's team is released) and its identity erased (`privacy.staff.erase_locked`, limited to
   the account's own traces unless the actor handles privacy requests), and the erasure
   ledger records it.

The audit trail keeps `user.deactivated`, `user.pseudonymised` and `user.deleted` with the ids
of actor and target. Nothing here is reachable from a support session (the views refuse it).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from arkray.activities.models import Activity, ActivityStatus
from arkray.audit import services as audit
from arkray.core import holds, ledger
from arkray.core.access import AccessScope
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    DomainError,
    InvalidInputError,
)
from arkray.core.models import HoldSubject
from arkray.identity import selectors as identity_selectors
from arkray.identity import services as identity
from arkray.identity.models import User, normalize_email
from arkray.identity.policy import Capability, has_capability
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import configuration as pipeline_configuration
from arkray.pipeline.models import Opportunity, Pipeline, StageCategory

from . import staff

AUDIT_USER_DELETED = "user.deleted"
# How much one request hands over before it answers "not completed": a reassignment is about
# a dozen queries, and a request lives for 30 s (gunicorn) at most.
MAX_LEADS_PER_REQUEST = 200
TIME_BUDGET_S = 10.0

CONFIRM_EMAIL = "Type the account's email address to confirm."
NEEDS_NEW_OWNER = "Choose who takes over this user's work."
NEW_OWNER_REFUSED = "Choose an active user who can take over this work."
HELD = (
    "This user is under a legal hold, so their account can't be deleted until the hold is"
    " released (an operator releases it, with the matter's reference)."
)
BLOCKED = "This account can't be deleted right now. Ask a super admin to look into it."


class AccountUnderLegalHold(holds.UnderLegalHold):
    """409 `legal_hold`, worded for the person deleting the account (the base message names
    an id and an administrator who can't release holds)."""

    def __init__(self) -> None:
        ConflictError.__init__(self, HELD)


def _held_error(actor: User) -> DomainError:
    """Only whoever handles privacy requests learns that a hold exists (R148: holds are not
    an oracle for anyone else); an Admin gets a refusal that says nothing about why."""
    if has_capability(actor, Capability.PRIVACY_MANAGE):
        return AccountUnderLegalHold()
    return BusinessRuleViolation(BLOCKED)


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    """What deleting the account would do, for the confirmation dialog."""

    leads: int
    open_deals: int
    open_activities: int
    personal_pipelines: int
    team_members: int
    legal_hold: bool
    needs_new_owner: bool
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """`completed` False: this request handed over `reassigned_leads` and `remaining_leads`
    are left; the same request is sent again."""

    completed: bool
    reassigned_leads: int
    remaining_leads: int


def leads_to_hand_over(user_id: UUID) -> QuerySet[Lead]:
    """The leads the account's work sits on: every lead that is not archived, and archived
    ones that still carry its open deals or open tasks and meetings (an archived lead can't
    be edited, but the account can't be deleted while it owns open work)."""
    open_deals = Opportunity.objects.filter(
        owner_id=user_id, status=StageCategory.OPEN, archived_at__isnull=True
    ).values("lead_id")
    open_work = Activity.objects.filter(
        owner_id=user_id,
        archived_at__isnull=True,
        status__in=[ActivityStatus.OPEN, ActivityStatus.SCHEDULED],
    ).values("lead_id")
    return Lead.objects.filter(owner_id=user_id).filter(
        Q(archived_at__isnull=True) | Q(pk__in=open_deals) | Q(pk__in=open_work)
    )


def preview(actor: User, user_id: UUID) -> DeletionPreview:
    """The counts and whether the actor may delete the account now. Read-only; the account
    must be within the actor's reach (404 otherwise). A refusal is `blocked_reason`, not an
    error: the dialog explains it."""
    user = identity_selectors.administered_user_detail(actor, user_id)
    _, deals, work = staff.owned_work(user)
    leads = leads_to_hand_over(user.pk).count()
    held = holds.is_held(HoldSubject.USER, user.pk)
    reason: str | None = None
    try:
        if user.pk == actor.pk:
            raise BusinessRuleViolation(identity.DELETE_OWN_ACCOUNT_REFUSED)
        identity.refuse_if_pseudonymised(user)
        identity.refuse_account_operation_unless_permitted(actor, user)
        if held:
            raise _held_error(actor)
    except DomainError as exc:
        reason = str(exc)
    return DeletionPreview(
        leads=leads,
        open_deals=deals,
        open_activities=work,
        personal_pipelines=Pipeline.objects.filter(owner_id=user.pk, is_active=True).count(),
        team_members=User.objects.filter(manager_id=user.pk).count(),
        legal_hold=held and has_capability(actor, Capability.PRIVACY_MANAGE),
        needs_new_owner=bool(leads or deals or work),
        blocked_reason=reason,
    )


def _check_request(
    actor: User, user: User, *, version: int, confirm_email: str, locked: bool
) -> None:
    """The request's own conditions on the account: its version, the typed email and the
    legal hold. `locked`: in the final transaction, under the hold barrier, so a hold being
    placed now has committed or waits (R134); before that, a plain read."""
    if user.version != version:
        raise ConflictError()
    if normalize_email(confirm_email) != user.email:
        raise BusinessRuleViolation(CONFIRM_EMAIL)
    if locked:
        try:
            holds.refuse_if_held(HoldSubject.USER, user.pk)
        except holds.UnderLegalHold:
            raise _held_error(actor) from None
    elif holds.is_held(HoldSubject.USER, user.pk):
        raise _held_error(actor)


def _new_owner(actor: User, user: User, reassign_to: UUID | None) -> UUID:
    if reassign_to is None:
        raise InvalidInputError(details={"reassign_to": [NEEDS_NEW_OWNER]})
    # Within the actor's own reach and active, under a share lock on the new owner's row; the
    # account being deleted is never its own successor. Every reassignment checks again.
    with transaction.atomic():
        if reassign_to == user.pk or not lead_services.lock_assignee(actor, reassign_to):
            raise InvalidInputError(details={"reassign_to": [NEW_OWNER_REFUSED]})
    return reassign_to


def _hand_over(actor_id: UUID, user_id: UUID, new_owner: UUID) -> tuple[int, int]:
    """Hand leads over one at a time until the request's budget is spent; returns how many
    went and how many are left. The actor and target are re-read before each (one who lost
    the right a moment ago stops here), and a lead edited at the same moment is skipped, not
    failed: the next round takes it."""
    deadline = time.monotonic() + TIME_BUDGET_S
    handed = 0
    skipped: set[UUID] = set()
    while handed < MAX_LEADS_PER_REQUEST and time.monotonic() < deadline:
        actor, user = identity.reread_account_for_removal(actor_id, user_id)
        row = (
            leads_to_hand_over(user.pk)
            .exclude(pk__in=skipped)
            .order_by("id")
            .values_list("id", "version")
            .first()
        )
        if row is None:
            break
        lead_id, version = row
        try:
            lead_services.reassign_lead(
                actor=actor,
                scope=AccessScope.for_user(actor.pk, user.pk),
                lead_id=lead_id,
                version=version,
                owner_id=new_owner,
                allow_archived=True,
            )
        except ConflictError:
            skipped.add(lead_id)
            continue
        handed += 1
    return handed, leads_to_hand_over(user_id).count()


def _archive_personal_pipelines(actor_id: UUID, user_id: UUID) -> int:
    """Archive the account's personal pipelines that hold no open deal. One that still holds
    a deal (its new owner's now) stays active so the deal keeps its board; one the archive
    limit refuses stays too. Never deleted: opportunities and prices reference them. Each in
    its own transaction (the pipeline's locks, then the owner's advisory lock), before the
    final transaction takes the account's row."""
    actor, user = identity.reread_account_for_removal(actor_id, user_id)
    scope = AccessScope.for_user(actor.pk, user.pk)
    archived = 0
    for pipeline_id, version in list(
        Pipeline.objects.filter(owner_id=user.pk, is_active=True).values_list("id", "version")
    ):
        try:
            pipeline_configuration.archive_pipeline(
                actor=actor, scope=scope, pipeline_id=pipeline_id, version=version
            )
        except (BusinessRuleViolation, ConflictError):
            continue
        archived += 1
    return archived


def _finish(
    *,
    actor_id: UUID,
    user_id: UUID,
    version: int,
    confirm_email: str,
    reassign_to: UUID | None,
    new_owner: UUID | None,
    reassigned: int,
    archived_pipelines: int,
) -> DeletionResult:
    with transaction.atomic():
        # The same order as privacy.staff.pseudonymise: the hold barrier (this transaction's
        # first lock), the user-administration lock, then the account's row.
        holds.barrier()
        identity.serialise_user_administration()
        actor, user = identity.lock_account_for_removal(actor_id, user_id)
        _check_request(actor, user, version=version, confirm_email=confirm_email, locked=True)
        left = leads_to_hand_over(user.pk).count()
        if left:
            # Work arrived since the check (a lead assigned to them, a write finishing): nothing
            # is changed here. With a new owner named, the request is repeated and hands it
            # over; without one, it has to name one.
            if reassign_to is None:
                raise InvalidInputError(details={"reassign_to": [NEEDS_NEW_OWNER]})
            return DeletionResult(False, reassigned, left)
        leads, deals, work = staff.owned_work(user)
        if leads or deals or work:  # not on any lead we could hand over
            raise BusinessRuleViolation(staff.OWNS_WORK.format(leads=leads, deals=deals, work=work))
        limited = not has_capability(actor, Capability.PRIVACY_MANAGE)
        removal = identity.deactivate_for_removal(actor, user, now=timezone.now())
        _, erased_at = staff.erase_locked(
            user,
            operator_id=actor.pk,
            administrator=identity.is_administrator(user),
            by_deletion=True,
            limited=limited,
        )
        audit.record(
            AUDIT_USER_DELETED,
            actor_id=actor.pk,
            target_type="user",
            target_id=user.pk,
            subject_user_id=user.pk,
            metadata={
                "role": user.role,
                "erasure": "limited" if limited else "full",
                "reassigned_to": None if new_owner is None else str(new_owner),
                "reassigned_leads": reassigned,
                "archived_pipelines": archived_pipelines,
                "unassigned_members": removal.unassigned_members,
            },
        )
        ledger.append("user_pseudonymised", user.pk, at=erased_at)
    return DeletionResult(True, reassigned, 0)


def delete_account(
    *,
    actor_id: UUID,
    user_id: UUID,
    version: int,
    confirm_email: str,
    reassign_to: UUID | None,
) -> DeletionResult:
    actor, user = identity.reread_account_for_removal(actor_id, user_id)
    _check_request(actor, user, version=version, confirm_email=confirm_email, locked=False)

    new_owner: UUID | None = None
    reassigned = 0
    if leads_to_hand_over(user.pk).exists() or any(staff.owned_work(user)):
        new_owner = _new_owner(actor, user, reassign_to)
        reassigned, left = _hand_over(actor_id, user_id, new_owner)
        if left:
            return DeletionResult(False, reassigned, left)
    archived = _archive_personal_pipelines(actor_id, user_id)
    return _finish(
        actor_id=actor_id,
        user_id=user_id,
        version=version,
        confirm_email=confirm_email,
        reassign_to=reassign_to,
        new_owner=new_owner,
        reassigned=reassigned,
        archived_pipelines=archived,
    )
