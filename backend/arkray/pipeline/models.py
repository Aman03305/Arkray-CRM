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

from django.db import models
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Coalesce, Lower
from django.utils import timezone

from arkray.core.models import AppendOnlyModel, TimeStampedModel, UUIDPrimaryKeyModel
from arkray.identity.models import User
from arkray.leads.models import Lead

KEY_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
PIPELINE_NAME_MAX_LENGTH = 100
STAGE_NAME_MAX_LENGTH = 50
TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 5000
LOST_REASON_MAX_LENGTH = 500

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
    """A sales process: an ordered set of stages. v1 ships one ("Sales Pipeline"), but
    nothing assumes there is only one."""

    key = models.CharField(max_length=32, unique=True)  # immutable identifier
    name = models.CharField(max_length=PIPELINE_NAME_MAX_LENGTH, unique=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "pipeline_pipeline"
        constraints = [
            models.CheckConstraint(
                condition=Q(key__regex=KEY_PATTERN), name="pipeline_pipeline_key_format"
            ),
            models.CheckConstraint(condition=~Q(name=""), name="pipeline_pipeline_name_present"),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(is_active=True),
                name="pipeline_pipeline_default_is_active",
            ),
            models.UniqueConstraint(
                fields=["is_default"],
                condition=Q(is_default=True),
                name="pipeline_pipeline_one_default",
            ),
        ]

    def __str__(self) -> str:
        return self.name


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


# NULL unless the opportunity is open: the foreign key onto the lead's (id, owner) then
# only binds open opportunities (MATCH SIMPLE skips rows with a NULL column).
OPEN_OWNER = Case(When(status=StageCategory.OPEN, then=F("owner_id")), default=None)
EXPECTED_CLOSE_SORT = Coalesce(F("expected_close_date"), Value(UNDATED))
CLOSED_SORT = Coalesce(F("closed_at"), Value(NEVER_CLOSED))


class Opportunity(UUIDPrimaryKeyModel, TimeStampedModel):
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
            models.Index(F("created_at").desc(), F("id").desc(), name="pipeline_opp_created_idx"),
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
