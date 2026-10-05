"""Opportunity use cases. Each runs in one transaction that also writes its stage history,
audit event and domain events, so the record, its history and every subscriber agree.

Authorization, applied here for every caller (API, conversion, future imports and tools):
- the opportunity (and its lead) are looked up through the caller's AccessScope: outside
  it, NotFoundError (404), indistinguishable from a record that doesn't exist;
- writing requires identity.workspaces.authorize_write (own workspace: crm.access_own;
  another user's or the organisation's: crm.manage_any), else 403;
- an opportunity's owner is always its lead's owner (who must be an active, assignable
  user). Since Leads left the UI (ADR-0027) the lead is the opportunity's hidden customer
  record: an opportunity created without one gets a new one, owned as a lead created in the
  same workspace would be (the creator in their own workspace, the user in theirs, a chosen
  active user organisation-wide: only that last is an assignment, crm.assign_any). Changing
  the owner afterwards is `reassign_opportunity`, which reassigns the customer record
  (crm.assign_any), so the customer's other open opportunities and current work move too.

Lock order (docs/pipeline.md#lock-order), the same in every operation, so two operations
can never wait for each other in a cycle:

    1. the lead          leads.selectors.lock_lead: FOR NO KEY UPDATE, or FOR UPDATE when
                         the operation changes the lead (conversion; reassignment in leads)
    2. opportunities     FOR NO KEY UPDATE OF the opportunity row only (never the joined
                         stage: shared configuration rows are only key-share-checked);
                         several at once only in ascending id order
    3. configuration     the pipeline row FOR SHARE (writers of custom values: creating, or
                         editing them) or FOR KEY SHARE (moves, restores), then the target
                         stage FOR KEY SHARE, each re-read under its lock. Configuration
                         changes (configuration.py) lock the pipeline and stage rows
                         exclusively and never lock a lead or an opportunity, so they can't
                         be part of a cycle; a stage being archived can't receive a move
                         that read it as active (docs/pipeline.md#configuration)
    4. user rows         FOR SHARE (identity.selectors.lock_assignable_user); leaf locks:
                         whoever holds one never waits for a lead or opportunity lock
                         held by a transaction that wants the user row exclusively
    5. inserts           stage history, negotiated prices, audit, idempotency records

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

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import idempotency
from arkray.core.access import AccessScope, ScopeKind
from arkray.core.context import current_support_session_id
from arkray.core.domain_events import publish
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from arkray.identity.models import User
from arkray.identity.policy import Capability, has_capability
from arkray.identity.selectors import lock_assignable_user
from arkray.identity.workspaces import authorize_write
from arkray.leads import selectors as lead_selectors
from arkray.leads import services as lead_services
from arkray.leads.models import NAME_MAX_LENGTH, Lead, StatusCategory

from . import events, selectors, validation
from .models import (
    NegotiationPrice,
    NegotiationSource,
    Opportunity,
    Pipeline,
    Stage,
    StageCategory,
    StageHistory,
    business_today,
)

AUDIT_CREATED = "opportunity.created"
AUDIT_UPDATED = "opportunity.updated"
AUDIT_STAGE_CHANGED = "opportunity.stage_changed"
AUDIT_WON = "opportunity.won"
AUDIT_LOST = "opportunity.lost"
AUDIT_REOPENED = "opportunity.reopened"
AUDIT_OWNER_CHANGED = "opportunity.owner_changed"
AUDIT_ARCHIVED = "opportunity.archived"
AUDIT_RESTORED = "opportunity.restored"
AUDIT_NEGOTIATED_PRICE = "opportunity.negotiated_price_recorded"
AUDIT_LEAD_CONVERTED = "lead.converted"

IDEMPOTENT_CREATE = "pipeline.create_opportunity"
IDEMPOTENT_CONVERT = "pipeline.convert_lead"

ARCHIVED_READ_ONLY = "This opportunity is archived. Restore it to make changes."
LEAD_ARCHIVED = "This lead is archived. Restore it before adding opportunities."
# Users see a lead as the opportunity's customer (ADR-0027): messages they can meet say so.
OWNER_NOT_ASSIGNABLE = (
    "This customer's owner is deactivated. Change the owner to an active user first."
)
CLOSED_TO_CLOSED = "This opportunity is closed. Reopen it by moving it to an open stage first."
REOPEN_ELSEWHERE = (
    "This opportunity's customer now belongs to someone else, so it can't be reopened here. "
    "Ask an administrator to reopen it."
)
# The same rule for an administrator in a user's workspace (Phase 6): they are the
# administrator, and the organisation-wide view (whose scope includes the new owner) can.
REOPEN_ELSEWHERE_DELEGATED = (
    "This opportunity's customer now belongs to someone else, so it can't be reopened in this "
    "user's workspace. Reopen it from the organisation-wide Pipeline."
)
CLOSED_PROBABILITY = (
    "Won opportunities are 100% and lost ones 0%; the probability can't be changed."
)
LOST_REASON_ONLY_WHEN_LOST = "Only lost opportunities have a lost reason."
SAME_STAGE_LOST_REASON = (
    "The opportunity is already in this stage. Edit it to change the lost reason."
)
LEAD_ARCHIVED_REOPEN = "This customer's record is archived, so its opportunities can't be reopened."
LEAD_ARCHIVED_RESTORE = (
    "This customer's record is archived, so its opportunities can't be restored."
)
CUSTOMER_REQUIRED = "Enter the customer name or the account name."
OWNER_FOLLOWS_LEAD = "An opportunity for an existing lead is owned by the lead's owner."
OWNER_IS_SELF_ONLY = "Opportunities you create in your own workspace are owned by you."
OWNER_IS_SUBJECT_ONLY = (
    "Opportunities created in this workspace belong to the user whose workspace it is."
)
OWNER_REQUIRED = "Choose who owns this opportunity."
CLOSED_OWNER = (
    "Won and lost opportunities keep the owner who closed them; only open ones change owner."
)
LEAD_ARCHIVED_OWNER = "This customer's record is archived, so the owner can't be changed."
ALREADY_CONVERTED = "This lead has already been converted."
NO_CONVERTED_STATUS = "No lead status for converted leads is configured."
NEEDS_OPPORTUNITY = (
    "A lead becomes Converted with its first opportunity. Use Convert to create one, or add "
    "an opportunity before changing the status."
)
INVALID_STAGE = "Choose a stage of this opportunity's pipeline."
INVALID_PIPELINE = "Choose an active pipeline."
PIPELINE_ARCHIVED = "This pipeline is archived. Restore the pipeline to make changes here."
STAGE_REMOVED_RESTORE = (
    "Its stage was removed from the pipeline, so it can't be restored. Its history is kept."
)
PRICE_REQUIRED = "Enter the negotiated price."
PRICE_ONLY_FOR_NEGOTIATION = "Only a move into a negotiation stage takes a negotiated price."
SAME_STAGE_PRICE = (
    "The opportunity is already in this stage. Record a new negotiated price instead."
)
NOT_IN_NEGOTIATION = (
    "Negotiated prices are recorded while the opportunity is in a negotiation stage."
)
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


# Shared locks on configuration rows (lock order step 3), each in the statement that reads
# the row: PostgreSQL re-checks the WHERE clause once it has the lock (READ COMMITTED), so a
# pipeline archived or a stage retired by a configuration change that committed meanwhile is
# not found, and nothing can change them (archive, custom fields) before we commit.
_SHARE_USABLE_PIPELINE = (
    "SELECT * FROM pipeline_pipeline WHERE id = %s AND is_active"
    " AND (owner_id IS NULL OR owner_id = %s) FOR SHARE"
)
_SHARE_PIPELINE = "SELECT 1 FROM pipeline_pipeline WHERE id = %s FOR SHARE"
_KEY_SHARE_ACTIVE_PIPELINE = (
    "SELECT 1 FROM pipeline_pipeline WHERE id = %s AND is_active FOR KEY SHARE"
)
_KEY_SHARE_ACTIVE_STAGE = (
    "SELECT * FROM pipeline_stage WHERE id = %s AND pipeline_id = %s AND is_active FOR KEY SHARE"
)


def _hold(statement: str, *params: Any) -> bool:
    """Run a locking read; whether it found the row."""
    with connection.cursor() as cursor:
        cursor.execute(statement, list(params))
        return cursor.fetchone() is not None


def _pipeline(pipeline_id: UUID | None, owner_id: UUID) -> Pipeline:
    """An active pipeline an opportunity of `owner_id` may be created in: a shared one, or
    the owner's own (the default when None). FOR SHARE: its custom fields can't change and it
    can't be archived until we commit. Unknown, archived and other users' pipelines are the
    same validation error."""
    if pipeline_id is None:
        pipeline_id = selectors.default_pipeline_id()
        if pipeline_id is None:
            raise InvalidInputError(details={"pipeline": [INVALID_PIPELINE]})
    found = list(Pipeline.objects.raw(_SHARE_USABLE_PIPELINE, [pipeline_id, owner_id]))
    if not found:
        raise InvalidInputError(details={"pipeline": [INVALID_PIPELINE]})
    return found[0]


def _locked_stage(stage_id: UUID, pipeline_id: UUID) -> Stage | None:
    """An active stage of the pipeline, FOR KEY SHARE: a stage being retired or retyped
    (configuration.py, FOR UPDATE) can't receive anything meanwhile."""
    found = list(Stage.objects.raw(_KEY_SHARE_ACTIVE_STAGE, [stage_id, pipeline_id]))
    return found[0] if found else None


