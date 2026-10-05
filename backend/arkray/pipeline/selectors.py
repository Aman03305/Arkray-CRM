"""Read queries for pipelines and opportunities. Every query over opportunities starts from
`scope.apply()`, and so does every total: nothing is ever summed before the scope is
applied (docs/pipeline.md#authorization). The only unscoped reader is `opportunity_by_id`,
which services use to return a record the actor has just been authorised to change.

Filters and sorts are allowlisted and bounded; every ordering ends in the primary key, so
keyset cursors are exact (core.keyset). The board loads a bounded number of cards per
stage in one query (docs/pipeline.md#the-board).
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import connection, transaction
from django.db.models import Count, F, Prefetch, Q, QuerySet, Subquery

from arkray.core.access import AccessScope
from arkray.core.errors import InvalidInputError, NotFoundError
from arkray.core.keyset import (
    CursorBinding,
    KeysetOrdering,
    KeysetPage,
    KeysetPaginator,
    SortKey,
)
from arkray.core.knowledge import KnowledgeDocument, SourceType, compose
from arkray.core.ranking import Matches, SearchQuery, top_matches

from . import metrics
from .models import (
    SEARCH_TEXT,
    CustomField,
    NegotiationPrice,
    Opportunity,
    Pipeline,
    Stage,
    StageCategory,
    StageHistory,
)

INVALID_PIPELINE = "Choose a pipeline from the list."
# The organisation-wide pipeline list (administrators) is bounded whatever the number of
# users: shared pipelines first, then personal ones by owner (docs/pipeline.md).
MAX_LISTED_PIPELINES = 500
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
    "account_name",
    "customer_name",
    "instrument_name",
    "negotiated_price",
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
def _with_stages(queryset: QuerySet[Pipeline]) -> QuerySet[Pipeline]:
    """Stages in position order (retired ones included: opportunities and history may still
    reference them): one query for any number of pipelines."""
    return queryset.select_related("owner").prefetch_related(
        Prefetch("stages", queryset=Stage.objects.order_by("position", "id"))
    )


def _with_configuration(queryset: QuerySet[Pipeline]) -> QuerySet[Pipeline]:
    """Stages and the active custom fields, in order: two queries for any number."""
    return _with_stages(queryset).prefetch_related(
        Prefetch(
            "fields", queryset=CustomField.objects.filter(is_active=True).order_by("position", "id")
        ),
    )


def pipelines() -> list[Pipeline]:
    """The organisation's shared pipelines (the default first) with their stages: what
    every signed-in user may use (`/config/pipelines`, kept for compatibility). Personal
    pipelines are listed per workspace (visible_pipelines)."""
    return list(
        _with_configuration(Pipeline.objects.filter(owner__isnull=True)).order_by(
            "-is_default", "name", "id"
        )
    )


def visible(scope: AccessScope) -> Q:
    """The pipelines `scope` may see: the organisation's shared pipelines, the personal
    pipelines of the workspace's owner, and any pipeline holding one of the workspace's
    opportunities (a deal follows its lead when the lead is reassigned, and must never
    become invisible because it sits in its previous owner's pipeline). Organisation-wide:
    every pipeline."""
    if scope.is_organization_wide:
        return Q()
    holding = scope.apply(Opportunity.objects.all()).values("pipeline_id")
    return (
        Q(owner__isnull=True)
        | Q(owner_id__in=scope.owner_ids)
        | Q(pk__in=Subquery(holding.order_by().distinct()))
    )


def visible_pipelines(scope: AccessScope, *, archived: bool = False) -> list[Pipeline]:
    """The pipelines `scope` may see (active ones, or only archived ones), with their
    configuration: shared first (the default first), then personal ones by owner. Bounded
    (MAX_LISTED_PIPELINES)."""
    queryset = Pipeline.objects.filter(visible(scope), is_active=not archived)
    return list(
        _with_configuration(queryset).order_by(
            "-is_default",
            F("owner_id").asc(nulls_first=True),
            "name",
            "id",
        )[:MAX_LISTED_PIPELINES]
    )


def visible_pipeline(scope: AccessScope, pipeline_id: UUID) -> Pipeline:
    """One pipeline `scope` may see, with its configuration (NotFoundError otherwise)."""
    found = _with_configuration(Pipeline.objects.filter(visible(scope), pk=pipeline_id)).first()
    if found is None:
        raise NotFoundError()
    return found


def default_pipeline_id() -> UUID | None:
    return Pipeline.objects.filter(is_default=True).values_list("pk", flat=True).first()


def pipeline_for_board(scope: AccessScope, pipeline_id: UUID | None) -> Pipeline:
    """The requested pipeline (the organisation's default one if None), with its stages, if
    `scope` may see it. An unknown or invisible id is a validation error, the same for both
    (a guessed id reveals nothing)."""
    which = Q(is_default=True) if pipeline_id is None else Q(pk=pipeline_id)
    found = _with_stages(Pipeline.objects.filter(visible(scope), which)).first()
    if found is None:
        raise InvalidInputError(details={"pipeline": [INVALID_PIPELINE]})
    return found


def active_fields(pipeline_id: UUID) -> list[CustomField]:
    """A pipeline's active custom fields in order (configuration: the caller has already
    decided the pipeline may be used)."""
    return list(
        CustomField.objects.filter(pipeline_id=pipeline_id, is_active=True).order_by(
            "position", "id"
        )
    )


# --- opportunities -----------------------------------------------------------------------------
def listable(scope: AccessScope) -> QuerySet[Opportunity]:
    """Every opportunity the scope may list, whatever the filters: where a page cursor's
    boundary opportunity is re-read (core.keyset), so a cursor can never measure an amount
    outside the scope."""
    return scope.apply(Opportunity.objects.all())


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


# What a global search result shows of an opportunity: title, status, stage, the lead (only
# if visible in the scope; the serializer decides) and the owner. No amounts.
_SEARCH_RESULT_FIELDS = (
    "id",
    "title",
    "status",
    "created_at",
    "account_name",
    "customer_name",
    "stage__id",
    "stage__name",
    "lead__id",
    "lead__first_name",
    "lead__last_name",
    "lead__organization_name",
    "lead__display_name",
    "lead__owner_id",
    *(f"owner__{f}" for f in _PERSON),
)


def search(scope: AccessScope, query: SearchQuery, *, limit: int) -> Matches[Opportunity]:
    """Global search's opportunities: every search word occurs in the title or the customer
    snapshot (models.SEARCH_TEXT, case-insensitive substring), over the opportunities `scope`
    may list, archived ones left out; open, won and lost alike (closed deals are history
    people look for). Ranked by core.ranking against the title, newest first among equals."""
    scoped = scope.apply(Opportunity.objects.filter(archived_at__isnull=True)).annotate(
        search_text=SEARCH_TEXT
    )
    return top_matches(
        scoped,
        text="search_text",
        query=query,
        label="title",
        newest=("-created_at", "-id"),
        limit=limit,
        shape=lambda found: found.select_related("lead", "owner", "stage").only(
            *_SEARCH_RESULT_FIELDS
        ),
    )


def _with_relations(queryset: QuerySet[Opportunity]) -> QuerySet[Opportunity]:
    return (
        queryset.select_related("lead", "owner", "created_by", "pipeline", "stage")
        .only(
            *_CARD_FIELDS,
            "description",
            "lost_reason",
            "opportunity_date",
            "contact_phone",
            "contact_email",
            "address",
            "work_load",
            "expected_cpt",
            "custom_fields",
            "negotiated_at",
            "pipeline__id",
            "pipeline__key",
            "pipeline__name",
            "pipeline__owner_id",
            "pipeline__is_active",
            "stage__id",
            "stage__key",
            "stage__name",
            "stage__position",
            "stage__probability",
            "stage__category",
            "stage__is_negotiation",
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


def hold_opportunity_key(opportunity_id: UUID) -> None:
    """FOR KEY SHARE on an opportunity until the transaction ends: as
    `leads.selectors.hold_lead_key`, for rows that reference it (the Ask Arkray index)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pipeline_opportunity WHERE id = %s FOR KEY SHARE", [opportunity_id]
        )


