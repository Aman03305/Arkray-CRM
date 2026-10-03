"""Every important activity and timeline query shape is servable by the index designed for it.

The timings come from a 403,000-activity benchmark (tests/performance/bench_activities.py,
docs/activities.md#performance). This test pins the *shapes*: it captures the exact SQL the
selectors run, forbids sequential scans (and, for list pages, explicit sorts) and checks
the planner reaches for the intended index. A change to an ordering, a filter or an index
that breaks the match fails here instead of in production.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities import selectors
from arkray.activities.models import Activity, ActivityStatus, ActivityType, TimelineEntry
from arkray.activities.selectors import TIMELINE_ORDERING, ActivityFilters
from arkray.core.access import AccessScope
from arkray.core.keyset import KeysetPaginator
from arkray.leads.models import Lead
from tests.factories import AdminFactory, UserFactory

pytestmark = pytest.mark.django_db

N_OWNERS, LEADS_PER_OWNER, PER_LEAD = 20, 15, 8
NOW = timezone.now()


@pytest.fixture(scope="module")
def dataset(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        owners = [UserFactory() for _ in range(N_OWNERS)]
        admin = AdminFactory()
        leads = Lead.objects.bulk_create(
            [
                Lead(first_name="Lead", last_name=str(i), owner=o, created_by=o, status_id="new")
                for o in owners
                for i in range(LEADS_PER_OWNER)
            ]
        )
        rows = []
        for n, lead in enumerate(lead for lead in leads for _ in range(PER_LEAD)):
            kind = (ActivityType.TASK, ActivityType.MEETING, ActivityType.NOTE)[n % 3]
            status = {
                ActivityType.TASK: (ActivityStatus.OPEN, ActivityStatus.COMPLETED)[n % 2],
                ActivityType.MEETING: (ActivityStatus.SCHEDULED, ActivityStatus.COMPLETED)[n % 2],
                ActivityType.NOTE: None,
            }[kind]
            start = NOW + timedelta(hours=(n % 300) - 150)
            rows.append(
                Activity(
                    type=kind,
                    lead=lead,
                    owner_id=lead.owner_id,
                    created_by_id=lead.owner_id,
                    title="" if kind == ActivityType.NOTE else f"Activity {n}",
                    description="Note body" if kind == ActivityType.NOTE else "",
                    status=status,
                    priority="normal" if kind == ActivityType.TASK else None,
                    due_at=start if kind == ActivityType.TASK and n % 4 else None,
                    starts_at=start if kind == ActivityType.MEETING else None,
                    ends_at=start + timedelta(hours=1) if kind == ActivityType.MEETING else None,
                    completed_at=NOW if status == ActivityStatus.COMPLETED else None,
                    completed_by_id=lead.owner_id if status == ActivityStatus.COMPLETED else None,
                    archived_at=NOW if n % 29 == 0 else None,
                    created_at=NOW - timedelta(minutes=n),
                )
            )
        created = Activity.objects.bulk_create(rows, batch_size=1000)
        TimelineEntry.objects.bulk_create(
            [
                TimelineEntry(
                    lead_id=a.lead_id,
                    activity_id=a.pk,
                    kind=f"{a.type}.created"
                    if a.type == ActivityType.TASK
                    else ("meeting.scheduled" if a.type == ActivityType.MEETING else "note.added"),
                    actor_id=a.owner_id,
                    occurred_at=a.created_at,
                    data={},
                )
                for a in created
            ],
            batch_size=1000,
        )
        with connection.cursor() as cursor:
            for table in ("leads_lead", "activities_activity", "activities_timeline_entry"):
                cursor.execute(f"ANALYZE {table}")
        yield owners, admin, leads
        with connection.cursor() as cursor:
            # The timeline is append-only (trigger): clear it the way tests' flushes do.
            cursor.execute("TRUNCATE activities_timeline_entry")
        Activity.objects.all().delete()
        Lead.objects.filter(pk__in=[lead.pk for lead in leads]).delete()
        for user in [*owners, admin]:
            user.delete()


def plan(sql: str, *disabled: str) -> str:
    with connection.cursor() as cursor:
        for setting in ("enable_seqscan", *disabled):
            cursor.execute(f"SET LOCAL {setting} = off")
        cursor.execute(f"EXPLAIN {sql}")
        return "\n".join(row[0] for row in cursor.fetchall())


def scans(text: str, table: str = "activities_activity") -> set[str]:
    found = set(
        re.findall(rf"(?:Index Scan|Index Only Scan)(?: Backward)? using (\w+) on {table}", text)
    )
    found |= set(re.findall(r"Bitmap Index Scan on (\w+)", text))
    if re.search(rf"Seq Scan on {table}\b", text):
        found.add("Seq Scan")
    return found


def page_sql(scope, filters, ordering, cursor=None) -> str:
    paginator = KeysetPaginator(selectors.ordering(ordering, scope, filters), page_size=25)
    queryset = selectors.activity_list(scope, filters, now=NOW)
    sql, params = paginator.window(queryset, cursor)[0].query.sql_with_params()
    with connection.cursor() as c:
        return str(c.mogrify(sql, params))


def scopes(dataset):
    owners, admin, _ = dataset
    return {"own": AccessScope.own(owners[0].pk), "org": AccessScope.organization(admin.pk)}


# (label, filters, ordering, the index suffixes that may serve it: "activities_owner_" +
# suffix for one owner, "activities_" + suffix organisation-wide).
SCHEDULED, CURRENT, WHEN, TYPE_WHEN = "sched_idx", "current_idx", "when_idx", "type_when_idx"
TAB_SHAPES = [
    ("tasks open, soonest", ActivityFilters(type="task", status="open"), "scheduled", {SCHEDULED}),
    ("tasks open, latest", ActivityFilters(type="task", status="open"), "-scheduled", {SCHEDULED}),
    ("overdue", ActivityFilters(overdue=True), "scheduled", {SCHEDULED, CURRENT}),
    (
        "meetings scheduled",
        ActivityFilters(type="meeting", status="scheduled"),
        "scheduled",
        {SCHEDULED},
    ),
    ("all by due/start", ActivityFilters(), "scheduled", {WHEN}),
    # Current work and one type of any status read indexes without the closed history: the
    # all-statuses index made them filter past it (2M activities: up to 0.5 s; review).
    ("current work", ActivityFilters(current=True), "scheduled", {CURRENT}),
    ("current work, latest", ActivityFilters(current=True), "-scheduled", {CURRENT}),
    ("upcoming", ActivityFilters(upcoming=True), "scheduled", {CURRENT}),
    (
        "upcoming meetings",
        ActivityFilters(type="meeting", upcoming=True),
        "scheduled",
        {SCHEDULED, CURRENT, TYPE_WHEN},  # each with a range from now on
    ),
    ("tasks any status", ActivityFilters(type="task"), "scheduled", {TYPE_WHEN}),
    ("meetings any status, latest", ActivityFilters(type="meeting"), "-scheduled", {TYPE_WHEN}),
]


@pytest.mark.parametrize(("kind", "prefix"), [("own", "activities_owner_"), ("org", "activities_")])
@pytest.mark.parametrize(("label", "filters", "ordering", "suffixes"), TAB_SHAPES)
def test_schedule_views_come_out_of_the_schedule_indexes(
    dataset, kind, prefix, label, filters, ordering, suffixes
):
    text = plan(page_sql(scopes(dataset)[kind], filters, ordering), "enable_sort")
    assert scans(text) & {prefix + suffix for suffix in suffixes}, (label, text)
    assert "Seq Scan" not in scans(text), text
    assert "Sort" not in text, (label, text)


@pytest.mark.parametrize("kind", ["own", "org"])
@pytest.mark.parametrize("ordering", ["-created_at", "created_at"])
@pytest.mark.parametrize(
    "filters", [ActivityFilters(), ActivityFilters(type="note"), ActivityFilters(archived=True)]
)
def test_newest_first_views_use_the_created_indexes(dataset, kind, ordering, filters):
    text = plan(page_sql(scopes(dataset)[kind], filters, ordering), "enable_sort")
    # Exactly one candidate each: one owner's list can't be served by walking the whole
    # organisation and filtering (Phase 4 review, P1: 48 ms at 403k activities, up to
    # 500 ms at 2M, for an owner whose matching rows were rare or old).
    expected = "activities_owner_created_idx" if kind == "own" else "activities_created_idx"
    assert scans(text) == {expected}, text
    assert "Sort" not in text, text


@pytest.mark.parametrize("ordering", ["-created_at", "created_at"])
@pytest.mark.parametrize(
    "filters", [ActivityFilters(), ActivityFilters(type="note"), ActivityFilters(archived=True)]
)
def test_an_admins_owner_filter_reads_that_owners_index(dataset, ordering, filters):
    owners, admin, _ = dataset
    org = AccessScope.organization(admin.pk)
    narrowed = dataclasses.replace(filters, owner_id=owners[0].pk)
    text = plan(page_sql(org, narrowed, ordering), "enable_sort")
    assert scans(text) == {"activities_owner_created_idx"}, text
    assert "Sort" not in text, text


def test_a_deep_cursor_still_starts_at_the_index(dataset):
    scope = scopes(dataset)["own"]
    filters = ActivityFilters(type="task", status="open")
    first = KeysetPaginator(selectors.ORDERINGS["scheduled"], page_size=5).paginate(
        selectors.activity_list(scope, filters, now=NOW), None
    )
    text = plan(page_sql(scope, filters, "scheduled", first.next_cursor))
    assert scans(text) & {
        "activities_owner_sched_idx",
        "activities_owner_when_idx",
        "activities_owner_current_idx",
    }, text
    assert "Seq Scan" not in scans(text), text


@pytest.mark.parametrize("kind", ["own", "org"])
def test_the_summary_never_scans_one_owners_table_sequentially(dataset, kind):
    scope = scopes(dataset)[kind]
    with CaptureQueriesContext(connection) as queries:
        selectors.activity_summary(scope, now=NOW)
    # Two aggregates, each one bounded range of a schedule index: the open tasks, and the
    # meetings from today's start on (Phase 5 review: one OR over both read the owner's whole
    # history, and organisation-wide flipped to a sequential scan as open work grew).
    tasks, meetings = [q["sql"] for q in queries.captured_queries]
    prefix = "activities_owner_" if kind == "own" else "activities_"
    for sql in (tasks, meetings):
        text = plan(sql)
        assert "Seq Scan" not in scans(text), text
        assert any(name.startswith(prefix) for name in scans(text)), text
    assert prefix + "sched_idx" in scans(plan(tasks)), plan(tasks)


def test_everything_about_one_lead_uses_the_lead_index(dataset):
    _, admin, leads = dataset
    lead = leads[0]
    org = AccessScope.organization(admin.pk)
    shapes = [
        page_sql(org, ActivityFilters(lead_id=lead.pk), "-created_at"),
        page_sql(org, ActivityFilters(lead_id=lead.pk, current=True), "scheduled"),
    ]
    for sql in shapes:
        assert "activities_lead_idx" in scans(plan(sql)), plan(sql)
    with connection.cursor() as c:
        fk_check = plan(
            c.mogrify(
                "SELECT 1 FROM activities_activity WHERE lead_id = %s AND current_owner_id = %s",
                [lead.pk, lead.owner_id],
            )
        )
        reassignment = plan(
            c.mogrify(
                "SELECT id FROM activities_activity WHERE lead_id = %s AND (type = 'note' OR "
                "status IN ('open', 'scheduled')) ORDER BY id",
                [lead.pk],
            )
        )
    assert "activities_lead_idx" in scans(fk_check), fk_check
    assert "activities_lead_idx" in scans(reassignment), reassignment


@pytest.mark.parametrize("kind", ["own", "org"])
def test_a_timeline_page_walks_the_leads_entries_in_order(dataset, kind):
    _, admin, leads = dataset
    scope = (
        AccessScope.own(leads[0].owner_id) if kind == "own" else AccessScope.organization(admin.pk)
    )
    queryset = selectors.lead_timeline(scope, leads[0].pk)
    sql, params = (
        KeysetPaginator(TIMELINE_ORDERING, page_size=20)
        .window(queryset, None)[0]
        .query.sql_with_params()
    )
    with connection.cursor() as c:
        text = plan(c.mogrify(sql, params), "enable_sort")
    assert "timeline_lead_idx" in scans(text, "activities_timeline_entry"), text
    assert "Sort" not in text, text
