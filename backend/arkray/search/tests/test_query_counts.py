"""No N+1, no per-user or per-result queries: a search costs a constant number of queries
whatever the number of records, matches, results and users, in every kind of workspace.
Pinned exactly, so an accidental extra query per request is noticed too.

Per request: session (1) + user (1) [+ subject user exists-check (1) in a user's workspace]
[+ workspace-access audit insert, once per window] + search's five: one per kind of record
(leads, opportunities, tasks, meetings, notes), each with what its results display joined.
Outside a test's transaction one more, `SET TRANSACTION` (the read-only snapshot), comes
first, between BEGIN and COMMIT: see test_search_runs_in_one_read_only_snapshot.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from arkray.activities.models import ActivityStatus
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
)
from tests.helpers import signed_in

from .conftest import GROUPS, search_url

OWN, USER, ORGANISATION = 7, 8, 7


def seed(n, owners):
    """n matching records of every kind per owner, each on its own lead, with closed and
    archived ones mixed in."""
    for owner in owners:
        for i in range(n):
            lead = LeadFactory(owner=owner, first_name="Count", last_name=f"Lead {i}")
            OpportunityFactory(lead=lead, title=f"Count deal {i}", stage_key=("new", "won")[i % 2])
            TaskFactory(lead=lead, title=f"Count task {i}")
            MeetingFactory(lead=lead, title=f"Count meeting {i}")
            NoteFactory(lead=lead, description=f"Count note {i}")
            if i % 4 == 0:
                TaskFactory(lead=lead, title="Count done", status=ActivityStatus.COMPLETED)


def count(client, workspace="me", on_commit=None):
    """Queries of a second request: the first delegated access in a window writes the
    workspace-access audit row (and commits, which records the window)."""
    if on_commit is None:
        client.get(search_url("count", workspace))
    else:
        with on_commit(execute=True):
            client.get(search_url("count", workspace))
    with CaptureQueriesContext(connection) as queries:
        response = client.get(search_url("count", workspace))
    assert response.status_code == 200, response.content
    return len(queries), response.json()


@pytest.mark.django_db
@pytest.mark.parametrize("n", [3, 30])
def test_own_search(user_a, n):
    seed(n, [user_a])
    queries, body = count(signed_in(user_a))
    assert all(1 <= len(body[group]["results"]) <= 5 for group in GROUPS)
    assert queries == OWN


@pytest.mark.django_db
@pytest.mark.parametrize("n", [3, 30])
def test_a_selected_users_search(admin, user_a, n, django_capture_on_commit_callbacks):
    seed(n, [user_a])
    queries, body = count(signed_in(admin), str(user_a.pk), django_capture_on_commit_callbacks)
    assert all(1 <= len(body[group]["results"]) <= 5 for group in GROUPS)
    assert queries == USER


@pytest.mark.django_db
@pytest.mark.parametrize(("n", "users"), [(2, 2), (10, 12)])
def test_the_organisation_search(admin, n, users, django_capture_on_commit_callbacks):
    """Six times the users and five times the records each: still one query per kind.
    Owner names come from the same join (no per-user query)."""
    owners = [UserFactory() for _ in range(users)]
    seed(n, owners)
    queries, body = count(signed_in(admin), "all", django_capture_on_commit_callbacks)
    assert all(1 <= len(body[group]["results"]) <= 5 for group in GROUPS)
    assert len({row["owner"]["id"] for row in body["leads"]["results"]}) >= 1
    assert queries == ORGANISATION


@pytest.mark.django_db
def test_no_matches_costs_the_same(user_a):
    seed(5, [user_a])
    client = signed_in(user_a)
    client.get(search_url("nothing-at-all"))
    with CaptureQueriesContext(connection) as queries:
        assert client.get(search_url("nothing-at-all")).status_code == 200
    assert len(queries) == OWN


@pytest.mark.django_db
def test_an_invalid_query_costs_no_search_queries(user_a):
    client = signed_in(user_a)
    with CaptureQueriesContext(connection) as queries:
        assert client.get(search_url("x")).status_code == 400
    assert len(queries) == 2  # the session and the user: nothing is searched


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_search_runs_in_one_read_only_snapshot(admin, user_a):
    """Outside a test transaction (as in production) the five queries run in one REPEATABLE
    READ, READ ONLY transaction opened by `SET TRANSACTION`: one more query (plus BEGIN and
    COMMIT). PostgreSQL itself refuses any write inside it."""
    seed(2, [user_a])
    for client, workspace, expected in (
        (signed_in(user_a), "me", OWN + 1),
        (signed_in(admin), str(user_a.pk), USER + 1),
        (signed_in(admin), "all", ORGANISATION + 1),
    ):
        client.get(search_url("count", workspace))  # commits for real: records the audit window
        with CaptureQueriesContext(connection) as captured:
            assert client.get(search_url("count", workspace)).status_code == 200
        statements = [q["sql"] for q in captured.captured_queries]
        assert len([s for s in statements if s not in ("BEGIN", "COMMIT")]) == expected
        begin = statements.index("BEGIN")
        assert statements[begin + 1] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        assert statements[begin + 7 :] == ["COMMIT"]  # the five searches, then commit
