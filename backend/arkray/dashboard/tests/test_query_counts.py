"""No N+1, no per-user loops: the dashboard costs a constant number of queries whatever the
number of leads, opportunities, tasks, meetings and users, in every kind of workspace.
Pinned exactly, so an accidental extra query per request is noticed too.

Per request: session (1) + user (1) [+ subject user exists-check (1) in a user's workspace]
[+ workspace-access audit insert, once per window] + the dashboard's seven: lead figures and
pipeline totals (one aggregate each), the activity figures (two aggregates: open tasks;
meetings from today on), today's newest leads (owner joined), the next meetings and the
next open tasks (lead and owner joined). Outside a test's transaction one more,
`SET TRANSACTION` (the snapshot), comes first, between BEGIN and COMMIT: see
test_the_figures_share_one_read_only_snapshot.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from arkray.activities.models import ActivityStatus
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
)
from tests.helpers import signed_in

from .conftest import NOW, dashboard_url

# Delegated and organisation-wide requests also read the audit window (PostgreSQL since
# Phase 9): one more query than the user's own.
OWN, USER, ORGANISATION = 9, 11, 10


@pytest.fixture(autouse=True)
def _clock(frozen_now):
    """The view's clock is fixed at 00:15 IST, and every record is seeded relative to it:
    the seeded leads are today's whatever the wall clock says (the domain review found
    the counts failing in the first seconds after midnight IST)."""
    return frozen_now


def seed(n, owners):
    """n of everything for each owner: leads created today, open/won opportunities, open
    and overdue tasks, today's and upcoming meetings, each on its own lead."""
    now = NOW
    for owner in owners:
        for i in range(n):
            lead = LeadFactory(owner=owner, created_at=now - timedelta(seconds=i))
            OpportunityFactory(
                lead=lead, stage_key=("proposal", "won")[i % 2], value=Decimal("1000.00")
            )
            TaskFactory(lead=lead, due_at=now + timedelta(hours=i - n // 2))
            start = now + timedelta(minutes=10 + i)
            MeetingFactory(lead=lead, starts_at=start, ends_at=start + timedelta(minutes=30))
            if i % 4 == 0:
                TaskFactory(lead=lead, status=ActivityStatus.COMPLETED)


def count(client, workspace="me", on_commit=None):
    """Queries of a second request: the first delegated access in a window writes the
    workspace-access audit row (and commits, which records the window)."""
    if on_commit is None:
        client.get(dashboard_url(workspace))
    else:
        with on_commit(execute=True):
            client.get(dashboard_url(workspace))
    with CaptureQueriesContext(connection) as queries:
        response = client.get(dashboard_url(workspace))
    assert response.status_code == 200, response.content
    return len(queries), response.json()


@pytest.mark.django_db
@pytest.mark.parametrize("n", [10, 100])
def test_own_dashboard(user_a, n):
    seed(n, [user_a])
    queries, body = count(signed_in(user_a))
    assert body["leads"]["total"] == n
    assert len(body["new_leads"]) == len(body["next_tasks"]) == len(body["upcoming_meetings"]) == 5
    assert queries == OWN


@pytest.mark.django_db
@pytest.mark.parametrize("n", [10, 100])
def test_a_selected_users_dashboard(admin, user_a, n, django_capture_on_commit_callbacks):
    seed(n, [user_a])
    queries, body = count(signed_in(admin), str(user_a.pk), django_capture_on_commit_callbacks)
    assert body["leads"]["total"] == n
    assert queries == USER


@pytest.mark.django_db
@pytest.mark.parametrize(("n", "users"), [(10, 2), (40, 10)])
def test_the_organisation_dashboard(admin, n, users, django_capture_on_commit_callbacks):
    """Ten times the users and four times the records each: still one query per figure.
    Owner names in the lists come from the same join (no per-user query)."""
    owners = [UserFactory() for _ in range(users)]
    seed(n, owners)
    queries, body = count(signed_in(admin), "all", django_capture_on_commit_callbacks)
    assert body["leads"]["total"] == n * users
    assert len({row["owner"]["id"] for row in body["new_leads"]}) >= 1
    assert queries == ORGANISATION


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_the_figures_share_one_read_only_snapshot(admin, user_a):
    """Outside a test transaction (as in production) the seven queries run in one REPEATABLE
    READ, read-only transaction opened by `SET TRANSACTION`: one more query (plus BEGIN and
    COMMIT), and every figure and list describes the same moment."""
    seed(3, [user_a])
    for client, workspace, expected in (
        (signed_in(user_a), "me", OWN + 1),
        (signed_in(admin), str(user_a.pk), USER + 1),
        (signed_in(admin), "all", ORGANISATION + 1),
    ):
        client.get(dashboard_url(workspace))  # commits for real: records the audit window
        with CaptureQueriesContext(connection) as captured:
            assert client.get(dashboard_url(workspace)).status_code == 200
        statements = [q["sql"] for q in captured.captured_queries]
        assert len([s for s in statements if s not in ("BEGIN", "COMMIT")]) == expected
        begin = statements.index("BEGIN")
        assert statements[begin + 1] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        assert statements[begin + 9 :] == ["COMMIT"]  # the seven figure queries, then commit
