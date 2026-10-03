"""Keyset (cursor) pagination over composite, nullable sort keys.

DRF's CursorPagination positions its cursor on the *first* ordering column only and skips
ties with an OFFSET. That breaks down for the sorts CRM lists need: many leads share a
name (ties grow, and concurrent inserts into a tie group shift the offset) and "last
contacted" is NULL for leads nobody has contacted (DRF cannot encode a NULL position).

Here the cursor carries the full sort key of the boundary row, and the next page is
"every row strictly after that key" in the ordering, expanded into plain predicates:

    (a, b, id) after (va, vb, vid)  ==  a > va
                                     OR a = va AND b > vb
                                     OR a = va AND b = vb AND id > vid

with explicit NULL placement per key, so pages never overlap and never skip rows, and a
row inserted meanwhile appears exactly once, in its sorted place. The last key must be
unique and non-null (the primary key), which makes the order total and the cursor exact.

Cursors are signed (django.core.signing), bound to the ordering they were issued for, and
carry sort values only: a forged, stale or cross-ordering cursor is a clean 400, never a
500, and never widens what the caller may see (the queryset is scoped before paginating).

Sort keys holding personal data (a lead's or a user's name) are `private`: their value is
**not** written into the cursor, which ends up in URLs and possibly proxy logs. The cursor
holds the boundary row's primary key instead, and its private values are re-read from that
row when the next page is requested (one indexed lookup). Signing means only ids the server
issued can be used, and the values only position the page; they are never returned. If the
boundary row was renamed meanwhile, the page continues from its new name. This also keeps
cursors short whatever the script of the name (Phase 2 review: a 200-character Devanagari
name produced a cursor longer than the server accepts).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

from django.core import signing
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.db.models import F, Q, QuerySet
from django.db.models.expressions import OrderBy
from rest_framework.request import Request
from rest_framework.utils.urls import replace_query_param

from .errors import InvalidInputError

M = TypeVar("M", bound=models.Model)

_SALT = "arkray.core.keyset"
MAX_CURSOR_LENGTH = 1000
INVALID_CURSOR = "This page link is invalid or has expired. Start from the first page."


@dataclass(frozen=True, slots=True)
class SortKey:
    """One column of an ordering. `nulls_first` matters only for `nullable` columns; for
    NOT NULL columns PostgreSQL's default placement is kept, so plain indexes match."""

    field: str
    descending: bool = False
    nullable: bool = False
    nulls_first: bool = False
    private: bool = False  # never written into the cursor (see the module docstring)

    def reversed(self) -> SortKey:
        return SortKey(
            self.field, not self.descending, self.nullable, not self.nulls_first, self.private
        )

    def order_by(self) -> OrderBy:
        expression = F(self.field)
        if not self.nullable:
            return expression.desc() if self.descending else expression.asc()
        placement = {"nulls_first": True} if self.nulls_first else {"nulls_last": True}
        return expression.desc(**placement) if self.descending else expression.asc(**placement)

    def beyond(self, value: Any) -> Q | None:
        """Rows whose value for this key sorts strictly after `value` (None: no such rows)."""
        if value is None:
            return Q(**{f"{self.field}__isnull": False}) if self.nulls_first else None
        after = Q(**{f"{self.field}__{'lt' if self.descending else 'gt'}": value})
        if self.nullable and not self.nulls_first:
            after |= Q(**{f"{self.field}__isnull": True})
        return after

    def equal(self, value: Any) -> Q:
        if value is None:
            return Q(**{f"{self.field}__isnull": True})
        return Q(**{self.field: value})

    def bound(self, value: Any) -> Q | None:
        """A redundant range predicate on the leading key (inclusive), so PostgreSQL can
        start an index scan at the cursor instead of filtering from the beginning."""
        if self.nullable or value is None:
            return None
        return Q(**{f"{self.field}__{'lte' if self.descending else 'gte'}": value})


@dataclass(frozen=True, slots=True)
class KeysetOrdering:
    name: str
    keys: tuple[SortKey, ...]

    def __post_init__(self) -> None:
        if not self.keys or self.keys[-1].nullable or self.keys[-1].private:
            raise ValueError("A keyset ordering must end in a unique, non-null key.")

    def reversed(self) -> tuple[SortKey, ...]:
        return tuple(key.reversed() for key in self.keys)


@dataclass(frozen=True, slots=True)
class KeysetPage[T]:
    items: list[T]
    next_cursor: str | None
    previous_cursor: str | None


def _after(keys: Sequence[SortKey], values: Sequence[Any]) -> Q:
    """Rows strictly after `values` in the ordering `keys` (lexicographic, NULL-aware)."""
    alternatives: list[Q] = []
    prefix = Q()
    for key, value in zip(keys, values, strict=True):
        beyond = key.beyond(value)
        if beyond is not None:
            alternatives.append(prefix & beyond)
        prefix &= key.equal(value)
    if not alternatives:
        return Q(pk__in=[])  # nothing sorts after the very last possible key
    combined = alternatives[0]
    for alternative in alternatives[1:]:
        combined |= alternative
    bound = keys[0].bound(values[0])
    return combined & bound if bound is not None else combined