def _stage(pipeline: Pipeline, stage_id: UUID | None) -> Stage:
    """An active stage of `pipeline`: the given one, or the first open stage."""
    if stage_id is None:
        stage_id = (
            Stage.objects.filter(pipeline=pipeline, is_active=True, category=StageCategory.OPEN)
            .order_by("position")
            .values_list("pk", flat=True)
            .first()
        )
        if stage_id is None:
            raise InvalidInputError(details={"stage": [INVALID_STAGE]})
    stage = _locked_stage(stage_id, pipeline.pk)
    if stage is None:
        raise InvalidInputError(details={"stage": [INVALID_STAGE]})
    return stage


def _price_for(stage: Stage, price: Decimal | None) -> Decimal | None:
    """Entering a negotiation stage requires the negotiated price; no other stage takes one."""
    if stage.is_negotiation and price is None:
        raise InvalidInputError(details={"negotiated_price": [PRICE_REQUIRED]})
    if not stage.is_negotiation and price is not None:
        raise InvalidInputError(details={"negotiated_price": [PRICE_ONLY_FOR_NEGOTIATION]})
    return price


def _clean_price(price: Any) -> Decimal | None:
    if price is None:
        return None
    try:
        return validation.clean_price(price)
    except ValueError as exc:
        raise InvalidInputError(details={"negotiated_price": [str(exc)]}) from None


