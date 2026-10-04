"""Read queries for leads. Every query over leads starts from `scope.apply()`: there is no
unscoped reader here except `lead_by_id`, which services use after a write the actor was
already authorised to make.

Search, filters and sorts are allowlisted and bounded (docs/leads.md#search-filters-sorting):
- `q`: 2-100 characters, at most 5 searched words (core.text's search-input rules, shared
  with global search); every word must occur in the lead's names, organisation, email or
  phone digits (case-insensitive substring, served by one trigram index; `matching`).
  Phone-like words ("98765-43210") are reduced to digits first.
- filters: status, source, rating, owner (organisation-wide workspace only; it can only
  narrow the scope), created date range in the business time zone, archived.
- sorts: see ORDERINGS. Every ordering ends in the primary key, so keyset cursors are exact.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from django.db import connection
from django.db.models import BooleanField, Count, ExpressionWrapper, Q, QuerySet, Value
from django.db.models.functions import Lower, Upper

from arkray.core.access import AccessScope
from arkray.core.business_time import business_midnight, today_bounds
from arkray.core.errors import InvalidInputError, NotFoundError
from arkray.core.keyset import KeysetOrdering, SortKey
from arkray.core.knowledge import KnowledgeDocument, SourceType, compose
from arkray.core.ranking import Matches, SearchQuery, top_matches
from arkray.core.text import TextRejected, clean_line, search_needles

from .models import Lead, LeadSource, LeadStatus, StatusCategory
from .phones import phone_key, search_digits

DUPLICATE_LIMIT = 5  # shown
DUPLICATE_SCAN_LIMIT = 50  # considered (bounded even for a shared switchboard number)
NEW_LEADS_DEFAULT = 5

ORDERINGS: dict[str, KeysetOrdering] = {
    ordering.name: ordering
    for ordering in (
        KeysetOrdering(
            "-created_at", (SortKey("created_at", descending=True), SortKey("id", descending=True))
        ),
        KeysetOrdering("created_at", (SortKey("created_at"), SortKey("id"))),
        # The name is personal data: a private key, never copied into cursors (core.keyset).
        KeysetOrdering("name", (SortKey("display_name", private=True), SortKey("id"))),
        KeysetOrdering(
            "-updated_at", (SortKey("updated_at", descending=True), SortKey("id", descending=True))
        ),
        # Most recently contacted first; never-contacted leads last. `last_contacted_sort`
        # is last_contacted_at with "never" as the oldest possible date: NOT NULL, so cursors
        # bound the index scan and deep pages stay fast.
        KeysetOrdering(
            "-last_contacted_at",
            (SortKey("last_contacted_sort", descending=True), SortKey("id", descending=True)),
        ),
        # Longest without contact first, starting with leads nobody has contacted yet.
        KeysetOrdering("last_contacted_at", (SortKey("last_contacted_sort"), SortKey("id"))),
    )
}
DEFAULT_ORDERING = "-created_at"

# Columns the list needs from the owner (never the password hash or security fields).
_OWNER_FIELDS = ("owner__id", "owner__first_name", "owner__last_name", "owner__is_active")


@dataclass(frozen=True, slots=True)
class LeadFilters:
    q: str = ""
    status: str | None = None
    source: str | None = None
    rating: str | None = None
    owner_id: UUID | None = None
    created_from: date | None = None
    created_to: date | None = None
    archived: bool = False


@dataclass(frozen=True, slots=True)
class LeadSummary:
    """The authoritative lead figures for a scope at one moment (the dashboard shows exactly
    these; it must not re-derive them)."""

    total: int  # not archived
    new_today: int  # not archived, created during today's business day


def _contains(needle: str) -> Q:
    # upper() in PostgreSQL, like the indexed column, so both sides fold alike.
    return Q(search_text__contains=Upper(Value(needle)))


def needles(q: str) -> list[str]:
    """The words of `q` that are searched (core.text.search_needles); 400 for characters no
    stored text can contain (the API's validation refuses them first)."""
    try:
        return search_needles(q)
    except TextRejected as exc:
        raise InvalidInputError(details={"q": [str(exc)]}) from None


def match_condition(terms: Sequence[str]) -> Q:
    """**The** lead search rule, for the Leads list and global search alike: every term must
    occur in the lead's names, organisation, email or phone digits. A phone-like term
    ("98765-43210") matches phone digits or the text as typed ("1-800 Flowers",
    "Expo 2024-25")."""
    condition = Q()
    for needle in terms:
        term = _contains(needle)
        digits = search_digits(needle)
        if digits is not None and digits != needle:
            term |= _contains(digits)
        condition &= term
    return condition


def matching(queryset: QuerySet[Lead], terms: Sequence[str]) -> QuerySet[Lead]:
    return queryset.filter(match_condition(terms))


def _searched(queryset: QuerySet[Lead], q: str) -> QuerySet[Lead]:
    return matching(queryset, needles(q))


def listable(scope: AccessScope) -> QuerySet[Lead]:
    """Every lead the scope may list, archived or not, whatever the filters: where a page
    cursor's boundary lead is re-read (core.keyset), so a cursor can never measure a lead
    outside the scope."""
    return scope.apply(Lead.objects.all())


def lead_list(scope: AccessScope, filters: LeadFilters) -> QuerySet[Lead]:
    """The scoped, filtered leads for a list page (ordered and paginated by the caller)."""
    queryset = scope.apply(Lead.objects.all())
    queryset = queryset.filter(archived_at__isnull=not filters.archived)
    if filters.owner_id is not None:
        queryset = queryset.filter(owner_id=filters.owner_id)  # narrows the scope, never widens
    if filters.status is not None:
        queryset = queryset.filter(status__key=filters.status)
    if filters.source is not None:
        queryset = queryset.filter(source__key=filters.source)
    if filters.rating is not None:
        queryset = queryset.filter(rating=filters.rating)
    if filters.created_from is not None:
        queryset = queryset.filter(created_at__gte=business_midnight(filters.created_from))
    if filters.created_to is not None:
        end = business_midnight(filters.created_to + timedelta(days=1))
        queryset = queryset.filter(created_at__lt=end)
    queryset = _searched(queryset, filters.q)
    return queryset.select_related("owner", "status", "source").only(
        "id",
        "first_name",
        "last_name",
        "organization_name",
        "job_title",
        "email",
        "phone",
        "mobile",
        "rating",
        "last_contacted_at",
        "archived_at",
        "version",
        "created_at",
        "updated_at",
        "display_name",
        "last_contacted_sort",
        "status__key",
        "status__name",
        "status__category",
        "source__key",
        "source__name",
        *_OWNER_FIELDS,
    )


# What a global search result shows of a lead (docs/search.md#results): its name,
# organisation, status and owner. No contact data, even when the search matched it.
_SEARCH_RESULT_FIELDS = (
    "id",
    "display_name",
    "organization_name",
    "created_at",
    "status__key",
    "status__name",
    "status__category",
    *_OWNER_FIELDS,
)


def search(scope: AccessScope, query: SearchQuery, *, limit: int) -> Matches[Lead]:
    """Global search's leads: the Leads list's own search rule (`match_condition`) over the
    leads `scope` may list, archived ones left out, ranked by core.ranking against the
    display name, newest first among equals. The scope is applied before anything is
    matched."""
    return top_matches(
        scope.apply(Lead.objects.filter(archived_at__isnull=True)),
        text="search_text",
        condition=match_condition(query.terms),
        query=query,
        label="display_name",
        newest=("-created_at", "-id"),
        limit=limit,
        shape=lambda leads: leads.select_related("owner", "status").only(*_SEARCH_RESULT_FIELDS),
    )


def lead_summary(scope: AccessScope, *, now: datetime) -> LeadSummary:
    """Total leads and new leads today in `scope`, in one aggregate query: archived leads
    never count (the Leads list hides them by default, so each figure opens a list of
    exactly the leads it counts). "Today" is the business day containing `now`
    (core.business_time). A lead counts for its current owner: one created today and
    reassigned is the new owner's new lead."""
    start, end = today_bounds(now)
    today = Q(created_at__gte=start, created_at__lt=end)
    # Every column this reads is in leads_owner_created_idx (owner, created_at, id, and the
    # archive state it carries), so both counts come from an index-only scan.
    row = scope.apply(Lead.objects.filter(archived_at__isnull=True)).aggregate(
        total=Count("id"), new_today=Count("id", filter=today)
    )
    return LeadSummary(**row)


@dataclass(frozen=True, slots=True)
class StatusCount:
    key: str
    name: str
    category: str
    count: int


def status_breakdown(scope: AccessScope) -> list[StatusCount]:
    """How many non-archived leads in `scope` have each status (statuses in their configured
    order; statuses no lead has are left out). One grouped query."""
    rows = (
        scope.apply(Lead.objects.filter(archived_at__isnull=True))
        .values("status__key", "status__name", "status__category", "status__position")
        .order_by()
        .annotate(count=Count("id"))
    )
    ordered = sorted(rows, key=lambda r: (r["status__position"], r["status__name"]))
    return [
        StatusCount(r["status__key"], r["status__name"], r["status__category"], r["count"])
        for r in ordered
    ]


@dataclass(frozen=True, slots=True)
class OwnerLeadCount:
    owner_id: UUID
    total: int
    new_today: int


def lead_counts_by_owner(scope: AccessScope, *, now: datetime, limit: int) -> list[OwnerLeadCount]:
    """Non-archived leads and today's new leads per owner in `scope` (organisation-wide
    questions such as "who has the most leads"), most leads first, at most `limit` owners.
    The same definitions as `lead_summary`. One grouped query."""
    if not 1 <= limit <= 50:
        raise ValueError("limit out of range")
    start, end = today_bounds(now)
    rows = (
        scope.apply(Lead.objects.filter(archived_at__isnull=True))
        .values("owner_id")
        .order_by()
        .annotate(
            total=Count("id"),
            new_today=Count("id", filter=Q(created_at__gte=start, created_at__lt=end)),
        )
        .order_by("-total", "owner_id")[:limit]
    )
    return [OwnerLeadCount(**row) for row in rows]


def new_leads_today(
    scope: AccessScope, *, now: datetime, limit: int = NEW_LEADS_DEFAULT
) -> list[Lead]:
    """The newest of today's leads in `scope` (bounded; for the dashboard): exactly the
    leads `lead_summary` counts as new today, newest first, with their owner joined (one
    query). Only what a summary row shows is loaded: no contact data."""
    if not 1 <= limit <= 50:
        raise ValueError("limit out of range")
    start, end = today_bounds(now)
    return list(
        scope.apply(
            Lead.objects.filter(archived_at__isnull=True, created_at__gte=start, created_at__lt=end)
        )
        .select_related("owner")
        .only("id", "organization_name", "display_name", "created_at", *_OWNER_FIELDS)
        .order_by("-created_at", "-id")[:limit]
    )


def _with_relations(queryset: QuerySet[Lead]) -> QuerySet[Lead]:
    return queryset.select_related("owner", "created_by", "status", "source").defer(
        "search_text",
        "owner__password",
        "owner__session_epoch",
        "created_by__password",
        "created_by__session_epoch",
    )


def lead_detail(scope: AccessScope, lead_id: UUID) -> Lead:
    """One lead, if `scope` may see it. Otherwise NotFoundError, exactly as if it did not
    exist, so a guessed id reveals nothing."""
    lead = _with_relations(scope.apply(Lead.objects.filter(pk=lead_id))).first()
    if lead is None:
        raise NotFoundError()
    return lead


def lead_ref(scope: AccessScope, lead_id: UUID) -> Lead:
    """The identity and state of a lead `scope` may see (id, owner, archive state), for
    modules whose records hang off leads (Phase 4: whether its timeline may be read).
    NotFoundError outside the scope. Not a display read: no names or contact data."""
    lead = (
        scope.apply(Lead.objects.filter(pk=lead_id)).only("id", "owner_id", "archived_at").first()
    )
    if lead is None:
        raise NotFoundError()
    return lead


def lead_by_id(lead_id: UUID) -> Lead:
    """Unscoped: only for services returning a record the actor has just changed."""
    return _with_relations(Lead.objects.filter(pk=lead_id)).get()


def lock_lead(scope: AccessScope, lead_id: UUID, *, exclusive: bool = False) -> Lead:
    """Lock one lead inside `scope` until the caller's transaction ends (NotFoundError
    outside it). For modules whose records depend on the lead, e.g. an opportunity's owner
    follows its lead's owner (docs/pipeline.md#lock-order).

    The default lock (FOR NO KEY UPDATE) keeps the lead's owner, status and archive state
    fixed meanwhile: a reassignment waits for the caller to commit. `exclusive` (FOR UPDATE,
    the lock the lead services take) is for callers that will change the lead themselves,
    so the lock is never upgraded mid-transaction (a deadlock risk)."""
    locked = Lead.objects.select_for_update(no_key=not exclusive).filter(pk=lead_id)
    lead = scope.apply(locked).only(*_LOCKED_FIELDS).first()
    if lead is None:
        raise NotFoundError()
    return lead


def lock_lead_by_id(lead_id: UUID) -> Lead:
    """Lock (FOR NO KEY UPDATE) the lead of a record the caller has *already* found in its
    scope, e.g. the lead of an opportunity whose owner kept it after the lead was reassigned
    (the lock order starts at the lead). Not a read path: only the owner and state columns
    are loaded, and they must never be shown to the caller."""
    return Lead.objects.select_for_update(no_key=True).only(*_LOCKED_FIELDS).get(pk=lead_id)


_LOCKED_FIELDS = ("id", "owner_id", "status_id", "archived_at", "version")


def hold_lead_key(lead_id: UUID) -> None:
    """FOR KEY SHARE on a lead (what a foreign-key check takes) until the transaction ends,
    for writers of rows that only *reference* it (the Ask Arkray index). Taken before they
    lock anything else, they never wait for the lead while holding rows that a writer
    locking the lead (an erasure) needs next: that was a deadlock (whole-software audit)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM leads_lead WHERE id = %s FOR KEY SHARE", [lead_id])


def possible_duplicates(
    scope: AccessScope,
    *,
    email: str = "",
    phones: Sequence[str] = (),
    exclude_id: UUID | None = None,
) -> list[tuple[Lead, list[str]]]:
    """Leads *in this scope* with the same email (case-insensitive) or phone number (same
    canonical key), with what matched. Never looks outside the scope: it must not become a
    way to discover other users' leads. Archived leads are included (flagged).

    At most 50 matches are considered and the newest 5 of those are returned; with more than
    50 matches (a shared switchboard number) the 5 shown are recent but not guaranteed to be
    the very newest."""
    try:
        email_value = clean_line(email)
        phone_values = [clean_line(p) for p in phones]
    except TextRejected:
        return []  # nothing stored can contain such characters
    keys = sorted({k for p in phone_values if (k := phone_key(p))})
    if not email_value and not keys:
        return []
    # Case folding happens in PostgreSQL on both sides, exactly as the index does.
    email_match = Q(email_lower=Lower(Value(email_value))) & ~Q(email="")
    matches = Q(pk__in=[])
    if email_value:
        matches |= email_match
    if keys:
        matches |= Q(phone_keys__overlap=keys)
    queryset = scope.apply(
        Lead.objects.annotate(email_lower=Lower("email")).annotate(
            email_hit=ExpressionWrapper(
                email_match if email_value else Q(pk__in=[]), output_field=BooleanField()
            )
        )
    ).filter(matches)
    if exclude_id is not None:
        queryset = queryset.exclude(pk=exclude_id)
    # Deliberately unordered in SQL: that lets PostgreSQL combine the email and phone indexes
    # (BitmapOr, < 1 ms at 300k leads) instead of walking a date index hoping to meet a match
    # (213 ms). The few candidates are ordered here.
    candidates = list(
        queryset.select_related("owner")
        .only(
            "id",
            "organization_name",
            "email",
            "phone_keys",
            "archived_at",
            "display_name",
            "created_at",
            *_OWNER_FIELDS,
        )
        .order_by()[:DUPLICATE_SCAN_LIMIT]
    )
    candidates.sort(key=lambda lead: (lead.created_at, lead.pk), reverse=True)
    results: list[tuple[Lead, list[str]]] = []
    for lead in candidates[:DUPLICATE_LIMIT]:
        matched = []
        if getattr(lead, "email_hit", False):  # annotated above
            matched.append("email")
        if set(lead.phone_keys) & set(keys):
            matched.append("phone")
        results.append((lead, matched))
    return results


def statuses() -> list[LeadStatus]:
    return list(LeadStatus.objects.order_by("position", "name"))


def sources() -> list[LeadSource]:
    return list(LeadSource.objects.order_by("position", "name"))


def default_status() -> LeadStatus:
    status = LeadStatus.objects.filter(is_default=True).first()
    if status is None:  # the seed migration creates one; a database without it is broken
        raise LeadStatus.DoesNotExist("No default lead status is configured.")
    return status


def status_by_key(key: str) -> LeadStatus | None:
    return LeadStatus.objects.filter(key=key).first()


def first_active_status(category: StatusCategory) -> LeadStatus | None:
    """The first active status (by position) of a category, e.g. the "Converted" status a
    lead conversion moves the lead into, whatever it is called."""
    return LeadStatus.objects.filter(category=category, is_active=True).order_by("position").first()


# --- Ask Arkray's semantic index (Phase 8, docs/rag-architecture.md) -------------------------
# A lead is embedded only when someone wrote a description: its name and organisation alone
# are found by global search, and a million name-only vectors would cost index space and
# backfill time for nothing. The text is descriptive only: never email, phones or address
# lines.
_KNOWLEDGE_FIELDS = (
    "id",
    "owner_id",
    "display_name",
    "organization_name",
    "job_title",
    "city",
    "state",
    "country",
    "description",
    "created_at",
    "updated_at",
)
_HAS_KNOWLEDGE = Q(archived_at__isnull=True) & ~Q(description="")


def _knowledge(queryset: QuerySet[Lead]) -> list[KnowledgeDocument]:
    documents = []
    for lead in queryset.filter(_HAS_KNOWLEDGE).only(*_KNOWLEDGE_FIELDS):
        place = ", ".join(part for part in (lead.city, lead.state, lead.country) if part)
        documents.append(
            KnowledgeDocument(
                source_type=SourceType.LEAD,
                source_id=lead.pk,
                owner_id=lead.owner_id,
                lead_id=lead.pk,
                opportunity_id=None,
                label=lead.display_name,
                text=compose(
                    ("Lead", lead.display_name),
                    ("Organisation", lead.organization_name),
                    ("Job title", lead.job_title),
                    ("Location", place),
                    ("Description", lead.description),
                ),
                occurred_at=lead.created_at,
                updated_at=lead.updated_at,
            )
        )
    return documents


def knowledge_documents(scope: AccessScope, lead_ids: Collection[UUID]) -> list[KnowledgeDocument]:
    """The embeddable text of the given leads that `scope` may see now (archived leads and
    leads without a description have none). Retrieval re-reads every hit through this, so a
    reassigned or archived lead's stale vectors can never surface its text."""
    if not lead_ids:
        return []
    return _knowledge(scope.apply(Lead.objects.filter(pk__in=list(lead_ids))))


def knowledge_documents_for_indexing(lead_ids: Collection[UUID]) -> list[KnowledgeDocument]:
    """Unscoped: only for the indexer, which runs as the system and stores each document
    with its owner (never shown to anyone directly)."""
    if not lead_ids:
        return []
    return _knowledge(Lead.objects.filter(pk__in=list(lead_ids)))


def knowledge_source_ids(*, after: UUID | None, limit: int) -> list[UUID]:
    """The ids of leads that have a knowledge document, in id order, for rebuilding and
    reconciling the index in bounded batches (unscoped; indexer only)."""
    queryset = Lead.objects.filter(_HAS_KNOWLEDGE)
    if after is not None:
        queryset = queryset.filter(pk__gt=after)
    return list(queryset.order_by("pk").values_list("pk", flat=True)[:limit])
