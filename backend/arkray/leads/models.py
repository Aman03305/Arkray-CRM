"""Leads: the central sales-prospect record (docs/leads.md).

Arkray CRM has no Company, Account or Product entity. A lead is a person and/or an
organisation being sold to; `organization_name` is a plain text attribute of the lead, and
has nothing to do with Arkray's own users or tenancy.

Statuses and sources are rows in small configuration tables rather than code enums, so new
values can be introduced without a schema change. Leads reference them by their immutable
`key` ("new", "cold_call"), which is also what the API speaks; business rules key off a
status's `category`, never its display name.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import F, Func, Q, Value
from django.db.models.functions import Coalesce, Concat, Lower, NullIf, Trim, Upper

from arkray.core.models import TimeStampedModel, UUIDPrimaryKeyModel
from arkray.identity.models import User

from .phones import PHONE_MAX_LENGTH, phone_key

KEY_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
NAME_MAX_LENGTH = 100
ORGANIZATION_MAX_LENGTH = 200
JOB_TITLE_MAX_LENGTH = 100
EMAIL_MAX_LENGTH = 254
ADDRESS_LINE_MAX_LENGTH = 200
LOCALITY_MAX_LENGTH = 100
POSTAL_CODE_MAX_LENGTH = 20
DESCRIPTION_MAX_LENGTH = 5000
PHONE_FIELDS = ("phone", "mobile", "alternate_phone")


class StatusCategory(models.TextChoices):
    """What a status *means*. Rules and reports use this, so statuses can be renamed or
    added (for example "Nurturing" as another open status) without touching code."""

    OPEN = "open", "Open"
    QUALIFIED = "qualified", "Qualified"
    UNQUALIFIED = "unqualified", "Unqualified"
    CONVERTED = "converted", "Converted"


class Rating(models.TextChoices):
    """A salesperson's own judgement of interest. Not an AI or computed score."""

    HOT = "hot", "Hot"
    WARM = "warm", "Warm"
    COLD = "cold", "Cold"