def _record_price(
    opportunity: Opportunity,
    actor_id: UUID,
    *,
    price: Decimal,
    stage: Stage,
    source: NegotiationSource,
    at: datetime,
) -> None:
    """Append one negotiated price (never overwriting an earlier one) and keep the
    opportunity's copy of the latest. The opportunity row is locked by the caller."""
    NegotiationPrice.objects.create(
        opportunity=opportunity,
        price=price,
        currency=settings.CRM_CURRENCY,
        stage=stage,
        stage_name=stage.name,
        source=source,
        opportunity_version=opportunity.version,
        actor_id=actor_id,
        subject_user_id=opportunity.owner_id if opportunity.owner_id != actor_id else None,
        support_session_id=current_support_session_id(),
        occurred_at=at,
    )


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
    negotiated_price: Decimal | None,
    via_conversion: bool,
) -> Opportunity:
    """Create an opportunity for an already-locked lead (inside the caller's transaction)."""
    if lead.archived_at is not None:
        raise BusinessRuleViolation(LEAD_ARCHIVED)
    pipeline = _pipeline(pipeline_id, lead.owner_id)
    stage = _stage(pipeline, stage_id)
    _require_assignable(lead.owner_id)
    probability, overridden = _probability_for(stage, cleaned.get("probability"))
    lost_reason = cleaned.get("lost_reason", "")
    if lost_reason and stage.category != StageCategory.LOST:
        raise InvalidInputError(details={"lost_reason": [LOST_REASON_ONLY_WHEN_LOST]})
    price = _price_for(stage, negotiated_price)
    custom, _ = validation.clean_custom_values(
        selectors.active_fields(pipeline.pk),
        cleaned.get("custom_fields", {}),
        current={},
        creating=True,
    )
    # The customer defaults to the lead (an editable snapshot from now on).
    account_default, customer_default = lead_selectors.customer_names(lead.pk)
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
        opportunity_date=cleaned.get("opportunity_date") or business_today(),
        account_name=cleaned.get("account_name") or account_default,
        customer_name=cleaned.get("customer_name") or customer_default,
        contact_phone=cleaned.get("contact_phone", ""),
        contact_email=cleaned.get("contact_email", ""),
        address=cleaned.get("address", ""),
        instrument_name=cleaned.get("instrument_name", ""),
        work_load=cleaned.get("work_load", ""),
        custom_fields=custom,
        negotiated_price=price,
        negotiated_at=now if price is not None else None,
        created_by_id=actor.pk,
        created_at=now,
    )
    _history(opportunity, actor.pk, from_stage=None, to_stage=stage, at=now)
    if price is not None:
        _record_price(
            opportunity,
            actor.pk,
            price=price,
            stage=stage,
            source=NegotiationSource.CREATION,
            at=now,
        )
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


