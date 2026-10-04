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
When every key is NOT NULL and they all sort one way, the same condition is also written
as one row comparison, `ROW(a, b, id) > ROW(va, vb, vid)`, which PostgreSQL can start an
index scan at exactly (Phase 10, R49: a page deep inside a large group of equal leading
values otherwise re-read the group from its start); other orderings bound the scan by
their leading key.

Cursors are signed (django.core.signing), bound to the ordering they were issued for, and
carry sort values only: a forged, stale or cross-ordering cursor is a clean 400, never a
500, and never widens what the caller may see (the queryset is scoped before paginating).

Since Phase 9 a cursor is also bound to what it continues (`CursorBinding`): the list (its
purpose, including the record whose timeline or history it pages), the signed-in user, the
workspace and the filters. It carries an HMAC of these, never the values themselves, so a
cursor replayed by another user, in another workspace, on another list or with other
filters is refused like a forged one. It also expires (`KEYSET_CURSOR_MAX_AGE_S`, the
longest a session can last). Neither ever widened access (see below); they remove a
cursor's use as a portable token and bound how long a leaked URL stays meaningful.

Sort keys holding personal or business-sensitive data (a lead's or a user's name, a deal's
amount) are `private`: their value is **not** written into the cursor, which ends up in URLs
and possibly proxy logs. The cursor holds the boundary row's primary key instead, and its
private values are re-read from that row when the next page is requested (one indexed
lookup). If the boundary row was renamed meanwhile, the page continues from its new name.
This also keeps cursors short whatever the script of the name (Phase 2 review: a
200-character Devanagari name produced a cursor longer than the server accepts).

The re-read goes **through what the caller may list** (`visible`, by default the very
queryset being paged), never the whole table. The values only position the page, but a
position is still a measurement: replaying someone else's cursor (or one harvested before
the row moved to another workspace) while moving one's own record across the boundary
would otherwise binary-search a hidden amount or name (Phase 6 review, P1). A boundary row
the caller can't see makes the cursor invalid (400).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any, TypeVar
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.db.models import F, Func, Q, QuerySet, Value
from django.db.models.expressions import OrderBy
from django.db.models.lookups import GreaterThan, LessThan
from django.utils.crypto import salted_hmac
from rest_framework.request import Request
from rest_framework.utils.urls import replace_query_param

from .access import AccessScope
from .errors import InvalidInputError

M = TypeVar("M", bound=models.Model)

_CIPHER_SALT = "arkray.core.keyset.cipher"


def _fernet(key: str) -> Fernet:
    digest = salted_hmac(_CIPHER_SALT, "cursor", secret=key, algorithm="sha256").digest()
    return Fernet(base64.urlsafe_b64encode(digest))


@lru_cache(maxsize=4)
def _cipher(keys: tuple[str, ...]) -> MultiFernet:
    """Authenticated encryption for cursors, keyed from SECRET_KEY and then its fallbacks
    (rotation keeps open page links working; new cursors use the current key)."""
    return MultiFernet([_fernet(key) for key in keys])


def _keys() -> tuple[str, ...]:
    return (settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS)


def _seal(payload: dict[str, Any]) -> str:
    """A cursor: the payload encrypted and authenticated (Fernet), never just signed. Its
    sort values and row ids are unreadable (R48: a sequential id read from one's own cursor
    measured how many events were written organisation-wide)."""
    token = _cipher(_keys()).encrypt(json.dumps(payload, separators=(",", ":")).encode())
    return token.decode("ascii")


def _open(cursor: str) -> Any:
    """The payload of a cursor this server sealed, if it is younger than
    KEYSET_CURSOR_MAX_AGE_S (InvalidToken otherwise)."""
    data = _cipher(_keys()).decrypt(cursor.encode("ascii"), ttl=settings.KEYSET_CURSOR_MAX_AGE_S)
    return json.loads(data)


MAX_CURSOR_LENGTH = 1000
INVALID_CURSOR = "This page link is invalid or has expired. Start from the first page."
_BINDING_SALT = "arkray.core.keyset.binding"
# Not part of a binding: the cursor itself, and the page size (a client may change it
# between pages; it never changes which rows come next).
_UNBOUND_PARAMS = frozenset({"cursor", "page_size"})


def _canonical(value: Any) -> Any:
    if isinstance(value, set | frozenset):
        return sorted(str(item) for item in value)
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, bool | int) or value is None:
        return value
    return str(value)  # dates, UUIDs, decimals, choices: their exact text


