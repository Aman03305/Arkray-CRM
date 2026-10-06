"""Pipelines, stages and opportunities (docs/pipeline.md).

An opportunity is a potential sale to one lead (Arkray CRM has no Company, Account or
Product entity). It sits in one stage of one pipeline; stages are configuration rows, not
code, and business rules key off a stage's `category` (open, won, lost), never its name.

Integrity the database enforces, whatever code path writes (docs/database.md#pipeline):
- an opportunity's `status` *is* its stage's category and its `pipeline` is its stage's
  pipeline: a composite foreign key onto the stage's (id, pipeline, category), so the two
  can never disagree, and a stage's category can't change while opportunities use it;
- an *open* opportunity is owned by its lead's owner: a composite foreign key from
  (lead, open_owner_id) onto the lead's (id, owner), checked at commit. Closed opportunities
  keep the owner who closed them (their `open_owner_id` is NULL, so the key doesn't apply);
- won is 100 %, lost is 0 %, closed_at is set exactly when closed, money is NUMERIC.
Both composite keys are created by migration 0001 (Django can't declare them).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import Case, F, Func, Q, Value, When
from django.db.models.functions import Coalesce, Concat, Lower, Upper
from django.db.models.lookups import Exact
from django.utils import timezone

from arkray.core.business_time import business_date
from arkray.core.models import AppendOnlyModel, TimeStampedModel, UUIDPrimaryKeyModel
from arkray.identity.models import User
from arkray.leads.models import EMAIL_MAX_LENGTH, Lead
from arkray.leads.phones import PHONE_MAX_LENGTH

KEY_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
PIPELINE_NAME_MAX_LENGTH = 100
STAGE_NAME_MAX_LENGTH = 50
TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 5000
LOST_REASON_MAX_LENGTH = 500
# Customer and instrument details (docs/pipeline.md#opportunities). Plain attributes of the
# opportunity: Arkray CRM has no Account, Contact or Product entity.
ACCOUNT_NAME_MAX_LENGTH = 200
CUSTOMER_NAME_MAX_LENGTH = 200
ADDRESS_MAX_LENGTH = 1000
INSTRUMENT_NAME_MAX_LENGTH = 200
# Free text such as "300 tests/day": the product has no workload unit semantics, so none is
# invented (a number with a wrong unit would be worse than the salesperson's own words).
WORK_LOAD_MAX_LENGTH = 100
# Expected CPT, free text for the same reason: nothing in the CRM's documentation defines CPT
# or its unit, so the salesperson's own words are kept until the business confirms them
# (docs/pipeline.md#expected-cpt). One bound and one rule for every CPT: validation.clean_cpt.
CPT_MAX_LENGTH = 100
EXPECTED_CPT_MAX_LENGTH = CPT_MAX_LENGTH
# The agreed CPT asked for with the agreed (negotiated) price on entering a negotiation stage
# (ADR-0029): free text for the same reason as Expected CPT.
AGREED_CPT_MAX_LENGTH = CPT_MAX_LENGTH
# Configuration bounds (per owner / per pipeline), so nobody can grow the configuration
# without limit (docs/pipeline.md#configuration).
MAX_PIPELINES_PER_OWNER = 25
MAX_STAGES_PER_PIPELINE = 20
MAX_FIELDS_PER_PIPELINE = 30
MAX_FIELD_OPTIONS = 50
FIELD_NAME_MAX_LENGTH = 60
FIELD_OPTION_MAX_LENGTH = 100
# The serialised custom values of one opportunity (bytes of JSON).
CUSTOM_VALUES_MAX_BYTES = 32 * 1024

# Money: NUMERIC(14, 2), i.e. up to 999,999,999,999.99 (just under ₹1 lakh crore) in the
# organisation currency (settings.CRM_CURRENCY). Never a float anywhere.
MONEY_DIGITS, MONEY_PLACES = 14, 2
MAX_VALUE = Decimal("999999999999.99")
# Probabilities: NUMERIC(5, 2) percentages, 0.00-100.00.
PROBABILITY_DIGITS, PROBABILITY_PLACES = 5, 2
CERTAIN, IMPOSSIBLE = Decimal("100"), Decimal("0")
# Both have exactly two decimal places (paise; hundredths of a percent).
HUNDREDTH = Decimal("0.01")
# Expected close dates are business dates (no time, no time zone) within a sane range.
EARLIEST_CLOSE_DATE, LATEST_CLOSE_DATE = date(2000, 1, 1), date(2099, 12, 31)
# Sort sentinels that keep the board's sort keys NOT NULL (cursors then bound index scans,
# as for leads' last-contact sort): "no expected close date" sorts after every real date,
# "never closed" before every real closing time.
UNDATED = date(9999, 12, 31)
NEVER_CLOSED = datetime(1900, 1, 1, tzinfo=UTC)


class StageCategory(models.TextChoices):
    """What a stage *means*. Won/lost behaviour follows the category, never the name."""

    OPEN = "open", "Open"
    WON = "won", "Won"
    LOST = "lost", "Lost"


CLOSED = (StageCategory.WON, StageCategory.LOST)


class Pipeline(UUIDPrimaryKeyModel, TimeStampedModel):
    """A sales process: an ordered set of stages.

    An *organisation* pipeline (`owner` NULL) is shared: everyone can use it and
    administrators (config.manage) configure it; the seeded "Sales Pipeline" is one, and the
    default. A *personal* pipeline belongs to one user: they (and administrators managing
    their workspace) configure it, and it holds their opportunities (docs/pipeline.md).
    Archived pipelines (`is_active` false) are kept: opportunities and history reference
    them."""

    key = models.CharField(max_length=32, unique=True)  # immutable identifier
    name = models.CharField(max_length=PIPELINE_NAME_MAX_LENGTH)
    owner = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+", db_index=False
    )
    # Provenance (e.g. the administrator who made it for a user); NULL for seeded pipelines.
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+", db_index=False
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    # Every configuration change (name, stages, custom fields, archive) bumps it: two people
    # editing the same pipeline can't overwrite each other (409).
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "pipeline_pipeline"
        indexes = [
            # One user's pipelines (their pipeline list; the per-owner limit).
            models.Index(
                F("owner"),
                F("name"),
                name="pipeline_pipeline_owner_idx",
                condition=Q(owner__isnull=False),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(key__regex=KEY_PATTERN), name="pipeline_pipeline_key_format"
            ),
            models.CheckConstraint(condition=~Q(name=""), name="pipeline_pipeline_name_present"),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(is_active=True),
                name="pipeline_pipeline_default_is_active",
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(owner__isnull=True),
                name="pipeline_pipeline_default_is_shared",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="pipeline_pipeline_version_positive"
            ),
            models.UniqueConstraint(
                fields=["is_default"],
                condition=Q(is_default=True),
                name="pipeline_pipeline_one_default",
            ),
            # Names are unique among the active pipelines of one owner (or among the
            # organisation's), case-insensitively; different users may reuse a name.
            models.UniqueConstraint(
                Lower("name"),
                condition=Q(owner__isnull=True, is_active=True),
                name="pipeline_pipeline_shared_name_unique",
            ),
            models.UniqueConstraint(
                F("owner"),
                Lower("name"),
                condition=Q(owner__isnull=False, is_active=True),
                name="pipeline_pipeline_owner_name_unique",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_shared(self) -> bool:
        return self.owner_id is None


class Stage(UUIDPrimaryKeyModel, TimeStampedModel):
    """One column of a pipeline. Renaming, re-probability-ing, reordering and retiring a
    stage never rewrites opportunities or their history (docs/pipeline.md#stages)."""

    pipeline = models.ForeignKey(
        Pipeline, on_delete=models.PROTECT, related_name="stages", db_index=False
    )
    key = models.CharField(max_length=32)  # immutable identifier within the pipeline
    name = models.CharField(max_length=STAGE_NAME_MAX_LENGTH)
    # Explicit, unique order within the pipeline (Kanban column order). The uniqueness is
    # checked at commit, so a reorder can swap positions inside one transaction.
    position = models.PositiveSmallIntegerField()
    # The probability an opportunity adopts when it enters this stage.
    probability = models.DecimalField(
        max_digits=PROBABILITY_DIGITS, decimal_places=PROBABILITY_PLACES
    )
    category = models.CharField(max_length=8, choices=StageCategory.choices)
    # A negotiation stage (an open stage): entering it requires the agreed price and agreed
    # CPT, which are recorded in NegotiationPrice (docs/pipeline.md#negotiation). Domain
    # data, not the name: "Negotiation" renamed to "Commercial discussion" keeps the behaviour.
    is_negotiation = models.BooleanField(default=False)
    # Retired stages stay (opportunities and history may reference them) but nothing can
    # move into them.
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "pipeline_stage"
        constraints = [
            models.CheckConstraint(
                condition=Q(key__regex=KEY_PATTERN), name="pipeline_stage_key_format"
            ),
            models.CheckConstraint(condition=~Q(name=""), name="pipeline_stage_name_present"),
            models.CheckConstraint(
                condition=Q(category__in=StageCategory.values),
                name="pipeline_stage_category_valid",
            ),
            models.CheckConstraint(
                condition=Q(is_negotiation=False) | Q(category=StageCategory.OPEN),
                name="pipeline_stage_negotiation_is_open",
            ),
            models.CheckConstraint(
                condition=Q(probability__gte=IMPOSSIBLE, probability__lte=CERTAIN),
                name="pipeline_stage_probability_range",
            ),
            models.CheckConstraint(
                condition=~Q(category=StageCategory.WON) | Q(probability=CERTAIN),
                name="pipeline_stage_won_is_certain",
            ),
            models.CheckConstraint(
                condition=~Q(category=StageCategory.LOST) | Q(probability=IMPOSSIBLE),
                name="pipeline_stage_lost_is_impossible",
            ),
            models.UniqueConstraint(fields=["pipeline", "key"], name="pipeline_stage_key_unique"),
            models.UniqueConstraint(
                fields=["pipeline", "position"],
                name="pipeline_stage_position_unique",
                deferrable=models.Deferrable.DEFERRED,
            ),
            models.UniqueConstraint(
                F("pipeline"),
                Lower("name"),
                condition=Q(is_active=True),
                name="pipeline_stage_active_name_unique",
            ),
            # The target of the opportunities' (stage, pipeline, status) foreign key.
            models.UniqueConstraint(
                fields=["id", "pipeline", "category"], name="pipeline_stage_category_key"
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_closed(self) -> bool:
        return self.category in CLOSED

    @property
    def stage_type(self) -> str:
        """What the stage means, as one value: open, negotiation, won or lost."""
        return StageType.NEGOTIATION if self.is_negotiation else str(self.category)


class StageType(models.TextChoices):
    """The configurable meaning of a stage (the stage manager's "Type"): a category plus the
    negotiation flag."""

    OPEN = "open", "Open"
    NEGOTIATION = "negotiation", "Negotiation"
    WON = "won", "Won"
    LOST = "lost", "Lost"


def business_today() -> date:
    return business_date(timezone.now())


# NULL unless the opportunity is open: the foreign key onto the lead's (id, owner) then
# only binds open opportunities (MATCH SIMPLE skips rows with a NULL column).
OPEN_OWNER = Case(When(status=StageCategory.OPEN, then=F("owner_id")), default=None)
EXPECTED_CLOSE_SORT = Coalesce(F("expected_close_date"), Value(UNDATED))
CLOSED_SORT = Coalesce(F("closed_at"), Value(NEVER_CLOSED))
# What global search matches (docs/search.md#opportunities): the title and the opportunity's
# own customer snapshot (account and customer names), upper-cased like every search text so
# PostgreSQL folds both sides alike, separated by a space (search words never contain one, so
# no word matches across two fields). The customer names are the opportunity's own fields,
# shown to everyone who can see it; since Leads left the UI (ADR-0027) they are how people find
# a customer's deals. Not the lead's name: a closed opportunity's lead may since belong to
# someone else, and its name is then shown to the opportunity's owner as "restricted" (matching
# on it would reveal it). Not amounts.
SEARCH_TEXT = Upper(Concat("title", Value(" "), "account_name", Value(" "), "customer_name"))


class Opportunity(UUIDPrimaryKeyModel, TimeStampedModel):
    # The opportunity's name: derived from its customer and instrument by the services
    # (naming.py), never typed by a user. Older opportunities may keep a typed one.
    title = models.CharField(max_length=TITLE_MAX_LENGTH)
    # The prospect. Fixed for the opportunity's lifetime.
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name="+", db_index=False)
    # The responsible salesperson and the authorization key of every read path. While open
    # it is always the lead's owner (database-enforced, see the module docstring).
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    pipeline = models.ForeignKey(
        Pipeline, on_delete=models.PROTECT, related_name="+", db_index=False
    )
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name="+", db_index=False)
    # The stage's category, copied so the board and the pipeline totals can use partial
    # indexes. Never independently editable: the composite foreign key makes it equal to
    # the stage's category.
    status = models.CharField(max_length=8, choices=StageCategory.choices)

    value = models.DecimalField(max_digits=MONEY_DIGITS, decimal_places=MONEY_PLACES)
    probability = models.DecimalField(
        max_digits=PROBABILITY_DIGITS, decimal_places=PROBABILITY_PLACES
    )
    # True when someone set `probability` by hand instead of taking the stage's default.
    # An override belongs to the stage it was made in: moving the opportunity resets it.
    probability_overridden = models.BooleanField(default=False)
    expected_close_date = models.DateField(null=True, blank=True)
    description = models.TextField(max_length=DESCRIPTION_MAX_LENGTH, blank=True, default="")
    # Why a lost opportunity was lost (optional, free text; only while lost).
    lost_reason = models.CharField(max_length=LOST_REASON_MAX_LENGTH, blank=True, default="")
    closed_at = models.DateTimeField(null=True, blank=True)

    # --- the deal (docs/pipeline.md#opportunities) --------------------------------------------
    # The business date of the opportunity (when it arose), as opposed to the expected
    # closing date; defaults to today in the business time zone.
    opportunity_date = models.DateField(default=business_today)
    # The customer: copied from the lead when not given (an opportunity-specific snapshot the
    # salesperson may change: the lab buying may differ from the lead's organisation).
    account_name = models.CharField(max_length=ACCOUNT_NAME_MAX_LENGTH)
    customer_name = models.CharField(max_length=CUSTOMER_NAME_MAX_LENGTH)
    contact_phone = models.CharField(max_length=PHONE_MAX_LENGTH, blank=True, default="")
    contact_email = models.CharField(max_length=EMAIL_MAX_LENGTH, blank=True, default="")
    address = models.TextField(max_length=ADDRESS_MAX_LENGTH, blank=True, default="")
    # One of instruments.INSTRUMENTS (checked by the services for new and changed values; no
    # database constraint, so opportunities from before the list keep their text).
    instrument_name = models.CharField(
        max_length=INSTRUMENT_NAME_MAX_LENGTH, blank=True, default=""
    )
    work_load = models.CharField(max_length=WORK_LOAD_MAX_LENGTH, blank=True, default="")
    # db_default too: the column keeps its DEFAULT, so the previous release (which never names
    # it) can still insert during a rolling deploy or after a rollback (backend review, P2).
    expected_cpt = models.CharField(
        max_length=EXPECTED_CPT_MAX_LENGTH, blank=True, default="", db_default=""
    )
    # Values of the pipeline's custom fields, {field id: canonical value}; validated against
    # the definitions by validation.clean_custom_values (never arbitrary keys or HTML).
    custom_fields = models.JSONField(default=dict, blank=True)
    # The latest agreed (negotiated) price (INR) and when it was recorded: a copy of the
    # newest NegotiationPrice row (the history is authoritative). Kept after the deal leaves
    # negotiation; entering a negotiation stage again asks for new terms. The agreed CPT has
    # no copy here: it is read from that newest row (selectors.LATEST_AGREED_CPT), so it
    # always goes with the price shown, also beside a price the previous release recorded
    # without one (ADR-0029, review P2).
    negotiated_price = models.DecimalField(
        max_digits=MONEY_DIGITS, decimal_places=MONEY_PLACES, null=True, blank=True
    )
    negotiated_at = models.DateTimeField(null=True, blank=True)

    # Provenance, never changes (e.g. the administrator who created it for a salesperson).
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    # Opportunities are never deleted; archiving hides them from boards, lists and totals.
    archived_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    open_owner_id = models.GeneratedField(
        expression=OPEN_OWNER, output_field=models.UUIDField(null=True), db_persist=True
    )
    expected_close_sort = models.GeneratedField(
        expression=EXPECTED_CLOSE_SORT, output_field=models.DateField(), db_persist=True
    )
    closed_sort = models.GeneratedField(
        expression=CLOSED_SORT, output_field=models.DateTimeField(), db_persist=True
    )

    class Meta:
        db_table = "pipeline_opportunity"
        # Every index serves a measured query (docs/database.md#pipeline_opportunity).
        indexes = [
            # Kanban columns of one owner: open stages by expected close (soonest first,
            # undated last), closed stages by closing time (latest first); also one owner's
            # stage counts and the open-pipeline totals (owner_id prefix).
            models.Index(
                F("owner"),
                F("stage"),
                F("expected_close_sort"),
                F("created_at"),
                F("id"),
                name="pipeline_opp_owner_open_idx",
                condition=Q(status=StageCategory.OPEN, archived_at__isnull=True),
            ),
            models.Index(
                F("owner"),
                F("stage"),
                F("closed_sort").desc(),
                F("id").desc(),
                name="pipeline_opp_owner_closed_idx",
                condition=~Q(status=StageCategory.OPEN) & Q(archived_at__isnull=True),
            ),
            # The same columns organisation-wide (administrators).
            models.Index(
                F("stage"),
                F("expected_close_sort"),
                F("created_at"),
                F("id"),
                name="pipeline_opp_open_idx",
                condition=Q(status=StageCategory.OPEN, archived_at__isnull=True),
            ),
            models.Index(
                F("stage"),
                F("closed_sort").desc(),
                F("id").desc(),
                name="pipeline_opp_closed_idx",
                condition=~Q(status=StageCategory.OPEN) & Q(archived_at__isnull=True),
            ),
            # A lead's opportunities (lead page, reassignment, conversion check) and the
            # ownership foreign key's lookups when a lead's owner changes.
            models.Index(
                F("lead"), F("created_at").desc(), F("id").desc(), name="pipeline_opp_lead_idx"
            ),
            # Lists: one owner's newest/oldest (and the archived view), organisation-wide.
            models.Index(
                F("owner"),
                F("created_at").desc(),
                F("id").desc(),
                name="pipeline_opp_owner_created_idx",
            ),
            # Which pipelines hold one owner's deals (`selectors.visible`, index-only) and one
            # owner's deals in one pipeline (a personal pipeline's board): product enhancement
            # phase, 11.6 -> 1.8 ms and 10.4 -> 1.9 ms for an owner of 20,000 (docs/database.md).
            models.Index(F("owner"), F("pipeline"), name="pipeline_opp_owner_pipe_idx"),
            models.Index(F("created_at").desc(), F("id").desc(), name="pipeline_opp_created_idx"),
            # Global search (Phase 7; the customer names since ADR-0027): a substring of the
            # title or customer, in any workspace. Without it an organisation-wide search read
            # every opportunity (100-150 ms at 300,000) and one owner's walked all of theirs
            # (30 ms at 20,000). Trigrams only: the text is short, so rechecking every owner's
            # candidates costs little, and with the owner first (as the activity indexes)
            # PostgreSQL also chose it for 17 pipeline list and board queries;
            # docs/search.md#database-and-indexes.
            GinIndex(
                OpClass(SEARCH_TEXT, name="gin_trgm_ops"),
                name="pipeline_opp_text_trgm",
                condition=Q(archived_at__isnull=True),
            ),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(title=""), name="pipeline_opp_title_present"),
            models.CheckConstraint(
                condition=Q(status__in=StageCategory.values), name="pipeline_opp_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(value__gte=Decimal("0")), name="pipeline_opp_value_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(probability__gte=IMPOSSIBLE, probability__lte=CERTAIN),
                name="pipeline_opp_probability_range",
            ),
            models.CheckConstraint(
                condition=~Q(status=StageCategory.WON) | Q(probability=CERTAIN),
                name="pipeline_opp_won_is_certain",
            ),
            models.CheckConstraint(
                condition=~Q(status=StageCategory.LOST) | Q(probability=IMPOSSIBLE),
                name="pipeline_opp_lost_is_impossible",
            ),
            models.CheckConstraint(
                condition=Q(status=StageCategory.OPEN, closed_at__isnull=True)
                | (~Q(status=StageCategory.OPEN) & Q(closed_at__isnull=False)),
                name="pipeline_opp_closed_at_matches_status",
            ),
            models.CheckConstraint(
                condition=Q(probability_overridden=False) | Q(status=StageCategory.OPEN),
                name="pipeline_opp_override_only_while_open",
            ),
            models.CheckConstraint(
                condition=Q(lost_reason="") | Q(status=StageCategory.LOST),
                name="pipeline_opp_lost_reason_only_when_lost",
            ),
            models.CheckConstraint(
                condition=Q(expected_close_date__isnull=True)
                | Q(expected_close_date__range=(EARLIEST_CLOSE_DATE, LATEST_CLOSE_DATE)),
                name="pipeline_opp_expected_close_range",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="pipeline_opp_version_positive"
            ),
            models.CheckConstraint(
                condition=Q(opportunity_date__range=(EARLIEST_CLOSE_DATE, LATEST_CLOSE_DATE)),
                name="pipeline_opp_date_range",
            ),
            models.CheckConstraint(
                condition=~Q(account_name=""), name="pipeline_opp_account_present"
            ),
            models.CheckConstraint(
                condition=~Q(customer_name=""), name="pipeline_opp_customer_present"
            ),
            models.CheckConstraint(
                condition=Q(negotiated_price__isnull=True, negotiated_at__isnull=True)
                | Q(negotiated_price__gte=Decimal("0"), negotiated_at__isnull=False),
                name="pipeline_opp_negotiated_complete",
            ),
            models.CheckConstraint(
                condition=Exact(
                    Func(
                        F("custom_fields"), function="jsonb_typeof", output_field=models.TextField()
                    ),
                    "object",
                ),
                name="pipeline_opp_custom_fields_object",
            ),
            # Trivially unique (id is the key); it exists so an activity can reference an
            # opportunity *together with its lead*: an activity's lead is always its
            # opportunity's lead, enforced by a foreign key (docs/activities.md, Phase 4).
            models.UniqueConstraint(fields=["id", "lead"], name="pipeline_opportunity_id_lead_key"),
        ]

    def __str__(self) -> str:
        return f"Opportunity({self.pk})"  # never the title: this string can reach logs

    @property
    def is_open(self) -> bool:
        return self.status == StageCategory.OPEN


