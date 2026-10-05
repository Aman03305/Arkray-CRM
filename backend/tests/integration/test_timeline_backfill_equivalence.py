"""activities.0003 rebuilds exactly the timeline the live subscribers record, for history
written by the real Phase 2 and 3 services: the same entries (lead, opportunity, kind, actor
and data), in the same order. A difference would give a lead created before Phase 4 a
timeline unlike that of a lead whose history happened afterwards.

The live entries come from the subscribers; the rebuilt ones from the backfill's source
query (the SELECT under its INSERT, run read-only), evaluated over the audit trail and stage
history the same services wrote.
"""

from __future__ import annotations

import importlib
import json
from decimal import Decimal

import pytest
from django.db import connection

from arkray.activities.models import TimelineEntry
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import Opportunity
from tests.factories import AdminFactory, UserFactory, default_stage

pytestmark = pytest.mark.django_db

backfill = importlib.import_module("arkray.activities.migrations.0003_backfill_timeline")


def entry(lead_id, opportunity_id, kind, actor_id, data):
    return (
        str(lead_id),
        str(opportunity_id) if opportunity_id else None,
        kind,
        str(actor_id) if actor_id else None,
        json.dumps(data, sort_keys=True),
    )


def rebuilt_entries():
    """What the backfill would insert now, ignoring its only-into-an-empty-table guard."""
    sql = backfill.BACKFILL
    select = sql[sql.index("SELECT lead_id, opportunity_id, NULL") :]
    select = select.replace("WHERE NOT EXISTS (SELECT 1 FROM activities_timeline_entry)", "")
    with connection.cursor() as cursor:
        cursor.execute(select)
        rows = cursor.fetchall()
    return [
        entry(
            lead_id,
            opportunity_id,
            kind,
            actor_id,
            json.loads(data) if isinstance(data, str) else data,
        )
        for lead_id, opportunity_id, _, kind, actor_id, _, data in rows
    ]


def live_entries():
    return [
        entry(e.lead_id, e.opportunity_id, e.kind, e.actor_id, e.data)
        for e in TimelineEntry.objects.filter(activity__isnull=True).order_by("occurred_at", "id")
    ]


def test_the_backfill_rebuilds_what_the_live_subscribers_recorded():
    a, b, admin = UserFactory(), UserFactory(), AdminFactory()
    own, org = AccessScope.own(a.pk), AccessScope.organization(admin.pk)
    lead = lead_services.create_lead(
        actor=a, scope=own, fields={"first_name": "Asha", "last_name": "K"}
    ).lead
    lead = lead_services.change_status(
        actor=a, scope=own, lead_id=lead.pk, version=lead.version, status="contacted"
    )
    opportunity = pipeline_services.convert_lead(
        actor=a,
        scope=own,
        lead_id=lead.pk,
        lead_version=lead.version,
        fields={"value": Decimal("1000"), "instrument_name": "Adams 8380 V-lite"},
    ).opportunity
    # A second opportunity, created directly in a won stage.
    pipeline_services.create_opportunity(
        actor=admin,
        scope=org,
        lead_id=lead.pk,
        fields={"value": Decimal("50")},
        stage_id=default_stage("won").pk,
    )
    for key in ("qualified", "won", "proposal", "lost"):
        pipeline_services.move_opportunity(
            actor=a,
            scope=own,
            opportunity_id=opportunity.pk,
            version=Opportunity.objects.get(pk=opportunity.pk).version,
            stage_id=default_stage(key).pk,
        )
    lead = Lead.objects.get(pk=lead.pk)
    lead_services.reassign_lead(
        actor=admin, scope=org, lead_id=lead.pk, version=lead.version, owner_id=b.pk
    )
    lead = Lead.objects.get(pk=lead.pk)
    lead_services.archive_lead(actor=admin, scope=org, lead_id=lead.pk, version=lead.version)
    lead = Lead.objects.get(pk=lead.pk)
    lead_services.restore_lead(actor=admin, scope=org, lead_id=lead.pk, version=lead.version)

    live, rebuilt = live_entries(), rebuilt_entries()
    assert rebuilt == live, (
        f"live only: {[e for e in live if e not in rebuilt]}\n"
        f"backfill only: {[e for e in rebuilt if e not in live]}"
    )
