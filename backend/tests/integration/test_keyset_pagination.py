"""Keyset pagination (arkray.core.keyset) against real rows in PostgreSQL.

The reference for every ordering is PostgreSQL itself: walking the pages must yield exactly
the rows of the same query without pagination, in the same order, whatever the ties and
NULLs, forwards and backwards, and stay exact while rows are inserted meanwhile.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from django.core import signing
from django.utils import timezone

from arkray.core import keyset
from arkray.core.access import AccessScope
from arkray.core.errors import InvalidInputError
from arkray.core.keyset import KeysetOrdering, KeysetPaginator, SortKey
from arkray.leads.models import Lead
from arkray.leads.selectors import ORDERINGS
from tests.factories import LeadFactory, UserFactory

pytestmark = pytest.mark.django_db

NAMES = ["Asha", "Asha", "asha", "Zoë", "Émile", "Rahul", "रवि", "李", "Asha", "Bo"]
SOME_ID = "5a1e4d2c-0000-4000-8000-00000000abcd"


@pytest.fixture
def owner():
    return UserFactory()


@pytest.fixture
def leads(owner):
    """70 leads full of ties: shared creation times, names and contact times, NULLs."""
    base = timezone.now() - timedelta(days=30)
    rows = [
        LeadFactory(
            owner=owner,
            first_name=NAMES[i % len(NAMES)],
            last_name="" if i % 4 else "Kumar",
            last_contacted_at=None if i % 3 == 0 else base + timedelta(hours=i % 5),
        )
        for i in range(70)
    ]
    # Force creation-time ties (five rows per timestamp).
    for i, lead in enumerate(rows):
        Lead.objects.filter(pk=lead.pk).update(
            created_at=base + timedelta(minutes=i // 5),
            updated_at=base + timedelta(hours=1, minutes=i // 7),
        )
    return rows


def scoped(owner):
    return AccessScope.own(owner.pk).apply(Lead.objects.all())


def reference(queryset, ordering: KeysetOrdering):
    ordered = queryset.order_by(*(k.order_by() for k in ordering.keys))
    return list(ordered.values_list("pk", flat=True))


def walk_forward(queryset, ordering, page_size):
    paginator = KeysetPaginator(ordering, page_size=page_size, binding=None)
    seen, cursor, pages = [], None, []
    while True:
        page = paginator.paginate(queryset, cursor)
        pages.append(page)
        seen.extend(lead.pk for lead in page.items)
        if page.next_cursor is None:
            return seen, pages
        cursor = page.next_cursor
        assert len(pages) < 200, "pagination does not terminate"


@pytest.mark.parametrize("name", sorted(ORDERINGS))
@pytest.mark.parametrize("page_size", [1, 7, 25, 100])
def test_walking_every_page_yields_exactly_the_ordered_rows(leads, owner, name, page_size):
    ordering = ORDERINGS[name]
    seen, pages = walk_forward(scoped(owner), ordering, page_size)
    assert seen == reference(scoped(owner), ordering)
    assert all(len(p.items) <= page_size for p in pages)
    assert pages[0].previous_cursor is None


@pytest.mark.parametrize("name", sorted(ORDERINGS))
def test_walking_backwards_from_the_last_page_yields_the_same_rows(leads, owner, name):
    ordering = ORDERINGS[name]
    _, pages = walk_forward(scoped(owner), ordering, 9)
    paginator = KeysetPaginator(ordering, page_size=9, binding=None)
    collected = list(pages[-1].items)
    cursor = pages[-1].previous_cursor
    while cursor:
        page = paginator.paginate(scoped(owner), cursor)
        collected[:0] = page.items
        cursor = page.previous_cursor
    assert [lead.pk for lead in collected] == reference(scoped(owner), ordering)


@pytest.mark.parametrize("name", sorted(ORDERINGS))
def test_rows_inserted_while_paging_neither_duplicate_nor_hide_existing_rows(leads, owner, name):
    ordering = ORDERINGS[name]
    paginator = KeysetPaginator(ordering, page_size=10, binding=None)
    original = set(reference(scoped(owner), ordering))
    first = paginator.paginate(scoped(owner), None)
    seen = [lead.pk for lead in first.items]
    # Someone adds leads that sort before, among and after the rows already shown.
    inserted = [
        LeadFactory(owner=owner, first_name=n, last_contacted_at=t)
        for n, t in itertools.product(["Aaron", "Asha", "Zz"], [None, timezone.now()])
    ]
    cursor = first.next_cursor
    while cursor:
        page = paginator.paginate(scoped(owner), cursor)
        seen.extend(lead.pk for lead in page.items)
        cursor = page.next_cursor
    assert len(seen) == len(set(seen)), "a row was shown twice"
    assert original <= set(seen), "an existing row was skipped"
    assert set(seen) - original <= {lead.pk for lead in inserted}


def test_rows_archived_while_paging_do_not_break_the_walk(leads, owner):
    queryset = scoped(owner).filter(archived_at__isnull=True)
    paginator = KeysetPaginator(ORDERINGS["-created_at"], page_size=10, binding=None)
    first = paginator.paginate(queryset, None)
    boundary = first.items[-1]
    Lead.objects.filter(pk=boundary.pk).update(archived_at=timezone.now())  # the cursor row
    second = paginator.paginate(queryset, first.next_cursor)
    assert second.items
    assert boundary.pk not in {lead.pk for lead in second.items}
    assert not {lead.pk for lead in first.items} & {lead.pk for lead in second.items}


def test_an_empty_scope_has_one_empty_page(owner):
    page = KeysetPaginator(ORDERINGS["name"], page_size=25, binding=None).paginate(
        scoped(owner), None
    )
    assert (page.items, page.next_cursor, page.previous_cursor) == ([], None, None)


class TestInvalidCursors:
    ORDERING = ORDERINGS["-last_contacted_at"]

    def paginate(self, cursor):
        return KeysetPaginator(self.ORDERING, page_size=5, binding=None).paginate(
            Lead.objects.all(), cursor
        )

    def forged(self, payload):
        return keyset._seal(payload)

    @pytest.mark.parametrize(
        "cursor",
        ["garbage", "x" * 5000, "e30:1abcde:tampered-signature", "../../etc/passwd", "' OR 1=1 --"],
    )
    def test_malformed_cursors_are_a_clean_validation_error(self, cursor):
        with pytest.raises(InvalidInputError) as caught:
            self.paginate(cursor)
        assert caught.value.details == {"cursor": [caught.value.message]}

    @pytest.mark.parametrize(
        "payload",
        [
            {"o": "name", "d": "next", "v": ["Asha", SOME_ID]},
            {"o": "-last_contacted_at", "d": "sideways", "v": [None, SOME_ID]},
            {"o": "-last_contacted_at", "d": "next", "v": ["not-a-date", SOME_ID]},
            {"o": "-last_contacted_at", "d": "next", "v": ["2026-09-30T10:00:00", SOME_ID]},
            {"o": "-last_contacted_at", "d": "next", "v": [None, None]},
            {"o": "-last_contacted_at", "d": "next", "v": [None, "not-a-uuid"]},
            {"o": "-last_contacted_at", "d": "next", "v": [None]},
            {"o": "-last_contacted_at", "d": "next", "v": {"a": 1}},
            ["not", "a", "dict"],
        ],
    )
    def test_well_signed_but_wrong_cursors_are_refused(self, payload):
        with pytest.raises(InvalidInputError):
            self.paginate(self.forged(payload))

    def test_a_cursor_signed_or_sealed_elsewhere_is_refused(self):
        payload = {"o": "-last_contacted_at", "d": "next", "v": [None, None], "b": None}
        for cursor in (signing.dumps(payload), keyset._fernet("another-key").encrypt(b"{}")):
            with pytest.raises(InvalidInputError):
                self.paginate(cursor if isinstance(cursor, str) else cursor.decode())


def test_an_ordering_must_end_in_a_non_null_key():
    with pytest.raises(ValueError, match="unique, non-null"):
        KeysetOrdering("bad", (SortKey("last_contacted_at", nullable=True),))
    with pytest.raises(ValueError, match="unique, non-null"):
        KeysetOrdering("empty", ())


# --- R49 (Phase 10): the whole sort row bounds the scan -------------------------------------
UNIFORM = KeysetOrdering("uniform", (SortKey("created_at"), SortKey("id")))
UNIFORM_DESC = KeysetOrdering(
    "uniform_desc", (SortKey("created_at", descending=True), SortKey("id", descending=True))
)
MIXED = KeysetOrdering("mixed", (SortKey("created_at"), SortKey("id", descending=True)))
NULLABLE = KeysetOrdering("nullable", (SortKey("last_contacted_at", nullable=True), SortKey("id")))


def second_page_sql(queryset, ordering: KeysetOrdering) -> str:
    paginator = KeysetPaginator(ordering, page_size=5, binding=None)
    first = paginator.paginate(queryset, None)
    window, _ = paginator.window(queryset, first.next_cursor)
    return str(window.query)


@pytest.mark.parametrize(
    ("ordering", "bounded_by_row"),
    [(UNIFORM, True), (UNIFORM_DESC, True), (MIXED, False), (NULLABLE, False)],
)
def test_a_uniform_not_null_ordering_starts_the_scan_at_the_cursors_whole_row(
    leads, owner, ordering, bounded_by_row
):
    """A page deep inside a large group of equal leading values re-read the group from its
    start (18,182 undated open tasks: 4.18 ms 5,000 rows in, 0.15 ms with the row bound).
    NULL placement and mixed directions can't be a row comparison: they keep the leading
    key's bound."""
    sql = second_page_sql(scoped(owner), ordering)
    assert ("ROW(" in sql) is bounded_by_row, sql


@pytest.mark.parametrize("ordering", [UNIFORM, UNIFORM_DESC, MIXED, NULLABLE])
@pytest.mark.parametrize("page_size", [1, 4, 13])
def test_one_large_group_of_equal_leading_values_pages_exactly(owner, ordering, page_size):
    """Every row shares the leading value but four: only the id orders the group."""
    rows = [LeadFactory(owner=owner) for _ in range(40)]
    same = timezone.now() - timedelta(days=3)
    Lead.objects.filter(pk__in=[lead.pk for lead in rows[4:]]).update(
        created_at=same, last_contacted_at=same
    )
    seen, _ = walk_forward(scoped(owner), ordering, page_size)
    assert seen == reference(scoped(owner), ordering)
    paginator = KeysetPaginator(ordering, page_size=page_size, binding=None)
    _, pages = walk_forward(scoped(owner), ordering, page_size)
    collected, cursor = list(pages[-1].items), pages[-1].previous_cursor
    while cursor:
        page = paginator.paginate(scoped(owner), cursor)
        collected[:0] = page.items
        cursor = page.previous_cursor
    assert [lead.pk for lead in collected] == reference(scoped(owner), ordering)
