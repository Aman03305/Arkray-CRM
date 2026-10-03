"""Read queries for pipelines and opportunities. Every query over opportunities starts from
`scope.apply()`, and so does every total: nothing is ever summed before the scope is
applied (docs/pipeline.md#authorization). The only unscoped reader is `opportunity_by_id`,
which services use to return a record the actor has just been authorised to change.

Filters and sorts are allowlisted and bounded; every ordering ends in the primary key, so
keyset cursors are exact (core.keyset). The board loads a bounded number of cards per
stage in one query (docs/pipeline.md#the-board).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from uuid import UUID

from django.db import connection, transaction
from django.db.models import Count, Prefetch, QuerySet

from arkray.core.access import AccessScope
from arkray.core.errors import InvalidInputError, NotFoundError
from arkray.core.keyset import KeysetOrdering, KeysetPage, KeysetPaginator, SortKey

from . import metrics
from .models import Opportunity, Pipeline, Stage, StageCategory, StageHistory

INVALID_PIPELINE = "Choose a pipeline from the list."
BOARD_CARDS_DEFAULT, BOARD_CARDS_MAX = 20, 50

ORDERINGS: dict[str, KeysetOrdering] = {
    ordering.name: ordering
    for ordering in (
        KeysetOrdering(
            "-created_at", (SortKey("created_at", descending=True), SortKey("id", descending=True))
        ),
        KeysetOrdering("created_at", (SortKey("created_at"), SortKey("id"))),
        # Amounts are business-sensitive: private keys, never copied into cursors (which
        # end up in URLs and proxy logs); re-read from the boundary row (review).
        KeysetOrdering(
            "-value",
            (SortKey("value", descending=True, private=True), SortKey("id", descending=True)),
        ),
        KeysetOrdering("value", (SortKey("value", private=True), SortKey("id"))),
        # Closing soonest first; opportunities without an expected close date last (the
        # sort key is NOT NULL: "undated" is 9999-12-31), then oldest first.
        KeysetOrdering(
            "expected_close",
            (SortKey("expected_close_sort"), SortKey("created_at"), SortKey("id")),
        ),
        KeysetOrdering(
            "-updated_at", (SortKey("updated_at", descending=True), SortKey("id", descending=True))
        ),
        # Most recently closed first; open opportunities ("never closed") last.
        KeysetOrdering(
            "-closed_at", (SortKey("closed_sort", descending=True), SortKey("id", descending=True))
        ),
    )
}
DEFAULT_ORDERING = "-created_at"
# The board's card order within a stage (docs/pipeline.md#card-order): open stages show
# what closes soonest first; won and lost stages show the latest outcomes first.
BOARD_ORDERING = {
    StageCategory.OPEN: ORDERINGS["expected_close"],
    StageCategory.WON: ORDERINGS["-closed_at"],
    StageCategory.LOST: ORDERINGS["-closed_at"],
}

_PERSON = ("id", "first_name", "last_name", "is_active")
_CARD_FIELDS = (
    "id",
    "title",
    "status",
    "value",
    "probability",
    "probability_overridden",
    "expected_close_date",
    "closed_at",
    "archived_at",
    "version",
    "created_at",
    "updated_at",
    "expected_close_sort",
    "closed_sort",
    "pipeline_id",
    "stage_id",
    "lead__id",
    "lead__first_name",
    "lead__last_name",
    "lead__organization_name",
    "lead__display_name",
    "lead__owner_id",
    *(f"owner__{f}" for f in _PERSON),
)


@dataclass(frozen=True, slots=True)
class OpportunityFilters:
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None
    status: str | None = None
    lead_id: UUID | None = None
    owner_id: UUID | None = None  # organisation-wide workspace only; narrows, never widens
    expected_close_from: date | None = None
    expected_close_to: date | None = None
    probability_min: Decimal | None = None
    probability_max: Decimal | None = None
    archived: bool = False


@dataclass(frozen=True, slots=True)
class PipelineTotals:
    pipeline_value: Decimal
    weighted_pipeline: Decimal
    open_count: int


@dataclass(frozen=True, slots=True)
class BoardColumn:
    stage: Stage
    count: int
    total_value: Decimal
    weighted_value: Decimal
    page: KeysetPage[Opportunity]
    ordering: str


@dataclass(frozen=True, slots=True)
class Board:
    pipeline: Pipeline
    columns: list[BoardColumn]
    totals: PipelineTotals


# --- configuration ---------------------------------------------------------------------------
def pipelines() -> list[Pipeline]:
    """Every pipeline (the default first) with its stages in position order, retired ones
    included (opportunities and history may still reference them). Two queries."""
    stages = Prefetch("stages", queryset=Stage.objects.order_by("position", "id"))
    return list(Pipeline.objects.prefetch_related(stages).order_by("-is_default", "name", "id"))


def pipeline_for_board(pipeline_id: UUID | None) -> Pipeline:
    """The requested pipeline (the default one if None), with its stages. An unknown id is
    a validation error: pipelines are configuration, visible to everyone."""
    queryset = Pipeline.objects.prefetch_related(
        Prefetch("stages", queryset=Stage.objects.order_by("position", "id"))
    )
    pipeline = (
        queryset.filter(pk=pipeline_id).first()
        if pipeline_id is not None
        else queryset.filter(is_default=True).first()
    )
    if pipeline is None:
        raise InvalidInputError(details={"pipeline": [INVALID_PIPELINE]})
    return pipeline


# --- opportunities -----------------------------------------------------------------------------
def _filtered(scope: AccessScope, filters: OpportunityFilters) -> QuerySet[Opportunity]:
    queryset = scope.apply(Opportunity.objects.all())
    queryset = queryset.filter(archived_at__isnull=not filters.archived)
    if filters.owner_id is not None:
        queryset = queryset.filter(owner_id=filters.owner_id)  # narrows the scope only
    if filters.pipeline_id is not None:
        queryset = queryset.filter(pipeline_id=filters.pipeline_id)
    if filters.stage_id is not None:
        queryset = queryset.filter(stage_id=filters.stage_id)
    if filters.status is not None:
        queryset = queryset.filter(status=filters.status)
    if filters.lead_id is not None:
        queryset = queryset.filter(lead_id=filters.lead_id)
    if filters.expected_close_from is not None:
        queryset = queryset.filter(expected_close_date__gte=filters.expected_close_from)
    if filters.expected_close_to is not None:
        queryset = queryset.filter(expected_close_date__lte=filters.expected_close_to)
    if filters.probability_min is not None:
        queryset = queryset.filter(probability__gte=filters.probability_min)
    if filters.probability_max is not None:
        queryset = queryset.filter(probability__lte=filters.probability_max)
    return queryset


def _cards(queryset: QuerySet[Opportunity]) -> QuerySet[Opportunity]:
    """The columns a card or list row needs (lead and owner joined), with the weighted
    value computed by PostgreSQL."""
    return (
        queryset.select_related("lead", "owner")
        .only(*_CARD_FIELDS)
        .annotate(weighted_value=metrics.WEIGHTED_VALUE)
    )


def opportunity_list(scope: AccessScope, filters: OpportunityFilters) -> QuerySet[Opportunity]:
    """The scoped, filtered opportunities of a list page (ordered and paginated by the
    caller). A stage filter also states the stage's category as the status (one primary-key
    lookup): opportunities of a stage always have that status, and saying so lets
    PostgreSQL use the board's partial indexes (0.5 ms instead of a sequential scan)."""
    if filters.stage_id is not None and filters.status is None:
        category = Stage.objects.filter(pk=filters.stage_id).values_list("category", flat=True)
        filters = replace(filters, status=category.first())
    return _cards(_filtered(scope, filters))