def _encode(value: Any) -> Any:
    """A sort value as JSON: exact text for datetimes, dates, decimals (money is never
    turned into a float) and UUIDs; decoded again by the model field's to_python()."""
    if isinstance(value, date):  # datetimes included
        return value.isoformat()
    if isinstance(value, Decimal | UUID):
        return str(value)
    return value


def _decode_field(model: type[models.Model], name: str) -> models.Field[Any, Any]:
    field = model._meta.get_field(name)
    if isinstance(field, models.GeneratedField) and field.output_field is not None:
        return field.output_field  # the stored type
    if not isinstance(field, models.Field):
        raise FieldDoesNotExist(name)
    return field


class KeysetPaginator:
    def __init__(self, ordering: KeysetOrdering, *, page_size: int) -> None:
        self.ordering = ordering
        self.page_size = page_size

    def _cursor(self, row: models.Model, direction: str) -> str:
        values = [
            None if key.private else _encode(getattr(row, key.field)) for key in self.ordering.keys
        ]
        return signing.dumps({"o": self.ordering.name, "d": direction, "v": values}, salt=_SALT)

    def _private_values(self, model: type[models.Model], boundary: Any) -> dict[str, Any]:
        """The boundary row's current values for the private keys (re-read by its id)."""
        fields = [key.field for key in self.ordering.keys if key.private]
        row = (
            model._default_manager.filter(**{self.ordering.keys[-1].field: boundary})
            .values(*fields)
            .first()
        )
        if row is None:
            raise ValueError("The page boundary no longer exists.")
        return row

    def _decode(self, model: type[models.Model], cursor: str) -> tuple[str, list[Any]]:
        try:
            if len(cursor) > MAX_CURSOR_LENGTH:
                raise ValueError
            payload = signing.loads(cursor, salt=_SALT)
            if (
                not isinstance(payload, dict)
                or payload.get("o") != self.ordering.name
                or payload.get("d") not in ("next", "prev")
                or not isinstance(payload.get("v"), list)
                or len(payload["v"]) != len(self.ordering.keys)
            ):
                raise ValueError
            values: list[Any] = []
            for key, raw in zip(self.ordering.keys, payload["v"], strict=True):
                if key.private:
                    if raw is not None:
                        raise ValueError
                    values.append(None)  # filled in below from the boundary row
                    continue
                if raw is None:
                    if not key.nullable:
                        raise ValueError
                    values.append(None)
                    continue
                value = _decode_field(model, key.field).to_python(raw)
                if isinstance(value, datetime) and value.tzinfo is None:
                    raise ValueError
                values.append(value)
            if any(key.private for key in self.ordering.keys):
                current = self._private_values(model, values[-1])
                values = [
                    current[key.field] if key.private else value
                    for key, value in zip(self.ordering.keys, values, strict=True)
                ]
        except (signing.BadSignature, ValidationError, ValueError, TypeError, FieldDoesNotExist):
            raise InvalidInputError(INVALID_CURSOR, details={"cursor": [INVALID_CURSOR]}) from None
        return payload["d"], values

    def window(self, queryset: QuerySet[M], cursor: str | None) -> tuple[QuerySet[M], str]:
        """The one query a page runs (page_size + 1 rows, to see whether more follow) and
        its direction. Exposed so tests can EXPLAIN exactly what production executes."""
        if not cursor:
            keys, direction = self.ordering.keys, "next"
            filtered = queryset
        else:
            direction, values = self._decode(queryset.model, cursor)
            keys = self.ordering.keys if direction == "next" else self.ordering.reversed()
            filtered = queryset.filter(_after(keys, values))
        ordered = filtered.order_by(*(k.order_by() for k in keys))
        return ordered[: self.page_size + 1], direction

    def paginate(self, queryset: QuerySet[M], cursor: str | None) -> KeysetPage[M]:
        window, direction = self.window(queryset, cursor)
        return self.page(list(window), cursor, direction)

    def page(self, rows: list[M], cursor: str | None, direction: str) -> KeysetPage[M]:
        """The page (and its cursors) from the rows a `window` query returned. Separate
        from `paginate` for callers that fetch several windows in one query (the pipeline
        board: one UNION ALL of a window per stage)."""
        has_more = len(rows) > self.page_size
        items = rows[: self.page_size]
        if direction == "prev":
            items.reverse()
        if not items:
            return KeysetPage(items, None, None)
        next_cursor: str | None
        previous_cursor: str | None
        if direction == "next":
            next_cursor = self._cursor(items[-1], "next") if has_more else None
            previous_cursor = self._cursor(items[0], "prev") if cursor else None
        else:
            next_cursor = self._cursor(items[-1], "next")
            previous_cursor = self._cursor(items[0], "prev") if has_more else None
        return KeysetPage(items, next_cursor, previous_cursor)


def page_links(request: Request, page: KeysetPage[Any]) -> dict[str, str | None]:
    """`next` / `previous` as absolute URLs of this same request with only the cursor changed
    (the shape DRF's paginators return, so clients treat every list alike)."""
    url = request.build_absolute_uri()

    def link(cursor: str | None) -> str | None:
        return replace_query_param(url, "cursor", cursor) if cursor else None

    return {"next": link(page.next_cursor), "previous": link(page.previous_cursor)}
