"""THE definitions of the pipeline's money figures. Everything that shows one (the board,
opportunity pages, the Phase 5 dashboard, Ask Arkray's tools in Phase 8) uses these
expressions through arkray.pipeline.selectors; nothing re-derives them.

    Weighted value     = value x probability / 100                 (one opportunity)
    Pipeline value     = SUM(value)                                 (OPEN opportunities)
    Weighted pipeline  = SUM(value x probability / 100)             (OPEN opportunities)

Only open, non-archived opportunities count towards the pipeline: won, lost and archived
ones never do. The caller always applies the AccessScope first (selectors do), so a total
can only ever add up opportunities the caller may see.

Exactness: PostgreSQL NUMERIC throughout. `value x probability x 0.01` is a product of
exact decimals (no division, whose result scale PostgreSQL would choose and round at),
summed exactly, then rounded ONCE to paise, half away from zero (PostgreSQL's ROUND on
NUMERIC). So a total is the exact sum rounded, which may differ from the sum of the rounded
per-opportunity figures by at most half a paisa per opportunity (docs/pipeline.md#money).
No float is ever involved: the architecture tests ban FloatField, the API speaks decimal
strings, and the frontend never does arithmetic on amounts.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, QuerySet, Sum, Value
from django.db.models.expressions import CombinedExpression
from django.db.models.functions import Coalesce, Round

from .models import HUNDREDTH, Opportunity, StageCategory

PAISA = HUNDREDTH
_ZERO = Value(Decimal("0.00"), output_field=DecimalField())


def _weighted() -> ExpressionWrapper[CombinedExpression]:
    """value x probability x 0.01, exact (NUMERIC multiplication never rounds)."""
    return ExpressionWrapper(
        F("value") * F("probability") * Value(PAISA, output_field=DecimalField()),
        output_field=DecimalField(),
    )


# One opportunity's weighted value, rounded to paise.
WEIGHTED_VALUE = Round(_weighted(), 2, output_field=DecimalField())

OPEN = Q(status=StageCategory.OPEN)


def value_sum(condition: Q | None = None) -> Coalesce:
    return Coalesce(Sum("value", filter=condition), _ZERO, output_field=DecimalField())


def weighted_sum(condition: Q | None = None) -> Coalesce:
    """The exact sum of weighted values, rounded once."""
    return Coalesce(
        Round(Sum(_weighted(), filter=condition), 2, output_field=DecimalField()),
        _ZERO,
        output_field=DecimalField(),
    )


def open_pipeline_totals(queryset: QuerySet[Opportunity]) -> dict[str, Any]:
    """Pipeline value, weighted pipeline and open count of an already-scoped queryset of
    non-archived opportunities. The OPEN restriction is applied here, as a WHERE clause
    (so the open partial indexes serve it), never left to the caller."""
    return queryset.filter(OPEN).aggregate(
        pipeline_value=value_sum(), weighted_pipeline=weighted_sum(), open_count=Count("id")
    )


def weighted_value(value: Decimal, probability: Decimal) -> Decimal:
    """The same formula in Python, for code that already holds the numbers (tests assert
    it agrees with the SQL). Decimal only: a float is refused rather than rounded."""
    if not isinstance(value, Decimal) or not isinstance(probability, Decimal):
        raise TypeError("Money and probabilities are Decimal, never float.")
    return (value * probability * PAISA).quantize(PAISA, rounding=ROUND_HALF_UP)