class StageHistory(AppendOnlyModel):
    """One stage transition of an opportunity (its creation included: from_stage NULL).
    Insert-only (ORM guard + PostgreSQL trigger). Stage names and categories are copied, so
    the history still reads correctly after a stage is renamed or retired."""

    id = models.BigAutoField(primary_key=True)
    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.PROTECT, related_name="+", db_index=False
    )
    from_stage = models.ForeignKey(
        Stage, on_delete=models.PROTECT, null=True, blank=True, related_name="+", db_index=False
    )
    to_stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name="+", db_index=False)
    from_stage_name = models.CharField(max_length=STAGE_NAME_MAX_LENGTH, blank=True, default="")
    to_stage_name = models.CharField(max_length=STAGE_NAME_MAX_LENGTH)
    from_status = models.CharField(max_length=8, blank=True, default="")
    to_status = models.CharField(max_length=8, choices=StageCategory.choices)
    # The opportunity as it entered the stage (for won/lost value history and analytics).
    value = models.DecimalField(max_digits=MONEY_DIGITS, decimal_places=MONEY_PLACES)
    probability = models.DecimalField(
        max_digits=PROBABILITY_DIGITS, decimal_places=PROBABILITY_PLACES
    )
    lost_reason = models.CharField(max_length=LOST_REASON_MAX_LENGTH, blank=True, default="")
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "pipeline_stage_history"
        indexes = [
            models.Index(
                F("opportunity"),
                F("occurred_at").desc(),
                F("id").desc(),
                name="pipeline_history_opp_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(to_status__in=StageCategory.values),
                name="pipeline_history_to_status_valid",
            ),
            # Creation rows have no "from"; every other row has a complete one.
            models.CheckConstraint(
                condition=Q(from_stage__isnull=True, from_status="", from_stage_name="")
                | (
                    Q(from_stage__isnull=False, from_status__in=StageCategory.values)
                    & ~Q(from_stage_name="")
                ),
                name="pipeline_history_from_complete",
            ),
            models.CheckConstraint(
                condition=Q(lost_reason="") | Q(to_status=StageCategory.LOST),
                name="pipeline_history_lost_reason_only_when_lost",
            ),
        ]

    def __str__(self) -> str:
        return f"StageHistory({self.pk})"


