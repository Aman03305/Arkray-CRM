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
from arkray.core.errors import BusinessRuleViolation, InvalidInputError, NotFoundError
from arkray.identity.models import User

from . import models as m

TOPIC_PURGE_VALUES = "pipeline.purge_custom_values"
AUDIT_REQUESTED = "pipeline.field_values_deletion_requested"
AUDIT_PURGED = "pipeline.field_values_deleted"
BATCH = 1000
MAX_IDLE_ROUNDS = 25
STILL_ACTIVE = "Remove the field first: only a removed field's values can be deleted."
CONFIRM = "Type the field's name to confirm: its values will be deleted for good."


def request_for(actor_id: UUID, field: m.CustomField, scope: AccessScope) -> None:
    """Record the request and queue the deletion (inside the caller's transaction; the
    job writes the ledger entry once this has committed)."""
    audit.record(
        AUDIT_REQUESTED,
        actor_id=actor_id,
        target_type="custom_field",
        target_id=field.pk,
        metadata={"workspace": scope.kind.value, "pipeline_id": str(field.pipeline_id)},
    )
    outbox.enqueue(TOPIC_PURGE_VALUES, {"field_id": str(field.pk)})


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
        request_for(actor.pk, field, scope)


def purge(field_id: UUID, *, replay: bool = False) -> dict[str, int]:
    """Remove the field's key from every opportunity holding it (the outbox job, and the
    erasure replay). Idempotent. A field active again (impossible today) is left alone."""
    field = m.CustomField.objects.filter(pk=field_id).first()
    if field is None or field.is_active:
        return {"rows": 0, "held": 0}
    key = str(field.pk)
    if not replay:
        # The removal has committed (this runs after it): from here a restored backup must
        # delete the values again. A retried job appends again, which replay takes as
        # already applied.
        with transaction.atomic():
            ledger.append("custom_values_deleted", field.pk)
    held_leads = list(holds.held_leads())
    removed = idle = 0
    while True:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pipeline_opportunity SET custom_fields = custom_fields - %s"
                " WHERE id IN ("
                "  SELECT id FROM pipeline_opportunity"
                "  WHERE pipeline_id = %s AND custom_fields ? %s AND NOT (lead_id = ANY(%s))"
                "  ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)",
                [key, field.pipeline_id, key, held_leads, BATCH],
            )
            done = cursor.rowcount
        removed += done
        if done:
            idle = 0
            continue
        remaining = m.Opportunity.objects.filter(
            pipeline_id=field.pipeline_id, custom_fields__has_key=key
        ).exclude(lead_id__in=held_leads)
        if not remaining.exists():
            break
        # Rows another transaction holds: wait for them a little, then let the outbox retry.
        idle += 1
        if idle > MAX_IDLE_ROUNDS:
            raise RuntimeError("Some opportunities stayed locked; the job will retry.")
        time.sleep(0.2)
    held = m.Opportunity.objects.filter(
        pipeline_id=field.pipeline_id, custom_fields__has_key=key, lead_id__in=held_leads
    ).count()
    audit.record(
        AUDIT_PURGED,
        actor_id=None,
        target_type="custom_field",
        target_id=field.pk,
        metadata={"rows": removed, "held": held, **({"replay": True} if replay else {})},
    )
    return {"rows": removed, "held": held}


@outbox.handler(TOPIC_PURGE_VALUES, queue="default")
def purge_values(payload: dict[str, Any]) -> None:
    purge(UUID(str(payload["field_id"])))
