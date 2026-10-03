"""Read queries for leads. Every query over leads starts from `scope.apply()`: there is no
unscoped reader here except `lead_by_id`, which services use after a write the actor was
already authorised to make.

Search, filters and sorts are allowlisted and bounded (docs/leads.md#search-filters-sorting):
- `q`: 2-100 characters, at most 5 whitespace-separated terms; every term must occur in the
  lead's names, organisation, email or phone digits (case-insensitive substring, served by
  one trigram index). Phone-like terms ("98765-43210") are reduced to digits first.
- filters: status, source, rating, owner (organisation-wide workspace only; it can only
  narrow the scope), created date range in the business time zone, archived.
- sorts: see ORDERINGS. Every ordering ends in the primary key, so keyset cursors are exact.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import BooleanField, ExpressionWrapper, Q, QuerySet, Value
from django.db.models.functions import Lower, Upper

from arkray.core.access import AccessScope
from arkray.core.errors import InvalidInputError, NotFoundError
from arkray.core.keyset import KeysetOrdering, SortKey
from arkray.core.text import TextRejected, clean_line
from arkray.identity.selectors import search_terms

from .models import Lead, LeadSource, LeadStatus, StatusCategory
from .phones import phone_key, search_digits

DUPLICATE_LIMIT = 5  # shown
DUPLICATE_SCAN_LIMIT = 50  # considered (bounded even for a shared switchboard number)
SEARCH_TERM_MIN_LENGTH = 2

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


def _business_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(settings.CRM_TIME_ZONE))


def _contains(needle: str) -> Q:
    # upper() in PostgreSQL, like the indexed column, so both sides fold alike.
    return Q(search_text__contains=Upper(Value(needle)))


def search_needles(q: str) -> list[str]:
    """The terms of `q` that are searched: cleaned, at least 2 characters, at most 5."""
    needles = []
    for term in search_terms(q):
        try:
            needle = clean_line(term)
        except TextRejected as exc:
            raise InvalidInputError(details={"q": [str(exc)]}) from None
        if len(needle) >= SEARCH_TERM_MIN_LENGTH:
            needles.append(needle)
    return needles


def _searched(queryset: QuerySet[Lead], q: str) -> QuerySet[Lead]:
    """Every term must match. A phone-like term ("98765-43210") matches phone digits or the
    text as typed ("1-800 Flowers", "Expo 2024-25")."""
    for needle in search_needles(q):
        condition = _contains(needle)
        digits = search_digits(needle)
        if digits is not None and digits != needle:
            condition |= _contains(digits)
        queryset = queryset.filter(condition)
    return queryset


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
        queryset = queryset.filter(created_at__gte=_business_midnight(filters.created_from))
    if filters.created_to is not None:
        end = _business_midnight(filters.created_to + timedelta(days=1))
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