class NegotiationSource(models.TextChoices):
    """How an agreed price (and CPT) was recorded."""

    STAGE_ENTRY = "stage_entry", "Entered a negotiation stage"
    REVISION = "revision", "Revised during negotiation"
    CREATION = "creation", "Created in a negotiation stage"


class NegotiationPrice(AppendOnlyModel):
    """One agreed (negotiated) price of an opportunity and the agreed CPT recorded with it
    (docs/pipeline.md#negotiation). Insert-only (ORM guard + PostgreSQL trigger): new terms
    never overwrite earlier ones, so the history ₹12,00,000 -> ₹11,00,000 -> ₹10,50,000
    stays auditable. Written in the transaction that changes the opportunity, under its
    lock."""

    id = models.BigAutoField(primary_key=True)
    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.PROTECT, related_name="+", db_index=False
    )
    price = models.DecimalField(max_digits=MONEY_DIGITS, decimal_places=MONEY_PLACES)
    currency = models.CharField(max_length=3)
    # Free text as the salesperson stated it (ADR-0029); "" on rows recorded before the agreed
    # CPT was asked for. db_default: the previous release inserts rows without it.
    agreed_cpt = models.CharField(
        max_length=AGREED_CPT_MAX_LENGTH, blank=True, default="", db_default=""
    )
    # The negotiation stage the deal was in (and its name then: stages may be renamed).
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name="+", db_index=False)
    stage_name = models.CharField(max_length=STAGE_NAME_MAX_LENGTH)
    source = models.CharField(max_length=16, choices=NegotiationSource.choices)
    # The opportunity's version after the change that recorded this price.
    opportunity_version = models.PositiveIntegerField()
    # Who recorded it, and whose workspace it concerned when that was someone else (an
    # administrator working in a user's workspace): never recorded as the user's own act.
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    subject_user = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+", db_index=False
    )
    # The administrator's support session, when recorded in one (identity.SupportSession).
    support_session_id = models.UUIDField(null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "pipeline_negotiation_price"
        indexes = [
            models.Index(
                F("opportunity"),
                F("occurred_at").desc(),
                F("id").desc(),
                name="pipeline_negotiation_opp_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(price__gte=Decimal("0")), name="pipeline_negotiation_price_valid"
            ),
            models.CheckConstraint(
                condition=Q(source__in=NegotiationSource.values),
                name="pipeline_negotiation_source_valid",
            ),
            models.CheckConstraint(
                condition=~Q(stage_name="") & Q(currency__regex=r"^[A-Z]{3}$"),
                name="pipeline_negotiation_complete",
            ),
        ]

    def __str__(self) -> str:
        return f"NegotiationPrice({self.pk})"  # never the price: this string can reach logs


class FieldType(models.TextChoices):
    TEXT = "text", "Text"
    LONG_TEXT = "long_text", "Long text"
    NUMBER = "number", "Number"
    CURRENCY = "currency", "Currency (INR)"
    DATE = "date", "Date"
    BOOLEAN = "boolean", "Yes / no"
    SINGLE_SELECT = "single_select", "Single choice"
    MULTI_SELECT = "multi_select", "Multiple choice"


SELECT_TYPES = (FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT)


class CustomField(UUIDPrimaryKeyModel, TimeStampedModel):
    """An additional opportunity field, defined per pipeline (docs/pipeline.md#custom-fields):
    it is configured by whoever may configure the pipeline, and applies to the pipeline's
    opportunities. Values live in Opportunity.custom_fields (JSON), so defining a field never
    changes the database schema. The type never changes after creation (remove the field and
    add another); removed fields are archived and their values kept, hidden."""

    pipeline = models.ForeignKey(
        Pipeline, on_delete=models.PROTECT, related_name="fields", db_index=False
    )
    name = models.CharField(max_length=FIELD_NAME_MAX_LENGTH)
    field_type = models.CharField(max_length=16, choices=FieldType.choices)
    required = models.BooleanField(default=False)
    # Select fields only: [{"id": "<10 hex>", "label": "..."}]. Values store option ids, so
    # renaming an option never rewrites opportunities.
    options = models.JSONField(default=list, blank=True)
    position = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+", db_index=False
    )

    class Meta:
        db_table = "pipeline_custom_field"
        indexes = [
            models.Index(F("pipeline"), F("position"), name="pipeline_field_pipeline_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(name=""), name="pipeline_field_name_present"),
            models.CheckConstraint(
                condition=Q(field_type__in=FieldType.values), name="pipeline_field_type_valid"
            ),
            models.CheckConstraint(
                condition=Exact(
                    Func(F("options"), function="jsonb_typeof", output_field=models.TextField()),
                    "array",
                ),
                name="pipeline_field_options_array",
            ),
            models.UniqueConstraint(
                F("pipeline"),
                Lower("name"),
                condition=Q(is_active=True),
                name="pipeline_field_active_name_unique",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_select(self) -> bool:
        return self.field_type in SELECT_TYPES
