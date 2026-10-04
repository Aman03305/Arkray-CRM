"""Ranking search matches: bounded, deterministic and explainable (docs/search.md#ranking).

A module that makes its records searchable supplies its *scoped* records (what the caller's
AccessScope may see, already narrowed to what is searched: not archived, one type) and its
*condition* (every search word occurs in its search text; each module decides which of its
columns that is). This module ranks the matches, the same way for every kind of record:

1. **Window: the WINDOW newest matches** (newest first, then id); among the RECENT newest
   records only, for words the index can't narrow by (below).
2. **Match tier**, against the record's *label*, the text shown as the result's title:
   0 the label is the query; 1 the label starts with it; 2 a word of the label (after a
   space) starts with it; 3 the words occur elsewhere (a lead found by its email, a word in
   the middle of a word, a meeting found by its location). Case is folded by PostgreSQL's
   `upper()` on both sides, as in matching.
3. **Newest first**, then **id**: no two results tie, so the same data always gives the same
   order.

Nothing is scored across kinds of record: results stay grouped by kind (a lead's tier and a
note's tier are not comparable), and each group shows at most `limit` results with a flag
saying whether more matched. No counts are computed or returned.

How the window is found, in one statement, whatever the words (docs/search.md#how-a-search-
runs). PostgreSQL can't tell how common a word is in a substring search, and each of the two
obvious plans is unbounded one way: walking the newest records until enough match never ends
for a rare word, and collecting every match through the trigram index never ends for a common
one (both measured at 0.3-2 s at 2,000,000 activities). So:

- **Recent pass.** The RECENT newest scoped records are read in order, each one checked as
  it is read (the check is part of that scan; no other plan is possible). Its cost is fixed.
  Two checks per record, on its text computed once: does it match, and would the trigram
  index return it for the words (it contains every fragment the index looks up)? Words in
  at least WINDOW / RECENT (2 %) of recent records end the search here.
- **Older pass,** only when fewer than WINDOW recent records match *or would be returned by
  the index*, and there are older records: the matches older than the recent ones, through
  the trigram indexes. So the index returns about 2 % of the older records at most, however
  rare the whole word is ("the-" matches almost nothing, but the index returns every note
  with "the"); words common long ago but rare now are the one case it reads more
  (docs/search.md#limits). Otherwise the words are searched among the recent records only.

Both passes are built from the modules' own querysets, compiled by Django with every value a
bound parameter; this module adds only constants and quoted table and column names. The
scope is part of both passes, so the window never holds (or is shaped by) a record outside it.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.db import connection
from django.db.models import (
    BooleanField,
    Case,
    Expression,
    ExpressionWrapper,
    IntegerField,
    Model,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Concat, Upper

from .text import clean_search_line, search_needles

WINDOW = 100  # matches ranked per kind of record (docs/search.md#result-limits)
RECENT = 5000  # newest records the recent pass reads per kind of record (docs/search.md#tuning)
CANDIDATE_PATTERNS = 8  # fragments the recent pass checks for the gate's candidate count
INDEXABLE_RUN = 3  # letters, digits and marks in a row that make a word searchable
TIER_ANNOTATION = "match_tier"
_LABEL = "search_label"
_HIT = "search_hit"
NOTHING_TO_INDEX = "Search for a word with at least 3 letters or digits in a row."
# A word's letters and digits, and the marks of its script after them: "शर्मा" is one word
# of 5 even though its virama separates two of pg_trgm's words. Generic combining marks (the
# blocks every script borrows, and variation selectors) and enclosing marks don't count:
# "on" + U+0308 is two letters, which pg_trgm looks up as "words ending in on" (review, P2).
# Joiners (ZWJ, ZWNJ) neither count nor break a run. frontend/src/features/search/query.ts
# has the same rule.
_BASE_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Nd", "Nl"})
_MARK_CATEGORIES = frozenset({"Mn", "Mc"})
_GENERIC_MARKS = (
    (0x0300, 0x036F),  # Combining Diacritical Marks
    (0x1AB0, 0x1AFF),  # ... Extended
    (0x1DC0, 0x1DFF),  # ... Supplement
    (0x20D0, 0x20FF),  # ... for Symbols
    (0xFE00, 0xFE0F),  # Variation Selectors
    (0xFE20, 0xFE2F),  # Combining Half Marks
    (0xE0100, 0xE01EF),  # Variation Selectors Supplement
)
_JOINERS = frozenset({chr(0x200C), chr(0x200D)})
# What the older pass's gate asks PostgreSQL, the authority on what its index looks up
# ([[:alnum:]] is the iswalnum pg_trgm uses; equal for every character, pinned by
# tests/security/test_phase7_review_regressions.py): a word with three alphanumerics in a
# row (a trigram that isn't padded, so not "every word ending in on"), or two in a row in
# an Indian script (its marks split pg_trgm's words: "शर्मा" is looked up as "शर" and "मा",
# padded; measured 4-15 ms). Any other word is matched among the recent records only.
_INDIAN_SCRIPTS = f"{chr(0x0900)}-{chr(0x0DFF)}"  # Devanagari to Sinhala
_INDEXABLE_SQL = f"[[:alnum:]]{{3}}|(?=[[:alnum:]]{{2}})[{_INDIAN_SCRIPTS}]{{2}}"


def _counts_as_mark(char: str, category: str) -> bool:
    code = ord(char)
    return category in _MARK_CATEGORIES and not any(
        low <= code <= high for low, high in _GENERIC_MARKS
    )


def indexable(word: str) -> bool:
    """Is `word` worth searching through a trigram index? Only with INDEXABLE_RUN letters or
    digits in a row (the marks of a letter count with it): "---", "★★★", "a-b", emoji and
    "on" plus a combining accent NFC can't merge give it nothing (or only fragments matching
    almost everything) to look up, and searching for them read every record (Phase 7 review,
    P1 and P2). Such words only rank. PostgreSQL's character tables differ from Python's on
    a few hundred rare characters (letters newer than its Unicode data), and its words split
    at some marks, so the older pass checks again in SQL before it runs (`_window`)."""
    run = 0
    for char in word:
        category = unicodedata.category(char)
        if category in _BASE_CATEGORIES or (run and _counts_as_mark(char, category)):
            run += 1
        elif not (run and char in _JOINERS):
            run = 0
        if run >= INDEXABLE_RUN:
            return True
    return False


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """What was asked: the cleaned query as a whole (match tiers compare labels with it) and
    the words searched (every one must match). Built only by `parse`, so it is always valid."""

    text: str
    terms: tuple[str, ...]

    @classmethod
    def parse(cls, q: str) -> SearchQuery:
        """core.text's search-input rules (shared with the Leads list), except that only
        words a trigram index can look up are searched (`indexable`: 3 letters or digits in
        a row), so the older pass always narrows by every word. Other words still count in
        the match tier ("Om Prakash" puts Om Prakash first). TextRejected for invisible or
        control characters, ValueError when too long or when no word can be searched."""
        text = clean_search_line(q)
        terms = tuple(search_needles(text, searchable=indexable))
        if not terms:
            raise ValueError(NOTHING_TO_INDEX)
        return cls(text=text, terms=terms)


@dataclass(frozen=True, slots=True)
class Matches[T]:
    items: list[T]
    has_more: bool  # more than `limit` matched (within the window)


def _compiled(queryset: QuerySet[Any]) -> tuple[str, tuple[Any, ...]]:
    sql, params = queryset.query.sql_with_params()
    return sql, tuple(params)


def _contains_every_term(column: str, terms: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """`column LIKE ALL (ARRAY[...])`: `column` (upper-cased text) contains every term, the
    patterns built like Django's `contains` lookup (`upper()` on the term too, LIKE's
    wildcards escaped). The column is computed once per row and checked against every word
    (one `contains` per word read the note and upper-cased it once per word: review, P2-4,
    1.1 s -> 0.29 s for 5,000 notes of 10,000 characters and five words)."""
    patterns = ", ".join(["('%%' || UPPER(%s) || '%%')"] * len(terms))
    escaped = tuple(connection.ops.prep_for_like_query(term) for term in terms)
    return f"({column} LIKE ALL (ARRAY[{patterns}]))", escaped


def _looked_up(column: str, terms: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """`column` contains what the trigram index looks up for the terms, as PostgreSQL splits
    them into words (`[[:alnum:]]`, pg_trgm's own word characters): every 2-character word
    and every trigram of the longer ones, the first CANDIDATE_PATTERNS of those
    alphabetically. A record the index would return contains them all, so among the recent
    records this counts (a little more than) the index's candidates for the words."""
    sql = (
        f"({column} LIKE ALL (ARRAY("  # noqa: S608 (constants and placeholders only)
        "SELECT DISTINCT '%%' || UPPER(substr(f[1], i, 3)) || '%%' AS p"
        f" FROM unnest(ARRAY[{', '.join(['%s'] * len(terms))}]::text[]) AS term,"
        " regexp_matches(term, '([[:alnum:]]{2,})', 'g') AS f,"
        " generate_series(1, greatest(length(f[1]) - 2, 1)) AS i"
        f" ORDER BY p LIMIT {CANDIDATE_PATTERNS})))"
    )
    return sql, tuple(terms)


def _window[M: Model](
    scoped: QuerySet[M],
    *,
    text: str,
    condition: Q | None,
    newest: Sequence[str],
    terms: Sequence[str],
) -> RawSQL:
    """The ids of the WINDOW newest records of `scoped` matching (module docstring): every
    term occurs in `text` (an upper-cased field or annotation), or the module's own
    `condition`. `newest` is (recency field descending, "-id"); `terms` are the searched
    words, which the older pass's gate checks the way PostgreSQL's index sees them."""
    model = scoped.model
    quote = connection.ops.quote_name
    recency = newest[0].removeprefix("-")
    table = quote(model._meta.db_table)
    pk_column = f"{table}.{quote(model._meta.pk.column)}"
    rows = scoped.order_by(*newest)
    columns, aliases = ["pk", recency, text], "id, recency, t"
    if condition is None:
        condition = Q()
        for term in terms:
            condition &= Q(**{f"{text}__contains": Upper(Value(term))})
        hit_sql, hit_params = _contains_every_term("search_rows.t", terms)
    else:
        rows = rows.annotate(**{_HIT: ExpressionWrapper(condition, output_field=BooleanField())})
        columns.append(_HIT)
        aliases += ", hit"
        hit_sql, hit_params = "search_rows.hit", ()
    rows_sql, rows_params = _compiled(rows.values_list(*columns)[:RECENT])
    looked_up_sql, looked_up_params = _looked_up("search_rows.t", terms)
    # SQL text below: constants, identifiers quoted from the model's metadata and SQL Django
    # compiled from querysets; every value (search words, scope ids, limits) is a bound
    # parameter (pinned by test_the_sql_holds_no_searched_text).
    #
    # The recent pass reads RECENT records once each (the text is computed in the inner
    # query, then checked twice): does it match, and would the index return it?
    recent_sql = (
        f"SELECT search_rows.id, search_rows.recency, {hit_sql}, {looked_up_sql} "  # noqa: S608
        f"FROM ({rows_sql}) AS search_rows ({aliases})"
    )
    # The older pass: a one-time gate, then every match the recent pass didn't read. It runs
    # only if all RECENT records were read, fewer than WINDOW of them match or would be
    # returned by the index for the words (so the index returns about as few of the older
    # records: "the-" is looked up as "the" plus "words ending in he", which nearly every
    # note has: 1.2 s organisation-wide before; review), and PostgreSQL's index can narrow
    # by a word at all. Otherwise it never runs, and the words are searched among the
    # recent records only.
    # The recent records are excluded by id, not by a recency boundary: a boundary would be
    # an index range the planner could choose to walk (it prices it at a third of the
    # table), reading every older record. This way only the trigram index (or, in one
    # user's workspace, their own records' index) can serve it.
    older = RawSQL(  # noqa: S611
        "(SELECT count(*) FROM search_recent) = %s "  # noqa: S608
        "AND (SELECT count(*) FROM search_recent WHERE hit OR candidate) < %s "
        f"AND ({' OR '.join(['%s ~ %s'] * len(terms))}) "
        f"AND {pk_column} NOT IN (SELECT id FROM search_recent)",
        (RECENT, WINDOW, *(value for term in terms for value in (term, _INDEXABLE_SQL))),
        output_field=BooleanField(),
    )
    older_sql, older_params = _compiled(
        scoped.filter(condition).filter(older).order_by().values_list("pk", recency)
    )
    sql = (
        "WITH search_recent (id, recency, hit, candidate) AS MATERIALIZED "  # noqa: S608
        f"({recent_sql}), "
        f"search_older (id, recency) AS MATERIALIZED ({older_sql}) "
        "SELECT id FROM ("
        "SELECT id, recency FROM search_recent WHERE hit "
        "UNION ALL SELECT id, recency FROM search_older"
        ") AS search_window ORDER BY recency DESC, id DESC LIMIT %s"
    )
    params = (*hit_params, *looked_up_params, *rows_params, *older_params, WINDOW)
    return RawSQL(sql, params)  # noqa: S611


def top_matches[M: Model](
    scoped: QuerySet[M],
    *,
    text: str,
    query: SearchQuery,
    label: Expression | str,
    newest: Sequence[str],
    limit: int,
    shape: Callable[[QuerySet[M]], QuerySet[M]] = lambda queryset: queryset,
    condition: Q | None = None,
) -> Matches[M]:
    """The best `limit` records of `scoped` in which every search word occurs in `text`
    (the name of an upper-cased field or annotation of `scoped`, served by a trigram index),
    in one query; or that match the module's own `condition` instead, if it has one (a
    lead's phone-like word also matches its phone digits). Either way the words must occur
    in `text`, which the gate's candidate count reads.

    `newest` orders newest first and ends in the primary key; `shape` adds what the results
    display (joins, the columns to load). The outer query reads only the window's rows
    again. The label is compared in PostgreSQL and never selected (an alias, not an
    annotation): for a note it is the whole body, which must not leave the database."""
    if not 1 <= limit <= 50:
        raise ValueError("limit out of range")
    asked = Upper(Value(query.text))
    window = _window(scoped, text=text, condition=condition, newest=newest, terms=query.terms)
    ranked = (
        scoped.model._default_manager.filter(pk__in=window)
        .alias(**{_LABEL: Upper(label)})
        .annotate(
            **{
                TIER_ANNOTATION: Case(
                    When(Q(**{_LABEL: asked}), then=Value(0)),
                    When(Q(**{f"{_LABEL}__startswith": asked}), then=Value(1)),
                    When(Q(**{f"{_LABEL}__contains": Concat(Value(" "), asked)}), then=Value(2)),
                    default=Value(3),
                    output_field=IntegerField(),
                )
            }
        )
        .order_by(TIER_ANNOTATION, *newest)
    )
    found = list(shape(ranked)[: limit + 1])
    return Matches(items=found[:limit], has_more=len(found) > limit)
