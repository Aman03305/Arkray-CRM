"""Global search's query shapes (docs/search.md#how-a-search-runs; timings from the
1M-lead / 2M-activity benchmark, tests/performance/bench_search.py).

For every kind of record and every kind of scope this pins, on the exact SQL search runs,
what doesn't depend on table size:
- the recent pass walks the scope's newest-first index, in order (never a sort);
- the older pass is behind a one-time gate: "never executed" when the recent pass found
  enough, run when it didn't;
- nothing reads a table sequentially, and the older pass never walks the organisation's
  newest-first index (both would read every record);
- in one user's workspace the activity trigram indexes are looked up with the owner (their
  first column): never every owner's candidates, rechecked and then filtered by owner
  (260-460 ms at 2M activities before; review, P2).
Which index serves the older pass at production size (the trigram indexes: on small tables
PostgreSQL rightly prefers a b-tree) is checked on the benchmark database by
`bench_search.py --check-plans` (results in docs/search.md#indexes).
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from arkray.activities.models import Activity, ActivityStatus, ActivityType, Priority
from arkray.core import ranking
from arkray.core.access import AccessScope
from arkray.core.ranking import SearchQuery
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity
from arkray.search import selectors
from tests.factories import AdminFactory, UserFactory, default_stage

pytestmark = pytest.mark.django_db

# Large enough that index choices are made as at production size (an owner holds 2.5 %;
# the heaviest at 1M leads holds 6.7 %).
N_OWNERS, PER_OWNER = 40, 150
WORDS = ["quotation", "follow", "analyser", "tender", "demo", "renewal"]


@pytest.fixture(scope="module")
def dataset(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        owners = [UserFactory() for _ in range(N_OWNERS)]
        admin = AdminFactory()
        now = timezone.now()
        stage = default_stage("new")
        leads = Lead.objects.bulk_create(
            Lead(
                first_name=["Rahul", "Priya", "Amit"][i % 3],
                last_name=WORDS[i % len(WORDS)],
                organization_name=f"Clinic {i % 41}",
                status_id="new",
                owner=owners[i % N_OWNERS],
                created_by=owners[i % N_OWNERS],
                created_at=now - timedelta(minutes=i),
            )
            for i in range(N_OWNERS * PER_OWNER)
        )
        Opportunity.objects.bulk_create(
            Opportunity(
                title=f"{WORDS[i % len(WORDS)]} deal {i}",
                lead=lead,
                owner=lead.owner,
                created_by=lead.owner,
                stage=stage,
                pipeline=stage.pipeline,
                status=stage.category,
                value=1000,
                probability=stage.probability,
                account_name="Account",
                customer_name="Customer",
                created_at=lead.created_at,
            )
            for i, lead in enumerate(leads)
        )
        activities = []
        for i, lead in enumerate(leads):
            word = WORDS[i % len(WORDS)]
            common = {"lead": lead, "owner": lead.owner, "created_by": lead.owner}
            activities += [
                Activity(
                    type=ActivityType.TASK,
                    title=f"Send {word}",
                    status=ActivityStatus.OPEN,
                    priority=Priority.NORMAL,
                    created_at=lead.created_at,
                    **common,
                ),
                Activity(
                    type=ActivityType.MEETING,
                    title=f"{word} meeting",
                    location="Pune",
                    status=ActivityStatus.SCHEDULED,
                    starts_at=now,
                    ends_at=now + timedelta(hours=1),
                    created_at=lead.created_at,
                    **common,
                ),
                Activity(
                    type=ActivityType.NOTE,
                    description=f"Spoke about the {word} today.",
                    created_at=lead.created_at,
                    **common,
                ),
            ]
        Activity.objects.bulk_create(activities, batch_size=2000)
        with connection.cursor() as cursor:
            cursor.execute(
                "ANALYZE leads_lead; ANALYZE pipeline_opportunity; ANALYZE activities_activity", []
            )
        yield owners, admin
        Activity.objects.all().delete()
        Opportunity.objects.all().delete()
        Lead.objects.all().delete()
        for user in [*owners, admin]:
            user.delete()


def statements(scope: AccessScope, q: str) -> list[str]:
    """The SQL of each group's query, as global search runs it (captured, then explained)."""
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        selectors.global_search(scope, SearchQuery.parse(q))
    return [c["sql"] for c in captured.captured_queries if c["sql"].startswith("SELECT")]


def explain(sql: str, *, analyze: bool = False) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off", [])
        cursor.execute(f"EXPLAIN {'(ANALYZE) ' if analyze else ''}{sql}")
        return "\n".join(row[0] for row in cursor.fetchall())


def cte(plan: str, name: str) -> str:
    """The plan lines of one CTE (up to the next CTE or the main query)."""
    lines = plan.splitlines()
    start = next(i for i, line in enumerate(lines) if f"CTE {name}" in line)
    depth = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= depth:
            break
        body.append(line)
    return "\n".join(body)


