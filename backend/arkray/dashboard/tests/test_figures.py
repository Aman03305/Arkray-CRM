"""The dashboard's figures are exactly the owning modules' authoritative figures: the pipeline
totals of Phase 3, the activity summary and lists of Phase 4 and the lead figures, each
computed in the request's scope, with "today" as the business day in Asia/Kolkata."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from arkray.activities.models import ActivityStatus
from arkray.activities.selectors import activity_summary, next_open_tasks, upcoming_meetings
from arkray.core.access import AccessScope
from arkray.core.business_time import business_date
from arkray.dashboard import selectors
from arkray.dashboard.selectors import LIST_LIMIT
from arkray.leads.selectors import lead_summary, new_leads_today
from arkray.pipeline.models import Pipeline, Stage
from arkray.pipeline.selectors import OpportunityFilters, pipeline_totals
from tests.factories import LeadFactory, MeetingFactory, OpportunityFactory, TaskFactory

from .conftest import IST_DAY_END, IST_DAY_START, MICROSECOND, NOW, dashboard_url

pytestmark = pytest.mark.django_db

OLD = NOW - timedelta(days=30)
D = Decimal


def lead(owner, created_at=OLD, **extra):
    return LeadFactory(owner=owner, created_at=created_at, **extra)


def meeting(lead_, start, status=ActivityStatus.SCHEDULED, **extra):
    return MeetingFactory(
        lead=lead_, starts_at=start, ends_at=start + timedelta(minutes=30), status=status, **extra
    )


def get(client, workspace="me"):
    response = client.get(dashboard_url(workspace))
    assert response.status_code == 200, response.content
    return response.json()


# --- pipeline value and weighted pipeline (Phase 3's definitions) -----------------------------
def test_the_briefs_example_open_only(user_a_client, user_a, frozen_now):
    customer = lead(user_a)
    OpportunityFactory(lead=customer, stage_key="proposal", value=D("1000000.00"))  # 50 %
    OpportunityFactory(
        lead=customer,
        stage_key="proposal",
        value=D("500000.00"),
        probability=D("80.00"),
        probability_overridden=True,
    )
    OpportunityFactory(lead=customer, stage_key="won", value=D("200000.00"))  # 100 %
    OpportunityFactory(lead=customer, stage_key="lost", value=D("300000.00"))
    OpportunityFactory(
        lead=customer, stage_key="negotiation", value=D("400000.00"), archived_at=NOW
    )

    body = get(user_a_client)

    assert body["pipeline"] == {
        "pipeline_value": "1500000.00",
        "weighted_pipeline": "900000.00",
        "open_count": 2,
    }
    assert body["currency"] == "INR"


def test_money_is_exact_decimal_text_rounded_once(user_a_client, user_a, frozen_now):
    customer = lead(user_a)
    # 0.1 + 0.2 is 0.30000000000000004 in binary floating point; weighted values of half a
    # paisa and of 749999999999.9925 are summed exactly and rounded once (rounding each
    # opportunity's weighted value first would give 18750000000000.08).
    for value in ("0.10", "0.20"):
        OpportunityFactory(lead=customer, stage_key="won", value=D("1.00"))
        OpportunityFactory(
            lead=customer,
            stage_key="new",
            value=D(value),
            probability=D("100.00"),
            probability_overridden=True,
        )
    for _ in range(3):
        OpportunityFactory(
            lead=customer,
            stage_key="new",
            value=D("0.01"),
            probability=D("50.00"),
            probability_overridden=True,
        )
    # Far beyond the integers a float holds exactly once multiplied into paise.
    for _ in range(25):
        OpportunityFactory(lead=customer, stage_key="negotiation", value=D("999999999999.99"))

    pipeline = get(user_a_client)["pipeline"]

    assert pipeline == {
        "pipeline_value": "25000000000000.08",
        "weighted_pipeline": "18750000000000.13",
        "open_count": 30,
    }
    totals = pipeline_totals(AccessScope.own(user_a.pk), OpportunityFilters())
    assert D(pipeline["pipeline_value"]) == totals.pipeline_value
    assert D(pipeline["weighted_pipeline"]) == totals.weighted_pipeline
    assert isinstance(pipeline["pipeline_value"], str)
    assert isinstance(pipeline["weighted_pipeline"], str)


def test_pipeline_figures_are_the_pipeline_summary(user_a_client, user_a, frozen_now):
    """Random amounts, probabilities and outcomes: the dashboard shows what the pipeline's
    own summary endpoint shows, digit for digit (one implementation, never re-derived)."""
    rng = random.Random(5)
    customer = lead(user_a)
    for _ in range(60):
        stage = rng.choice(["new", "qualified", "proposal", "negotiation", "won", "lost"])
        extra = {}
        if stage not in ("won", "lost") and rng.random() < 0.5:
            extra = {"probability": D(rng.randint(0, 10000)) / 100, "probability_overridden": True}
        OpportunityFactory(
            lead=customer,
            stage_key=stage,
            value=D(rng.randint(0, 10**12)) / 100,
            archived_at=NOW if rng.random() < 0.1 else None,
            **extra,
        )
    summary = user_a_client.get("/api/v1/workspaces/me/pipeline-summary").json()
    assert get(user_a_client)["pipeline"] == summary["totals"]


# --- tasks (Phase 4's definitions) --------------------------------------------------------------
def test_task_figures_and_list_follow_phase4(user_a_client, user_a, frozen_now):
    customer = lead(user_a)
    overdue_yesterday = TaskFactory(lead=customer, due_at=IST_DAY_START - timedelta(minutes=1))
    overdue_today = TaskFactory(lead=customer, due_at=IST_DAY_START + timedelta(minutes=5))
    due_later_today = TaskFactory(lead=customer, due_at=IST_DAY_END - timedelta(minutes=1))
    future = TaskFactory(lead=customer, due_at=IST_DAY_END + timedelta(days=2))
    undated = TaskFactory(lead=customer)
    TaskFactory(lead=customer, due_at=NOW, status=ActivityStatus.COMPLETED)
    TaskFactory(lead=customer, due_at=NOW, status=ActivityStatus.CANCELLED)
    TaskFactory(lead=customer, due_at=NOW - timedelta(days=3), archived_at=NOW)  # was overdue

    body = get(user_a_client)

    figures = body["activities"]
    assert figures["open_tasks"] == 5
    assert figures["tasks_due_today"] == 2  # earlier today (also overdue) and later today
    assert figures["overdue_tasks"] == 2
    expected = [overdue_yesterday, overdue_today, due_later_today, future, undated]
    assert [row["id"] for row in body["next_tasks"]] == [str(t.pk) for t in expected]
    assert [row["is_overdue"] for row in body["next_tasks"]] == [True, True, False, False, False]
    assert {row["status"] for row in body["next_tasks"]} == {"open"}


# --- meetings (Phase 4's definitions) ------------------------------------------------------------
def test_meeting_figures_and_list_follow_phase4(user_a_client, user_a, frozen_now):
    customer = lead(user_a)
    later_today = meeting(customer, NOW + timedelta(hours=3))
    meeting(customer, IST_DAY_START + timedelta(minutes=5))  # started 10 minutes ago
    meeting(customer, IST_DAY_START + timedelta(minutes=1), ActivityStatus.COMPLETED)
    meeting(customer, NOW + timedelta(hours=1), ActivityStatus.CANCELLED)
    meeting(customer, NOW + timedelta(hours=2), archived_at=NOW)
    meeting(customer, IST_DAY_START - timedelta(hours=2))  # yesterday, never completed
    future = meeting(customer, IST_DAY_END + timedelta(hours=2))

    body = get(user_a_client)

    assert body["activities"]["meetings_today"] == 3  # scheduled (2) and completed (1)
    assert body["activities"]["upcoming_meetings"] == 2  # from now on: later today, tomorrow
    assert [row["id"] for row in body["upcoming_meetings"]] == [str(later_today.pk), str(future.pk)]


# --- leads ---------------------------------------------------------------------------------------
def test_lead_figures_follow_the_business_day_not_utc(user_a_client, user_a, user_b, frozen_now):
    lead(user_a, created_at=IST_DAY_START - MICROSECOND)  # 23:59:59.999999 IST, 2 October
    lead(user_a, created_at=NOW.replace(hour=0, minute=0))  # 00:00 UTC = 05:30 IST, 2 October
    at_midnight = lead(user_a, created_at=IST_DAY_START, first_name="Midnight", last_name="Lead")
    just_now = lead(user_a, created_at=NOW - timedelta(minutes=1), organization_name="Acme Labs")
    lead(user_a, created_at=NOW - timedelta(minutes=2), archived_at=NOW)  # archived: never
    lead(user_a)  # a month old
    lead(user_b, created_at=NOW - timedelta(minutes=3))  # someone else's

    body = get(user_a_client)

    assert body["business_date"] == "2026-10-03"  # while UTC is still on 2 October
    assert body["time_zone"] == "Asia/Kolkata"
    assert body["leads"] == {"total": 5, "new_today": 2}
    assert body["new_leads"] == [
        {
            "id": str(just_now.pk),
            "display_name": just_now.display_name,
            "organization_name": "Acme Labs",
            "owner": {"id": str(user_a.pk), "full_name": "Rahul Sharma", "is_active": True},
            "created_at": "2026-10-02T18:44:00Z",
        },
        {
            "id": str(at_midnight.pk),
            "display_name": "Midnight Lead",
            "organization_name": at_midnight.organization_name,
            "owner": {"id": str(user_a.pk), "full_name": "Rahul Sharma", "is_active": True},
            "created_at": "2026-10-02T18:30:00Z",
        },
    ]


def test_a_day_is_half_open_at_both_ends(user_a):
    """The last microsecond of the business day is today; its end is tomorrow."""
    scope = AccessScope.own(user_a.pk)
    last_moment = IST_DAY_END - MICROSECOND
    lead(user_a, created_at=last_moment)
    lead(user_a, created_at=IST_DAY_START - MICROSECOND)
    assert lead_summary(scope, now=last_moment).new_today == 1
    assert lead_summary(scope, now=IST_DAY_END).new_today == 0
    assert business_date(last_moment) != business_date(IST_DAY_END)


def test_supporting_lists_are_bounded(user_a_client, user_a, frozen_now):
    customer = lead(user_a)
    for minutes in range(1, 9):
        lead(user_a, created_at=NOW - timedelta(minutes=minutes))
        TaskFactory(lead=customer, due_at=NOW + timedelta(hours=minutes))
        meeting(customer, NOW + timedelta(hours=minutes))

    body = get(user_a_client)

    assert body["leads"]["new_today"] == 8
    assert body["activities"]["open_tasks"] == 8
    assert body["activities"]["upcoming_meetings"] == 8
    for rows in (body["new_leads"], body["next_tasks"], body["upcoming_meetings"]):
        assert len(rows) == LIST_LIMIT == 5
    created = [row["created_at"] for row in body["new_leads"]]
    assert created == sorted(created, reverse=True)  # newest first


def test_rows_show_names_never_contact_data(user_a_client, user_a, frozen_now):
    customer = lead(
        user_a,
        created_at=NOW - timedelta(minutes=1),
        email="asha@apollo.example",
        phone="+91 98765 43210",
    )
    TaskFactory(lead=customer, due_at=NOW + timedelta(hours=1))
    raw = user_a_client.get(dashboard_url()).content.decode()
    assert "asha@apollo.example" not in raw
    assert "98765" not in raw
    assert user_a.email not in raw
    (row,) = get(user_a_client)["new_leads"]
    assert set(row) == {"id", "display_name", "organization_name", "owner", "created_at"}
    assert set(row["owner"]) == {"id", "full_name", "is_active"}


def test_a_new_user_sees_zeros_not_errors(user_a_client, frozen_now):
    assert get(user_a_client) == {
        "currency": "INR",
        "time_zone": "Asia/Kolkata",
        "business_date": "2026-10-03",
        "leads": {"total": 0, "new_today": 0},
        "pipeline": {"pipeline_value": "0.00", "weighted_pipeline": "0.00", "open_count": 0},
        "activities": {
            "open_tasks": 0,
            "tasks_due_today": 0,
            "overdue_tasks": 0,
            "meetings_today": 0,
            "upcoming_meetings": 0,
        },
        "new_leads": [],
        "upcoming_meetings": [],
        "next_tasks": [],
    }


def test_today_is_the_requests_clock_not_the_servers(user_a):
    """`business_date` and every "today" come from the `now` the request read, never from
    another clock (the domain review's mutation survived only on the day the tests ran)."""
    later = datetime(2031, 1, 14, 20, 0, tzinfo=UTC)  # 01:30 IST on 15 January 2031
    lead(user_a, created_at=later - timedelta(minutes=5))
    figures = selectors.dashboard(AccessScope.own(user_a.pk), now=later)
    assert figures.business_date == date(2031, 1, 15)
    assert figures.leads.new_today == 1


def test_the_pipeline_figures_cover_every_pipeline(user_a_client, user_a, frozen_now):
    """Pipeline value = all of the workspace's open opportunities, whatever pipeline they are
    in (Phase 3's pipeline-summary without a pipeline filter). The board shows one pipeline
    at a time, so with a second pipeline its totals are that pipeline's share
    (docs/dashboard.md)."""
    customer = lead(user_a)
    OpportunityFactory(lead=customer, stage_key="proposal", value=D("100000.00"))
    service = Pipeline.objects.create(key="service", name="Service contracts")
    open_stage = Stage.objects.create(
        pipeline=service,
        key="quote",
        name="Quote",
        position=10,
        probability=D("10"),
        category="open",
    )
    OpportunityFactory(lead=customer, stage=open_stage, value=D("50000.00"))

    body = get(user_a_client)

    assert body["pipeline"] == {
        "pipeline_value": "150000.00",
        "weighted_pipeline": "55000.00",
        "open_count": 2,
    }
    summary = user_a_client.get("/api/v1/workspaces/me/pipeline-summary").json()
    assert body["pipeline"] == summary["totals"]
    board = user_a_client.get("/api/v1/workspaces/me/pipeline-board").json()
    assert board["totals"]["pipeline_value"] == "100000.00"  # the default pipeline's share


# --- composition: nothing re-derived -------------------------------------------------------------
def test_every_figure_is_the_owning_modules_figure(admin, user_a, user_b):
    """In every kind of workspace, each figure and list is the owning module's selector
    called with that workspace's scope: the dashboard defines nothing itself."""
    rng = random.Random(7)
    for owner in (user_a, user_b, admin):
        for _ in range(4):
            customer = lead(owner, created_at=NOW - timedelta(minutes=rng.randint(0, 600)))
            OpportunityFactory(
                lead=customer,
                stage_key=rng.choice(["new", "proposal", "won", "lost"]),
                value=D(rng.randint(1, 10**9)) / 100,
            )
            TaskFactory(lead=customer, due_at=NOW + timedelta(hours=rng.randint(-48, 48)))
            meeting(customer, NOW + timedelta(hours=rng.randint(-24, 72)))

    for scope in (
        AccessScope.own(user_a.pk),
        AccessScope.for_user(admin.pk, user_b.pk),
        AccessScope.organization(admin.pk),
    ):
        figures = selectors.dashboard(scope, now=NOW)
        assert figures.business_date == business_date(NOW)
        assert figures.leads == lead_summary(scope, now=NOW)
        assert figures.pipeline == pipeline_totals(scope, OpportunityFilters())
        assert figures.activities == activity_summary(scope, now=NOW)
        assert figures.new_leads == new_leads_today(scope, now=NOW, limit=LIST_LIMIT)
        assert figures.upcoming_meetings == upcoming_meetings(scope, now=NOW, limit=LIST_LIMIT)
        assert figures.next_tasks == next_open_tasks(scope, now=NOW, limit=LIST_LIMIT)