def _split_name(name: str) -> tuple[str, str]:
    """A customer name as a lead's first and last name (each at most 100 characters; the
    customer name may be 200), cut at a space so the lead's display name reads the same."""
    if len(name) <= NAME_MAX_LENGTH:
        return name, ""
    cut = name.rfind(" ", 1, NAME_MAX_LENGTH + 1)
    if cut <= 0:
        cut = NAME_MAX_LENGTH
    return name[:cut].rstrip(), name[cut:].strip()[:NAME_MAX_LENGTH]


def _customer_record(cleaned: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of the hidden customer record (a lead, ADR-0027) a new opportunity without
    a lead gets: its names, email and phone from the opportunity's customer details, already
    cleaned by the opportunity's own rules (which are the lead's: pipeline.validation)."""
    customer = cleaned.get("customer_name", "")
    account = cleaned.get("account_name", "")
    if not (customer or account):
        raise InvalidInputError(details={"customer_name": [CUSTOMER_REQUIRED]})
    first, last = _split_name(customer)
    record = {"first_name": first, "last_name": last, "organization_name": account}
    if cleaned.get("contact_email"):
        record["email"] = cleaned["contact_email"]
    if cleaned.get("contact_phone"):
        record["phone"] = cleaned["contact_phone"]
    return record


def _check_new_owner(actor: User, scope: AccessScope, owner_id: UUID | None) -> None:
    """The owner a request may name for a new customer record, checked up front so the
    answer speaks of opportunities. leads.services.create_lead applies the same rules (and
    the capability and active-user checks) under its locks."""
    if scope.kind is ScopeKind.ORGANIZATION:
        if owner_id is None:
            raise InvalidInputError(details={"owner": [OWNER_REQUIRED]})
        return
    if owner_id is None:
        return
    if scope.kind is ScopeKind.SELF and owner_id != actor.pk:
        raise InvalidInputError(details={"owner": [OWNER_IS_SELF_ONLY]})
    if scope.kind is ScopeKind.USER and owner_id != scope.subject_user_id:
        raise InvalidInputError(details={"owner": [OWNER_IS_SUBJECT_ONLY]})


def create_opportunity(
    *,
    actor: User,
    scope: AccessScope,
    lead_id: UUID | None,
    fields: Mapping[str, Any],
    owner_id: UUID | None = None,
    pipeline_id: UUID | None = None,
    stage_id: UUID | None = None,
    negotiated_price: Any = None,
    idempotency_key: UUID | None = None,
) -> CreateResult:
    """Create an opportunity in `scope`, for a lead of the scope or (`lead_id` None: the UI
    since ADR-0027) with a new hidden customer record made from its customer details, in the
    same transaction. Its owner is the lead's owner: for a new record, as for a lead created
    in `scope` (`owner_id` names it organisation-wide, where it is required). The pipeline
    defaults to the organisation's default pipeline and the stage to its first open stage.
    Created in a negotiation stage, it needs the negotiated price.

    Lock order: a new record's owner is share-locked (leads.services.create_lead) before the
    pipeline. That can't close a cycle: nothing locks a pipeline and then a user row in a mode
    that conflicts with a share lock (configuration.py only share-locks owners), and the new
    lead row is invisible to everyone else until this commits."""
    authorize_write(actor, scope)
    cleaned = _creation_fields(fields)
    price = _clean_price(negotiated_price)
    customer: dict[str, Any] | None = None
    if lead_id is not None and owner_id is not None:
        raise InvalidInputError(details={"owner": [OWNER_FOLLOWS_LEAD]})
    if lead_id is None:
        _check_new_owner(actor, scope, owner_id)
        customer = _customer_record(cleaned)
    digest = idempotency.request_digest(
        scope.kind.value,
        str(scope.subject_user_id),
        str(lead_id),
        str(owner_id),
        cleaned,
        str(pipeline_id),
        str(stage_id),
        str(price),
    )
    if idempotency_key is not None:
        earlier = idempotency.replayed_resource(
            actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest
        )
        if earlier is not None:
            return CreateResult(_replay(scope, earlier), replayed=True)
    try:
        with transaction.atomic():
            if customer is None:
                assert lead_id is not None  # noqa: S101 — no customer record only with a lead
                target = lead_id
            else:
                # The leads module's own operation: its audit event and LeadCreated included.
                target = lead_services.create_lead(
                    actor=actor, scope=scope, fields=customer, owner_id=owner_id
                ).lead.pk
            lead = lead_selectors.lock_lead(scope, target)
            opportunity = _insert(
                actor=actor,
                scope=scope,
                lead=lead,
                cleaned=cleaned,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                negotiated_price=price,
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
    negotiated_price: Any = None,
    idempotency_key: UUID | None = None,
) -> ConversionResult:
    """Convert a lead: create its opportunity and move the lead to the Converted status, in
    one transaction (all or nothing). Converting twice is refused, so a double click can't
    create two opportunities even without an Idempotency-Key; with one, the retry replays
    the first conversion."""
    authorize_write(actor, scope)
    cleaned = _creation_fields(fields)
    price = _clean_price(negotiated_price)
    digest = idempotency.request_digest(
        scope.kind.value,
        str(scope.subject_user_id),
        str(lead_id),
        lead_version,
        cleaned,
        str(pipeline_id),
        str(stage_id),
        str(price),
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
                negotiated_price=price,
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
    """Change the deal's fields (validation.EDITABLE_FIELDS): title, value, probability,
    dates, customer and instrument details, description, custom values or (while lost) the
    lost reason. `probability: None` returns to the stage's default; custom values merge
    (null clears one). Only fields whose value actually changes are written and audited (by
    name, and custom fields by id: no values, no text)."""
    authorize_write(actor, scope)
    cleaned = validation.clean_fields(changes)
    custom_changed: list[str] = []
    with transaction.atomic():
        opportunity, _ = _lock(scope, opportunity_id)
        _require_version(opportunity, version)
        _require_not_archived(opportunity)
        if "custom_fields" in cleaned:
            # FOR SHARE on the pipeline: its field definitions can't change meanwhile.
            _hold(_SHARE_PIPELINE, opportunity.pipeline_id)
            cleaned["custom_fields"], custom_changed = validation.clean_custom_values(
                selectors.active_fields(opportunity.pipeline_id),
                cleaned["custom_fields"],
                current=opportunity.custom_fields,
                creating=False,
            )
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
            **({"custom_fields": custom_changed} if custom_changed else {}),
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
    negotiated_price: Any = None,
) -> Opportunity:
    """THE stage transition: every stage change (board drag and drop, the "Move to stage"
    menu, won, lost, reopen) goes through here.

    - into a negotiation stage: the negotiated price is required (every time: leaving
      negotiation and coming back asks again) and appended to the price history; no other
      target takes one. No path can skip it: the API, the board and the detail page all
      call this.

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
    price = _clean_price(negotiated_price)
    with transaction.atomic():
        opportunity, lead = _lock(scope, opportunity_id)
        if opportunity.stage_id == stage_id:
            # A retry of a move that already happened: nothing to do. A lost reason or a
            # price sent with it would be silently dropped, so it is refused instead.
            if reason:
                raise InvalidInputError(details={"lost_reason": [SAME_STAGE_LOST_REASON]})
            if price is not None:
                raise InvalidInputError(details={"negotiated_price": [SAME_STAGE_PRICE]})
            return selectors.opportunity_by_id(opportunity.pk)
        _require_version(opportunity, version)
        _require_not_archived(opportunity)
        if not _hold(_KEY_SHARE_ACTIVE_PIPELINE, opportunity.pipeline_id):
            raise BusinessRuleViolation(PIPELINE_ARCHIVED)
        target = _locked_stage(stage_id, opportunity.pipeline_id)
        if target is None:
            raise InvalidInputError(details={"stage": [INVALID_STAGE]})
        if reason and target.category != StageCategory.LOST:
            raise InvalidInputError(details={"lost_reason": [LOST_REASON_ONLY_WHEN_LOST]})
        _price_for(target, price)
        source = opportunity.stage
        was_open = opportunity.is_open
        if not was_open and target.is_closed:
            raise BusinessRuleViolation(CLOSED_TO_CLOSED)

        previous_owner = opportunity.owner_id
        if not was_open:  # reopening: open again means owned by the lead's (active) owner
            # Whether the lead lives elsewhere is checked first: its archive state is then the
            # other workspace's business and must not be revealed (Phase 6 review, an oracle).
            if lead.owner_id != previous_owner and not scope.permits_owner(lead.owner_id):
                raise BusinessRuleViolation(
                    REOPEN_ELSEWHERE_DELEGATED if scope.is_delegated else REOPEN_ELSEWHERE
                )
            if lead.archived_at is not None:
                # Reopening adds open pipeline to the lead, like creating an opportunity would.
                raise BusinessRuleViolation(LEAD_ARCHIVED_REOPEN)
            _require_assignable(lead.owner_id)
            opportunity.owner_id = lead.owner_id

        now = timezone.now()
        opportunity.stage = target
        opportunity.status = target.category
        opportunity.probability = target.probability
        opportunity.probability_overridden = False
        opportunity.closed_at = now if target.is_closed else None
        opportunity.lost_reason = reason if target.category == StageCategory.LOST else ""
        if price is not None:
            opportunity.negotiated_price = price
            opportunity.negotiated_at = now
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
                "negotiated_price",
                "negotiated_at",
                "version",
                "updated_at",
            ]
        )
        _history(opportunity, actor.pk, from_stage=source, to_stage=target, at=now)
        if price is not None:
            _record_price(
                opportunity,
                actor.pk,
                price=price,
                stage=target,
                source=NegotiationSource.STAGE_ENTRY,
                at=now,
            )
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
                **({"negotiated_price_recorded": True} if price is not None else {}),
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


