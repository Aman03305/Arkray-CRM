# 0016. Composite, NULL-aware keyset pagination with signed cursors

Status: Accepted
Date: 2026-09-30

## Context
CRM lists must paginate stably under concurrent inserts and offer sorts like "name" (many
ties) and "last contacted" (NULL for leads nobody contacted). DRF's `CursorPagination`
positions its cursor on the first ordering column only and skips ties with an OFFSET: ties
make pages drift when rows are inserted, and a NULL position can't be encoded at all. A
forged cursor must never cause a 500 or widen visibility.

## Decision
- `arkray.core.keyset`: an ordering is a tuple of `SortKey`s (field, direction, nullable,
  NULL placement) ending in the primary key. The cursor stores the **whole sort key of the
  boundary row**; the next page is "rows strictly after it", expanded into lexicographic
  predicates with explicit NULL handling, plus a redundant range bound on the leading key
  so PostgreSQL starts its index scan at the cursor.
- Cursors are signed (`django.core.signing`, own salt), bound to the ordering name and
  direction, capped in length; anything invalid is a 400 `validation_error` on `cursor`.
- Cursors carry sort values only. Scope is applied to the queryset before pagination; the
  workspace always comes from the URL.
- Sort keys that hold personal data (names) are **private**: the cursor carries the
  boundary row's id instead, and the value is re-read from that row (by id, signed, used only
  as a position) when the next page is requested. Cursors in URLs never contain names and
  stay short in any script (both found in the Phase 2 review). The re-read is restricted to
  the rows the caller may list (Phase 6 review, P1): a position is still a measurement, so
  a boundary outside the caller's scope invalidates the cursor.
- Sorts over nullable columns should use a NOT NULL generated sort column (e.g.
  `last_contacted_sort`) so the cursor can bound the index scan; the NULL-aware predicates
  remain for correctness but can't bound a scan by themselves (review: deep pages 35–145 ms).
- Responses keep DRF's shape (`results`, `next`, `previous` URLs) and carry no counts.
- Each offered sort has a matching index (NULL placement included), verified by a
  query-plan regression test.

## Consequences
- Exact pages for any sort, tested against PostgreSQL's own ordering (ties, NULLs,
  both directions, inserts between pages).
- Adding a sort means adding a `SortKey` tuple and, if needed, an index; nothing else.
- Cursors become invalid if the secret key rotates without a fallback (the user starts
  from page one).
- Phase 1's users list keeps DRF's paginator (created_at ordering, no ties in practice);
  it can switch later without an API change.

## Alternatives considered
- DRF `CursorPagination`: see Context.
- OFFSET pagination: slow deep pages, duplicates/skips under inserts.
- Unsigned cursors: workable (scope protects data), but invalid cursors would surface as
  confusing query errors and a type-confused value could reach the database.
