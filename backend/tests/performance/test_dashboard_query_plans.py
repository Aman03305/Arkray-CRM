"""Every dashboard query is servable by the index designed for it.

The timings come from tests/performance/bench_dashboard.py (docs/dashboard.md#performance).
This test pins the *shapes*: it captures the exact SQL `dashboard.selectors.dashboard()`
runs, in order, and checks each plan with sequential scans disabled. In particular the lead
figures must stay an index-only scan of leads_owner_created_idx (which carries the archive
state for this): without it the heaviest owner's count read every one of their rows (93 ms
at 66,700 leads).
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities.models import Activity, ActivityStatus, ActivityType
from arkray.core.access import AccessScope
from arkray.dashboard import selectors
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity, Pipeline
from tests.factories import AdminFactory, UserFactory

pytestmark = pytest.mark.django_db

N_OWNERS, LEADS_PER_OWNER = 12, 40
NOW = timezone.now()


@pytest.fixture(scope="module")
def dataset(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        owners = [UserFactory() for _ in range(N_OWNERS)]
        admin = AdminFactory()
        leads = Lead.objects.bulk_create(
            [
                Lead(
                    first_name="Lead",
                    last_name=str(i),
                    owner=o,
                    created_by=o,
                    status_id="new",
                    created_at=NOW - timedelta(hours=i * 3),
                    archived_at=NOW if i % 10 == 0 else None,
                )
                for o in owners
                for i in range(LEADS_PER_OWNER)
            ]
        )
        stage = Pipeline.objects.get(is_default=True).stages.get(key="proposal")
        Opportunity.objects.bulk_create(
            [
                Opportunity(
                    title=f"Deal {n}",
                    lead=lead,
                    owner_id=lead.owner_id,
                    created_by_id=lead.owner_id,
                    pipeline_id=stage.pipeline_id,
                    stage=stage,
                    status="open",
                    value=Decimal("1000.00"),
                    probability=stage.probability,
                )
                for n, lead in enumerate(leads)
                if n % 2
            ]
        )
        rows = []
        for n, lead in enumerate(leads):
            when = NOW + timedelta(hours=(n % 96) - 48)
            if n % 2:
                rows.append(
                    Activity(
                        type=ActivityType.TASK,
                        lead=lead,
                        owner_id=lead.owner_id,
                        created_by_id=lead.owner_id,
                        title=f"Task {n}",
                        status=ActivityStatus.OPEN,
                        priority="normal",
                        due_at=when,
                    )
                )
            else:
                rows.append(
                    Activity(
                        type=ActivityType.MEETING,
                        lead=lead,
                        owner_id=lead.owner_id,
                        created_by_id=lead.owner_id,
                        title=f"Meeting {n}",
                        status=ActivityStatus.SCHEDULED,
                        starts_at=when,
                        ends_at=when + timedelta(hours=1),
                    )
                )
        Activity.objects.bulk_create(rows, batch_size=1000)
        with connection.cursor() as cursor:
            # As autovacuum keeps it in production: index-only scans need the visibility map.
            for table in ("leads_lead", "pipeline_opportunity", "activities_activity"):
                cursor.execute(f"VACUUM ANALYZE {table}")
        yield owners, admin
        Activity.objects.all().delete()
        Opportunity.objects.all().delete()
        Lead.objects.filter(pk__in=[lead.pk for lead in leads]).delete()
        for user in [*owners, admin]:
            user.delete()


def statements(scope: AccessScope) -> list[str]:
    """The dashboard's seven queries, verbatim and in order: lead figures, pipeline totals,
    activity figures, today's newest leads, next meetings, next open tasks."""
    with CaptureQueriesContext(connection) as captured:
        selectors.dashboard(scope, now=NOW)
    found = [q["sql"] for q in captured.captured_queries if q["sql"].startswith("SELECT")]
    assert len(found) == 7, found
    return found


def plan(sql: str, *disabled: str) -> str:
    with connection.cursor() as cursor:
        for setting in ("enable_seqscan", *disabled):
            cursor.execute(f"SET LOCAL {setting} = off")
        cursor.execute(f"EXPLAIN {sql}")
        return "\n".join(row[0] for row in cursor.fetchall())


def scans(text: str) -> set[str]:
    found = set(re.findall(r"(?:Index Scan|Index Only Scan)(?: Backward)? using (\w+)", text))
    found |= set(re.findall(r"Bitmap Index Scan on (\w+)", text))
    found |= {f"Seq Scan on {t}" for t in re.findall(r"Seq Scan on (\w+)", text)}
    return found


def scopes(dataset):
    owners, admin = dataset
    return {"own": AccessScope.own(owners[0].pk), "org": AccessScope.organization(admin.pk)}


@pytest.mark.parametrize("kind", ["own", "org"])
def test_lead_figures_are_counted_from_the_index_alone(dataset, kind):
    lead_figures = statements(scopes(dataset)[kind])[0]
    assert "leads_lead" in lead_figures
    assert "COUNT" in lead_figures
    text = plan(lead_figures, "enable_bitmapscan")
    assert "Index Only Scan using leads_owner_created_idx on leads_lead" in text, text


@pytest.mark.parametrize("kind", ["own", "org"])
def test_todays_newest_leads_come_out_of_a_date_index_without_sorting(dataset, kind):
    newest = statements(scopes(dataset)[kind])[4]
    text = plan(newest, "enable_sort")
    expected = "leads_owner_created_idx" if kind == "own" else "leads_created_idx"
    assert expected in scans(text), text
    assert "Sort" not in text, text


@pytest.mark.parametrize("kind", ["own", "org"])
def test_the_activity_lists_and_figures_use_the_schedule_indexes(dataset, kind):
    prefix = "activities_owner_" if kind == "own" else "activities_"
    _, _, task_figures, meeting_figures, _, meetings, tasks = statements(scopes(dataset)[kind])
    for sql in (task_figures, meeting_figures, meetings, tasks):
        text = plan(sql)
        assert any(name.startswith(prefix) for name in scans(text)), text
        assert "Seq Scan on activities_activity" not in scans(text), text


def test_one_owners_pipeline_totals_read_only_that_owners_opportunities(dataset):
    """Through an owner-leading index (which one depends on the open/closed mix: the open
    one at benchmark scale), never by scanning the organisation's opportunities."""
    totals = statements(scopes(dataset)["own"])[1]
    assert "pipeline_opportunity" in totals
    assert "SUM" in totals
    used = scans(plan(totals))
    assert used & {"pipeline_opp_owner_open_idx", "pipeline_opp_owner_created_idx"}, used
    assert "Seq Scan on pipeline_opportunity" not in used, used
