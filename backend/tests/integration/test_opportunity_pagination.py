"""Keyset pagination of opportunities against real rows (as for leads,
tests/integration/test_keyset_pagination.py): walking every ordering's pages, forwards and
backwards, yields exactly PostgreSQL's own order, whatever the ties, including the Decimal
(value) and date (expected close) sort keys that leads never had."""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core import signing
from django.utils import timezone

from arkray.core.access import AccessScope
from arkray.core.keyset import KeysetPaginator
from arkray.pipeline import selectors
from arkray.pipeline.models import Opportunity
from arkray.pipeline.selectors import ORDERINGS, OpportunityFilters
from tests.factories import LeadFactory, OpportunityFactory, UserFactory, default_stage

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    base = timezone.now() - timedelta(days=10)
    stages = [default_stage(k) for k in ("new", "proposal", "won", "lost")]
    rows = []
    for i in range(41):
        stage = stages[i % 4]
        rows.append(
            OpportunityFactory.build(
                lead=lead,
                stage=stage,
                value=[Decimal("100.00"), Decimal("100.50"), Decimal("0"), Decimal("99999999.99")][
                    i % 4
                ],
                expected_close_date=[None, date(2026, 10, 1), date(2026, 12, 31)][i % 3],
                created_at=base + timedelta(hours=i // 3),  # ties in threes
                closed_at=None if stage.category == "open" else base + timedelta(hours=i // 5),
            )
        )
    Opportunity.objects.bulk_create(rows)
    Opportunity.objects.filter(pk__in=[r.pk for r in rows[::2]]).update(updated_at=base)
    return AccessScope.own(owner.pk)


def walk(queryset, ordering, page_size, *, backwards=False):
    paginator = KeysetPaginator(ordering, page_size=page_size)
    page = paginator.paginate(queryset, None)
    seen = [o.pk for o in page.items]
    while page.next_cursor:
        page = paginator.paginate(queryset, page.next_cursor)
        seen += [o.pk for o in page.items]
    if not backwards:
        return seen
    back = [o.pk for o in page.items]
    while page.previous_cursor:
        page = paginator.paginate(queryset, page.previous_cursor)
        back = [o.pk for o in page.items] + back
    return back


@pytest.mark.parametrize(
    ("name", "page_size", "backwards"),
    list(itertools.product(sorted(ORDERINGS), [1, 4, 7, 50], [False, True])),
)
def test_every_ordering_walks_exactly_like_postgresql(scope, name, page_size, backwards):
    ordering = ORDERINGS[name]
    queryset = selectors.opportunity_list(scope, OpportunityFilters())
    expected = list(
        queryset.order_by(*(key.order_by() for key in ordering.keys)).values_list("pk", flat=True)
    )
    assert len(expected) == 41
    assert walk(queryset, ordering, page_size, backwards=backwards) == expected


def test_a_value_cursor_never_carries_the_amount(scope):
    """Deal values are private sort keys (review): the cursor, which ends up in URLs and
    proxy logs, holds only the boundary row's id; the amount is re-read from that row."""
    paginator = KeysetPaginator(ORDERINGS["-value"], page_size=3)
    queryset = selectors.opportunity_list(scope, OpportunityFilters())
    page = paginator.paginate(queryset, None)
    payload = signing.loads(page.next_cursor, salt="arkray.core.keyset")
    assert payload["v"][0] is None
    assert "99999999" not in page.next_cursor
    assert "99999999.99" not in str(payload)


def test_expected_close_cursors_hold_exact_dates(scope):
    """Date sort keys travel as ISO text and decode back to dates (not datetimes)."""
    paginator = KeysetPaginator(ORDERINGS["expected_close"], page_size=3)
    page = paginator.paginate(selectors.opportunity_list(scope, OpportunityFilters()), None)
    payload = signing.loads(page.next_cursor, salt="arkray.core.keyset")
    assert payload["v"][0] == "2026-10-01"
