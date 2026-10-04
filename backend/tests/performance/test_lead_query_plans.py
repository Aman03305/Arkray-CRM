"""Every important Leads query shape is servable by the index designed for it.

The timings come from a 300k-lead benchmark (docs/database.md#leads_lead). This test pins
the *shapes*: it builds the exact SQL the API runs (selectors + keyset paginator), forbids
sequential scans, and checks the planner reaches for the intended index. A change to an
ordering, its NULL placement, a filter or an index definition that breaks the match fails
here instead of in production.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.db import connection
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from arkray.core.access import AccessScope
from arkray.core.keyset import KeysetPaginator
from arkray.leads import selectors
from arkray.leads.models import Lead
from tests.factories import AdminFactory, UserFactory

pytestmark = pytest.mark.django_db

N_OWNERS, PER_OWNER = 20, 150


@pytest.fixture(scope="module")
def dataset(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        owners = [UserFactory() for _ in range(N_OWNERS)]
        admin = AdminFactory()
        now = timezone.now()
        leads = [
            Lead(
                first_name=["Rahul", "Priya", "Amit", "Sneha", "राजेश"][i % 5],
                last_name=["Sharma", "Patel", "Iyer", ""][i % 4],
                organization_name=f"Clinic {i % 97}",
                email=f"lead{i}@clinic{i % 97}.example",
                phone=f"+91 98{i:08d}",
                phone_keys=[f"+9198{i:08d}"],
                status_id=["new", "contacted", "qualified"][i % 3],
                source_id=["website", "referral", None][i % 3],
                rating=["hot", "warm", None][i % 3],
                owner=owners[i % N_OWNERS],
                created_by=owners[i % N_OWNERS],
                created_at=now - timedelta(minutes=i),
                last_contacted_at=None if i % 3 == 0 else now - timedelta(hours=i),
                archived_at=now if i % 25 == 0 else None,
            )
            for i in range(N_OWNERS * PER_OWNER)
        ]
        Lead.objects.bulk_create(leads, batch_size=1000)
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE leads_lead")
        yield owners, admin
        Lead.objects.all().delete()
        for user in [*owners, admin]:
            user.delete()


def plan(queryset, *disabled: str) -> str:
    """EXPLAIN with sequential scans (and any other `disabled` plan types) ruled out, so
    the plan shows which index *can* serve the query, independent of this small table."""
    sql, params = queryset.query.sql_with_params()
    with connection.cursor() as cursor:
        for setting in ("enable_seqscan", *disabled):
            cursor.execute(f"SET LOCAL {setting} = off")
        cursor.execute(f"EXPLAIN {sql}", params)
        return "\n".join(row[0] for row in cursor.fetchall())


def lead_scans(text: str) -> set[str]:
    """Index names used to read leads_lead ('Seq Scan' if the table is read in full)."""
    found = set(
        re.findall(r"(?:Index Scan|Index Only Scan)(?: Backward)? using (\w+) on leads_lead", text)
    )
    found |= set(re.findall(r"Bitmap Index Scan on (\w+)", text))
    if re.search(r"Seq Scan on leads_lead\b", text):
        found.add("Seq Scan")
    return found


def page_query(scope, filters, ordering, *, second_page=False):
    queryset = selectors.lead_list(scope, filters)
    paginator = KeysetPaginator(selectors.ORDERINGS[ordering], page_size=25, binding=None)
    cursor = paginator.paginate(queryset, None).next_cursor if second_page else None
    return paginator.window(queryset, cursor)[0]


F = selectors.LeadFilters

CASES = [
    # (label, scope kind, filters, ordering, expected index)
    ("own newest", "own", F(), "-created_at", "leads_owner_created_idx"),
    ("own oldest", "own", F(), "created_at", "leads_owner_created_idx"),
    ("own recently updated", "own", F(), "-updated_at", "leads_owner_updated_idx"),
    ("own recently contacted", "own", F(), "-last_contacted_at", "leads_owner_contacted_idx"),
    ("own least recently contacted", "own", F(), "last_contacted_at", "leads_owner_contacted_idx"),
    ("own status filter", "own", F(status="qualified"), "-created_at", "leads_owner_created_idx"),
    # Phase 2 review: without an owner-leading name index these walked the org-wide one.
    ("own name", "own", F(), "name", "leads_owner_name_idx"),
    ("own name, status filter", "own", F(status="qualified"), "name", "leads_owner_name_idx"),
    ("own name, archived", "own", F(archived=True), "name", "leads_owner_name_idx"),
    ("own archived", "own", F(archived=True), "-created_at", "leads_owner_created_idx"),
    ("org newest", "org", F(), "-created_at", "leads_created_idx"),
    ("org oldest", "org", F(), "created_at", "leads_created_idx"),
    ("org name", "org", F(), "name", "leads_name_idx"),
    ("org recently updated", "org", F(), "-updated_at", "leads_updated_idx"),
    ("org recently contacted", "org", F(), "-last_contacted_at", "leads_contacted_idx"),
    ("org least recently contacted", "org", F(), "last_contacted_at", "leads_contacted_idx"),
    ("org one owner", "org-owner", F(), "-created_at", "leads_owner_created_idx"),
]


@pytest.mark.parametrize(
    ("label", "kind", "filters", "ordering", "index"), CASES, ids=[c[0] for c in CASES]
)
@pytest.mark.parametrize("second_page", [False, True], ids=["page 1", "page 2"])
def test_list_queries_use_their_index(dataset, label, kind, filters, ordering, index, second_page):
    owners, admin = dataset
    if kind == "own":
        scope = AccessScope.own(owners[0].pk)
    else:
        scope = AccessScope.organization(admin.pk)
        if kind == "org-owner":
            filters = F(owner_id=owners[0].pk)
    # No explicit sort allowed: the index itself must deliver the page in order.
    text = plan(page_query(scope, filters, ordering, second_page=second_page), "enable_sort")
    assert index in lead_scans(text), text
    assert "Seq Scan" not in lead_scans(text), text
    assert "Sort" not in text, text


@pytest.mark.parametrize("q", ["patel", "clinic 42", "9800000123", "lead77@clinic77"])
def test_organisation_search_can_use_the_trigram_index(dataset, q):
    _, admin = dataset
    queryset = selectors.lead_list(AccessScope.organization(admin.pk), F(q=q)).order_by()
    # Only bitmap scans allowed: they need an indexable condition, so this proves the
    # search predicate matches the trigram index.
    assert "leads_search_trgm" in lead_scans(plan(queryset, "enable_indexscan"))


def test_duplicate_lookup_combines_the_email_and_phone_indexes(dataset):
    _, admin = dataset
    matches = (Q(email_lower="lead5@clinic5.example") & ~Q(email="")) | Q(
        phone_keys__overlap=["+919800000005"]
    )
    queryset = (
        AccessScope.organization(admin.pk)
        .apply(Lead.objects.annotate(email_lower=Lower("email")))
        .filter(matches)
        .order_by()[: selectors.DUPLICATE_SCAN_LIMIT]
    )
    assert {"leads_email_lower_idx", "leads_phone_keys_gin"} <= lead_scans(plan(queryset))


def test_the_duplicate_selector_matches_the_planned_query(dataset):
    """The selector itself returns the match through that query shape."""
    _, admin = dataset
    found = selectors.possible_duplicates(
        AccessScope.organization(admin.pk), email="LEAD5@clinic5.example", phones=["+91 9800000005"]
    )
    assert [matched for _, matched in found] == [["email", "phone"]]


@pytest.mark.parametrize("ordering", ["-last_contacted_at", "last_contacted_at"])
def test_deep_last_contact_pages_start_the_index_scan_at_the_cursor(dataset, ordering):
    """Phase 2 review: with a NULL-able sort key the cursor couldn't bound the scan, so a
    page's cost grew with its depth. The NOT NULL sort column makes it an index condition;
    since Phase 10 (R49) the whole (sort, id) row, so the scan starts exactly at the cursor."""
    _, admin = dataset
    text = plan(page_query(AccessScope.organization(admin.pk), F(), ordering, second_page=True))
    assert re.search(r"Index Cond: \(ROW\(last_contacted_sort, id\) [<>] ROW\(", text), text