def opportunity_is_archived(opportunity_id: UUID) -> bool:
    """For a module holding the opportunity's lead lock (archive state changes only under
    it): whether an activity's opportunity is archived. Reveals nothing else."""
    return Opportunity.objects.filter(pk=opportunity_id, archived_at__isnull=False).exists()


def opportunity_ref(scope: AccessScope, opportunity_id: UUID) -> Opportunity:
    """The identity and state of an opportunity `scope` may see (id, lead, owner, status,
    archive state), for modules whose records hang off opportunities (Phase 4: an activity
    linked to it). NotFoundError outside the scope, exactly as if it didn't exist. Not a
    display read: no title, amounts or text."""
    found = (
        scope.apply(Opportunity.objects.filter(pk=opportunity_id))
        .only("id", "lead_id", "owner_id", "status", "archived_at")
        .first()
    )
    if found is None:
        raise NotFoundError()
    return found


def first_opportunities(scope: AccessScope, lead_ids: Collection[UUID]) -> dict[UUID, Opportunity]:
    """Each given lead's first non-archived opportunity that `scope` may see (the one an
    opportunity-created lead was made for), by lead id: its id, title and instrument only.
    For a short list of leads (the dashboard's new leads); one query over the lead index."""
    if not lead_ids:
        return {}
    rows = (
        scope.apply(
            Opportunity.objects.filter(lead_id__in=list(lead_ids), archived_at__isnull=True)
        )
        .order_by("lead_id", "created_at", "id")
        .distinct("lead_id")
        .only("id", "lead_id", "title", "instrument_name")
    )
    return {row.lead_id: row for row in rows}


