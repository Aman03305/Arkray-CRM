"""No N+1: list and detail endpoints cost a constant number of queries, whatever the number
of leads, owners, statuses and sources on the page, in every kind of workspace.

The counts are pinned exactly, so an accidental extra query per request is noticed too.
Per request: session (1) + user (1) [+ subject user exists-check (1) for a user's workspace]
[+ workspace-access audit insert (1), once per window] + the query itself.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.factories import LeadFactory, UserFactory

from .conftest import lead_url, leads_url

pytestmark = pytest.mark.django_db

SOURCES = ["website", "referral", "campaign", None]
STATUSES = ["new", "contacted", "qualified", "unqualified", "converted"]


def seed(n, owners):
    for i in range(n):
        LeadFactory(
            owner=owners[i % len(owners)],
            status_id=STATUSES[i % len(STATUSES)],
            source_id=SOURCES[i % len(SOURCES)],
            rating=["hot", "warm", None][i % 3],
        )


def count(client, url, params=None):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, {"page_size": 100} if params is None else params)
    assert response.status_code == 200, response.content
    return len(queries), len(response.json().get("results", [None]))


def warm_up(client, url):
    """The first delegated access writes the workspace-access audit row; later ones in the
    window don't (in tests the window marker needs an explicit commit callback)."""
    client.get(url)


@pytest.mark.parametrize("n", [10, 100])
def test_own_workspace_list(user_a_client, user_a, n):
    seed(n, [user_a])
    queries, rows = count(user_a_client, leads_url())
    assert rows == min(n, 100)
    assert queries == 3  # session, user, leads (owner/status/source joined)


@pytest.mark.parametrize("n", [10, 100])
def test_a_users_workspace_list_opened_by_an_admin(
    admin_client, user_a, n, django_capture_on_commit_callbacks
):
    seed(n, [user_a])
    with django_capture_on_commit_callbacks(execute=True):
        warm_up(admin_client, leads_url(str(user_a.pk)))
    queries, rows = count(admin_client, leads_url(str(user_a.pk)))
    assert rows == min(n, 100)
    assert queries == 4  # session, user, subject exists, leads


@pytest.mark.parametrize("n", [10, 100])
def test_organisation_list_with_many_owners(admin_client, n, django_capture_on_commit_callbacks):
    owners = [UserFactory() for _ in range(7)]
    seed(n, owners)
    with django_capture_on_commit_callbacks(execute=True):
        warm_up(admin_client, leads_url("all"))
    queries, rows = count(admin_client, leads_url("all"))
    assert rows == min(n, 100)
    assert queries == 3


@pytest.mark.parametrize(
    "params",
    [
        {"q": "apollo diagnostics", "page_size": 100},
        {"status": "new", "source": "website", "rating": "hot", "page_size": 100},
        {"ordering": "name", "page_size": 100},
        {"ordering": "last_contacted_at", "page_size": 100},
    ],
)
def test_search_filters_and_sorts_stay_constant(user_a_client, user_a, params):
    seed(60, [user_a])
    assert count(user_a_client, leads_url(), params)[0] == 3


def test_following_a_cursor_costs_the_same(user_a_client, user_a):
    seed(30, [user_a])
    first = user_a_client.get(leads_url(), {"page_size": 10}).json()
    with CaptureQueriesContext(connection) as queries:
        user_a_client.get(first["next"])
    assert len(queries) == 3


def test_detail(user_a_client, user_a):
    lead = LeadFactory(owner=user_a, source_id="website")
    queries, _ = count(user_a_client, lead_url(lead.pk), {})
    assert queries == 3  # session, user, lead (owner, creator, status, source joined)


def test_duplicate_check(user_a_client, user_a):
    seed(20, [user_a])
    with CaptureQueriesContext(connection) as queries:
        user_a_client.get(
            leads_url(suffix="/duplicates"), {"email": "lead1@hospital.example", "phone": ["+91 1"]}
        )
    assert len(queries) == 3


def test_options(user_a_client):
    queries, _ = count(user_a_client, "/api/v1/config/lead-options", {})
    assert queries == 4  # session, user, statuses, sources


def test_assignees(admin_client):
    for _ in range(30):
        UserFactory()
    queries, rows = count(admin_client, "/api/v1/assignees", {})
    assert rows == 31
    assert queries == 3