def record_negotiated_price(
    *,
    actor: User,
    scope: AccessScope,
    opportunity_id: UUID,
    version: int,
    price: Any,
) -> Opportunity:
    """Record a new negotiated price while the opportunity is in a negotiation stage (the
    negotiation goes on: ₹12,00,000 -> ₹11,00,000 -> ₹10,50,000). Appends to the history,
    never overwrites it. The same price as the latest changes nothing (a retry)."""
    authorize_write(actor, scope)
    amount = _clean_price(price)
    if amount is None:
        raise InvalidInputError(details={"price": [PRICE_REQUIRED]})
    with transaction.atomic():
        opportunity, _ = _lock(scope, opportunity_id)
        stage = opportunity.stage
        latest = (
            NegotiationPrice.objects.filter(opportunity_id=opportunity.pk)
            .order_by("-occurred_at", "-id")
            .values_list("price", "stage_id")
            .first()
        )
        if (
            latest == (amount, stage.pk)
            and stage.is_negotiation
            and opportunity.is_open
            and opportunity.archived_at is None
        ):
            # A retry of the price just recorded in this stage. (A stage retyped to
            # negotiation while the deal sat in it has no price of its own yet: recorded.)
            return selectors.opportunity_by_id(opportunity.pk)
        _require_version(opportunity, version)
        _require_not_archived(opportunity)
        if not stage.is_negotiation or not opportunity.is_open:
            raise BusinessRuleViolation(NOT_IN_NEGOTIATION)
        now = timezone.now()
        opportunity.negotiated_price = amount
        opportunity.negotiated_at = now
        opportunity.version += 1
        opportunity.updated_at = now
        opportunity.save(
            update_fields=["negotiated_price", "negotiated_at", "version", "updated_at"]
        )
        _record_price(
            opportunity,
            actor.pk,
            price=amount,
            stage=stage,
            source=NegotiationSource.REVISION,
            at=now,
        )
        _audit(
            AUDIT_NEGOTIATED_PRICE,
            actor.pk,
            opportunity,
            workspace=scope.kind.value,
            subject=opportunity.owner_id,
            stage=stage.key,
            source=NegotiationSource.REVISION.value,
        )
        publish(
            events.OpportunityUpdated(
                opportunity_id=opportunity.pk,
                lead_id=opportunity.lead_id,
                owner_id=opportunity.owner_id,
                actor_id=actor.pk,
                occurred_at=now,
                fields=("negotiated_price",),
            )
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
        # An archived lead is read-only, its pipeline too, in a workspace that holds the
        # lead. A closed deal its closer kept after the lead moved on is theirs to restore:
        # the lead's state belongs to the other workspace and isn't revealed (Phase 6 review).
        if lead.archived_at is not None and scope.permits_owner(lead.owner_id):
            raise BusinessRuleViolation(LEAD_ARCHIVED_RESTORE)
        if opportunity.is_open:
            # Restored open pipeline is current work again: like creating or reopening it,
            # never for a deactivated owner (Phase 6 review), never in an archived pipeline.
            if not _hold(_KEY_SHARE_ACTIVE_PIPELINE, opportunity.pipeline_id):
                raise BusinessRuleViolation(PIPELINE_ARCHIVED)
            _require_assignable(opportunity.owner_id)
        # A stage removed while only archived deals held it is retired: restoring one would
        # make it current in a stage no longer on the board (enhancement review). KEY SHARE
        # (after the pipeline's, as moves) keeps it from being removed until this commits.
        if not _hold(_KEY_SHARE_ACTIVE_STAGE, opportunity.stage_id, opportunity.pipeline_id):
            raise BusinessRuleViolation(STAGE_REMOVED_RESTORE)
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


# --- owner -------------------------------------------------------------------------------------
def reassign_opportunity(
    *, actor: User, scope: AccessScope, opportunity_id: UUID, version: int, owner_id: UUID
) -> Opportunity:
    """Hand an open opportunity to another salesperson: "Change owner" on the deal, where
    the Leads screen's Assign used to be (ADR-0027). An opportunity's owner is its customer
    record's (lead's) owner, so this reassigns the lead through the leads module's own
    operation (crm.assign_any, active owner, its audit and LeadReassigned): the customer's
    other open opportunities and current work move too, in this transaction. Won and lost
    opportunities keep the owner who closed them, so only an open one can be reassigned;
    archived ones are read-only. Choosing the current owner changes nothing.

    Lock order: the lead first (FOR UPDATE, as leads.services.reassign_lead takes it, so the
    lock is never upgraded), then the opportunity; the subscribers lock the rest in order."""
    if not has_capability(actor, Capability.CRM_ASSIGN_ANY):
        raise PermissionDeniedError()
    authorize_write(actor, scope)
    with transaction.atomic():
        found = (
            scope.apply(Opportunity.objects.filter(pk=opportunity_id))
            .values_list("lead_id", "status")
            .first()
        )
        if found is None:
            raise NotFoundError()
        lead_id, status = found
        if status != StageCategory.OPEN:
            # Checked before the lead is looked up: a closed deal's lead may have moved on
            # to someone outside this workspace, and that is not this caller's to learn.
            raise BusinessRuleViolation(CLOSED_OWNER)
        lead = lead_selectors.lock_lead(scope, lead_id, exclusive=True)
        opportunity = (
            scope.apply(
                Opportunity.objects.select_for_update(no_key=True, of=("self",)).filter(
                    pk=opportunity_id
                )
            )
            .only("id", "lead_id", "owner_id", "status", "version", "archived_at")
            .first()
        )
        if opportunity is None:
            raise NotFoundError()
        if opportunity.owner_id == owner_id:
            return selectors.opportunity_by_id(opportunity.pk)
        _require_version(opportunity, version)
        _require_not_archived(opportunity)
        if opportunity.status != StageCategory.OPEN:  # closed meanwhile
            raise BusinessRuleViolation(CLOSED_OWNER)
        if lead.archived_at is not None:
            raise BusinessRuleViolation(LEAD_ARCHIVED_OWNER)
        lead_services.reassign_lead(
            actor=actor, scope=scope, lead_id=lead.pk, version=lead.version, owner_id=owner_id
        )
    return selectors.opportunity_by_id(opportunity_id)


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