NEWEST = {
    # (group position, own/user scope, organisation scope)
    0: ("leads_owner_created_idx", "leads_created_idx"),
    1: ("pipeline_opp_owner_created_idx", "pipeline_opp_created_idx"),
    # Activities rank by their own date (schedule_sort): one type's schedule index holds
    # exactly that type's live rows in that order, however rare the type is.
    2: ("activities_owner_type_when_idx", "activities_type_when_idx"),
    3: ("activities_owner_type_when_idx", "activities_type_when_idx"),
    4: ("activities_owner_type_when_idx", "activities_type_when_idx"),
}
TRIGRAM = {
    0: "leads_search_trgm",
    1: "pipeline_opp_text_trgm",  # renamed by pipeline.0007 (ADR-0027: title and customer names)
    2: "activities_task_search_trgm",
    3: "activities_meeting_search_trgm",
    4: "activities_note_search_trgm",
}


@pytest.mark.parametrize("kind", ["own", "user", "organisation"])
def test_each_pass_reads_the_index_designed_for_it(dataset, kind, monkeypatch):
    """RECENT is lowered to a tenth of an owner's records: the recent pass then has the shape it has
    at production size (a limited walk of the newest records)."""
    monkeypatch.setattr(ranking, "RECENT", 10)
    owners, admin = dataset
    scope = {
        "own": AccessScope.own(owners[0].pk),
        "user": AccessScope.for_user(admin.pk, owners[0].pk),
        "organisation": AccessScope.organization(admin.pk),
    }[kind]
    for position, sql in enumerate(statements(scope, "quotation")):
        plan = explain(sql)
        recent = cte(plan, "search_recent")
        newest = NEWEST[position][1 if kind == "organisation" else 0]
        assert re.search(rf"Index Scan(?: Backward)? using {newest}\b", recent), recent
        # Walked in order, never sorted (the only sort picks the words' fragments for the
        # candidate count: core.ranking._looked_up).
        assert all("substr(" in key for key in re.findall(r"Sort Key: (.*)", recent)), recent
        older = cte(plan, "search_older")
        assert "One-Time Filter" in older, older  # the gate
        if kind != "organisation":
            # In one user's workspace: the trigram index or that user's own records' index.
            used = set(re.findall(r"Bitmap Index Scan on (\w+)", older))
            assert used, older
            assert all(name == TRIGRAM[position] or "_owner_" in name for name in used), older
            if position > 1:
                # The activity trigram indexes only with the owner in their condition (leads
                # and opportunities, short texts, are trigram-only: docs/search.md#indexes).
                pattern = rf"Bitmap Index Scan on {TRIGRAM[position]}\n.*"
                for scan in re.finditer(pattern, older):
                    assert "Index Cond: ((owner_id = " in scan.group(0), older
            # Never the organisation's index (it would read every user's records).
            assert NEWEST[position][1] not in older, older
        assert "Seq Scan" not in plan, plan


def test_the_older_pass_never_runs_when_the_recent_pass_is_enough(dataset, monkeypatch):
    """A common word: the newest records hold enough matches, so the older pass's scan is
    "never executed" (its gate is a one-time filter)."""
    owners, _ = dataset
    monkeypatch.setattr(ranking, "RECENT", 60)
    monkeypatch.setattr(ranking, "WINDOW", 5)
    note_sql = statements(AccessScope.own(owners[0].pk), "Spoke")[4]  # every note has it
    older = cte(explain(note_sql, analyze=True), "search_older")
    assert "never executed" in older, older


def test_the_older_pass_runs_for_a_rare_word(dataset, monkeypatch):
    """Too few matches among the newest records: the older pass runs, through the index."""
    _, admin = dataset
    monkeypatch.setattr(ranking, "RECENT", 30)
    note_sql = statements(AccessScope.organization(admin.pk), "renewal")[4]
    older = cte(explain(note_sql, analyze=True), "search_older")
    scan = next(line for line in older.splitlines() if "Bitmap Heap Scan" in line)
    assert "never executed" not in scan, older
    assert "Seq Scan" not in older, older


def test_one_users_trigram_lookups_start_with_the_owner():
    """P2-2 (performance review): the activity trigram indexes lead with the owner
    (btree_gin), so a search in one person's workspace looks up only their records. The
    tables here are too small for PostgreSQL to choose them over the owner's b-tree;
    `bench_search.py --check-plans` checks it does at production size."""
    names = list(TRIGRAM.values())[2:]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY(%s)", [names]
        )
        definitions = dict(cursor.fetchall())
    assert sorted(definitions) == sorted(names)
    for name, definition in definitions.items():
        assert "USING gin (owner_id, upper(" in definition, (name, definition)
