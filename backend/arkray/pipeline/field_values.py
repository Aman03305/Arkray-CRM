"""Deleting the stored values of a removed custom field (privacy remediation P2-11;
docs/pipeline.md#custom-fields, docs/privacy.md#custom-fields).

Removing a field archives it and keeps its values, hidden: removal is reversible, and the
values may be needed (an audit, a report). When they mustn't be kept (a free-text field that
collected things it shouldn't have), whoever may configure the pipeline deletes them, on
purpose and only then:

- with the removal itself (`delete_removed_values` on replacing the fields), or later for
  an already removed field, confirming it by its name (`request`);
- the request is recorded (audit `pipeline.field_values_deletion_requested`) and queued, in
  the caller's transaction; a background job then removes the field's key from every
  opportunity of the pipeline, in batches (`purge`), with one atomic
  `custom_fields - key` per row, so a concurrent edit of the deal's other values is never
  lost, and a row another transaction holds is taken in a later batch. The ledger entry is
  written by the job, after the request committed, never inside the request: a request
  that fails after its entry was written would otherwise leave an entry for a deletion
  nobody made, which a replay would then carry out (backend review P1);
- recorded in the erasure ledger (core.ledger: a restored backup deletes them again) once,
  and only when a value is really removed (security review M3: the ledger is write-once,
  and repeating the request for the same field appended an entry each time, values or
  not, so any user could grow it without limit): a request finding no deal of its
  workspace still holding a value queues nothing, requests queued before the job runs
  share it (`dedupe_key`: the field and the workspace), and the job appends its entry in
  the transaction of its first batch that removes a value, last, just before that batch
  commits, as an erasure does. So an entry exists exactly when values were removed (a
  failed append rolls the batch back), a purge that finds nothing appends nothing, and a
  retried job appends again only if it removes more. The answer to the request is the
  same either way (202);
- one request, one job, one entry (review round 1, LEDGER-2 a: removing thirty fields with
  their values queued thirty jobs and appended thirty entries, about 9,000 entries a
  minute at the API's rate): the job deletes every field the request named and its entry
  names them all (`purge_fields`); and a person may ask for at most
  MAX_VALUE_DELETIONS_PER_USER_PER_DAY fields' deletions in any 24 hours (429 with
  Retry-After, nothing changed: the removal asking for it is refused as a whole);
- configuring a pipeline is not authority over every deal in it (final production audit
  SEC-1): a deal reassigned with its lead stays in its old owner's pipeline, out of that
  owner's sight. So a request is refused (403, nothing changed) while any deal holding the
  values lies outside the caller's scope, and the job only touches the deals of the
  workspace the request came from (`owner_id`, recorded in the job and its ledger entry):
  a deal reassigned between the request and the job keeps its value. Organisation-wide, an
  administrator deletes them on every deal. A team administrator's team workspace (scope
  TEAM, ADR-0035) has no single owner and is never organisation-wide: refused (403) here
  too, not only by configuration's checks, so no path can turn "no subject" into "every
  deal";
- deals whose lead is under a legal hold keep the value (counted, `held`); run the request
  again once the hold is released. Each batch reads the holds under the hold barrier
  (core.holds), so a hold placed while the job runs is honoured from the batch after it
  committed, and one placed during a batch waits for that batch;
- irreversible once the job has run. Before that the request can't be taken back either:
  restoring a removed field isn't possible (a new one is added instead), so nothing can
  bring the field back to life between request and deletion.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import connection, transaction
from django.utils import timezone

from arkray.audit import services as audit
from arkray.audit.models import AuditEvent
from arkray.core import holds, ledger, outbox
from arkray.core.access import AccessScope
from arkray.core.errors import (
    BusinessRuleViolation,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)
from arkray.identity.models import User

from . import models as m

TOPIC_PURGE_VALUES = "pipeline.purge_custom_values"
AUDIT_REQUESTED = "pipeline.field_values_deletion_requested"
AUDIT_PURGED = "pipeline.field_values_deleted"
BATCH = 1000
MAX_IDLE_ROUNDS = 25
# Fields whose stored values one person may ask to delete in any 24 hours, the removals that
# delete them included (review round 1, LEDGER-2 a: each deletion writes the erasure ledger
# for good). An owner decision; 0 turns it off.
MAX_VALUE_DELETIONS_PER_USER_PER_DAY = 100
DAY = timedelta(days=1)
_BUDGET_LOCK = 0x41524B56  # "ARKV": one person's value-deletion budget
BUDGET_REACHED = (
    "You have asked to delete the stored values of {limit} fields in the last 24 hours, the "
    "most allowed. Try again later, or ask an administrator."
)
STILL_ACTIVE = "Remove the field first: only a removed field's values can be deleted."
CONFIRM = "Type the field's name to confirm: its values will be deleted for good."
OTHERS_VALUES = (
    "Some opportunities in this pipeline belong to other users, so their values can't be "
    "deleted from here. Remove the field without deleting its values, or ask an "
    "administrator to delete them from the organization-wide workspace."
)
# Removing a field hides its values on every deal of the pipeline (deep security audit L-4).
OTHERS_VALUES_HIDDEN = (
    "Some opportunities in this pipeline belong to other users and hold values of this field, "
    "so it can't be removed from here: they would no longer see them. Ask an administrator to "
    "remove it from the organization-wide workspace."
)


def require_deletable(
    scope: AccessScope, pipeline_id: UUID, field_ids: list[UUID], message: str = OTHERS_VALUES
) -> None:
    """Refuse (403) while a deal outside `scope` holds a value of these fields. Called with
    the pipeline locked FOR NO KEY UPDATE, which every writer of custom values waits for:
    no value appears meanwhile. So does a reassignment (FOR SHARE on the personal pipelines
    holding the lead's deals, before it commits: services.follow_lead_owner; deep security
    audit L-4, R137): it either took that lock first, and has committed when this reads the
    owners, or waits until the caller's change has committed. The deletion job's owner
    filter still covers a deal that leaves the workspace before the job runs. Also for
    removing fields (`message`: OTHERS_VALUES_HIDDEN): a removed field's values are hidden
    from everyone.

    Outside the scope also means a deal the caller still owns whose customer they may not
    see (a closed deal kept after its lead was reassigned: pipeline.customer). Its values are
    the customer's, which they can't write either (deep security audit M-2; review P3-1).

    A team's workspace is refused outright (module docstring; configuration refuses it
    first)."""
    _refuse_team(scope)
    if scope.is_organization_wide or not field_ids:
        return
    holding = m.Opportunity.objects.filter(
        pipeline_id=pipeline_id, custom_fields__has_any_keys=[str(pk) for pk in field_ids]
    )
    subject = scope.subject_user_id
    if holding.exclude(owner_id=subject, lead__owner_id=subject).exists():
        raise PermissionDeniedError(message)


def _refuse_team(scope: AccessScope) -> None:
    """A team's workspace (ADR-0035) deletes nothing: its scope has no single owner to limit
    the job to, and is never the organisation (which the job's "no owner" means)."""
    if scope.is_team:
        from .configuration import TEAM_CONFIGURES_NOTHING

        raise PermissionDeniedError(TEAM_CONFIGURES_NOTHING)


def request_for(actor_id: UUID, field: m.CustomField, scope: AccessScope) -> None:
    """One field's request (`request_for_fields`)."""
    request_for_fields(actor_id, [field], scope)


def request_for_fields(actor_id: UUID, fields: Sequence[m.CustomField], scope: AccessScope) -> None:
    """Record the request for each field and queue ONE deletion for them all (inside the
    caller's transaction, after require_deletable; the job writes the ledger entry once this
    has committed). Outside the organisation-wide workspace the job is limited to the
    workspace owner's deals. Only the fields a deal of the workspace still holds a value of
    are queued (a removed field can't gain one), and nothing when none is; a request queued
    before the job runs shares it (module docstring, M3). Refused past the requester's daily
    budget (429, before anything is written: review round 1)."""
    if not fields:
        return
    _refuse_team(scope)  # before the owner below: None would mean every deal
    owner_id = scope.subject_user_id
    _charge_daily_budget(actor_id, len(fields))
    for field in fields:
        audit.record(
            AUDIT_REQUESTED,
            actor_id=actor_id,
            target_type="custom_field",
            target_id=field.pk,
            subject_user_id=owner_id if owner_id not in (None, actor_id) else None,
            metadata={"workspace": scope.kind.value, "pipeline_id": str(field.pipeline_id)},
        )
    queued = sorted(str(field.pk) for field in fields if holding(field, owner_id).exists())
    if not queued:
        return  # nothing left to delete here: no job, no ledger entry
    # One field (always so for `request`) keeps the format earlier releases read, so a
    # rolling deploy's older worker still runs it (review round 2, LEDGER2-6).
    payload: dict[str, Any] = {"field_id": queued[0]} if len(queued) == 1 else {"field_ids": queued}
    if owner_id is not None:
        payload["owner_id"] = str(owner_id)
    digest = hashlib.sha256(",".join(queued).encode()).hexdigest()[:32]
    outbox.enqueue(TOPIC_PURGE_VALUES, payload, dedupe_key=f"{digest}:{owner_id or 'organization'}")


def _charge_daily_budget(actor_id: UUID, count: int) -> None:
    """Refuse (429, Retry-After) a request that would take the requester past
    MAX_VALUE_DELETIONS_PER_USER_PER_DAY fields in any 24 hours (0: no budget), counted
    from their `pipeline.field_values_deletion_requested` audit events (append-only, through
    the actor's index), under a per-person advisory lock held until the request commits
    with its events, so requests at once can't all pass. The requester pays, also in
    someone else's workspace."""
    cap = MAX_VALUE_DELETIONS_PER_USER_PER_DAY
    if cap <= 0:
        return
    digest = int.from_bytes(hashlib.sha256(str(actor_id).encode()).digest()[:4], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_BUDGET_LOCK, digest])
    now = timezone.now()
    asked = AuditEvent.objects.filter(
        actor_id=actor_id, action=AUDIT_REQUESTED, occurred_at__gt=now - DAY
    ).order_by("-occurred_at")
    used = asked[:cap].count()  # bounded: at most `cap` index entries
    if used + count <= cap:
        return
    if count > cap:
        retry_after = DAY.total_seconds()
    else:  # possible again once enough of the newest `cap` are more than a day old
        oldest = asked.values_list("occurred_at", flat=True)[cap - count]
        retry_after = (oldest + DAY - now).total_seconds()
    raise RateLimitedError(BUDGET_REACHED.format(limit=cap), retry_after=max(60, int(retry_after)))


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
    """One field's values (`purge_fields`): the erasure replay, and earlier releases' jobs."""
    return purge_fields([field_id], owner_id=owner_id, replay=replay)


