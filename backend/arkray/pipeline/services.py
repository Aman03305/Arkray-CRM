"""Opportunity use cases. Each runs in one transaction that also writes its stage history,
audit event and domain events, so the record, its history and every subscriber agree.

Authorization, applied here for every caller (API, conversion, future imports and tools):
- the opportunity (and its lead) are looked up through the caller's AccessScope: outside
  it, NotFoundError (404), indistinguishable from a record that doesn't exist;
- writing requires identity.workspaces.authorize_write (own workspace: crm.access_own;
  another user's or the organisation's: crm.manage_any), else 403;
- nobody chooses an opportunity's owner: it is always the lead's owner (who must be an
  active, assignable user), so creating an opportunity can never assign work to someone.

Lock order (docs/pipeline.md#lock-order), the same in every operation, so two operations
can never wait for each other in a cycle:

    1. the lead          leads.selectors.lock_lead: FOR NO KEY UPDATE, or FOR UPDATE when
                         the operation changes the lead (conversion; reassignment in leads)
    2. opportunities     FOR NO KEY UPDATE OF the opportunity row only (never the joined
                         stage: shared configuration rows are only key-share-checked);
                         several at once only in ascending id order
    3. user rows         FOR SHARE (identity.selectors.lock_assignable_user); leaf locks:
                         whoever holds one never waits for a lead or opportunity lock
                         held by a transaction that wants the user row exclusively
    4. inserts           stage history, audit, idempotency records

Holding the lead's lock means its owner can't change until we commit: a reassignment
waits (and then moves what we created), or went first (and we no longer find the lead in
the previous owner's workspace). The database backs this up: an open opportunity whose
owner isn't its lead's owner can't be committed (models.Opportunity).

Concurrency: every change requires the `version` the client last saw (409 otherwise) and
increments it. Requests that change nothing (moving to the current stage, archiving an
archived opportunity) succeed without a new version, so retries are harmless.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import idempotency
from arkray.core.access import AccessScope
from arkray.core.domain_events import publish
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidInputError,
    NotFoundError,
)
from arkray.identity.models import User
from arkray.identity.selectors import lock_assignable_user
from arkray.identity.workspaces import authorize_write
from arkray.leads import selectors as lead_selectors
from arkray.leads import services as lead_services
from arkray.leads.models import Lead, StatusCategory

from . import events, selectors, validation
from .models import Opportunity, Pipeline, Stage, StageCategory, StageHistory

AUDIT_CREATED = "opportunity.created"
AUDIT_UPDATED = "opportunity.updated"
AUDIT_STAGE_CHANGED = "opportunity.stage_changed"
AUDIT_WON = "opportunity.won"
AUDIT_LOST = "opportunity.lost"
AUDIT_REOPENED = "opportunity.reopened"
AUDIT_OWNER_CHANGED = "opportunity.owner_changed"
AUDIT_ARCHIVED = "opportunity.archived"
AUDIT_RESTORED = "opportunity.restored"
AUDIT_LEAD_CONVERTED = "lead.converted"

IDEMPOTENT_CREATE = "pipeline.create_opportunity"
IDEMPOTENT_CONVERT = "pipeline.convert_lead"

ARCHIVED_READ_ONLY = "This opportunity is archived. Restore it to make changes."
LEAD_ARCHIVED = "This lead is archived. Restore it before adding opportunities."
OWNER_NOT_ASSIGNABLE = (
    "This lead's owner is deactivated. Reassign the lead to an active user first."
)
CLOSED_TO_CLOSED = "This opportunity is closed. Reopen it by moving it to an open stage first."
REOPEN_ELSEWHERE = (
    "This opportunity's lead now belongs to someone else, so it can't be reopened here. "
    "Ask an administrator to reopen it."
)
CLOSED_PROBABILITY = (
    "Won opportunities are 100% and lost ones 0%; the probability can't be changed."
)
LOST_REASON_ONLY_WHEN_LOST = "Only lost opportunities have a lost reason."
SAME_STAGE_LOST_REASON = (
    "The opportunity is already in this stage. Edit it to change the lost reason."
)
LEAD_ARCHIVED_REOPEN = "This lead is archived. Restore the lead before reopening its opportunities."
LEAD_ARCHIVED_RESTORE = "This lead is archived. Restore the lead first."
ALREADY_CONVERTED = "This lead has already been converted."
NO_CONVERTED_STATUS = "No lead status for converted leads is configured."
NEEDS_OPPORTUNITY = (
    "A lead becomes Converted with its first opportunity. Use Convert to create one, or add "
    "an opportunity before changing the status."
)
INVALID_STAGE = "Choose a stage of this opportunity's pipeline."
INVALID_PIPELINE = "Choose an active pipeline."
ALREADY_CREATED_ELSEWHERE = (
    "This was already created by an earlier request and has since left this workspace."
)


@dataclass(frozen=True, slots=True)
class CreateResult:
    opportunity: Opportunity
    replayed: bool  # an earlier request with the same Idempotency-Key created it


@dataclass(frozen=True, slots=True)
class ConversionResult:
    lead: Lead
    opportunity: Opportunity
    replayed: bool


# --- helpers -----------------------------------------------------------------------------------
def _audit(
    action: str,
    actor_id: UUID,
    opportunity: Opportunity,
    *,
    workspace: str | None,
    subject: UUID,
    **metadata: Any,
) -> None:
    audit.record(
        action,
        actor_id=actor_id,
        target_type="opportunity",
        target_id=opportunity.pk,
        subject_user_id=subject if subject != actor_id else None,
        metadata={**({"workspace": workspace} if workspace else {}), **metadata},
    )


def _lock(scope: AccessScope, opportunity_id: UUID) -> tuple[Opportunity, Lead]:
    """The opportunity, locked, and its lead, locked first (lock order). NotFoundError if
    the opportunity is outside the scope. The lead itself may be outside the scope: a
    closed opportunity keeps its owner after its lead is reassigned."""
    lead_id = (
        scope.apply(Opportunity.objects.filter(pk=opportunity_id))
        .values_list("lead_id", flat=True)
        .first()
    )
    if lead_id is None:
        raise NotFoundError()
    lead = lead_selectors.lock_lead_by_id(lead_id)
    # OF self: only the opportunity row. A bare FOR UPDATE over the join would also lock
    # the (shared, organisation-wide) stage row, and two users moving cards in opposite
    # directions deadlocked on the stages' foreign-key checks (Phase 3 review, P1). Stage
    # rows are never locked by opportunity writes, only KEY SHARE-checked by their keys.
    opportunity = (
        scope.apply(
            Opportunity.objects.select_for_update(no_key=True, of=("self",)).filter(
                pk=opportunity_id
            )
        )
        .select_related("stage")
        .first()
    )
    if opportunity is None:  # moved out of the scope (lead reassigned) meanwhile
        raise NotFoundError()
    return opportunity, lead


def _require_version(opportunity: Opportunity, version: int) -> None:
    if opportunity.version != version:
        raise ConflictError()


def _require_not_archived(opportunity: Opportunity) -> None:
    if opportunity.archived_at is not None:
        raise BusinessRuleViolation(ARCHIVED_READ_ONLY)


def _require_assignable(owner_id: UUID) -> None:
    if not lock_assignable_user(owner_id):
        raise BusinessRuleViolation(OWNER_NOT_ASSIGNABLE)


def _pipeline(pipeline_id: UUID | None) -> Pipeline:
    queryset = Pipeline.objects.filter(is_active=True)
    pipeline = (
        queryset.filter(pk=pipeline_id).first()
        if pipeline_id is not None
        else queryset.filter(is_default=True).first()
    )
    if pipeline is None:
        raise InvalidInputError(details={"pipeline": [INVALID_PIPELINE]})
    return pipeline


def _stage(pipeline: Pipeline, stage_id: UUID | None) -> Stage:
    """An active stage of `pipeline`: the given one, or the first open stage."""
    stages = Stage.objects.filter(pipeline=pipeline, is_active=True)
    stage = (
        stages.filter(pk=stage_id).first()
        if stage_id is not None
        else stages.filter(category=StageCategory.OPEN).order_by("position").first()
    )
    if stage is None:
        raise InvalidInputError(details={"stage": [INVALID_STAGE]})
    return stage


def _probability_for(stage: Stage, requested: Decimal | None) -> tuple[Decimal, bool]:
    """The probability an opportunity in `stage` gets, and whether it is an override.
    Closed stages have fixed probabilities; an open stage's default applies unless a
    different value is asked for."""
    if stage.is_closed:
        if requested is not None and requested != stage.probability:
            raise InvalidInputError(details={"probability": [CLOSED_PROBABILITY]})
        return stage.probability, False
    if requested is None or requested == stage.probability:
        return stage.probability, False
    return requested, True


def _history(
    opportunity: Opportunity,
    actor_id: UUID,
    *,
    from_stage: Stage | None,
    to_stage: Stage,
    at: datetime,
) -> None:
    StageHistory.objects.create(
        opportunity=opportunity,
        from_stage=from_stage,
        to_stage=to_stage,
        from_stage_name=from_stage.name if from_stage else "",
        to_stage_name=to_stage.name,
        from_status=from_stage.category if from_stage else "",
        to_status=to_stage.category,
        value=opportunity.value,
        probability=opportunity.probability,
        lost_reason=opportunity.lost_reason,
        actor_id=actor_id,
        occurred_at=at,
    )


def _publish_outcome(opportunity: Opportunity, actor_id: UUID, at: datetime) -> None:
    event: type[events.OpportunityWon | events.OpportunityLost]
    if opportunity.status == StageCategory.WON:
        event = events.OpportunityWon
    elif opportunity.status == StageCategory.LOST:
        event = events.OpportunityLost
    else:
        return
    publish(
        event(
            opportunity_id=opportunity.pk,
            lead_id=opportunity.lead_id,
            owner_id=opportunity.owner_id,
            actor_id=actor_id,
            occurred_at=at,
            stage_id=opportunity.stage_id,
        )
    )


def _replay(scope: AccessScope, opportunity_id: UUID) -> Opportunity:
    try:
        return selectors.opportunity_detail(scope, opportunity_id)
    except NotFoundError:
        raise ConflictError(ALREADY_CREATED_ELSEWHERE) from None


# --- create ----------------------------------------------------------------------------------
def _insert(
    *,
    actor: User,
    scope: AccessScope,
    lead: Lead,
    cleaned: Mapping[str, Any],
    pipeline_id: UUID | None,
    stage_id: UUID | None,
    via_conversion: bool,
) -> Opportunity:
    """Create an opportunity for an already-locked lead (inside the caller's transaction)."""
    if lead.archived_at is not None:
        raise BusinessRuleViolation(LEAD_ARCHIVED)
    _require_assignable(lead.owner_id)
    pipeline = _pipeline(pipeline_id)
    stage = _stage(pipeline, stage_id)
    probability, overridden = _probability_for(stage, cleaned.get("probability"))
    lost_reason = cleaned.get("lost_reason", "")
    if lost_reason and stage.category != StageCategory.LOST:
        raise InvalidInputError(details={"lost_reason": [LOST_REASON_ONLY_WHEN_LOST]})
    now = timezone.now()
    opportunity = Opportunity.objects.create(
        title=cleaned["title"],
        lead_id=lead.pk,
        owner_id=lead.owner_id,
        pipeline=pipeline,
        stage=stage,
        status=stage.category,
        value=cleaned["value"],
        probability=probability,
        probability_overridden=overridden,
        expected_close_date=cleaned.get("expected_close_date"),
        description=cleaned.get("description", ""),
        lost_reason=lost_reason,
        closed_at=now if stage.is_closed else None,
        created_by_id=actor.pk,
        created_at=now,
    )
    _history(opportunity, actor.pk, from_stage=None, to_stage=stage, at=now)
    _audit(
        AUDIT_CREATED,
        actor.pk,
        opportunity,
        workspace=scope.kind.value,
        subject=lead.owner_id,
        owner_id=str(lead.owner_id),
        lead_id=str(lead.pk),
        pipeline=pipeline.key,
        stage=stage.key,
        status=stage.category,
        via="conversion" if via_conversion else "create",
    )
    publish(
        events.OpportunityCreated(
            opportunity_id=opportunity.pk,
            lead_id=lead.pk,
            owner_id=lead.owner_id,
            actor_id=actor.pk,
            occurred_at=now,
            pipeline_id=pipeline.pk,
            stage_id=stage.pk,
            status=stage.category,
            via_conversion=via_conversion,
        )
    )
    _publish_outcome(opportunity, actor.pk, now)
    return opportunity


def _creation_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = validation.clean_fields(fields)
    errors = {f: ["This field is required."] for f in ("title", "value") if f not in cleaned}
    if errors:
        raise InvalidInputError(details=errors)
    return cleaned


def create_opportunity(
    *,
    actor: User,
    scope: AccessScope,
    lead_id: UUID,
    fields: Mapping[str, Any],
    pipeline_id: UUID | None = None,
    stage_id: UUID | None = None,
    idempotency_key: UUID | None = None,
) -> CreateResult:
    """Create an opportunity for a lead in `scope`. Its owner is the lead's owner; the
    pipeline defaults to the default pipeline and the stage to its first open stage."""
    authorize_write(actor, scope)
    cleaned = _creation_fields(fields)
    digest = idempotency.request_digest(
        scope.kind.value,
        str(scope.subject_user_id),
        str(lead_id),
        cleaned,
        str(pipeline_id),
        str(stage_id),
    )
    if idempotency_key is not None:
        earlier = idempotency.replayed_resource(
            actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest
        )
        if earlier is not None:
            return CreateResult(_replay(scope, earlier), replayed=True)
    try:
        with transaction.atomic():
            lead = lead_selectors.lock_lead(scope, lead_id)
            opportunity = _insert(
                actor=actor,
                scope=scope,
                lead=lead,
                cleaned=cleaned,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                via_conversion=False,
            )
            if idempotency_key is not None:
                idempotency.remember(
                    actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest, opportunity.pk
                )
    except IntegrityError as error:
        # A concurrent request with the same key won the race: return its result.
        if idempotency_key is not None and idempotency.is_duplicate_key(error):
            earlier = idempotency.replayed_resource(
                actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest
            )
            if earlier is not None:
                return CreateResult(_replay(scope, earlier), replayed=True)
        raise
    return CreateResult(selectors.opportunity_by_id(opportunity.pk), replayed=False)


# --- convert -----------------------------------------------------------------------------------
def convert_lead(
    *,
    actor: User,
    scope: AccessScope,
    lead_id: UUID,
    lead_version: int,
    fields: Mapping[str, Any],
    pipeline_id: UUID | None = None,
    stage_id: UUID | None = None,
    idempotency_key: UUID | None = None,
) -> ConversionResult:
    """Convert a lead: create its opportunity and move the lead to the Converted status, in
    one transaction (all or nothing). Converting twice is refused, so a double click can't
    create two opportunities even without an Idempotency-Key; with one, the retry replays
    the first conversion."""
    authorize_write(actor, scope)
    cleaned = _creation_fields(fields)
    digest = idempotency.request_digest(
        scope.kind.value,
        str(scope.subject_user_id),
        str(lead_id),
        lead_version,
        cleaned,
        str(pipeline_id),
        str(stage_id),
    )
    if idempotency_key is not None:
        earlier = idempotency.replayed_resource(
            actor.pk, IDEMPOTENT_CONVERT, idempotency_key, digest
        )
        if earlier is not None:
            return _replayed_conversion(scope, earlier)
    try:
        with transaction.atomic():
            # FOR UPDATE from the start: this transaction changes the lead's status, and a
            # lock is never upgraded mid-transaction.
            lead = lead_selectors.lock_lead(scope, lead_id, exclusive=True)
            if lead.version != lead_version:
                raise ConflictError()
            if lead.archived_at is not None:
                raise BusinessRuleViolation(LEAD_ARCHIVED)
            previous = lead_selectors.status_by_key(lead.status_id)
            # Converted means "has an opportunity of its owner's": a lead marked Converted
            # without one (Phase 2 allowed that) may be converted properly now.
            if (
                previous is not None
                and previous.category == StatusCategory.CONVERTED
                and has_owned_opportunity(lead.pk, lead.owner_id)
            ):
                raise BusinessRuleViolation(ALREADY_CONVERTED)
            converted = lead_selectors.first_active_status(StatusCategory.CONVERTED)
            if converted is None:
                raise BusinessRuleViolation(NO_CONVERTED_STATUS)
            opportunity = _insert(
                actor=actor,
                scope=scope,
                lead=lead,
                cleaned=cleaned,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                via_conversion=True,
            )
            # The leads module's own operation: status audit and LeadStatusChanged included.
            lead_services.change_status(
                actor=actor,
                scope=scope,
                lead_id=lead.pk,
                version=lead.version,
                status=converted.key,
            )
            audit.record(
                AUDIT_LEAD_CONVERTED,
                actor_id=actor.pk,
                target_type="lead",
                target_id=lead.pk,
                subject_user_id=lead.owner_id if lead.owner_id != actor.pk else None,
                metadata={
                    "workspace": scope.kind.value,
                    "opportunity_id": str(opportunity.pk),
                    "from_status": lead.status_id,
                    "to_status": converted.key,
                },
            )
            if idempotency_key is not None:
                idempotency.remember(
                    actor.pk, IDEMPOTENT_CONVERT, idempotency_key, digest, opportunity.pk
                )
    except IntegrityError as error:
        if idempotency_key is not None and idempotency.is_duplicate_key(error):
            earlier = idempotency.replayed_resource(
                actor.pk, IDEMPOTENT_CONVERT, idempotency_key, digest
            )
            if earlier is not None:
                return _replayed_conversion(scope, earlier)
        raise
    except (ConflictError, BusinessRuleViolation):
        # A duplicate of a request still in flight waited on the lead's lock, then found it
        # converted (or at a new version): if it was this very request, replay it (review).
        if idempotency_key is not None:
            earlier = idempotency.replayed_resource(
                actor.pk, IDEMPOTENT_CONVERT, idempotency_key, digest
            )
            if earlier is not None:
                return _replayed_conversion(scope, earlier)
        raise
    return ConversionResult(
        lead=lead_selectors.lead_by_id(lead_id),
        opportunity=selectors.opportunity_by_id(opportunity.pk),
        replayed=False,
    )


def _replayed_conversion(scope: AccessScope, opportunity_id: UUID) -> ConversionResult:
    opportunity = _replay(scope, opportunity_id)
    try:
        lead = lead_selectors.lead_detail(scope, opportunity.lead_id)
    except NotFoundError:
        raise ConflictError(ALREADY_CREATED_ELSEWHERE) from None
    return ConversionResult(lead=lead, opportunity=opportunity, replayed=True)


# --- edit --------------------------------------------------------------------------------------
def update_opportunity(
    *,
    actor: User,
    scope: AccessScope,
    opportunity_id: UUID,
    version: int,
    changes: Mapping[str, Any],
) -> Opportunity:
    """Change title, value, probability, expected close date, description or (while lost)
    lost reason. `probability: None` returns to the stage's default. Only fields whose
    value actually changes are written and audited (by name: no values, no text)."""
    authorize_write(actor, scope)
    cleaned = validation.clean_fields(changes)
    with transaction.atomic():
        opportunity, _ = _lock(scope, opportunity_id)
        _require_version(opportunity, version)
        _require_not_archived(opportunity)
        stage = opportunity.stage
        if "probability" in cleaned:
            probability, overridden = _probability_for(stage, cleaned.pop("probability"))
            cleaned["probability"] = probability
            cleaned["probability_overridden"] = overridden
        if cleaned.get("lost_reason") and opportunity.status != StageCategory.LOST:
            raise InvalidInputError(details={"lost_reason": [LOST_REASON_ONLY_WHEN_LOST]})
        changed = [f for f, v in cleaned.items() if getattr(opportunity, f) != v]
        if not changed:
            return selectors.opportunity_by_id(opportunity.pk)
        for field in changed:
            setattr(opportunity, field, cleaned[field])
        opportunity.version += 1
        opportunity.save(update_fields=[*changed, "version", "updated_at"])
        reported = sorted({"probability" if f == "probability_overridden" else f for f in changed})
        _audit(
            AUDIT_UPDATED,
            actor.pk,
            opportunity,
            workspace=scope.kind.value,
            subject=opportunity.owner_id,
            fields=reported,
        )
        publish(
            events.OpportunityUpdated(
                opportunity_id=opportunity.pk,
                lead_id=opportunity.lead_id,
                owner_id=opportunity.owner_id,
                actor_id=actor.pk,
                occurred_at=opportunity.updated_at,
                fields=tuple(reported),
            )
        )
    return selectors.opportunity_by_id(opportunity.pk)


# --- stage transitions -------------------------------------------------------------------------
def move_opportunity(
    *,
    actor: User,
    scope: AccessScope,
    opportunity_id: UUID,
    version: int,
    stage_id: UUID,
    lost_reason: str = "",
) -> Opportunity:
    """THE stage transition: every stage change (board drag and drop, the "Move to stage"
    menu, won, lost, reopen) goes through here.

    - open -> open: the probability becomes the new stage's default (an override belongs to
      the stage it was made in);
    - open -> won/lost: closed now (closed_at), probability 100/0, optional lost reason;
    - won/lost -> open: reopened; closed_at and lost reason cleared, the stage's default
      probability restored. An open opportunity is always owned by its lead's owner, so if
      the lead has changed hands since, reopening moves it to the lead's current owner,
      which only someone who can see that lead may do;
    - won/lost -> won/lost: refused (reopen first), so every close is a deliberate step.
    Each move appends one stage-history row and one audit event; moving to the current
    stage changes nothing (and succeeds)."""
    authorize_write(actor, scope)
    try:
        reason = validation.CLEANERS["lost_reason"](lost_reason)
    except ValueError as exc:
        raise InvalidInputError(details={"lost_reason": [str(exc)]}) from None
    with transaction.atomic():
        opportunity, lead = _lock(scope, opportunity_id)
        if opportunity.stage_id == stage_id:
            # A retry of a move that already happened: nothing to do. A lost reason sent
            # with it would be silently dropped, so it is refused instead (review).
            if reason:
                raise InvalidInputError(details={"lost_reason": [SAME_STAGE_LOST_REASON]})
            return selectors.opportunity_by_id(opportunity.pk)
        _require_version(opportunity, version)
        _require_not_archived(opportunity)
        target = Stage.objects.filter(
            pk=stage_id, pipeline_id=opportunity.pipeline_id, is_active=True
        ).first()
        if target is None:
            raise InvalidInputError(details={"stage": [INVALID_STAGE]})
        if reason and target.category != StageCategory.LOST:
            raise InvalidInputError(details={"lost_reason": [LOST_REASON_ONLY_WHEN_LOST]})
        source = opportunity.stage
        was_open = opportunity.is_open
        if not was_open and target.is_closed:
            raise BusinessRuleViolation(CLOSED_TO_CLOSED)
        if not was_open and lead.archived_at is not None:
            # Reopening adds open pipeline to the lead, like creating an opportunity would.
            raise BusinessRuleViolation(LEAD_ARCHIVED_REOPEN)

        previous_owner = opportunity.owner_id
        if not was_open:  # reopening: open again means owned by the lead's (active) owner
            if lead.owner_id != previous_owner and not scope.permits_owner(lead.owner_id):
                raise BusinessRuleViolation(REOPEN_ELSEWHERE)
            _require_assignable(lead.owner_id)
            opportunity.owner_id = lead.owner_id

        now = timezone.now()
        opportunity.stage = target
        opportunity.status = target.category
        opportunity.probability = target.probability
        opportunity.probability_overridden = False
        opportunity.closed_at = now if target.is_closed else None
        opportunity.lost_reason = reason if target.category == StageCategory.LOST else ""
        opportunity.version += 1
        opportunity.updated_at = now
        opportunity.save(
            update_fields=[
                "stage",
                "status",
                "owner",
                "probability",
                "probability_overridden",
                "closed_at",
                "lost_reason",
                "version",
                "updated_at",
            ]
        )
        _history(opportunity, actor.pk, from_stage=source, to_stage=target, at=now)
        if not was_open:
            action = AUDIT_REOPENED
        elif target.category == StageCategory.WON:
            action = AUDIT_WON
        elif target.category == StageCategory.LOST:
            action = AUDIT_LOST
        else:
            action = AUDIT_STAGE_CHANGED
        _audit(
            action,
            actor.pk,
            opportunity,
            workspace=scope.kind.value,
            subject=previous_owner,
            **{
                "from": source.key,
                "to": target.key,
                "from_status": source.category,
                "to_status": target.category,
            },
        )
        publish(
            events.OpportunityStageChanged(
                opportunity_id=opportunity.pk,
                lead_id=opportunity.lead_id,
                owner_id=opportunity.owner_id,
                actor_id=actor.pk,
                occurred_at=now,
                from_stage_id=source.pk,
                to_stage_id=target.pk,
                from_status=source.category,
                to_status=target.category,
            )
        )
        _publish_outcome(opportunity, actor.pk, now)
        if opportunity.owner_id != previous_owner:
            _owner_changed(
                opportunity,
                actor.pk,
                previous_owner,
                reason="reopened",
                at=now,
                workspace=scope.kind.value,
            )
    return selectors.opportunity_by_id(opportunity.pk)


def _owner_changed(
    opportunity: Opportunity,
    actor_id: UUID,
    previous_owner: UUID,
    *,
    reason: str,
    at: datetime,
    workspace: str | None,
) -> None:
    _audit(
        AUDIT_OWNER_CHANGED,
        actor_id,
        opportunity,
        workspace=workspace,
        subject=previous_owner,
        from_owner_id=str(previous_owner),
        to_owner_id=str(opportunity.owner_id),
        lead_id=str(opportunity.lead_id),
        reason=reason,
    )
    publish(
        events.OpportunityOwnerChanged(
            opportunity_id=opportunity.pk,
            lead_id=opportunity.lead_id,
            owner_id=opportunity.owner_id,
            actor_id=actor_id,
            occurred_at=at,
            from_owner_id=previous_owner,
            reason=reason,
        )
    )


# --- archive -----------------------------------------------------------------------------------
def archive_opportunity(
    *, actor: User, scope: AccessScope, opportunity_id: UUID, version: int
) -> Opportunity:
    """Hide an opportunity from boards, lists and pipeline totals. Nothing is deleted; its
    history stays and it can be restored. Closed is not archived: a won deal stays won."""
    authorize_write(actor, scope)
    with transaction.atomic():
        opportunity, _ = _lock(scope, opportunity_id)
        if opportunity.archived_at is not None:
            return selectors.opportunity_by_id(opportunity.pk)
        _require_version(opportunity, version)
        opportunity.archived_at = timezone.now()
        opportunity.version += 1
        opportunity.save(update_fields=["archived_at", "version", "updated_at"])
        _audit(
            AUDIT_ARCHIVED,
            actor.pk,
            opportunity,
            workspace=scope.kind.value,
            subject=opportunity.owner_id,
        )
        publish(
            events.OpportunityArchived(
                opportunity_id=opportunity.pk,
                lead_id=opportunity.lead_id,
                owner_id=opportunity.owner_id,
                actor_id=actor.pk,
                occurred_at=opportunity.archived_at,
            )
        )
    return selectors.opportunity_by_id(opportunity.pk)


def restore_opportunity(
    *, actor: User, scope: AccessScope, opportunity_id: UUID, version: int
) -> Opportunity:
    authorize_write(actor, scope)
    with transaction.atomic():
        opportunity, lead = _lock(scope, opportunity_id)
        if opportunity.archived_at is None:
            return selectors.opportunity_by_id(opportunity.pk)
        _require_version(opportunity, version)
        if lead.archived_at is not None:  # an archived lead is read-only, its pipeline too
            raise BusinessRuleViolation(LEAD_ARCHIVED_RESTORE)
        opportunity.archived_at = None
        opportunity.version += 1
        opportunity.save(update_fields=["archived_at", "version", "updated_at"])
        _audit(
            AUDIT_RESTORED,
            actor.pk,
            opportunity,
            workspace=scope.kind.value,
            subject=opportunity.owner_id,
        )
        publish(
            events.OpportunityRestored(
                opportunity_id=opportunity.pk,
                lead_id=opportunity.lead_id,
                owner_id=opportunity.owner_id,
                actor_id=actor.pk,
                occurred_at=opportunity.updated_at,
            )
        )
    return selectors.opportunity_by_id(opportunity.pk)


# --- reactions to lead changes (registered in subscribers.py) ----------------------------------
def follow_lead_owner(*, lead_id: UUID, to_owner_id: UUID, actor_id: UUID, at: datetime) -> int:
    """The lead was just reassigned (its row is locked by the reassignment): move its OPEN
    opportunities, archived ones included, to the new owner. Won and lost opportunities keep
    the owner who closed them (they record who did the work). Returns how many moved.
    Constant queries however many move: one lock, one update, one audit insert (review)."""
    moving = list(
        Opportunity.objects.select_for_update(no_key=True)
        .filter(lead_id=lead_id, status=StageCategory.OPEN)
        .exclude(owner_id=to_owner_id)
        .order_by("id")  # lock order for several opportunities
        .only("id", "lead_id", "owner_id")
    )
    if not moving:
        return 0
    Opportunity.objects.filter(pk__in=[o.pk for o in moving]).update(
        owner_id=to_owner_id, version=F("version") + 1, updated_at=at
    )
    audit.record_many(
        [
            audit.Entry(
                AUDIT_OWNER_CHANGED,
                actor_id,
                "opportunity",
                opportunity.pk,
                opportunity.owner_id if opportunity.owner_id != actor_id else None,
                {
                    "from_owner_id": str(opportunity.owner_id),
                    "to_owner_id": str(to_owner_id),
                    "lead_id": str(lead_id),
                    "reason": "lead_reassigned",
                },
            )
            for opportunity in moving
        ]
    )
    for opportunity in moving:
        publish(
            events.OpportunityOwnerChanged(
                opportunity_id=opportunity.pk,
                lead_id=lead_id,
                owner_id=to_owner_id,
                actor_id=actor_id,
                occurred_at=at,
                from_owner_id=opportunity.owner_id,
                reason="lead_reassigned",
            )
        )
    return len(moving)


def has_owned_opportunity(lead_id: UUID, owner_id: UUID) -> bool:
    """Does the lead have an opportunity (any status, archived or not) of this owner? The
    owner is the lead's current owner: opportunities someone else closed before the lead
    was reassigned are invisible to the owner and don't count (review: counting them let
    the owner learn that they exist)."""
    return Opportunity.objects.filter(lead_id=lead_id, owner_id=owner_id).exists()


def require_opportunity_for_conversion(lead_id: UUID, owner_id: UUID) -> None:
    """A lead may be Converted only once it has an opportunity of its owner's (created by
    the conversion itself, or earlier). Called inside the lead's status change, which it
    vetoes (422)."""
    if not has_owned_opportunity(lead_id, owner_id):
        raise BusinessRuleViolation(NEEDS_OPPORTUNITY)