def stages_by_id(stage_ids: list[UUID]) -> dict[UUID, Stage]:
    """Stages (configuration, the same for everyone) by id, in one query: e.g. the names a
    timeline entry records as they are at the moment of a transition."""
    return {stage.pk: stage for stage in Stage.objects.filter(pk__in=stage_ids)}


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
    binding_for: Callable[[UUID, str], CursorBinding] | None,
) -> Board:
    """See _board. Its queries run in one REPEATABLE READ, read-only transaction, so the
    counts, the totals and the cards describe the same moment: a move committing between
    them can't make the columns and the totals disagree (review). Inside a caller's
    transaction (tests, services) that transaction's snapshot rules apply."""
    if connection.in_atomic_block:
        return _board(
            scope, pipeline, filters, cards_per_stage=cards_per_stage, binding_for=binding_for
        )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _board(
            scope, pipeline, filters, cards_per_stage=cards_per_stage, binding_for=binding_for
        )


def _stage_aggregates(base: QuerySet[Opportunity]) -> dict[UUID, dict[str, Any]]:
    """Count, value and weighted value per stage of an already-scoped, filtered queryset
    (one grouped query, metrics' definitions)."""
    return {
        row["stage_id"]: row
        for row in base.values("stage_id")
        .order_by()
        .annotate(
            count=Count("id"),
            total_value=metrics.value_sum(),
            weighted_value=metrics.weighted_sum(),
        )
    }


@dataclass(frozen=True, slots=True)
class StageTotal:
    stage: Stage
    count: int
    total_value: Decimal
    weighted_value: Decimal


def stage_breakdown(scope: AccessScope, pipeline: Pipeline) -> list[StageTotal]:
    """Each stage of `pipeline` with the number, value and weighted value of the
    non-archived opportunities in it that `scope` may see: exactly the board's column
    figures, without the cards (Ask Arkray's "deals by stage"). Retired stages only while
    they still hold opportunities. One query (plus the pipeline's prefetched stages)."""
    per_stage = _stage_aggregates(_filtered(scope, OpportunityFilters(pipeline_id=pipeline.pk)))
    zero = Decimal("0.00")
    return [
        StageTotal(
            stage=stage,
            count=per_stage.get(stage.pk, {}).get("count", 0),
            total_value=per_stage.get(stage.pk, {}).get("total_value", zero),
            weighted_value=per_stage.get(stage.pk, {}).get("weighted_value", zero),
        )
        for stage in pipeline.stages.all()
        if stage.is_active or stage.pk in per_stage
    ]


@dataclass(frozen=True, slots=True)
class OwnerPipeline:
    owner_id: UUID
    open_count: int
    pipeline_value: Decimal
    weighted_pipeline: Decimal


def open_pipeline_by_owner(scope: AccessScope, *, limit: int) -> list[OwnerPipeline]:
    """Pipeline value and weighted pipeline per owner over the open, non-archived
    opportunities in `scope` (organisation-wide questions such as "whose pipeline is the
    largest"), largest pipeline value first, at most `limit` owners. One grouped query
    with metrics' definitions."""
    if not 1 <= limit <= 50:
        raise ValueError("limit out of range")
    rows = (
        _filtered(scope, OpportunityFilters())
        .filter(metrics.OPEN)
        .values("owner_id")
        .order_by()
        .annotate(
            open_count=Count("id"),
            pipeline_value=metrics.value_sum(),
            weighted_pipeline=metrics.weighted_sum(),
        )
        .order_by("-pipeline_value", "owner_id")[:limit]
    )
    return [OwnerPipeline(**row) for row in rows]


