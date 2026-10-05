"""The dashboard: a read-only view over the authoritative domains (docs/dashboard.md).

Nothing here defines a figure. Each one is the owning module's own selector, called with
the request's AccessScope, so every count and sum is computed from `scope.apply()` first:

    total leads, new leads today       leads.selectors.lead_summary
    today's newest leads               leads.selectors.new_leads_today
    each new lead's opportunity        pipeline.selectors.first_opportunities
    pipeline value, weighted pipeline  pipeline.selectors.pipeline_totals (pipeline.metrics)
    task and meeting figures           activities.selectors.activity_summary
    next meetings, next open tasks     activities.selectors.upcoming_meetings, next_open_tasks

The Lead table is the only source of the lead figures: an opportunity created from the
pipeline makes its own lead (ADR-0028), so it counts once, as that lead, never as a second
record. "Today" is the business day containing `now` (core.business_time), the same for every
figure. No cache, no stored metrics: each request reads PostgreSQL, so a committed change
shows on the next load.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.db import connection, transaction

from arkray.activities import selectors as activity_selectors
from arkray.activities.models import Activity
from arkray.activities.selectors import ActivitySummary
from arkray.core.access import AccessScope
from arkray.core.business_time import business_date
from arkray.leads import selectors as lead_selectors
from arkray.leads.models import Lead
from arkray.leads.selectors import LeadSummary
from arkray.pipeline import selectors as pipeline_selectors
from arkray.pipeline.models import Opportunity
from arkray.pipeline.selectors import OpportunityFilters, PipelineTotals

LIST_LIMIT = 5  # rows in each supporting list; the full lists are a click away


@dataclass(frozen=True, slots=True)
class Dashboard:
    business_date: date
    leads: LeadSummary
    pipeline: PipelineTotals
    activities: ActivitySummary
    new_leads: list[Lead]
    # The opportunity each new lead was made for (or its first), by lead id, when visible.
    new_lead_opportunities: dict[UUID, Opportunity]
    upcoming_meetings: list[Activity]
    next_tasks: list[Activity]


def dashboard(scope: AccessScope, *, now: datetime) -> Dashboard:
    """See _dashboard. Its queries run in one REPEATABLE READ, read-only transaction, so
    every figure and list describes the same moment: a lead created between the count and
    the list can't make "3 new leads today" show four rows. Inside a caller's transaction
    (tests) that transaction's snapshot rules apply."""
    if connection.in_atomic_block:
        return _dashboard(scope, now=now)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _dashboard(scope, now=now)


def _dashboard(scope: AccessScope, *, now: datetime) -> Dashboard:
    """At most eight bounded queries whatever the data: two aggregates (leads; pipeline), the
    activity summary (two aggregates), three lists of at most LIST_LIMIT rows with their
    related records joined, and then the new leads' opportunities (at most LIST_LIMIT rows,
    over the opportunities' lead index; skipped when there are no new leads)."""
    leads = lead_selectors.lead_summary(scope, now=now)
    pipeline = pipeline_selectors.pipeline_totals(scope, OpportunityFilters())
    activities = activity_selectors.activity_summary(scope, now=now)
    new_leads = lead_selectors.new_leads_today(scope, now=now, limit=LIST_LIMIT)
    upcoming = activity_selectors.upcoming_meetings(scope, now=now, limit=LIST_LIMIT)
    next_tasks = activity_selectors.next_open_tasks(scope, now=now, limit=LIST_LIMIT)
    return Dashboard(
        business_date=business_date(now),
        leads=leads,
        pipeline=pipeline,
        activities=activities,
        new_leads=new_leads,
        # No query when there are no new leads.
        new_lead_opportunities=pipeline_selectors.first_opportunities(
            scope, [lead.pk for lead in new_leads]
        ),
        upcoming_meetings=upcoming,
        next_tasks=next_tasks,
    )
