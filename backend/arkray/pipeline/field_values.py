"""Deleting the stored values of a removed custom field (privacy remediation P2-11;
docs/pipeline.md#custom-fields, docs/privacy.md#custom-fields).

Removing a field archives it and keeps its values, hidden: removal is reversible, and the
values may be needed (an audit, a report). When they mustn't be kept (a free-text field that
collected things it shouldn't have), whoever may configure the pipeline deletes them, on
purpose and only then:

- with the removal itself (`delete_removed_values` on replacing the fields), or later for
  an already removed field, confirming it by its name (`request`);
- the request is recorded (audit `pipeline.field_values_deletion_requested`) and queued, in
  the caller's transaction; a background job then records it in the erasure ledger
  (core.ledger: a restored backup deletes them again) and removes the field's key from
  every opportunity of the pipeline, in batches (`purge`), with one atomic
  `custom_fields - key` per row, so a concurrent edit of the deal's other values is never
  lost, and a row another transaction holds is taken in a later batch. The ledger entry is
  written by the job, after the request committed, never inside the request: a request
  that fails after its entry was written would otherwise leave an entry for a deletion
  nobody made, which a replay would then carry out (backend review P1);
- configuring a pipeline is not authority over every deal in it (final production audit
  SEC-1): a deal reassigned with its lead stays in its old owner's pipeline, out of that
  owner's sight. So a request is refused (403, nothing changed) while any deal holding the
  values lies outside the caller's scope, and the job only touches the deals of the
  workspace the request came from (`owner_id`, recorded in the job and its ledger entry):
  a deal reassigned between the request and the job keeps its value. Organisation-wide, an
  administrator deletes them on every deal;
- deals whose lead is under a legal hold keep the value (counted, `held`); run the request
  again once the hold is released;
- irreversible once the job has run. Before that the request can't be taken back either:
  restoring a removed field isn't possible (a new one is added instead), so nothing can
  bring the field back to life between request and deletion.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from django.db import connection, transaction

from arkray.audit import services as audit
from arkray.core import holds, ledger, outbox
from arkray.core.access import AccessScope
from arkray.core.errors import (
    BusinessRuleViolation,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from arkray.identity.models import User

from . import models as m

TOPIC_PURGE_VALUES = "pipeline.purge_custom_values"
AUDIT_REQUESTED = "pipeline.field_values_deletion_requested"
AUDIT_PURGED = "pipeline.field_values_deleted"
BATCH = 1000
MAX_IDLE_ROUNDS = 25
STILL_ACTIVE = "Remove the field first: only a removed field's values can be deleted."
CONFIRM = "Type the field's name to confirm: its values will be deleted for good."
OTHERS_VALUES = (
    "Some opportunities in this pipeline belong to other users, so their values can't be "
    "deleted from here. Remove the field without deleting its values, or ask an "
    "administrator to delete them from the organization-wide workspace."
)


def require_deletable(scope: AccessScope, pipeline_id: UUID, field_ids: list[UUID]) -> None:
    """Refuse (403) while a deal outside `scope` holds a value of these fields. Called with
    the pipeline locked FOR NO KEY UPDATE, which every writer of custom values waits for:
    no value appears meanwhile. Reassignments don't take that lock: the job's owner filter
    covers a deal that leaves the workspace after this check."""
    if scope.is_organization_wide or not field_ids:
        return
    holding = m.Opportunity.objects.filter(
        pipeline_id=pipeline_id, custom_fields__has_any_keys=[str(pk) for pk in field_ids]
    )
    if holding.exclude(owner_id=scope.subject_user_id).exists():
        raise PermissionDeniedError(OTHERS_VALUES)


def request_for(actor_id: UUID, field: m.CustomField, scope: AccessScope) -> None:
    """Record the request and queue the deletion (inside the caller's transaction, after
    require_deletable; the job writes the ledger entry once this has committed). Outside
    the organisation-wide workspace the job is limited to the workspace owner's deals."""
    owner_id = scope.subject_user_id
    audit.record(
        AUDIT_REQUESTED,
        actor_id=actor_id,
        target_type="custom_field",
        target_id=field.pk,
        subject_user_id=owner_id if owner_id not in (None, actor_id) else None,
        metadata={"workspace": scope.kind.value, "pipeline_id": str(field.pipeline_id)},
    )
    payload = {"field_id": str(field.pk)}
    if owner_id is not None:
        payload["owner_id"] = str(owner_id)
    outbox.enqueue(TOPIC_PURGE_VALUES, payload)


