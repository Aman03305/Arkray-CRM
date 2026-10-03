"""No N+1: every pipeline endpoint costs a constant number of queries, whatever the number of
opportunities, stages, leads and owners, in every kind of workspace. Pinned exactly, so an
accidental extra query per request is noticed too.

Per request: session (1) + user (1) [+ subject user exists-check (1) in a user's
workspace] [+ workspace-access audit insert, once per window] + the endpoint's own queries.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.factories import LeadFactory, OpportunityFactory, UserFactory

from .conftest import (
    board_url,
    opportunities_url,
    opportunity_url,
    summary_url,
)

pytestmark = pytest.mark.django_db

STAGE_KEYS = ["new", "qualified", "proposal", "negotiation", "won", "lost"]


def seed(n, owners, stages):
    leads = [LeadFactory(owner=owner) for owner in owners for _ in range(3)]
    for i in range(n):
        lead = leads[i % len(leads)]
        OpportunityFactory(
            lead=lead,
            stage=stages[STAGE_KEYS[i % len(STAGE_KEYS)]],
            value=Decimal(1000 + i),
        )


def count(client, url, params=None):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, params or {})
    assert response.status_code == 200, response.content
    return len(queries), response.json()


def warm_up(client, url):
    client.get(url)  # the first delegated access writes the workspace-access audit row


@pytest.mark.parametrize("n", [10, 100])
def test_board_own_workspace(user_a_client, user_a, stages, n):
    seed(n, [user_a], stages)
    queries, body = count(user_a_client, board_url(), {"cards_per_stage": 50})
    assert sum(c["count"] for c in body["columns"]) == n
    assert all(len(c["cards"]) == min(c["count"], 50) for c in body["columns"])
    # session, user, pipeline, stages, per-stage aggregates, totals, cards (one UNION ALL)
    assert queries == 7


@pytest.mark.parametrize("n", [10, 100])
def test_board_in_a_users_workspace(
    admin_client, user_a, stages, n, django_capture_on_commit_callbacks
):
    seed(n, [user_a], stages)
    with django_capture_on_commit_callbacks(execute=True):
        warm_up(admin_client, board_url(str(user_a.pk)))
    queries, _ = count(admin_client, board_url(str(user_a.pk)))
    assert queries == 8  # + subject exists


@pytest.mark.parametrize("n", [10, 100])
def test_board_organisation_wide_with_many_owners(
    admin_client, stages, n, django_capture_on_commit_callbacks
):
    seed(n, [UserFactory() for _ in range(7)], stages)
    with django_capture_on_commit_callbacks(execute=True):
        warm_up(admin_client, board_url("all"))
    queries, body = count(admin_client, board_url("all"))
    assert sum(c["count"] for c in body["columns"]) == n
    assert queries == 7


@pytest.mark.parametrize("n", [10, 100])
def test_list(user_a_client, user_a, stages, n):
    seed(n, [user_a], stages)
    queries, body = count(user_a_client, opportunities_url(), {"page_size": 100})
    assert len(body["results"]) == min(n, 100)
    assert queries == 3  # session, user, opportunities (lead and owner joined)


@pytest.mark.parametrize(
    "params",
    [
        {"ordering": "-value"},
        {"ordering": "expected_close", "status": "open"},
        {"lead": "00000000-0000-4000-8000-000000000000"},
        {"probability_min": "20", "probability_max": "80", "ordering": "-closed_at"},
        {"archived": "true"},
    ],
)
def test_list_filters_and_sorts_stay_constant(user_a_client, user_a, stages, params):
    seed(60, [user_a], stages)
    assert count(user_a_client, opportunities_url(), {**params, "page_size": 100})[0] == 3


@pytest.mark.parametrize("n", [10, 100])
def test_a_stage_list_adds_one_primary_key_lookup(user_a_client, user_a, stages, n):
    seed(n, [user_a], stages)
    queries, _ = count(
        user_a_client,
        opportunities_url(),
        {"stage": str(stages["won"].pk), "ordering": "-closed_at", "page_size": 100},
    )
    assert queries == 4  # + the stage's category


@pytest.mark.parametrize(
    ("ordering", "expected"), [("-created_at", 3), ("expected_close", 3), ("-value", 4)]
)
def test_following_a_cursor_costs_the_same(user_a_client, user_a, stages, ordering, expected):
    """A value-sorted cursor holds no amount (review): the boundary row's value is re-read
    by id, one indexed lookup, like the Leads name sort."""
    seed(30, [user_a], stages)
    first = user_a_client.get(opportunities_url(), {"page_size": 10, "ordering": ordering}).json()
    with CaptureQueriesContext(connection) as queries:
        user_a_client.get(first["next"])
    assert len(queries) == expected


@pytest.mark.parametrize("n", [10, 100])
def test_summary(user_a_client, user_a, stages, n):
    seed(n, [user_a], stages)
    assert count(user_a_client, summary_url())[0] == 3  # session, user, one aggregate


def test_detail(user_a_client, user_a, stages):
    opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
    # session, user, opportunity (lead, owner, creator, pipeline, stage joined)
    assert count(user_a_client, opportunity_url(opportunity.pk))[0] == 3


def test_history(user_a_client, user_a, stages):
    opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
    for version, key in enumerate(["qualified", "proposal", "negotiation", "won"], start=1):
        user_a_client.post(
            opportunity_url(opportunity.pk, action="move"),
            {"stage": str(stages[key].pk), "version": version},
            format="json",
        )
    queries, body = count(user_a_client, opportunity_url(opportunity.pk, action="history"))
    assert len(body["results"]) == 4
    assert queries == 4  # session, user, visibility check, history (actor joined)


def test_pipeline_configuration(user_a_client, stages):
    assert count(user_a_client, "/api/v1/config/pipelines")[0] == 4  # + pipelines, stages


@pytest.mark.parametrize("n_open", [1, 10])
def test_moving_an_opportunity_costs_the_same_whatever_else_exists(
    user_a_client, user_a, stages, n_open
):
    lead = LeadFactory(owner=user_a)
    for _ in range(n_open):
        OpportunityFactory(lead=lead)
    opportunity = OpportunityFactory(lead=lead)
    with CaptureQueriesContext(connection) as queries:
        response = user_a_client.post(
            opportunity_url(opportunity.pk, action="move"),
            {"stage": str(stages["proposal"].pk), "version": 1},
            format="json",
        )
    assert response.status_code == 200
    # session, user, savepoint, lead id, lead lock, opportunity lock, target stage, update,
    # history insert, audit insert, release savepoint, reload; since Phase 4 also the
    # timeline's stage-name snapshot and its entry (arkray.activities.subscribers)
    assert len(queries) == 14