def _with_relations(queryset: QuerySet[Opportunity]) -> QuerySet[Opportunity]:
    return (
        queryset.select_related("lead", "owner", "created_by", "pipeline", "stage")
        .only(
            *_CARD_FIELDS,
            "description",
            "lost_reason",
            "pipeline__id",
            "pipeline__key",
            "pipeline__name",
            "stage__id",
            "stage__key",
            "stage__name",
            "stage__position",
            "stage__probability",
            "stage__category",
            "stage__is_active",
            "lead__archived_at",
            *(f"created_by__{f}" for f in _PERSON),
        )
        .annotate(weighted_value=metrics.WEIGHTED_VALUE)
    )


def opportunity_detail(scope: AccessScope, opportunity_id: UUID) -> Opportunity:
    """One opportunity if `scope` may see it; otherwise NotFoundError, exactly as if it
    didn't exist, so a guessed id reveals nothing."""
    found = _with_relations(scope.apply(Opportunity.objects.filter(pk=opportunity_id))).first()
    if found is None:
        raise NotFoundError()
    return found


def opportunity_by_id(opportunity_id: UUID) -> Opportunity:
    """Unscoped: only for services returning a record the actor has just changed."""
    return _with_relations(Opportunity.objects.filter(pk=opportunity_id)).get()