def request(
    *, actor: User, scope: AccessScope, pipeline_id: UUID, field_id: UUID, confirm_name: str
) -> None:
    """Delete the values of an already removed field (whoever may configure the
    pipeline, confirming the field by its name)."""
    from .configuration import _lock

    with transaction.atomic():
        pipeline = _lock(actor, scope, pipeline_id, None, exclusive=False)
        field = m.CustomField.objects.filter(pipeline=pipeline, pk=field_id).first()
        if field is None:
            raise NotFoundError()
        if field.is_active:
            raise BusinessRuleViolation(STILL_ACTIVE)
        if confirm_name.strip().casefold() != field.name.strip().casefold():
            raise InvalidInputError(details={"confirm_name": [CONFIRM]})
        require_deletable(scope, pipeline.pk, [field.pk])
        request_for(actor.pk, field, scope)


def purge(field_id: UUID, *, owner_id: UUID | None = None, replay: bool = False) -> dict[str, int]:
    """Remove the field's key from every opportunity holding it, or only from `owner_id`'s
    (the outbox job, and the erasure replay). Idempotent. A field active again (impossible
    today) is left alone."""
    field = m.CustomField.objects.filter(pk=field_id).first()
    if field is None or field.is_active:
        return {"rows": 0, "held": 0}
    key = str(field.pk)
    if not replay:
        # The removal has committed (this runs after it): from here a restored backup must
        # delete the values again, on the same deals. A retried job appends again, which
        # replay takes as already applied.
        with transaction.atomic():
            ledger.append("custom_values_deleted", field.pk, owner=owner_id)
    owners = [owner_id] if owner_id is not None else None
    removed = idle = 0
    while True:
        # Read again for every batch: a hold placed while the job runs stops it for that
        # lead from the next batch on (a batch is not serialised with placing a hold).
        held_leads = list(holds.held_leads())
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pipeline_opportunity SET custom_fields = custom_fields - %s"
                " WHERE id IN ("
                "  SELECT id FROM pipeline_opportunity"
                "  WHERE pipeline_id = %s AND custom_fields ? %s AND NOT (lead_id = ANY(%s))"
                "  AND (%s::uuid[] IS NULL OR owner_id = ANY(%s::uuid[]))"
                "  ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)",
                [key, field.pipeline_id, key, held_leads, owners, owners, BATCH],
            )
            done = cursor.rowcount
        removed += done
        if done:
            idle = 0
            continue
        if not holding(field, owner_id).exclude(lead_id__in=held_leads).exists():
            break
        # Rows another transaction holds: wait for them a little, then let the outbox retry.
        idle += 1
        if idle > MAX_IDLE_ROUNDS:
            raise RuntimeError("Some opportunities stayed locked; the job will retry.")
        time.sleep(0.2)
    held = holding(field, owner_id).filter(lead_id__in=held_leads).count()
    audit.record(
        AUDIT_PURGED,
        actor_id=None,
        target_type="custom_field",
        target_id=field.pk,
        subject_user_id=owner_id,
        metadata={"rows": removed, "held": held, **({"replay": True} if replay else {})},
    )
    return {"rows": removed, "held": held}


def holding(field: m.CustomField, owner_id: UUID | None) -> Any:
    """The deals of the field's pipeline that still hold a value for it (only `owner_id`'s
    when given)."""
    deals = m.Opportunity.objects.filter(
        pipeline_id=field.pipeline_id, custom_fields__has_key=str(field.pk)
    )
    return deals if owner_id is None else deals.filter(owner_id=owner_id)


@outbox.handler(TOPIC_PURGE_VALUES, queue="default")
def purge_values(payload: dict[str, Any]) -> None:
    # A job queued before the owner filter existed (no "owner_id") keeps its old reach.
    owner = payload.get("owner_id")
    purge(UUID(str(payload["field_id"])), owner_id=UUID(str(owner)) if owner else None)