def _board(
    scope: AccessScope,
    pipeline: Pipeline,
    filters: OpportunityFilters,
    *,
    cards_per_stage: int,
    binding_for: Callable[[UUID, str], CursorBinding] | None,
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
    per_stage = _stage_aggregates(base)
    shown = [s for s in stages if s.is_active or s.pk in per_stage]
    totals = pipeline_totals(scope, filters)

    def paginator(stage: Stage) -> KeysetPaginator:
        # A column's cursor continues in the opportunities list: `binding_for` binds it
        # (stage id, ordering name) exactly as that list will check it.
        ordering = BOARD_ORDERING[StageCategory(stage.category)]
        binding = None if binding_for is None else binding_for(stage.pk, ordering.name)
        return KeysetPaginator(ordering, page_size=cards_per_stage, binding=binding)

    paginators = {stage.pk: paginator(stage) for stage in shown}
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


# --- negotiated prices -------------------------------------------------------------------------
NEGOTIATION_ORDERING = KeysetOrdering(
    "-occurred_at", (SortKey("occurred_at", descending=True), SortKey("id", descending=True))
)


def negotiation_history(scope: AccessScope, opportunity_id: UUID) -> QuerySet[NegotiationPrice]:
    """An opportunity's negotiated prices, newest first, if `scope` may see the opportunity
    (NotFoundError otherwise). Paginated by the caller."""
    if not scope.apply(Opportunity.objects.filter(pk=opportunity_id)).exists():
        raise NotFoundError()
    return (
        NegotiationPrice.objects.filter(opportunity_id=opportunity_id)
        .select_related("actor")
        .only(
            "id",
            "opportunity_id",
            "price",
            "currency",
            "stage_id",
            "stage_name",
            "source",
            "occurred_at",
            *(f"actor__{f}" for f in _PERSON),
        )
    )


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


# --- Ask Arkray's semantic index (Phase 8, docs/rag-architecture.md) -------------------------
# An opportunity is embedded only when someone wrote about it (a description or a lost
# reason); its title alone is found by global search. Never amounts: figures come from the
# deterministic tools (metrics), not from text similarity.
_KNOWLEDGE_FIELDS = (
    "id",
    "owner_id",
    "lead_id",
    "title",
    "description",
    "lost_reason",
    "created_at",
    "updated_at",
)
_HAS_KNOWLEDGE = Q(archived_at__isnull=True) & (~Q(description="") | ~Q(lost_reason=""))


def _knowledge(queryset: QuerySet[Opportunity]) -> list[KnowledgeDocument]:
    return [
        KnowledgeDocument(
            source_type=SourceType.OPPORTUNITY,
            source_id=found.pk,
            owner_id=found.owner_id,
            lead_id=found.lead_id,
            opportunity_id=found.pk,
            label=found.title,
            text=compose(
                ("Opportunity", found.title),
                ("Description", found.description),
                ("Lost reason", found.lost_reason),
            ),
            occurred_at=found.created_at,
            updated_at=found.updated_at,
        )
        for found in queryset.filter(_HAS_KNOWLEDGE).only(*_KNOWLEDGE_FIELDS)
    ]


def knowledge_documents(
    scope: AccessScope, opportunity_ids: Collection[UUID]
) -> list[KnowledgeDocument]:
    """The embeddable text of the given opportunities that `scope` may see now (archived
    ones have none). Retrieval re-reads every hit through this."""
    if not opportunity_ids:
        return []
    return _knowledge(scope.apply(Opportunity.objects.filter(pk__in=list(opportunity_ids))))


def knowledge_documents_for_indexing(
    opportunity_ids: Collection[UUID],
) -> list[KnowledgeDocument]:
    """Unscoped: only for the indexer (runs as the system)."""
    if not opportunity_ids:
        return []
    return _knowledge(Opportunity.objects.filter(pk__in=list(opportunity_ids)))


def knowledge_source_ids(*, after: UUID | None, limit: int) -> list[UUID]:
    """Ids of opportunities with a knowledge document, in id order (indexer only)."""
    queryset = Opportunity.objects.filter(_HAS_KNOWLEDGE)
    if after is not None:
        queryset = queryset.filter(pk__gt=after)
    return list(queryset.order_by("pk").values_list("pk", flat=True)[:limit])
