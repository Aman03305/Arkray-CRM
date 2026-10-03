"""Cross-user suite for the dashboard (Phase 5): the figures themselves are data.

Rahul (A), Priya (B) and the administrator each have distinctive records: leads, open, won
and lost opportunities, tasks and meetings in every state, with amounts chosen so any leak
is obvious (A's open pipeline is ₹111,111, B's ₹877,777). Every figure is checked EXACTLY:
a total that included one of the other user's records, or that changed when they added
records, would fail here even if no row of theirs were ever shown.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from arkray.activities.models import ActivityStatus
from arkray.dashboard.api import views
from arkray.leads.models import Lead
from tests.factories import LeadFactory, MeetingFactory, OpportunityFactory, TaskFactory
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 2, 18, 45, tzinfo=UTC)  # 00:15 IST, 3 October 2026
DAY_START = datetime(2026, 10, 2, 18, 30, tzinfo=UTC)  # 00:00 IST
OLD = NOW - timedelta(days=40)
D = Decimal


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(views, "timezone", SimpleNamespace(now=lambda: NOW))


def url(workspace="me"):
    return f"/api/v1/workspaces/{workspace}/dashboard"


def meeting(lead, start, status=ActivityStatus.SCHEDULED, **extra):
    return MeetingFactory(
        lead=lead, starts_at=start, ends_at=start + timedelta(minutes=30), status=status, **extra
    )


def build_a(owner):
    """Rahul: 3 live leads (2 new today), ₹111,111 open at 50 %, 2 open tasks, 2 meetings
    today, plus won, lost, completed, cancelled and archived records that never count."""
    old = LeadFactory(owner=owner, first_name="Alpha", last_name="Old", created_at=OLD)
    LeadFactory(
        owner=owner, first_name="Alpha", last_name="Today1", created_at=NOW - timedelta(minutes=1)
    )
    LeadFactory(
        owner=owner, first_name="Alpha", last_name="Today2", created_at=NOW - timedelta(minutes=2)
    )
    LeadFactory(
        owner=owner, first_name="Alpha", last_name="Archived", created_at=NOW, archived_at=NOW
    )
    OpportunityFactory(lead=old, title="Alpha open", stage_key="proposal", value=D("111111.00"))
    OpportunityFactory(lead=old, title="Alpha won", stage_key="won", value=D("222222.00"))
    OpportunityFactory(lead=old, title="Alpha lost", stage_key="lost", value=D("333333.00"))
    TaskFactory(lead=old, title="Alpha overdue task", due_at=DAY_START - timedelta(hours=1))
    TaskFactory(lead=old, title="Alpha task today", due_at=NOW + timedelta(hours=2))
    TaskFactory(lead=old, title="Alpha done", due_at=NOW, status=ActivityStatus.COMPLETED)
    TaskFactory(lead=old, title="Alpha dropped", due_at=NOW, status=ActivityStatus.CANCELLED)
    TaskFactory(lead=old, title="Alpha archived task", due_at=OLD, archived_at=NOW)
    meeting(old, NOW + timedelta(hours=1), title="Alpha meeting")
    meeting(old, DAY_START + timedelta(minutes=1), ActivityStatus.COMPLETED, title="Alpha met")
    meeting(old, NOW + timedelta(hours=4), ActivityStatus.CANCELLED, title="Alpha off")
    meeting(old, NOW + timedelta(hours=5), archived_at=NOW, title="Alpha archived meeting")


def build_b(owner):
    """Priya: 7 live leads (5 new today), ₹877,777 open, 6 open tasks (3 overdue, 2 due
    today), 3 meetings today and 5 upcoming."""
    old = LeadFactory(owner=owner, first_name="Bravo", last_name="Old", created_at=OLD)
    LeadFactory(owner=owner, first_name="Bravo", last_name="Older", created_at=OLD)
    for n in range(5):
        LeadFactory(
            owner=owner,
            first_name="Bravo",
            last_name=f"Today{n}",
            created_at=NOW - timedelta(minutes=3 + n),
        )
    OpportunityFactory(lead=old, title="Bravo open new", stage_key="new", value=D("777777.00"))
    OpportunityFactory(
        lead=old, title="Bravo open neg", stage_key="negotiation", value=D("100000.00")
    )
    OpportunityFactory(lead=old, title="Bravo won", stage_key="won", value=D("888888.00"))
    OpportunityFactory(lead=old, title="Bravo lost", stage_key="lost", value=D("999999.00"))
    for n in range(3):
        TaskFactory(lead=old, title=f"Bravo overdue {n}", due_at=DAY_START - timedelta(days=1 + n))
    for n in range(2):
        TaskFactory(lead=old, title=f"Bravo today {n}", due_at=NOW + timedelta(hours=3 + n))
    TaskFactory(lead=old, title="Bravo someday")
    for n in range(3):
        meeting(old, NOW + timedelta(hours=1 + n), title=f"Bravo meeting {n}")
    for n in range(2):
        meeting(old, NOW + timedelta(days=2 + n), title=f"Bravo later {n}")


def build_admin(owner):
    lead = LeadFactory(owner=owner, first_name="Charlie", last_name="Admin", created_at=OLD)
    OpportunityFactory(lead=lead, title="Charlie open", stage_key="new", value=D("5000.00"))
    TaskFactory(lead=lead, title="Charlie task")


EXPECTED_A = {
    "leads": {"total": 3, "new_today": 2},
    "pipeline": {"pipeline_value": "111111.00", "weighted_pipeline": "55555.50", "open_count": 1},
    "activities": {
        "open_tasks": 2,
        "tasks_due_today": 1,
        "overdue_tasks": 1,
        "meetings_today": 2,
        "upcoming_meetings": 1,
    },
}
EXPECTED_B = {
    "leads": {"total": 7, "new_today": 5},
    "pipeline": {"pipeline_value": "877777.00", "weighted_pipeline": "152777.70", "open_count": 2},
    "activities": {
        "open_tasks": 6,
        "tasks_due_today": 2,
        "overdue_tasks": 3,
        "meetings_today": 3,
        "upcoming_meetings": 5,
    },
}
EXPECTED_ORGANISATION = {
    "leads": {"total": 11, "new_today": 7},
    "pipeline": {"pipeline_value": "993888.00", "weighted_pipeline": "208833.20", "open_count": 4},
    "activities": {
        "open_tasks": 9,
        "tasks_due_today": 3,
        "overdue_tasks": 4,
        "meetings_today": 5,
        "upcoming_meetings": 6,
    },
}
# Amounts with their decimals (a bare "111111" could occur inside a hexadecimal id).
A_MARKERS = ("Alpha", "Rahul", "111111.00", "55555.50", "222222.00", "333333.00")
B_MARKERS = ("Bravo", "Priya", "877777.00", "152777.70", "777777.00", "888888.00", "999999.00")


def figures(body):
    return {key: body[key] for key in ("leads", "pipeline", "activities")}


def get(client, workspace="me"):
    response = client.get(url(workspace))
    assert response.status_code == 200, response.content
    return response.json()


@pytest.fixture
def world(admin, user_a, user_b):
    build_a(user_a)
    build_b(user_b)
    build_admin(admin)
    return SimpleNamespace(
        a=signed_in(user_a),
        b=signed_in(user_b),
        admin=signed_in(admin),
        user_a=user_a,
        user_b=user_b,
    )


def test_each_user_sees_exactly_their_own_figures(world):
    assert figures(get(world.a)) == EXPECTED_A
    assert figures(get(world.b)) == EXPECTED_B


@pytest.mark.parametrize(
    ("viewer", "own", "other"), [("a", A_MARKERS, B_MARKERS), ("b", B_MARKERS, A_MARKERS)]
)
def test_nothing_of_the_other_user_appears_anywhere(world, viewer, own, other):
    raw = getattr(world, viewer).get(url()).content.decode()
    assert any(marker in raw for marker in own)  # the markers do show up when they're yours
    for marker in other:
        assert marker not in raw, marker


def test_lists_hold_only_the_workspaces_records(world):
    for client, user, prefix in (
        (world.a, world.user_a, "Alpha"),
        (world.b, world.user_b, "Bravo"),
    ):
        body = get(client)
        assert {row["owner"]["id"] for row in body["new_leads"]} == {str(user.pk)}
        assert all(row["display_name"].startswith(prefix) for row in body["new_leads"])
        for row in [*body["next_tasks"], *body["upcoming_meetings"]]:
            assert row["owner"]["id"] == str(user.pk)
            assert row["title"].startswith(prefix)
            assert row["lead"]["display_name"].startswith(prefix)


def test_an_admin_viewing_a_user_sees_exactly_that_users_dashboard(world):
    """Selected-user figures and lists are the user's own dashboard, byte for byte: the
    admin's and the other user's records add nothing."""
    assert get(world.admin, str(world.user_a.pk)) == get(world.a)
    assert get(world.admin, str(world.user_b.pk)) == get(world.b)
    # ...and switching back and forth never mixes them.
    assert figures(get(world.admin, str(world.user_a.pk))) == EXPECTED_A
    assert figures(get(world.admin, str(world.user_b.pk))) == EXPECTED_B


def test_the_organisation_dashboard_is_every_users_total(world):
    body = get(world.admin, "all")
    assert figures(body) == EXPECTED_ORGANISATION
    # Today's newest leads across users, each naming the user it is assigned to.
    owners = {row["display_name"]: row["owner"]["full_name"] for row in body["new_leads"]}
    assert owners == {
        "Alpha Today1": "Rahul Sharma",
        "Alpha Today2": "Rahul Sharma",
        "Bravo Today0": "Priya Patel",
        "Bravo Today1": "Priya Patel",
        "Bravo Today2": "Priya Patel",
    }


def test_an_admins_own_workspace_is_their_own_records(world):
    body = get(world.admin)
    assert body["leads"] == {"total": 1, "new_today": 0}
    assert body["pipeline"]["pipeline_value"] == "5000.00"
    assert body["activities"]["open_tasks"] == 1


def test_the_other_users_records_never_move_a_figure(admin, user_a, user_b):
    """A's whole response is identical before and after B (and the admin) create records of
    every kind: no count or sum can be used to infer anything about them."""
    build_a(user_a)
    client = signed_in(user_a)
    before = get(client)
    build_b(user_b)
    build_admin(admin)
    after = get(client)
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)


def test_no_other_workspace_can_be_requested(world):
    missing = world.a.get(url(str(uuid.uuid4())))
    for workspace in (str(world.user_b.pk), "all"):
        response = world.a.get(url(workspace))
        assert response.status_code == 404
        assert without_request_id(response) == without_request_id(missing)


def test_figures_follow_a_reassigned_lead(world, user_a, user_b):
    """Reassigning A's lead (with its open opportunity and open work) moves those figures to
    B exactly; A keeps their completed meeting (owner-based history, R45)."""
    moved = Lead.objects.get(owner=user_a, last_name="Old")
    response = world.admin.post(
        f"/api/v1/workspaces/all/leads/{moved.pk}/assign",
        {"owner": str(user_b.pk), "version": moved.version},
        format="json",
    )
    assert response.status_code == 200, response.content

    a, b = get(world.a), get(world.b)
    assert a["leads"] == {"total": 2, "new_today": 2}
    assert a["pipeline"] == {"pipeline_value": "0.00", "weighted_pipeline": "0.00", "open_count": 0}
    assert a["activities"] == {
        "open_tasks": 0,
        "tasks_due_today": 0,
        "overdue_tasks": 0,
        "meetings_today": 1,  # the completed one stays with whoever held it
        "upcoming_meetings": 0,
    }
    assert b["leads"] == {"total": 8, "new_today": 5}
    assert b["pipeline"] == {
        "pipeline_value": "988888.00",
        "weighted_pipeline": "208333.20",
        "open_count": 3,
    }
    assert b["activities"]["open_tasks"] == 8
    assert b["activities"]["meetings_today"] == 4
    # The organisation's totals don't change: nothing was created or closed.
    assert figures(get(world.admin, "all")) == EXPECTED_ORGANISATION
