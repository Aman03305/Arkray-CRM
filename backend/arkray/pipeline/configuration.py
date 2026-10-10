"""Pipeline configuration: creating pipelines, editing their stages and custom fields,
archiving them (docs/pipeline.md#configuration).

Who may configure what (checked here for every caller):
- a *shared* pipeline (owner NULL; the organisation's): administrators with config.manage,
  from the organisation-wide workspace ("all");
- a *personal* pipeline: whoever may write in its owner's workspace (the owner, with
  crm.access_own; an administrator in that user's workspace or organisation-wide, with
  crm.manage_any, recorded as the actor with the owner as the subject). Seeing a pipeline
  (because one of your deals sits in it) never implies configuring it.
A pipeline the caller may not see is a 404; one they may see but not configure, a 403.

Concurrency: every change requires the pipeline version the client saw (409 otherwise) and
bumps it, so two people editing one pipeline can't overwrite each other. Lock order: the
pipeline row (FOR NO KEY UPDATE, or FOR UPDATE to archive it), then stage rows (FOR UPDATE
for a stage being removed or changing between open/won/lost; plain updates otherwise),
ascending id. Configuration changes never lock a lead or an opportunity; opportunity writes
take only shared locks on configuration rows after their own (services.py), so the two can't
wait for each other in a cycle. A stage being removed is locked FOR UPDATE before its
opportunities are counted: a move into it (FOR KEY SHARE first) either commits before the
count sees it, or waits and then finds the stage archived.

Nothing is ever deleted that anything references: a removed stage is archived when an
opportunity, its history or a negotiated price mentions it, and deleted only if it was never
used. Custom fields are archived, their values kept (hidden).
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.db.models.functions import Lower

from arkray.audit import services as audit
from arkray.core.access import AccessScope
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

from . import field_values, selectors, validation
from . import models as m

AUDIT_CREATED = "pipeline.created"
AUDIT_RENAMED = "pipeline.renamed"
AUDIT_STAGES_CHANGED = "pipeline.stages_changed"
AUDIT_FIELDS_CHANGED = "pipeline.fields_changed"
AUDIT_ARCHIVED = "pipeline.archived"
AUDIT_RESTORED = "pipeline.restored"

SHARED_ADMIN_ONLY = (
    "Shared pipelines are configured by administrators, from the organization-wide Pipeline."
)
NOT_YOURS = "Only this pipeline's owner (or an administrator) can change it."
NAME_TAKEN = "There's already a pipeline with this name."
TOO_MANY = f"Use at most {m.MAX_PIPELINES_PER_OWNER} active pipelines."
OWNER_INACTIVE = "This user is deactivated, so a pipeline can't be created for them."
ARCHIVED = "This pipeline is archived. Restore it to make changes."
DEFAULT_CANNOT_ARCHIVE = "The default pipeline can't be archived."
OPEN_DEALS = (
    "This pipeline still has {count} open opportunit{plural}. Close or archive them before "
    "archiving the pipeline."
)
STAGE_IN_USE = (
    "“{name}” has {count} opportunit{plural}. Move them to another stage before removing it."
)
STAGE_TYPE_LOCKED = (
    "“{name}” has opportunities, so it can't change between open, won and lost. Add a new "
    "stage instead."
)
FIELD_TYPE_LOCKED = "A field's type can't change. Remove the field and add a new one."
UNKNOWN_STAGE = "This stage isn't part of the pipeline any more. Reload and try again."
UNKNOWN_FIELD = "This field isn't part of the pipeline any more. Reload and try again."
UNKNOWN_OPTION = "This choice isn't part of the field any more. Reload and try again."

# Retired stages keep their rows; their positions move out of the board's range (the
# position is unique per pipeline, active or not).
RETIRED_POSITION_BASE = 10000
_SERIALISE_NAMESPACE = 0x41524B33  # "ARK3": per-owner pipeline creation


def _plural(count: int) -> dict[str, Any]:
    return {"count": count, "plural": "y" if count == 1 else "ies"}


def new_key(prefix: str) -> str:
    """An immutable identifier for a pipeline ("p…") or stage ("s…") made here."""
    return f"{prefix}{secrets.token_hex(8)}"


def can_manage(actor: User, scope: AccessScope, pipeline: m.Pipeline) -> bool:
    """Whether `actor`, working in `scope`, may configure `pipeline` (no exceptions)."""
    try:
        _require_manage(actor, scope, pipeline)
    except PermissionDeniedError:
        return False
    return True


def _require_manage(actor: User, scope: AccessScope, pipeline: m.Pipeline) -> None:
    if pipeline.owner_id is None:
        if not scope.is_organization_wide or not has_capability(actor, Capability.CONFIG_MANAGE):
            raise PermissionDeniedError(SHARED_ADMIN_ONLY)
    elif not scope.permits_owner(pipeline.owner_id):
        raise PermissionDeniedError(NOT_YOURS)
    authorize_write(actor, scope)


def _lock(
    actor: User, scope: AccessScope, pipeline_id: UUID, version: int | None, *, exclusive: bool
) -> m.Pipeline:
    """The pipeline, locked, if `scope` may see it (404) and `actor` may configure it (403),
    at the version the client saw (409)."""
    if not m.Pipeline.objects.filter(selectors.visible(scope), pk=pipeline_id).exists():
        raise NotFoundError()
    pipeline = m.Pipeline.objects.select_for_update(no_key=not exclusive).get(pk=pipeline_id)
    _require_manage(actor, scope, pipeline)
    if version is not None and pipeline.version != version:
        raise ConflictError()
    return pipeline


def _audit(
    action: str, actor: User, scope: AccessScope, pipeline: m.Pipeline, **metadata: Any
) -> None:
    audit.record(
        action,
        actor_id=actor.pk,
        target_type="pipeline",
        target_id=pipeline.pk,
        subject_user_id=pipeline.owner_id if pipeline.owner_id not in (None, actor.pk) else None,
        metadata={"workspace": scope.kind.value, **metadata},
    )


def _bump(pipeline: m.Pipeline) -> None:
    pipeline.version += 1
    pipeline.save(update_fields=["version", "updated_at"])


def _serialise_owner(owner_id: UUID | None) -> None:
    """One pipeline creation (or restore) at a time per owner, so the per-owner limit and
    name check hold under concurrency (the unique index backs the names up)."""
    key = str(owner_id or "shared")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", [_SERIALISE_NAMESPACE, key]
        )


def _owner_pipelines(owner_id: UUID | None) -> Any:
    active = m.Pipeline.objects.filter(is_active=True)
    return (
        active.filter(owner__isnull=True) if owner_id is None else active.filter(owner_id=owner_id)
    )


def _require_room_and_name(owner_id: UUID | None, name: str, *, exclude: UUID | None) -> None:
    pipelines = _owner_pipelines(owner_id)
    if exclude is not None:
        pipelines = pipelines.exclude(pk=exclude)
    if pipelines.annotate(lower=Lower("name")).filter(lower=name.lower()).exists():
        raise InvalidInputError(details={"name": [NAME_TAKEN]})
    if exclude is None and pipelines.count() >= m.MAX_PIPELINES_PER_OWNER:
        raise BusinessRuleViolation(TOO_MANY)


def _clean_name(name: Any) -> str:
    try:
        return validation.clean_config_name(name, m.PIPELINE_NAME_MAX_LENGTH)
    except ValueError as exc:
        raise InvalidInputError(details={"name": [str(exc)]}) from None


# --- pipelines ------------------------------------------------------------------------------------
def create_pipeline(
    *,
    actor: User,
    scope: AccessScope,
    name: Any,
    stages: Sequence[Mapping[str, Any]],
    fields: Sequence[Mapping[str, Any]] = (),
) -> m.Pipeline:
    """A new pipeline with its stages (and custom fields). In one's own workspace it is
    one's own; in a user's workspace (an administrator), that user's; organisation-wide, a
    shared pipeline (config.manage)."""
    authorize_write(actor, scope)
    owner_id = None if scope.is_organization_wide else scope.subject_user_id
    if owner_id is None and not has_capability(actor, Capability.CONFIG_MANAGE):
        raise PermissionDeniedError(SHARED_ADMIN_ONLY)
    clean_name = _clean_name(name)
    specs = validation.clean_stage_specs(stages)
    if any(spec.id is not None for spec in specs):
        raise InvalidInputError(details={"stages": ["A new pipeline's stages are all new."]})
    field_specs = validation.clean_field_specs(fields)
    if any(spec.id is not None for spec in field_specs):
        raise InvalidInputError(details={"fields": ["A new pipeline's fields are all new."]})
    try:
        with transaction.atomic():
            _serialise_owner(owner_id)
            if owner_id is not None and not lock_assignable_user(owner_id):
                raise BusinessRuleViolation(OWNER_INACTIVE)
            _require_room_and_name(owner_id, clean_name, exclude=None)
            pipeline = m.Pipeline.objects.create(
                key=new_key("p"), name=clean_name, owner_id=owner_id, created_by=actor
            )
            m.Stage.objects.bulk_create(
                [
                    m.Stage(
                        pipeline=pipeline,
                        key=new_key("s"),
                        name=spec.name,
                        position=(index + 1) * 10,
                        probability=spec.probability,
                        category=spec.category,
                        is_negotiation=spec.is_negotiation,
                    )
                    for index, spec in enumerate(specs)
                ]
            )
            _create_fields(pipeline, field_specs, actor, start=0)
            _audit(
                AUDIT_CREATED,
                actor,
                scope,
                pipeline,
                shared=owner_id is None,
                stages=len(specs),
                fields=len(field_specs),
            )
    except IntegrityError as error:
        if "pipeline_pipeline_" in str(error) and "name_unique" in str(error):
            raise InvalidInputError(details={"name": [NAME_TAKEN]}) from None
        raise
    return selectors.visible_pipeline(scope, pipeline.pk)


def rename_pipeline(
    *, actor: User, scope: AccessScope, pipeline_id: UUID, version: int, name: Any
) -> m.Pipeline:
    clean_name = _clean_name(name)
    with transaction.atomic():
        pipeline = _lock(actor, scope, pipeline_id, version, exclusive=False)
        if not pipeline.is_active:
            raise BusinessRuleViolation(ARCHIVED)
        if pipeline.name != clean_name:
            # As creation and restore: one name change per owner at a time, so two renames
            # (or a restore) to one name meet the check, never the unique index (a 500).
            _serialise_owner(pipeline.owner_id)
            _require_room_and_name(pipeline.owner_id, clean_name, exclude=pipeline.pk)
            pipeline.name = clean_name
            pipeline.version += 1
            pipeline.save(update_fields=["name", "version", "updated_at"])
            _audit(AUDIT_RENAMED, actor, scope, pipeline)
    return selectors.visible_pipeline(scope, pipeline_id)


def archive_pipeline(
    *, actor: User, scope: AccessScope, pipeline_id: UUID, version: int
) -> m.Pipeline:
    """Hide a pipeline from selectors and forms. Its opportunities, history and prices stay.
    Refused while it holds open opportunities (they would have no board), and for the
    default pipeline. FOR UPDATE: creations (FOR SHARE) and moves (FOR KEY SHARE) wait, then
    find it archived."""
    with transaction.atomic():
        pipeline = _lock(actor, scope, pipeline_id, None, exclusive=True)
        if not pipeline.is_active:
            return selectors.visible_pipeline(scope, pipeline_id)
        if pipeline.version != version:
            raise ConflictError()
        if pipeline.is_default:
            raise BusinessRuleViolation(DEFAULT_CANNOT_ARCHIVE)
        open_count = m.Opportunity.objects.filter(
            pipeline=pipeline, status=m.StageCategory.OPEN, archived_at__isnull=True
        ).count()
        if open_count:
            raise BusinessRuleViolation(OPEN_DEALS.format(**_plural(open_count)))
        pipeline.is_active = False
        pipeline.version += 1
        pipeline.save(update_fields=["is_active", "version", "updated_at"])
        _audit(AUDIT_ARCHIVED, actor, scope, pipeline)
    return selectors.visible_pipeline(scope, pipeline_id)


def restore_pipeline(
    *, actor: User, scope: AccessScope, pipeline_id: UUID, version: int
) -> m.Pipeline:
    with transaction.atomic():
        pipeline = _lock(actor, scope, pipeline_id, None, exclusive=False)
        if pipeline.is_active:
            return selectors.visible_pipeline(scope, pipeline_id)
        if pipeline.version != version:
            raise ConflictError()
        _serialise_owner(pipeline.owner_id)
        _require_room_and_name(pipeline.owner_id, pipeline.name, exclude=None)
        pipeline.is_active = True
        pipeline.version += 1
        pipeline.save(update_fields=["is_active", "version", "updated_at"])
        _audit(AUDIT_RESTORED, actor, scope, pipeline)
    return selectors.visible_pipeline(scope, pipeline_id)


# --- stages ---------------------------------------------------------------------------------------
def _stage_referenced(stage_id: UUID) -> bool:
    """Whether anything mentions the stage (then it is archived, never deleted)."""
    return (
        m.Opportunity.objects.filter(stage_id=stage_id).exists()
        or m.StageHistory.objects.filter(
            Q(from_stage_id=stage_id) | Q(to_stage_id=stage_id)
        ).exists()
        or m.NegotiationPrice.objects.filter(stage_id=stage_id).exists()
    )


def replace_stages(
    *,
    actor: User,
    scope: AccessScope,
    pipeline_id: UUID,
    version: int,
    stages: Sequence[Mapping[str, Any]],
) -> m.Pipeline:
    """Make the pipeline's active stages exactly `stages`, in that order: existing stages (by
    id) are renamed, retyped, re-probabilised and reordered; stages without an id are added;
    active stages left out are removed (archived if anything references them, else deleted).
    Never rewrites opportunities or their history: an opportunity keeps the probability it
    adopted, and history keeps the names it recorded (docs/pipeline.md#stages).

    Refused (422) when a removed stage still holds opportunities that aren't archived, or a
    stage holding any opportunity would change between open, won and lost."""
    specs = validation.clean_stage_specs(stages)
    with transaction.atomic():
        # FOR UPDATE, not NO KEY UPDATE: it conflicts with the KEY SHARE every move and
        # restore takes on the pipeline before its stages, so they queue here first. Stage
        # rows are then locked by this edit alone: changing a position (a unique key) locks
        # the row FOR UPDATE, and a move holding KEY SHARE on its target stage while it
        # inserts history (KEY SHARE on its source stage) would otherwise close a cycle
        # with a reorder (enhancement review P0; test_enhancement_races.py).
        pipeline = _lock(actor, scope, pipeline_id, version, exclusive=True)
        if not pipeline.is_active:
            raise BusinessRuleViolation(ARCHIVED)
        current = {
            stage.pk: stage for stage in m.Stage.objects.filter(pipeline=pipeline, is_active=True)
        }
        unknown = {
            f"stages[{i}].id": [UNKNOWN_STAGE]
            for i, spec in enumerate(specs)
            if spec.id is not None and spec.id not in current
        }
        if unknown:
            raise InvalidInputError(details=unknown)
        listed = {spec.id for spec in specs if spec.id is not None}
        removed_ids = sorted(set(current) - listed)
        retyped_ids = sorted(
            spec.id
            for spec in specs
            if spec.id is not None and current[spec.id].category != spec.category
        )
        # FOR UPDATE (it conflicts with the KEY SHARE every move and creation takes first):
        # in-flight moves into these stages finish before we count, later ones find them
        # archived (or retyped) when they get the lock.
        locked = list(
            m.Stage.objects.select_for_update()
            .filter(pk__in=[*removed_ids, *retyped_ids])
            .order_by("pk")
        )
        problems: list[str] = []
        for stage in locked:
            if stage.pk in removed_ids:
                count = m.Opportunity.objects.filter(stage=stage, archived_at__isnull=True).count()
                if count:
                    problems.append(STAGE_IN_USE.format(name=stage.name, **_plural(count)))
            elif m.Opportunity.objects.filter(stage=stage).exists():
                problems.append(STAGE_TYPE_LOCKED.format(name=stage.name))
        if problems:
            raise BusinessRuleViolation(" ".join(problems))

        retired_position = max(
            [
                RETIRED_POSITION_BASE,
                *(
                    position + 1
                    for position in m.Stage.objects.filter(
                        pipeline=pipeline, position__gte=RETIRED_POSITION_BASE
                    ).values_list("position", flat=True)
                ),
            ]
        )
        archived_keys: list[str] = []
        deleted_keys: list[str] = []
        for stage in locked:
            if stage.pk not in removed_ids:
                continue
            if _stage_referenced(stage.pk):
                stage.is_active = False
                stage.position = retired_position
                retired_position += 1
                stage.save(update_fields=["is_active", "position", "updated_at"])
                archived_keys.append(stage.key)
            else:
                deleted_keys.append(stage.key)
                stage.delete()
        originals = {
            pk: (s.name, s.probability, s.category, s.is_negotiation, s.position)
            for pk, s in current.items()
        }
        # Names are unique among active stages (not deferrable): park the kept stages under
        # their keys first, so names can be swapped in one save.
        kept = [current[spec.id] for spec in specs if spec.id is not None]
        for stage in kept:
            stage.name = f"~{stage.key}"
            stage.save(update_fields=["name"])
        added_keys: list[str] = []
        changed_keys: list[str] = []
        for index, spec in enumerate(specs):
            position = (index + 1) * 10
            if spec.id is None:
                stage = m.Stage.objects.create(
                    pipeline=pipeline,
                    key=new_key("s"),
                    name=spec.name,
                    position=position,
                    probability=spec.probability,
                    category=spec.category,
                    is_negotiation=spec.is_negotiation,
                )
                added_keys.append(stage.key)
                continue
            stage = current[spec.id]
            stage.name = spec.name
            stage.probability = spec.probability
            stage.category = spec.category
            stage.is_negotiation = spec.is_negotiation
            stage.position = position
            stage.save(
                update_fields=[
                    "name",
                    "probability",
                    "category",
                    "is_negotiation",
                    "position",
                    "updated_at",
                ]
            )
            after = (spec.name, spec.probability, spec.category, spec.is_negotiation, position)
            if originals[spec.id] != after:
                changed_keys.append(stage.key)
        _bump(pipeline)
        _audit(
            AUDIT_STAGES_CHANGED,
            actor,
            scope,
            pipeline,
            added=added_keys,
            changed=changed_keys,
            archived=archived_keys,
            deleted=deleted_keys,
        )
    return selectors.visible_pipeline(scope, pipeline_id)


# --- custom fields --------------------------------------------------------------------------------
def _create_fields(
    pipeline: m.Pipeline, specs: Sequence[validation.FieldSpec], actor: User, *, start: int
) -> list[m.CustomField]:
    return m.CustomField.objects.bulk_create(
        [
            m.CustomField(
                pipeline=pipeline,
                name=spec.name,
                field_type=spec.field_type,
                required=spec.required,
                options=[
                    {"id": validation.new_option_id(), "label": label} for _, label in spec.options
                ],
                position=start + index,
                created_by=actor,
            )
            for index, spec in enumerate(specs)
        ]
    )


def replace_fields(
    *,
    actor: User,
    scope: AccessScope,
    pipeline_id: UUID,
    version: int,
    fields: Sequence[Mapping[str, Any]],
    delete_removed_values: bool = False,
) -> m.Pipeline:
    """Make the pipeline's active custom fields exactly `fields`, in that order. Existing
    fields (by id) keep their type; their name, required flag and choices may change
    (existing choices by id, new ones added). Fields left out are archived: their values stay
    on the opportunities, hidden, unless `delete_removed_values` (then they are deleted by a
    job: pipeline.field_values). Never changes the database schema."""
    specs = validation.clean_field_specs(fields)
    with transaction.atomic():
        # FOR NO KEY UPDATE conflicts with the FOR SHARE writers of custom values take: no
        # opportunity is validated against definitions that are changing.
        pipeline = _lock(actor, scope, pipeline_id, version, exclusive=False)
        if not pipeline.is_active:
            raise BusinessRuleViolation(ARCHIVED)
        current = {
            field.pk: field
            for field in m.CustomField.objects.filter(pipeline=pipeline, is_active=True)
        }
        errors: dict[str, list[str]] = {}
        for index, spec in enumerate(specs):
            if spec.id is None:
                continue
            existing = current.get(spec.id)
            if existing is None:
                errors[f"fields[{index}].id"] = [UNKNOWN_FIELD]
            elif existing.field_type != spec.field_type:
                errors[f"fields[{index}].type"] = [FIELD_TYPE_LOCKED]
            elif spec.options:
                known = {option["id"] for option in existing.options}
                if any(oid is not None and oid not in known for oid, _ in spec.options):
                    errors[f"fields[{index}].options"] = [UNKNOWN_OPTION]
        if errors:
            raise InvalidInputError(details=errors)
        listed = {spec.id for spec in specs if spec.id is not None}
        originals = {pk: (f.name, f.required, f.options, f.position) for pk, f in current.items()}
        removed = [field for pk, field in current.items() if pk not in listed]
        for field in removed:
            field.is_active = False
            field.save(update_fields=["is_active", "updated_at"])
        kept = [current[spec.id] for spec in specs if spec.id is not None]
        for field in kept:  # names are unique among active fields: park them first
            field.name = f"~{field.pk}"[: m.FIELD_NAME_MAX_LENGTH]
            field.save(update_fields=["name"])
        added: list[str] = []
        changed: list[str] = []
        for index, spec in enumerate(specs):
            options = [
                {"id": oid or validation.new_option_id(), "label": label}
                for oid, label in spec.options
            ]
            if spec.id is None:
                (field,) = _create_fields(pipeline, [spec], actor, start=index)
                added.append(str(field.pk))
                continue
            field = current[spec.id]
            field.name = spec.name
            field.required = spec.required
            field.options = options
            field.position = index
            field.save(update_fields=["name", "required", "options", "position", "updated_at"])
            if originals[spec.id] != (spec.name, spec.required, options, index):
                changed.append(str(field.pk))
        _bump(pipeline)
        _audit(
            AUDIT_FIELDS_CHANGED,
            actor,
            scope,
            pipeline,
            added=added,
            changed=changed,
            archived=[str(field.pk) for field in removed],
        )
        if delete_removed_values:  # the job records it in the erasure ledger
            for field in removed:
                field_values.request_for(actor.pk, field, scope)
    return selectors.visible_pipeline(scope, pipeline_id)