def purge_fields(
    field_ids: Sequence[UUID], *, owner_id: UUID | None = None, replay: bool = False
) -> dict[str, int]:
    """Remove the fields' keys from every opportunity holding them, or only from
    `owner_id`'s (the outbox job, and the erasure replay), at most BATCH rows a transaction
    across the fields. Idempotent. A field active again (impossible today) is left alone.

    Recorded in the erasure ledger by the transaction that first removes a value of a field,
    last, just before it commits: one `custom_values_deleted` entry naming every field that
    batch began removing (ids separated by commas, core.ledger; review round 1, LEDGER-2 a:
    a removal of thirty fields appended thirty entries), with the workspace's owner. A
    field whose first value goes in a later batch (rows held by another transaction, more
    than BATCH deals) gets an entry of its own then. No value is gone without its entry, and
    none is written for a purge that finds nothing (security review M3). A retried job that
    removes more appends again, which replay takes as already applied."""
    fields = [f for f in m.CustomField.objects.filter(pk__in=list(field_ids)) if not f.is_active]
    if not fields:
        return {"rows": 0, "held": 0}
    owners = [owner_id] if owner_id is not None else None
    removed: dict[UUID, int] = {field.pk: 0 for field in fields}
    recorded = {field.pk for field in fields} if replay else set()  # a replay appends none
    remaining, held_leads, idle = list(fields), [], 0
    while remaining:
        progressed = 0
        # Each batch is serialised with placing a hold (core.holds.barrier, its first lock;
        # R134): the holds are read again inside it, so a hold placed while the job runs
        # has either committed before the batch reads them (its lead's deals keep the
        # value) or waits for the batch to commit.
        with transaction.atomic(), connection.cursor() as cursor:
            holds.barrier()
            held_leads = list(holds.held_leads())
            begun: list[UUID] = []
            for field in remaining:
                room = BATCH - progressed
                if room <= 0:
                    break
                key = str(field.pk)
                cursor.execute(
                    "UPDATE pipeline_opportunity SET custom_fields = custom_fields - %s"
                    " WHERE id IN ("
                    "  SELECT id FROM pipeline_opportunity"
                    "  WHERE pipeline_id = %s AND custom_fields ? %s AND NOT (lead_id = ANY(%s))"
                    "  AND (%s::uuid[] IS NULL OR (owner_id = ANY(%s::uuid[]) AND EXISTS ("
                    "   SELECT 1 FROM leads_lead l WHERE l.id = lead_id"
                    "   AND l.owner_id = ANY(%s::uuid[]))))"
                    "  ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)",
                    [key, field.pipeline_id, key, held_leads, owners, owners, owners, room],
                )
                done = cursor.rowcount
                progressed += done
                removed[field.pk] += done
                if done and field.pk not in recorded:
                    begun.append(field.pk)
            if begun:
                # The removal requested has committed (this runs after it), and values are
                # going now: from here a restored backup must delete them again, on the
                # same deals. Last in this batch's transaction, as an erasure's entry (the
                # ledger's append lock comes after the barrier and the row locks, core.holds
                # rule 3).
                ledger.append("custom_values_deleted", ledger.joined(begun), owner=owner_id)
                recorded.update(begun)
        remaining = [
            f for f in remaining if holding(f, owner_id).exclude(lead_id__in=held_leads).exists()
        ]
        if progressed:
            idle = 0
            continue
        if not remaining:
            break
        # Rows another transaction holds: wait for them a little, then let the outbox retry.
        idle += 1
        if idle > MAX_IDLE_ROUNDS:
            raise RuntimeError("Some opportunities stayed locked; the job will retry.")
        time.sleep(0.2)
    total = {"rows": 0, "held": 0}
    for field in fields:
        held = holding(field, owner_id).filter(lead_id__in=held_leads).count()
        audit.record(
            AUDIT_PURGED,
            actor_id=None,
            target_type="custom_field",
            target_id=field.pk,
            subject_user_id=owner_id,
            metadata={
                "rows": removed[field.pk],
                "held": held,
                **({"replay": True} if replay else {}),
            },
        )
        total["rows"] += removed[field.pk]
        total["held"] += held
    return total


def holding(field: m.CustomField, owner_id: UUID | None) -> Any:
    """The deals of the field's pipeline that still hold a value for it (only `owner_id`'s
    when given, and of those only the ones whose customer is theirs too: a closed deal they
    kept after its lead was reassigned holds the new owner's customer's value, which the
    workspace's request may not delete; review P3-1)."""
    deals = m.Opportunity.objects.filter(
        pipeline_id=field.pipeline_id, custom_fields__has_key=str(field.pk)
    )
    if owner_id is None:
        return deals
    return deals.filter(owner_id=owner_id, lead__owner_id=owner_id)


@outbox.handler(TOPIC_PURGE_VALUES, queue="default")
def purge_values(payload: dict[str, Any]) -> None:
    # A job queued by an earlier release names one field ("field_id"), and one queued
    # before the owner filter existed (no "owner_id") keeps its old reach.
    owner = payload.get("owner_id")
    ids = payload.get("field_ids") or [payload["field_id"]]
    purge_fields([UUID(str(i)) for i in ids], owner_id=UUID(str(owner)) if owner else None)
