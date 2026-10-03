"""Test data factories shared by all module test suites."""

import importlib
from datetime import timedelta
from decimal import Decimal

import factory
from django.utils import timezone

from arkray.activities.models import Activity, ActivityStatus, ActivityType, Priority
from arkray.identity.models import Role, User, UserStatus
from arkray.leads.models import Lead, LeadSource, LeadStatus
from arkray.pipeline.models import Opportunity, Pipeline, Stage

DEFAULT_PASSWORD = "correct-horse-battery-staple"


class UserFactory(factory.django.DjangoModelFactory[User]):
    """An active sales user. `is_active=False` gives a deactivated one."""

    class Meta:
        model = User
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.test")
    first_name = "Sales"
    last_name = factory.Sequence(lambda n: f"User {n}")
    role = Role.SALES_USER
    password = factory.django.Password(DEFAULT_PASSWORD)
    is_active = True
    status = factory.LazyAttribute(
        lambda o: UserStatus.ACTIVE if o.is_active else UserStatus.DEACTIVATED
    )
    activated_at = factory.LazyFunction(timezone.now)
    deactivated_at = factory.LazyAttribute(lambda o: None if o.is_active else timezone.now())


class AdminFactory(UserFactory):
    role = Role.ADMIN


class InvitedUserFactory(UserFactory):
    """Created by an admin, invitation not yet accepted: no usable password."""

    password = factory.django.Password(None)
    is_active = False
    status = factory.LazyAttribute(lambda _: UserStatus.INVITED)
    activated_at = factory.LazyFunction(lambda: None)
    deactivated_at = factory.LazyAttribute(lambda _: None)


class LeadFactory(factory.django.DjangoModelFactory[Lead]):
    """A lead owned (and created) by `owner`. Created directly, bypassing services: tests of
    the services themselves go through arkray.leads.services."""

    class Meta:
        model = Lead

    first_name = "Lead"
    last_name = factory.Sequence(lambda n: f"Person {n}")
    organization_name = "Apollo Diagnostics"
    email = factory.Sequence(lambda n: f"lead{n}@hospital.example")
    status_id = "new"
    owner = factory.SubFactory(UserFactory)
    created_by = factory.LazyAttribute(lambda o: o.owner)


def ensure_lead_configuration() -> None:
    """Re-create the migration-seeded lead statuses and sources. Transactional tests need
    this: the flush after each of them empties every table, seed data included."""
    seed = importlib.import_module("arkray.leads.migrations.0002_seed_statuses_and_sources")
    for key, name, category, position, is_default in seed.STATUSES:
        LeadStatus.objects.get_or_create(
            key=key,
            defaults={
                "name": name,
                "category": category,
                "position": position,
                "is_default": is_default,
            },
        )
    for key, name, position in seed.SOURCES:
        LeadSource.objects.get_or_create(key=key, defaults={"name": name, "position": position})


def ensure_pipeline_configuration() -> None:
    """Re-create the migration-seeded default pipeline and its stages (transactional tests
    flush them, like the lead statuses)."""
    seed = importlib.import_module("arkray.pipeline.migrations.0003_seed_default_pipeline")
    key, name = seed.PIPELINE
    pipeline, _ = Pipeline.objects.get_or_create(
        key=key, defaults={"name": name, "is_default": True}
    )
    for stage_key, stage_name, position, probability, category in seed.STAGES:
        Stage.objects.get_or_create(
            pipeline=pipeline,
            key=stage_key,
            defaults={
                "name": stage_name,
                "position": position,
                "probability": probability,
                "category": category,
            },
        )


def default_stage(key: str) -> Stage:
    return Stage.objects.select_related("pipeline").get(pipeline__is_default=True, key=key)


class OpportunityFactory(factory.django.DjangoModelFactory[Opportunity]):
    """An opportunity that respects the database invariants: owned by its lead's owner, its
    status and probability taken from its stage (`stage_key="won"`, or pass a Stage as
    `stage`), closed_at set exactly when closed. Created directly, bypassing services (no
    history or audit): tests of the services go through arkray.pipeline.services."""

    class Meta:
        model = Opportunity

    class Params:
        stage_key = "new"

    title = factory.Sequence(lambda n: f"Opportunity {n}")
    lead = factory.SubFactory(LeadFactory)
    owner = factory.LazyAttribute(lambda o: o.lead.owner)
    created_by = factory.LazyAttribute(lambda o: o.owner)
    stage = factory.LazyAttribute(lambda o: default_stage(o.stage_key))
    pipeline = factory.LazyAttribute(lambda o: o.stage.pipeline)
    status = factory.LazyAttribute(lambda o: o.stage.category)
    value = Decimal("100000.00")
    probability = factory.LazyAttribute(lambda o: o.stage.probability)
    closed_at = factory.LazyAttribute(
        lambda o: None if o.stage.category == "open" else timezone.now()
    )


_INITIAL_STATUS = {
    ActivityType.TASK: ActivityStatus.OPEN,
    ActivityType.MEETING: ActivityStatus.SCHEDULED,
    ActivityType.NOTE: None,
}


class ActivityFactory(factory.django.DjangoModelFactory[Activity]):
    """An activity that respects the database invariants: owned by its lead's owner (pass
    `owner` only for completed/cancelled work, which may keep a previous owner), type-
    specific columns filled exactly for its type, completion/cancellation fields set exactly
    for those statuses, the lead of its opportunity. Created directly, bypassing services (no
    timeline or audit): tests of the services go through arkray.activities.services."""

    class Meta:
        model = Activity

    type = ActivityType.TASK
    opportunity = None
    lead = factory.LazyAttribute(
        lambda o: o.opportunity.lead if o.opportunity is not None else LeadFactory()
    )
    owner = factory.LazyAttribute(lambda o: o.lead.owner)
    created_by = factory.LazyAttribute(lambda o: o.owner)
    title = factory.LazyAttribute(
        lambda o: "" if o.type == ActivityType.NOTE else f"{o.type.title()} subject"
    )
    description = factory.LazyAttribute(
        lambda o: "Spoke about the analyser upgrade." if o.type == ActivityType.NOTE else ""
    )
    status = factory.LazyAttribute(lambda o: _INITIAL_STATUS[ActivityType(o.type)])
    priority = factory.LazyAttribute(
        lambda o: Priority.NORMAL if o.type == ActivityType.TASK else None
    )
    due_at = None
    starts_at = factory.LazyAttribute(
        lambda o: timezone.now() + timedelta(days=1) if o.type == ActivityType.MEETING else None
    )
    ends_at = factory.LazyAttribute(
        lambda o: o.starts_at + timedelta(hours=1) if o.starts_at is not None else None
    )
    completed_at = factory.LazyAttribute(
        lambda o: timezone.now() if o.status == ActivityStatus.COMPLETED else None
    )
    completed_by = factory.LazyAttribute(
        lambda o: o.owner if o.status == ActivityStatus.COMPLETED else None
    )
    cancelled_at = factory.LazyAttribute(
        lambda o: timezone.now() if o.status == ActivityStatus.CANCELLED else None
    )
    cancelled_by = factory.LazyAttribute(
        lambda o: o.owner if o.status == ActivityStatus.CANCELLED else None
    )


class TaskFactory(ActivityFactory):
    type = ActivityType.TASK


class MeetingFactory(ActivityFactory):
    type = ActivityType.MEETING


class NoteFactory(ActivityFactory):
    type = ActivityType.NOTE