def pipeline_totals(scope: AccessScope, filters: OpportunityFilters) -> PipelineTotals:
    """Pipeline value, weighted pipeline and the number of open opportunities: over the
    open, non-archived opportunities in `scope` that match `filters`. One query."""
    if filters.archived:
        raise ValueError("Archived opportunities never count towards the pipeline.")
    row = metrics.open_pipeline_totals(_filtered(scope, filters))
    return PipelineTotals(row["pipeline_value"], row["weighted_pipeline"], row["open_count"])


def board(
    scope: AccessScope,
    pipeline: Pipeline,
    filters: OpportunityFilters,
    *,
    cards_per_stage: int = BOARD_CARDS_DEFAULT,
) -> Board:
    """See _board. Its queries run in one REPEATABLE READ, read-only transaction, so the
    counts, the totals and the cards describe the same moment: a move committing between
    them can't make the columns and the totals disagree (review). Inside a caller's
    transaction (tests, services) that transaction's snapshot rules apply."""
    if connection.in_atomic_block:
        return _board(scope, pipeline, filters, cards_per_stage=cards_per_stage)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _board(scope, pipeline, filters, cards_per_stage=cards_per_stage)


def _board(
    scope: AccessScope,
    pipeline: Pipeline,
    filters: OpportunityFilters,
    *,
    cards_per_stage: int,
) -> Board:
    """The Kanban board of one pipeline: every stage (retired stages only while they still
    hold opportunities), each with its count, value, weighted value and at most
    `cards_per_stage` cards in the board order, plus the open-pipeline totals.

    Bounded whatever the data: three queries (stage aggregates, totals, cards), the cards
    being one UNION ALL of an index-backed LIMIT per stage, never "all the rows"."""
    if not 0 <= cards_per_stage <= BOARD_CARDS_MAX:
        raise ValueError("cards_per_stage out of range")
    filters = replace(filters, pipeline_id=pipeline.pk)
    base = _filtered(scope, filters)
    stages: list[Stage] = list(pipeline.stages.all())
    per_stage = {
        row["stage_id"]: row
        for row in base.values("stage_id")
        .order_by()
        .annotate(
            count=Count("id"),
            total_value=metrics.value_sum(),
            weighted_value=metrics.weighted_sum(),
        )
    }
    shown = [s for s in stages if s.is_active or s.pk in per_stage]
    totals = pipeline_totals(scope, filters)

    paginators = {
        stage.pk: KeysetPaginator(
            BOARD_ORDERING[StageCategory(stage.category)], page_size=cards_per_stage
        )
        for stage in shown
    }
    # `status` equals the stage's category (database-enforced); stating it lets PostgreSQL
    # use the board's partial indexes (open / closed), which only cover one status each.
    windows = [
        paginators[stage.pk].window(
            _cards(base.filter(stage_id=stage.pk, status=stage.category)), None
        )[0]
        for stage in shown
        if cards_per_stage and per_stage.get(stage.pk, {}).get("count")
    ]
    rows_by_stage: dict[UUID, list[Opportunity]] = {}
    if windows:
        combined = windows[0].union(*windows[1:], all=True) if len(windows) > 1 else windows[0]
        for row in combined:
            rows_by_stage.setdefault(row.stage_id, []).append(row)

    zero = Decimal("0.00")
    columns = []
    for stage in shown:
        aggregate = per_stage.get(stage.pk, {})
        columns.append(
            BoardColumn(
                stage=stage,
                count=aggregate.get("count", 0),
                total_value=aggregate.get("total_value", zero),
                weighted_value=aggregate.get("weighted_value", zero),
                page=paginators[stage.pk].page(rows_by_stage.get(stage.pk, []), None, "next"),
                ordering=paginators[stage.pk].ordering.name,
            )
        )
    return Board(pipeline=pipeline, columns=columns, totals=totals)


# --- history -----------------------------------------------------------------------------------
HISTORY_ORDERING = KeysetOrdering(
    "-occurred_at", (SortKey("occurred_at", descending=True), SortKey("id", descending=True))
)


def stage_history(scope: AccessScope, opportunity_id: UUID) -> QuerySet[StageHistory]:
    """An opportunity's stage history, if `scope` may see the opportunity (NotFoundError
    otherwise). Newest first; paginated by the caller."""
    if not scope.apply(Opportunity.objects.filter(pk=opportunity_id)).exists():
        raise NotFoundError()
    return (
        StageHistory.objects.filter(opportunity_id=opportunity_id)
        .select_related("actor")
        .only(
            "id",
            "opportunity_id",
            "from_stage_id",
            "to_stage_id",
            "from_stage_name",
            "to_stage_name",
            "from_status",
            "to_status",
            "value",
            "probability",
            "lost_reason",
            "occurred_at",
            *(f"actor__{f}" for f in _PERSON),
        )
    )
