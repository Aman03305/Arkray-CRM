"""Every important pipeline query shape is servable by the index designed for it.

The timings come from a 300,000-opportunity benchmark (tests/performance/bench_pipeline.py,
docs/database.md#pipeline_opportunity). This test pins the *shapes*: it captures the exact
SQL the selectors run, forbids sequential scans (and, for pages, explicit sorts) and checks
the planner reaches for the intended index. A change to an ordering, a filter or an index
that breaks the match fails here instead of in production.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.core.access import AccessScope
from arkray.core.keyset import KeysetPaginator
from arkray.leads.models import Lead
from arkray.pipeline import selectors
from arkray.pipeline.models import Opportunity, Pipeline
from arkray.pipeline.selectors import BOARD_ORDERING, ORDERINGS, OpportunityFilters
from tests.factories import AdminFactory, OpportunityFactory, UserFactory

pytestmark = pytest.mark.django_db

N_OWNERS, LEADS_PER_OWNER, PER_LEAD = 30, 20, 5


@pytest.fixture(scope="module")
def dataset(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        owners = [UserFactory() for _ in range(N_OWNERS)]
        admin = AdminFactory()
        pipeline = Pipeline.objects.get(is_default=True)
        stages = list(pipeline.stages.order_by("position"))
        now = timezone.now()
        leads = Lead.objects.bulk_create(
            [
                Lead(
                    first_name="Lead",
                    last_name=str(i),
                    owner=owner,
                    created_by=owner,
                    status_id="new",
                )
                for owner in owners
                for i in range(LEADS_PER_OWNER)
            ]
        )
        rows = []
        for n, lead in enumerate(lead for lead in leads for _ in range(PER_LEAD)):
            stage = stages[n % len(stages)]
            rows.append(
                OpportunityFactory.build(
                    lead=lead,
                    stage=stage,
                    value=Decimal(n % 997),
                    expected_close_date=None
                    if n % 5 == 0
                    else (now + timedelta(days=n % 400)).date(),
                    created_at=now - timedelta(minutes=n),
                    closed_at=now - timedelta(minutes=n) if stage.category != "open" else None,
                    archived_at=now if n % 31 == 0 else None,
                )
            )
        Opportunity.objects.bulk_create(rows, batch_size=1000)
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE pipeline_opportunity")
        yield owners, admin, pipeline
        Opportunity.objects.all().delete()
        Lead.objects.filter(pk__in=[lead.pk for lead in leads]).delete()
        for user in [*owners, admin]:
            user.delete()


def plan(sql: str, *disabled: str) -> str:
    with connection.cursor() as cursor:
        for setting in ("enable_seqscan", *disabled):
            cursor.execute(f"SET LOCAL {setting} = off")
        cursor.execute(f"EXPLAIN {sql}")
        return "\n".join(row[0] for row in cursor.fetchall())


def scans(text: str) -> set[str]:
    found = set(
        re.findall(
            r"(?:Index Scan|Index Only Scan)(?: Backward)? using (\w+) on pipeline_opportunity",
            text,
        )
    )
    found |= set(re.findall(r"Bitmap Index Scan on (pipeline_\w+)", text))
    if re.search(r"Seq Scan on pipeline_opportunity\b", text):
        found.add("Seq Scan")
    return found


def captured(call) -> list[str]:
    with CaptureQueriesContext(connection) as queries:
        call()
    return [q["sql"] for q in queries.captured_queries if "pipeline_opportunity" in q["sql"]]


def scopes(dataset):
    owners, admin, _ = dataset
    return {"own": AccessScope.own(owners[0].pk), "org": AccessScope.organization(admin.pk)}


@pytest.mark.parametrize(("kind", "suffix"), [("own", "owner_"), ("org", "")])
def test_the_boards_cards_come_from_the_board_indexes(dataset, kind, suffix):
    _, _, pipeline = dataset
    scope = scopes(dataset)[kind]
    _aggregates, _totals, cards = captured(
        lambda: selectors.board(scope, pipeline, OpportunityFilters(), binding_for=None)
    )
    text = plan(cards, "enable_sort")
    assert {f"pipeline_opp_{suffix}open_idx", f"pipeline_opp_{suffix}closed_idx"} <= scans(text)
    assert "Seq Scan" not in scans(text), text
    assert "Sort" not in text, text  # every stage's page comes out of its index in order


def test_one_owners_totals_use_the_open_index(dataset):
    scope = scopes(dataset)["own"]
    (sql,) = captured(lambda: selectors.pipeline_totals(scope, OpportunityFilters()))
    assert "pipeline_opp_owner_open_idx" in scans(plan(sql)), plan(sql)


@pytest.mark.parametrize(("kind", "suffix"), [("own", "owner_"), ("org", "")])
@pytest.mark.parametrize("stage_key", ["new", "negotiation", "won", "lost"])
def test_a_stage_list_uses_the_board_indexes(dataset, kind, suffix, stage_key):
    _, _, pipeline = dataset
    scope = scopes(dataset)[kind]
    stage = pipeline.stages.get(key=stage_key)
    paginator = KeysetPaginator(BOARD_ORDERING[stage.category], page_size=25, binding=None)
    first = paginator.paginate(
        selectors.opportunity_list(scope, OpportunityFilters(stage_id=stage.pk)), None
    )
    queryset = selectors.opportunity_list(scope, OpportunityFilters(stage_id=stage.pk))
    for cursor in (None, first.next_cursor):
        sql, params = paginator.window(queryset, cursor)[0].query.sql_with_params()
        with connection.cursor() as c:
            sql = c.mogrify(sql, params)
        text = plan(sql, "enable_sort")
        expected = f"pipeline_opp_{suffix}{'open' if stage.category == 'open' else 'closed'}_idx"
        assert expected in scans(text), text
        assert "Sort" not in text, text


@pytest.mark.parametrize(
    ("kind", "index"),
    [("own", "pipeline_opp_owner_created_idx"), ("org", "pipeline_opp_created_idx")],
)
@pytest.mark.parametrize("ordering", ["-created_at", "created_at"])
@pytest.mark.parametrize("archived", [False, True])
def test_default_lists_use_the_created_indexes(dataset, kind, index, ordering, archived):
    scope = scopes(dataset)[kind]
    paginator = KeysetPaginator(ORDERINGS[ordering], page_size=25, binding=None)
    queryset = selectors.opportunity_list(scope, OpportunityFilters(archived=archived))
    sql, params = paginator.window(queryset, None)[0].query.sql_with_params()
    with connection.cursor() as c:
        text = plan(c.mogrify(sql, params), "enable_sort")
    assert index in scans(text), text


def test_everything_about_one_lead_uses_the_lead_index(dataset):
    lead_id = Opportunity.objects.values_list("lead_id", flat=True).first()
    owner_id = Lead.objects.values_list("owner_id", flat=True).get(pk=lead_id)
    shapes = [
        *captured(
            lambda: KeysetPaginator(ORDERINGS["-created_at"], page_size=25, binding=None).paginate(
                selectors.opportunity_list(
                    AccessScope.own(owner_id), OpportunityFilters(lead_id=lead_id)
                ),
                None,
            )
        ),
        *captured(
            lambda: list(Opportunity.objects.filter(lead_id=lead_id, status="open").order_by("id"))
        ),
        *captured(lambda: Opportunity.objects.filter(lead_id=lead_id).exists()),
    ]
    for sql in shapes:
        assert "pipeline_opp_lead_idx" in scans(plan(sql)), plan(sql)
    with connection.cursor() as c:
        fk_check = plan(
            c.mogrify(
                "SELECT 1 FROM pipeline_opportunity WHERE lead_id = %s AND open_owner_id = %s",
                [lead_id, owner_id],
            )
        )
    assert "pipeline_opp_lead_idx" in scans(fk_check), fk_check