class LeadStatus(UUIDPrimaryKeyModel):
    key = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=50, unique=True)
    category = models.CharField(max_length=16, choices=StatusCategory.choices)
    position = models.PositiveSmallIntegerField()
    # Retired statuses stay (leads may still be in them) but can't be chosen for new changes.
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        db_table = "leads_lead_status"
        constraints = [
            models.CheckConstraint(
                condition=Q(key__regex=KEY_PATTERN), name="leads_lead_status_key_format"
            ),
            models.CheckConstraint(condition=~Q(name=""), name="leads_lead_status_name_present"),
            models.CheckConstraint(
                condition=Q(category__in=StatusCategory.values),
                name="leads_lead_status_category_valid",
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(is_active=True),
                name="leads_lead_status_default_is_active",
            ),
            models.UniqueConstraint(
                fields=["is_default"],
                condition=Q(is_default=True),
                name="leads_lead_status_one_default",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class LeadSource(UUIDPrimaryKeyModel):
    key = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=50, unique=True)
    position = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "leads_lead_source"
        constraints = [
            models.CheckConstraint(
                condition=Q(key__regex=KEY_PATTERN), name="leads_lead_source_key_format"
            ),
            models.CheckConstraint(condition=~Q(name=""), name="leads_lead_source_name_present"),
        ]

    def __str__(self) -> str:
        return self.name


def _digits(field: str) -> Func:
    return Func(F(field), Value("[^0-9]"), Value(""), Value("g"), function="regexp_replace")


# The name people see and sort by: "first last", or the organisation for an organisation-only
# lead. Derived by the database, so it can never disagree with the fields it comes from.
DISPLAY_NAME = Coalesce(
    NullIf(Trim(Concat(F("first_name"), Value(" "), F("last_name"))), Value("")),
    F("organization_name"),
)

# "Never contacted" sorts as the oldest possible contact (real ones are from 2000 onwards),
# which keeps the last-contact sort key NOT NULL: cursors can then bound the index scan, so
# deep pages cost the same as the first (Phase 2 review: 35-145 ms deep pages with NULLs).
NEVER_CONTACTED = datetime(1900, 1, 1, tzinfo=UTC)
LAST_CONTACTED_SORT = Coalesce(F("last_contacted_at"), Value(NEVER_CONTACTED))

# One upper-cased text holding everything the Leads search matches: names, organisation,
# email and every phone number reduced to digits. One trigram index serves it
# (docs/database.md#leads_lead). The separator is a space, and search terms never contain
# spaces, so a term can never match across two fields.
SEARCH_TEXT = Upper(
    Concat(
        F("first_name"),
        Value(" "),
        F("last_name"),
        Value(" "),
        F("organization_name"),
        Value(" "),
        F("email"),
        Value(" "),
        _digits("phone"),
        Value(" "),
        _digits("mobile"),
        Value(" "),
        _digits("alternate_phone"),
    )
)


class LeadQuerySet(models.QuerySet["Lead"]):
    """Keeps `phone_keys` in step with the numbers on every ORM write path: `save()`
    recomputes them, `bulk_create` does too, and bulk updates of numbers (which can't) are
    refused rather than silently leaving stale matching keys behind."""

    def bulk_create(self, objs: Iterable[Lead], *args: Any, **kwargs: Any) -> list[Lead]:
        leads = list(objs)
        for lead in leads:
            lead.refresh_phone_keys()
        return super().bulk_create(leads, *args, **kwargs)

    def update(self, **kwargs: Any) -> int:
        _refuse_bulk_phone_writes(kwargs)
        return super().update(**kwargs)

    def bulk_update(
        self, objs: Iterable[Lead], fields: Iterable[str], *args: Any, **kwargs: Any
    ) -> int:
        fields = list(fields)
        _refuse_bulk_phone_writes(fields)
        return super().bulk_update(objs, fields, *args, **kwargs)


def _refuse_bulk_phone_writes(fields: Iterable[str]) -> None:
    if set(fields) & set(PHONE_FIELDS):
        raise ValueError("Change phone numbers with Lead.save(), which keeps phone_keys in sync.")


class Lead(UUIDPrimaryKeyModel, TimeStampedModel):
    # --- who ---------------------------------------------------------------------------------
    # Both optional: many people use one name, and a lead may be an organisation whose
    # contact person is not known yet. At least one of the three is required (constraint).
    first_name = models.CharField(max_length=NAME_MAX_LENGTH, blank=True, default="")
    last_name = models.CharField(max_length=NAME_MAX_LENGTH, blank=True, default="")
    organization_name = models.CharField(max_length=ORGANIZATION_MAX_LENGTH, blank=True, default="")
    job_title = models.CharField(max_length=JOB_TITLE_MAX_LENGTH, blank=True, default="")

    # --- contact (optional; contact data, not an identity: no uniqueness) ---------------------
    email = models.CharField(max_length=EMAIL_MAX_LENGTH, blank=True, default="")
    phone = models.CharField(max_length=PHONE_MAX_LENGTH, blank=True, default="")
    mobile = models.CharField(max_length=PHONE_MAX_LENGTH, blank=True, default="")
    alternate_phone = models.CharField(max_length=PHONE_MAX_LENGTH, blank=True, default="")
    # Canonical matching keys of the three numbers (phones.phone_key), kept in sync by save().
    phone_keys = ArrayField(models.CharField(max_length=40), default=list, blank=True)

    # --- address ---------------------------------------------------------------------------
    address_line_1 = models.CharField(max_length=ADDRESS_LINE_MAX_LENGTH, blank=True, default="")
    address_line_2 = models.CharField(max_length=ADDRESS_LINE_MAX_LENGTH, blank=True, default="")
    city = models.CharField(max_length=LOCALITY_MAX_LENGTH, blank=True, default="")
    state = models.CharField(max_length=LOCALITY_MAX_LENGTH, blank=True, default="")
    postal_code = models.CharField(max_length=POSTAL_CODE_MAX_LENGTH, blank=True, default="")
    country = models.CharField(max_length=2, blank=True, default="")  # ISO 3166-1 alpha-2

    # --- sales -------------------------------------------------------------------------------
    status = models.ForeignKey(
        LeadStatus,
        to_field="key",
        db_column="status_key",
        on_delete=models.PROTECT,
        related_name="+",
        db_index=False,  # filtered together with the owner: see the composite indexes
    )
    source = models.ForeignKey(
        LeadSource,
        to_field="key",
        db_column="source_key",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_index=False,
    )
    # NULL = not rated. An enum column with a CHECK constraint, not free text.
    rating = models.CharField(  # noqa: DJ001
        max_length=8, choices=Rating.choices, null=True, blank=True
    )
    # The one ownership concept ("owner" = "assigned to"): the responsible salesperson and the
    # authorization key of every read path. Changed only by services.reassign_lead().
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    # Provenance, never changes: e.g. the administrator who created a lead for a salesperson.
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    description = models.TextField(max_length=DESCRIPTION_MAX_LENGTH, blank=True, default="")

    # --- lifecycle -------------------------------------------------------------------------
    # Leads are never deleted; archiving hides them from the default list (restorable).
    archived_at = models.DateTimeField(null=True, blank=True)
    # Optimistic concurrency: every change increments it; writes must present the current one.
    version = models.PositiveIntegerField(default=1)

    display_name = models.GeneratedField(
        expression=DISPLAY_NAME,
        output_field=models.CharField(max_length=ORGANIZATION_MAX_LENGTH + 1),
        db_persist=True,
    )
    search_text = models.GeneratedField(
        expression=SEARCH_TEXT, output_field=models.TextField(), db_persist=True
    )
    last_contacted_sort = models.GeneratedField(
        expression=LAST_CONTACTED_SORT, output_field=models.DateTimeField(), db_persist=True
    )

    objects = LeadQuerySet.as_manager()

    class Meta:
        db_table = "leads_lead"
        # Every index serves a measured query (docs/database.md#leads_lead has the plans).
        # Owner-leading indexes serve one person's workspace; the others the organisation.
        indexes = [
            # One owner's leads, newest first (the default list), and the access path for
            # every per-owner filter and search. It carries the archive state (Phase 5) so
            # the dashboard's lead figures (selectors.lead_summary) are counted from the
            # index alone: without it the heaviest owner's count read every one of their rows
            # to check it, 93-133 ms at 66,700 leads and 177 ms organisation-wide at
            # 1,000,000 leads (8 ms and 50 ms with it; docs/dashboard.md#performance).
            models.Index(
                F("owner"),
                F("created_at").desc(),
                F("id").desc(),
                name="leads_owner_created_idx",
                include=["archived_at"],
            ),
            # One owner's leads by "recently updated" and by last contact (both directions:
            # a backward scan gives "longest since contact, never contacted first"). Without
            # these the planner walks the organisation-wide index for owners whose leads
            # cluster in time (310 ms measured at 300k leads; 0.1 ms with them).
            models.Index(
                F("owner"), F("updated_at").desc(), F("id").desc(), name="leads_owner_updated_idx"
            ),
            models.Index(
                F("owner"),
                F("last_contacted_sort").desc(),
                F("id").desc(),
                name="leads_owner_contacted_idx",
            ),
            # One owner's leads by name. Without it a salesperson's name-sorted list with a
            # selective filter walked the organisation-wide name index (136-368 ms at 300k
            # leads; Phase 2 review).
            models.Index(F("owner"), F("display_name"), F("id"), name="leads_owner_name_idx"),
            # Organisation-wide lists (administrators): newest, name, updated, last contact.
            models.Index(F("created_at").desc(), F("id").desc(), name="leads_created_idx"),
            models.Index(F("display_name"), F("id"), name="leads_name_idx"),
            models.Index(F("updated_at").desc(), F("id").desc(), name="leads_updated_idx"),
            models.Index(
                F("last_contacted_sort").desc(), F("id").desc(), name="leads_contacted_idx"
            ),
            # Leads search (substring match on names, organisation, email, phone digits).
            GinIndex(OpClass(F("search_text"), name="gin_trgm_ops"), name="leads_search_trgm"),
            # Duplicate assistance: same email (case-insensitive), same phone number.
            models.Index(Lower("email"), name="leads_email_lower_idx", condition=~Q(email="")),
            GinIndex(F("phone_keys"), name="leads_phone_keys_gin"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(first_name="", last_name="", organization_name=""),
                name="leads_lead_name_present",
            ),
            models.CheckConstraint(
                condition=Q(rating__isnull=True) | Q(rating__in=Rating.values),
                name="leads_lead_rating_valid",
            ),
            models.CheckConstraint(
                condition=Q(country="") | Q(country__regex=r"^[A-Z]{2}$"),
                name="leads_lead_country_format",
            ),
            models.CheckConstraint(condition=Q(version__gte=1), name="leads_lead_version_positive"),
            # No "updated_at >= created_at" CHECK: both come from the app servers' clocks, and
            # milliseconds of skew between two servers must not make an edit fail (review).
            # Trivially unique (id is the key); it exists so another table can reference a
            # lead *together with its owner*: an open opportunity's owner must be its lead's
            # owner, enforced by a foreign key (docs/pipeline.md#ownership).
            models.UniqueConstraint(fields=["id", "owner"], name="leads_lead_id_owner_key"),
        ]

    def __str__(self) -> str:
        return f"Lead({self.pk})"  # never contact data: this string can reach logs

    def refresh_phone_keys(self) -> None:
        self.phone_keys = sorted({k for f in PHONE_FIELDS if (k := phone_key(getattr(self, f)))})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.refresh_phone_keys()
        if kwargs.get("update_fields") is not None:
            fields = set(kwargs["update_fields"])  # may be any iterable, even a generator
            kwargs["update_fields"] = (
                fields | {"phone_keys"} if fields & set(PHONE_FIELDS) else fields
            )
        super().save(*args, **kwargs)
