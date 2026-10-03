"""The lead figures the dashboard shows (selectors.lead_summary and new_leads_today): exact
definitions at the Asia/Kolkata day boundary, scope, bounds, and agreement with the Leads
list each figure opens (the dashboard's cards open the list with these very filters)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from arkray.core.access import AccessScope
from arkray.core.business_time import business_date
from arkray.leads.selectors import (
    LeadFilters,
    LeadSummary,
    lead_list,
    lead_summary,
    new_leads_today,
)
from tests.factories import LeadFactory, UserFactory

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 2, 18, 45, tzinfo=UTC)  # 00:15 IST on 3 October
DAY_START = datetime(2026, 10, 2, 18, 30, tzinfo=UTC)


@pytest.fixture
def leads(admin, user_a, user_b):
    """Around the boundary for A, a few for B and the admin, archived ones for everyone."""
    for owner in (user_a, user_b, admin):
        LeadFactory(owner=owner, created_at=DAY_START - timedelta(microseconds=1))  # yesterday
        LeadFactory(owner=owner, created_at=DAY_START)  # today, at midnight
        LeadFactory(owner=owner, created_at=NOW - timedelta(minutes=5))
        LeadFactory(owner=owner, created_at=NOW - timedelta(minutes=1), archived_at=NOW)
        LeadFactory(owner=owner, created_at=NOW - timedelta(days=90))
    LeadFactory(owner=user_b, created_at=NOW - timedelta(minutes=2))
    return admin, user_a, user_b


def scopes(admin, user_a, user_b):
    return [
        AccessScope.own(user_a.pk),
        AccessScope.for_user(admin.pk, user_b.pk),
        AccessScope.organization(admin.pk),
    ]


def test_the_definitions(leads):
    _, user_a, _ = leads
    assert lead_summary(AccessScope.own(user_a.pk), now=NOW) == LeadSummary(total=4, new_today=2)


def test_each_figure_opens_the_list_it_counts(leads):
    today = business_date(NOW)
    for scope in scopes(*leads):
        summary = lead_summary(scope, now=NOW)
        assert summary.total == lead_list(scope, LeadFilters()).count()
        created_today = LeadFilters(created_from=today, created_to=today)
        assert summary.new_today == lead_list(scope, created_today).count()


def test_new_leads_are_the_newest_of_the_created_today_list(leads):
    today = business_date(NOW)
    for scope in scopes(*leads):
        listed = lead_list(scope, LeadFilters(created_from=today, created_to=today)).order_by(
            "-created_at", "-id"
        )
        assert new_leads_today(scope, now=NOW, limit=3) == list(listed[:3])


def test_counts_are_scoped(leads):
    admin, user_a, user_b = leads
    assert lead_summary(AccessScope.own(user_a.pk), now=NOW).total == 4
    assert lead_summary(AccessScope.for_user(admin.pk, user_b.pk), now=NOW).total == 5
    assert lead_summary(AccessScope.organization(admin.pk), now=NOW) == LeadSummary(13, 7)


def test_today_ends_at_the_next_midnight(user_a):
    """A lead stamped at or after the next midnight IST (a clock ahead, an import) is not
    today's: the upper bound of the business day holds for the figures and the list."""
    scope = AccessScope.own(user_a.pk)
    day_end = DAY_START + timedelta(days=1)
    LeadFactory(owner=user_a, created_at=day_end - timedelta(microseconds=1))  # today
    LeadFactory(owner=user_a, created_at=day_end)  # tomorrow's
    LeadFactory(owner=user_a, created_at=day_end + timedelta(hours=3))
    assert lead_summary(scope, now=NOW) == LeadSummary(total=3, new_today=1)
    (row,) = new_leads_today(scope, now=NOW)
    assert row.created_at == day_end - timedelta(microseconds=1)


def test_leads_created_at_the_same_moment_list_newest_id_first(user_a):
    """Ties in created_at (bulk creation) are ordered by id, newest first, like the Leads
    list's own default order: the five shown are the Leads list's first five."""
    leads = [LeadFactory(owner=user_a, created_at=NOW - timedelta(minutes=1)) for _ in range(7)]
    expected = sorted(leads, key=lambda lead: lead.pk, reverse=True)[:5]
    assert new_leads_today(AccessScope.own(user_a.pk), now=NOW) == expected


def test_an_empty_workspace_counts_zero():
    scope = AccessScope.own(UserFactory().pk)
    assert lead_summary(scope, now=NOW) == LeadSummary(0, 0)
    assert new_leads_today(scope, now=NOW) == []


def test_the_list_is_bounded_and_loads_no_contact_data(user_a):
    LeadFactory(owner=user_a, created_at=NOW, email="asha@apollo.example", phone="+91 98765 43210")
    for bad in (0, 51):
        with pytest.raises(ValueError, match="range"):
            new_leads_today(AccessScope.own(user_a.pk), now=NOW, limit=bad)
    with CaptureQueriesContext(connection) as queries:
        (row,) = new_leads_today(AccessScope.own(user_a.pk), now=NOW)
        assert row.owner.full_name == "Rahul Sharma"  # joined, no extra query
    (sql,) = [q["sql"] for q in queries.captured_queries]
    for column in ('"email"', '"phone"', '"mobile"', '"search_text"', '"password"'):
        assert column not in sql
