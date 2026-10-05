"""Lead use cases. Each runs in one transaction that also writes its audit event and
publishes its domain event, so the record, its audit trail and any subscriber's work
(Phase 3+) always agree.

Authorization, applied here for every caller (API, future imports, Ask Arkray tools):
- the lead is looked up through the caller's AccessScope: outside it, NotFoundError (404),
  indistinguishable from a lead that doesn't exist;
- writing requires identity.workspaces.authorize_write (own workspace: crm.access_own;
  another user's workspace or the organisation: crm.manage_any), else 403;
- changing ownership additionally requires crm.assign_any, and the new owner must be an
  active user who works in a CRM workspace.

Concurrency (docs/leads.md#concurrency): every change locks the lead row, requires the
`version` the client last saw (409 if someone else changed it meanwhile) and increments it.
Requests that would change nothing (moving to the current status or owner, archiving an
archived lead) succeed without a new version, so retries are harmless. Archived leads are
read-only until restored (422).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import idempotency
from arkray.core.access import AccessScope, ScopeKind
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

from . import events, selectors, validation
from .models import Lead, LeadSource, LeadStatus

AUDIT_LEAD_CREATED = "lead.created"
AUDIT_LEAD_UPDATED = "lead.updated"
AUDIT_LEAD_STATUS_CHANGED = "lead.status_changed"
AUDIT_LEAD_REASSIGNED = "lead.reassigned"
AUDIT_LEAD_ARCHIVED = "lead.archived"
AUDIT_LEAD_RESTORED = "lead.restored"

IDEMPOTENT_CREATE = "leads.create"
ARCHIVED_READ_ONLY = "This lead is archived. Restore it to make changes."
OWNER_NOT_ASSIGNABLE = "Choose an active user."
# In a selected user's workspace nobody chooses the owner, so "choose" would mislead (Phase 6).
# Also met when creating an opportunity, which brings its own customer record (ADR-0027).
SUBJECT_NOT_ASSIGNABLE = (
    "This user's account isn't active, so nothing new can be added to their workspace."
)
OWNER_IS_SELF_ONLY = "Leads you create in your own workspace are owned by you."
OWNER_IS_SUBJECT_ONLY = "Leads created in this workspace belong to the user whose workspace it is."
OWNER_REQUIRED = "Choose who owns this lead."
INVALID_STATUS = "Choose a status from the list."
INVALID_SOURCE = "Choose a source from the list."
ALREADY_CREATED_ELSEWHERE = (
    "This lead was already created by an earlier request and has since left this workspace."
)


@dataclass(frozen=True, slots=True)
class CreateResult:
    lead: Lead
    replayed: bool  # an earlier request with the same Idempotency-Key created it


# --- helpers ---------------------------------------------------------------------------------
def _replay(scope: AccessScope, lead_id: UUID) -> CreateResult:
    """The lead an earlier request with this key created. If it has since left the workspace
    (reassigned), say so plainly: a 404 would claim the create never happened."""
    try:
        return CreateResult(selectors.lead_detail(scope, lead_id), replayed=True)
    except NotFoundError:
        raise ConflictError(ALREADY_CREATED_ELSEWHERE) from None


def _audit(
    action: str, actor: User, scope: AccessScope, lead: Lead, subject: UUID, **metadata: Any
) -> None:
    audit.record(
        action,
        actor_id=actor.pk,
        target_type="lead",
        target_id=lead.pk,
        subject_user_id=subject if subject != actor.pk else None,
        metadata={"workspace": scope.kind.value, **metadata},
    )


def _lock(scope: AccessScope, lead_id: UUID) -> Lead:
    lead = scope.apply(Lead.objects.select_for_update().filter(pk=lead_id)).first()
    if lead is None:
        raise NotFoundError()
    return lead


def _require_version(lead: Lead, version: int) -> None:
    if lead.version != version:
        raise ConflictError()


def _require_not_archived(lead: Lead) -> None:
    if lead.archived_at is not None:
        raise BusinessRuleViolation(ARCHIVED_READ_ONLY)


def _active_status(key: str) -> LeadStatus:
    status = LeadStatus.objects.filter(key=key, is_active=True).first()
    if status is None:
        raise InvalidInputError(details={"status": [INVALID_STATUS]})
    return status


def _require_active_source(key: str | None) -> None:
    if key is not None and not LeadSource.objects.filter(key=key, is_active=True).exists():
        raise InvalidInputError(details={"source": [INVALID_SOURCE]})


def _owner_for_new_lead(actor: User, scope: AccessScope, requested: UUID | None) -> UUID:
    """Who owns a lead created in `scope`. A payload can never pick an owner the actor may
    not assign: in their own workspace everyone creates leads for themselves; creating a
    lead for someone else is an assignment (crm.assign_any) to an active user."""
    if scope.kind is ScopeKind.SELF:
        if requested is not None and requested != actor.pk:
            raise InvalidInputError(details={"owner": [OWNER_IS_SELF_ONLY]})
        # Share-locked like any other owner: a deactivation committing meanwhile waits for
        # this lead, or this sees it (whole-software audit: the lead landed afterwards).
        if not lock_assignable_user(actor.pk):
            raise PermissionDeniedError()
        return actor.pk
    if not has_capability(actor, Capability.CRM_ASSIGN_ANY):
        raise PermissionDeniedError()
    if scope.kind is ScopeKind.USER:
        subject = scope.subject_user_id
        assert subject is not None  # noqa: S101 — AccessScope invariant for USER scopes
        if requested is not None and requested != subject:
            raise InvalidInputError(details={"owner": [OWNER_IS_SUBJECT_ONLY]})
        if not lock_assignable_user(subject):
            raise InvalidInputError(details={"owner": [SUBJECT_NOT_ASSIGNABLE]})
        return subject
    if requested is None:
        raise InvalidInputError(details={"owner": [OWNER_REQUIRED]})
    if not lock_assignable_user(requested):
        raise InvalidInputError(details={"owner": [OWNER_NOT_ASSIGNABLE]})
    return requested


# --- create ----------------------------------------------------------------------------------
def create_lead(
    *,
    actor: User,
    scope: AccessScope,
    fields: Mapping[str, Any],
    status: str | None = None,
    owner_id: UUID | None = None,
    idempotency_key: UUID | None = None,
) -> CreateResult:
    authorize_write(actor, scope)
    cleaned = validation.clean_fields(fields)
    validation.require_identity(
        cleaned.get("first_name", ""),
        cleaned.get("last_name", ""),
        cleaned.get("organization_name", ""),
    )
    digest = idempotency.request_digest(
        scope.kind.value, str(scope.subject_user_id), cleaned, status, str(owner_id)
    )
    if idempotency_key is not None:
        earlier = idempotency.replayed_resource(
            actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest
        )
        if earlier is not None:
            return _replay(scope, earlier)

    try:
        with transaction.atomic():
            owner = _owner_for_new_lead(actor, scope, owner_id)
            initial = _active_status(status) if status is not None else selectors.default_status()
            source = cleaned.pop("source", None)
            _require_active_source(source)
            lead = Lead(
                **cleaned,
                source_id=source,
                status_id=initial.key,
                owner_id=owner,
                created_by_id=actor.pk,
            )
            lead.save()
            _audit(
                AUDIT_LEAD_CREATED,
                actor,
                scope,
                lead,
                owner,
                owner_id=str(owner),
                status=initial.key,
            )
            publish(
                events.LeadCreated(
                    lead_id=lead.pk,
                    owner_id=owner,
                    actor_id=actor.pk,
                    occurred_at=lead.created_at,
                    status=initial.key,
                )
            )
            if idempotency_key is not None:
                idempotency.remember(actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest, lead.pk)
    except IntegrityError as error:
        # A concurrent request with the same key won the race: return its lead instead.
        if idempotency_key is not None and idempotency.is_duplicate_key(error):
            earlier = idempotency.replayed_resource(
                actor.pk, IDEMPOTENT_CREATE, idempotency_key, digest
            )
            if earlier is not None:
                return _replay(scope, earlier)
        raise
    return CreateResult(selectors.lead_by_id(lead.pk), replayed=False)


# --- edit --------------------------------------------------------------------------------------
def update_lead(
    *, actor: User, scope: AccessScope, lead_id: UUID, version: int, changes: Mapping[str, Any]
) -> Lead:
    """Change profile fields (validation.EDITABLE_FIELDS). Only fields whose value actually
    changes are written and audited (by name; values are contact data and stay out of the
    audit log)."""
    authorize_write(actor, scope)
    cleaned = validation.clean_fields(changes)
    with transaction.atomic():
        lead = _lock(scope, lead_id)
        _require_version(lead, version)
        _require_not_archived(lead)
        changed: list[str] = []
        for field, value in cleaned.items():
            attname = "source_id" if field == "source" else field
            if getattr(lead, attname) == value:
                continue
            if field == "source":
                _require_active_source(value)
            setattr(lead, attname, value)
            changed.append(field)
        if not changed:
            return selectors.lead_by_id(lead.pk)
        validation.require_identity(lead.first_name, lead.last_name, lead.organization_name)
        lead.version += 1
        lead.save(update_fields=[*changed, "version", "updated_at"])
        _audit(AUDIT_LEAD_UPDATED, actor, scope, lead, lead.owner_id, fields=sorted(changed))
        publish(
            events.LeadUpdated(
                lead_id=lead.pk,
                owner_id=lead.owner_id,
                actor_id=actor.pk,
                occurred_at=lead.updated_at,
                fields=tuple(sorted(changed)),
            )
        )
    return selectors.lead_by_id(lead.pk)


def change_status(
    *, actor: User, scope: AccessScope, lead_id: UUID, version: int, status: str
) -> Lead:
    """Move a lead to another *active* status. In Phase 2 every move between active statuses
    is allowed (qualification is the salesperson's judgement and mistakes must be fixable);
    rules such as "converting needs an opportunity" attach here in Phase 3. A lead may stay in
    a status that has since been retired, but nothing can move into one."""
    authorize_write(actor, scope)
    with transaction.atomic():
        lead = _lock(scope, lead_id)
        if lead.status_id == status:
            return selectors.lead_by_id(lead.pk)
        _require_version(lead, version)
        _require_not_archived(lead)
        target = _active_status(status)
        previous = LeadStatus.objects.get(key=lead.status_id)
        lead.status_id = target.key
        lead.version += 1
        lead.save(update_fields=["status", "version", "updated_at"])
        _audit(
            AUDIT_LEAD_STATUS_CHANGED,
            actor,
            scope,
            lead,
            lead.owner_id,
            **{"from": previous.key, "to": target.key},
        )
        publish(
            events.LeadStatusChanged(
                lead_id=lead.pk,
                owner_id=lead.owner_id,
                actor_id=actor.pk,
                occurred_at=lead.updated_at,
                from_status=previous.key,
                to_status=target.key,
                from_category=previous.category,
                to_category=target.category,
            )
        )
    return selectors.lead_by_id(lead.pk)


def reassign_lead(
    *, actor: User, scope: AccessScope, lead_id: UUID, version: int, owner_id: UUID
) -> Lead:
    """Hand a lead to another salesperson: the explicit ownership operation.

    Ownership never changes through a generic edit. Subscribers to LeadReassigned (Phase 3/4)
    move open opportunities and current activities in this same transaction; who did what
    in the past (created_by, audit actors) is never rewritten.
    """
    if not has_capability(actor, Capability.CRM_ASSIGN_ANY):
        raise PermissionDeniedError()
    authorize_write(actor, scope)
    with transaction.atomic():
        lead = _lock(scope, lead_id)
        if lead.owner_id == owner_id:
            return selectors.lead_by_id(lead.pk)
        _require_version(lead, version)
        _require_not_archived(lead)
        if not lock_assignable_user(owner_id):
            raise InvalidInputError(details={"owner": [OWNER_NOT_ASSIGNABLE]})
        previous = lead.owner_id
        lead.owner_id = owner_id
        lead.version += 1
        lead.save(update_fields=["owner", "version", "updated_at"])
        _audit(
            AUDIT_LEAD_REASSIGNED,
            actor,
            scope,
            lead,
            # Whose workspace it concerned: the previous owner, or (when an admin hands over
            # their own lead) the new one, so it shows in that user's audit trail either way.
            previous if previous != actor.pk else owner_id,
            from_owner_id=str(previous),
            to_owner_id=str(owner_id),
        )
        publish(
            events.LeadReassigned(
                lead_id=lead.pk,
                owner_id=owner_id,
                actor_id=actor.pk,
                occurred_at=lead.updated_at,
                from_owner_id=previous,
            )
        )
    return selectors.lead_by_id(lead.pk)


# --- archive -----------------------------------------------------------------------------------
def archive_lead(*, actor: User, scope: AccessScope, lead_id: UUID, version: int) -> Lead:
    """Hide a lead from the default list. Nothing is deleted: its data, history and
    relationships stay intact, and it can be restored."""
    authorize_write(actor, scope)
    with transaction.atomic():
        lead = _lock(scope, lead_id)
        if lead.archived_at is not None:
            return selectors.lead_by_id(lead.pk)
        _require_version(lead, version)
        lead.archived_at = timezone.now()
        lead.version += 1
        lead.save(update_fields=["archived_at", "version", "updated_at"])
        _audit(AUDIT_LEAD_ARCHIVED, actor, scope, lead, lead.owner_id)
        publish(
            events.LeadArchived(
                lead_id=lead.pk,
                owner_id=lead.owner_id,
                actor_id=actor.pk,
                occurred_at=lead.archived_at,
            )
        )
    return selectors.lead_by_id(lead.pk)


def record_contact(
    *,
    actor_id: UUID,
    lead_id: UUID,
    contacted_at: datetime,
    via: str,
    via_id: UUID,
    workspace: str,
) -> bool:
    """A completed customer interaction took place at `contacted_at` (Phase 4: a completed
    meeting, at its start time): advance the lead's last contact to it, never backwards.
    Completing an older interaction after a newer one leaves the newer time in place (MAX
    semantics, docs/activities.md#last-contacted). Returns whether the lead changed.

    Called by the module that owns the interaction, inside its transaction, after it has
    authorised the actor and locked the lead (lock order: the lead comes first). The lead
    is locked again here in the same mode, FOR NO KEY UPDATE: a no-op for that caller (never
    an upgrade: last_contacted_at is not a key column), and the guarantee for any other.
    Like every lead change it bumps the version (an edit form opened earlier gets a 409
    instead of silently overwriting the new contact time), is audited by field name and
    publishes LeadUpdated. Archived leads are not refused: the interaction did happen.
    """
    lead = (
        Lead.objects.select_for_update(no_key=True)
        .only("id", "owner_id", "last_contacted_at")
        .get(pk=lead_id)
    )
    if lead.last_contacted_at is not None and lead.last_contacted_at >= contacted_at:
        return False
    now = timezone.now()
    # A queryset update: Lead.save() would recompute the (deferred) phone keys.
    Lead.objects.filter(pk=lead_id).update(
        last_contacted_at=contacted_at, version=F("version") + 1, updated_at=now
    )
    audit.record(
        AUDIT_LEAD_UPDATED,
        actor_id=actor_id,
        target_type="lead",
        target_id=lead_id,
        subject_user_id=lead.owner_id if lead.owner_id != actor_id else None,
        metadata={
            "workspace": workspace,
            "fields": ["last_contacted_at"],
            "via": via,
            "via_id": str(via_id),
        },
    )
    publish(
        events.LeadUpdated(
            lead_id=lead_id,
            owner_id=lead.owner_id,
            actor_id=actor_id,
            occurred_at=now,
            fields=("last_contacted_at",),
        )
    )
    return True


def restore_lead(*, actor: User, scope: AccessScope, lead_id: UUID, version: int) -> Lead:
    authorize_write(actor, scope)
    with transaction.atomic():
        lead = _lock(scope, lead_id)
        if lead.archived_at is None:
            return selectors.lead_by_id(lead.pk)
        _require_version(lead, version)
        lead.archived_at = None
        lead.version += 1
        lead.save(update_fields=["archived_at", "version", "updated_at"])
        _audit(AUDIT_LEAD_RESTORED, actor, scope, lead, lead.owner_id)
        publish(
            events.LeadRestored(
                lead_id=lead.pk,
                owner_id=lead.owner_id,
                actor_id=actor.pk,
                occurred_at=lead.updated_at,
            )
        )
    return selectors.lead_by_id(lead.pk)