@dataclass(frozen=True, slots=True)
class CursorBinding:
    """What a cursor may continue: `purpose` (the list, and the record whose timeline or
    history it pages), the signed-in user, the workspace (by scope, so `me` and one's own id
    are the same workspace) and the validated filters and ordering."""

    purpose: str
    actor_id: UUID
    workspace: str
    shape: str

    @classmethod
    def of(
        cls,
        purpose: str,
        params: Mapping[str, Any],
        *,
        actor_id: UUID,
        scope: AccessScope | None = None,
    ) -> CursorBinding:
        workspace = (
            "-"
            if scope is None
            else f"{scope.kind}:{','.join(sorted(str(o) for o in scope.owner_ids))}"
        )
        shape = {
            key: _canonical(value)
            for key, value in params.items()
            if key not in _UNBOUND_PARAMS and value is not None and value != ""
        }
        return cls(
            purpose, actor_id, workspace, json.dumps(shape, sort_keys=True, separators=(",", ":"))
        )

    def digest(self, secret: str | None = None) -> str:
        message = "\n".join((self.purpose, str(self.actor_id), self.workspace, self.shape))
        mac = salted_hmac(_BINDING_SALT, message, secret=secret, algorithm="sha256")
        return mac.hexdigest()[:32]

    def digests(self) -> frozenset[str]:
        """Under the current key and any fallback keys: rotating SECRET_KEY keeps page links
        working, as the signature itself does (Phase 9 review)."""
        keys = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
        return frozenset(self.digest(key) for key in keys)


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
    bound = _row_bound(keys, values) or keys[0].bound(values[0])
    return combined & bound if bound is not None else combined


def _row_bound(keys: Sequence[SortKey], values: Sequence[Any]) -> Q | None:
    """`ROW(k1, …, kn) > ROW(v1, …, vn)` (or `<` descending): the same rows as the expansion
    above, as one comparison PostgreSQL can start an index scan at, exactly at the cursor,
    instead of at the leading key's value (R49: a page deep inside a large group of equal
    leading values re-read the group from its start, e.g. 18,182 undated open tasks). Only
    when every key is NOT NULL and they all sort one way: a row comparison can't express
    NULL placement or mixed directions, which keep the leading-key bound."""
    pairs = zip(keys, values, strict=True)
    if len(keys) < 2 or any(key.nullable or value is None for key, value in pairs):
        return None
    if len({key.descending for key in keys}) != 1:
        return None
    row = Func(*(F(key.field) for key in keys), function="ROW", output_field=models.Field())
    cursor = Func(*(Value(value) for value in values), function="ROW", output_field=models.Field())
    comparison = LessThan if keys[0].descending else GreaterThan
    return Q(comparison(row, cursor))


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
    """`binding`: what the cursors may continue (required of every API list; None only for
    internal callers such as benchmarks and the pagination tests, whose cursors never leave
    the process)."""

    def __init__(
        self, ordering: KeysetOrdering, *, page_size: int, binding: CursorBinding | None
    ) -> None:
        self.ordering = ordering
        self.page_size = page_size
        self.binding = None if binding is None else binding.digest()
        self._accepted: frozenset[str | None] = (
            frozenset({None}) if binding is None else frozenset(binding.digests())
        )

    def _cursor(self, row: models.Model, direction: str) -> str:
        values = [
            None if key.private else _encode(getattr(row, key.field)) for key in self.ordering.keys
        ]
        payload = {"o": self.ordering.name, "d": direction, "v": values, "b": self.binding}
        return _seal(payload)

    def _private_values(self, visible: QuerySet[Any], boundary: Any) -> dict[str, Any]:
        """The boundary row's current values for the private keys, re-read by its id among
        the rows the caller may list (see the module docstring)."""
        fields = [key.field for key in self.ordering.keys if key.private]
        row = visible.filter(**{self.ordering.keys[-1].field: boundary}).values(*fields).first()
        if row is None:
            raise ValueError("The page boundary is gone or outside the caller's scope.")
        return dict(row)

    def _decode(self, visible: QuerySet[Any], cursor: str) -> tuple[str, list[Any]]:
        model = visible.model
        try:
            if len(cursor) > MAX_CURSOR_LENGTH:
                raise ValueError
            # Expired, forged and tampered cursors all fail to open.
            payload = _open(cursor)
            if (
                not isinstance(payload, dict)
                or payload.get("b") not in self._accepted
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
                current = self._private_values(visible, values[-1])
                values = [
                    current[key.field] if key.private else value
                    for key, value in zip(self.ordering.keys, values, strict=True)
                ]
        except (InvalidToken, ValidationError, ValueError, TypeError, FieldDoesNotExist):
            raise InvalidInputError(INVALID_CURSOR, details={"cursor": [INVALID_CURSOR]}) from None
        return payload["d"], values

    def window(
        self, queryset: QuerySet[M], cursor: str | None, *, visible: QuerySet[M] | None = None
    ) -> tuple[QuerySet[M], str]:
        """The one query a page runs (page_size + 1 rows, to see whether more follow) and
        its direction. Exposed so tests can EXPLAIN exactly what production executes.

        `visible`: the rows the caller may list whatever the filters (their scope), where a
        cursor's boundary row is looked up for private keys; without it, `queryset` itself
        (a boundary row that no longer matches the filters then invalidates the cursor)."""
        if not cursor:
            keys, direction = self.ordering.keys, "next"
            filtered = queryset
        else:
            direction, values = self._decode(queryset if visible is None else visible, cursor)
            keys = self.ordering.keys if direction == "next" else self.ordering.reversed()
            filtered = queryset.filter(_after(keys, values))
        ordered = filtered.order_by(*(k.order_by() for k in keys))
        return ordered[: self.page_size + 1], direction

    def paginate(
        self, queryset: QuerySet[M], cursor: str | None, *, visible: QuerySet[M] | None = None
    ) -> KeysetPage[M]:
        window, direction = self.window(queryset, cursor, visible=visible)
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
