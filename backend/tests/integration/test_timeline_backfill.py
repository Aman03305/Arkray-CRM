"""activities.0003 rebuilds the timelines of leads and opportunities that existed before
Phase 4 from the authoritative history the earlier phases kept (the audit trail and the
opportunities' stage history), invents nothing, and can never duplicate history."""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from arkray.activities.models import TimelineEntry
from arkray.audit.models import AuditEvent
from arkray.pipeline.models import StageHistory
from tests.factories import (
    AdminFactory,
    LeadFactory,
    OpportunityFactory,
    UserFactory,
    default_stage,
)
from tests.integration.migration_states import build, everything_but

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("crm_configuration")]


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


def everything():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def audit(action, actor, target_type, target, at, **metadata):
    return AuditEvent.objects.create(
        action=action,
        actor_type="user",
        actor_id=actor.pk,
        target_type=target_type,
        target_id=str(target),
        occurred_at=at,
        metadata=metadata,
    )


def history(opportunity, actor, at, from_key, to_key):
    source = default_stage(from_key) if from_key else None
    target = default_stage(to_key)
    StageHistory.objects.create(
        opportunity=opportunity,
        from_stage=source,
        to_stage=target,
        from_stage_name=source.name if source else "",
        to_stage_name=target.name,
        from_status=source.category if source else "",
        to_status=target.category,
        value=opportunity.value,
        probability=target.probability,
        actor=actor,
        occurred_at=at,
    )


def test_phase_2_and_3_history_becomes_the_timeline():
    # As before Phase 4: no activity tables at all. Built forwards from an empty schema, since
    # activities.0010 refuses to be reversed (docs/deployment.md#rollback); that re-seeds the
    # statuses and the default pipeline too.
    build(everything_but("activities"))
    try:
        owner, other, admin = UserFactory(), UserFactory(), AdminFactory()
        lead = LeadFactory(owner=other)
        t = timezone.now() - timedelta(days=10)
        audit("lead.created", owner, "lead", lead.pk, t, status="new", owner_id=str(owner.pk))
        audit(
            "lead.status_changed",
            owner,
            "lead",
            lead.pk,
            t + timedelta(hours=1),
            **{"from": "new", "to": "qualified"},
        )
        opportunity = OpportunityFactory(lead=lead, stage_key="won")
        audit(
            "opportunity.created",
            owner,
            "opportunity",
            opportunity.pk,
            t + timedelta(hours=2),
            via="conversion",
        )
        history(opportunity, owner, t + timedelta(hours=2), None, "new")
        history(opportunity, owner, t + timedelta(hours=3), "new", "won")
        audit(
            "lead.reassigned",
            admin,
            "lead",
            lead.pk,
            t + timedelta(hours=4),
            from_owner_id=str(owner.pk),
            to_owner_id=str(other.pk),
        )
        audit("lead.updated", owner, "lead", lead.pk, t + timedelta(hours=5), fields=["email"])
        audit("lead.created", owner, "lead", "4a7e0000-0000-4000-8000-00000000dead", t)  # gone

        migrate(everything())

        rows = list(TimelineEntry.objects.filter(lead=lead).order_by("occurred_at", "id"))
        assert [r.kind for r in rows] == [
            "lead.created",
            "lead.status_changed",
            "opportunity.created",
            "opportunity.won",
            "lead.reassigned",
        ]  # edits aren't timeline events; unknown leads get nothing
        created, changed, opp_created, won, reassigned = rows
        assert created.data == {"status": "new", "status_name": "New", "owner_id": str(owner.pk)}
        assert changed.data == {
            "from": "new",
            "from_name": "New",
            "to": "qualified",
            "to_name": "Qualified",
        }
        assert opp_created.data == {"stage": "New", "status": "open", "via_conversion": True}
        assert opp_created.opportunity_id == opportunity.pk
        assert won.data == {"from_stage": "New", "to_stage": "Won"}
        assert reassigned.data == {"from_owner_id": str(owner.pk), "to_owner_id": str(other.pk)}
        assert (created.actor_id, reassigned.actor_id) == (owner.pk, admin.pk)
        assert TimelineEntry.objects.count() == 5

        # Running it again never duplicates history (it only fills an empty table).
        backfill = importlib.import_module("arkray.activities.migrations.0003_backfill_timeline")
        with connection.cursor() as cursor:
            cursor.execute(backfill.BACKFILL)
        assert TimelineEntry.objects.count() == 5
    finally:
        migrate(everything())


def test_the_activity_tables_exist_again_afterwards():
    """Runs after the round trip above (same module, declared order)."""
    with connection.cursor() as cursor:
        for table in ("activities_activity", "activities_timeline_entry"):
            cursor.execute("SELECT to_regclass(%s)", [table])
            assert cursor.fetchone() == (table,), table
