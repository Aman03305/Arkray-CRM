# 0022. Global search: authorised, grouped lexical search with a bounded two-pass window

Status: Accepted
Date: 2026-10-04

## Context
Phase 7 adds one search box over leads, opportunities, tasks, meetings and notes, in every
workspace (own, a selected user's, the organisation's). It must never return, rank by, or
be slowed by records outside the caller's scope; it must stay bounded for any word at
1,000,000 leads and 2,000,000 activities; it must be deterministic and explainable; and
notes are sensitive. Phase 2 already had a hardened Leads search (upper-cased substring
match on one generated column, one trigram index).

Substring search through trigram indexes has a known trap: PostgreSQL can't estimate how
common a word is, and each of the two obvious plans is unbounded one way. Walking the newest
records until enough match never ends for a rare word; collecting every match through the
index and sorting never ends for a common one. On the benchmark both happened: 0.3-2 s for a
single group with the indexes in place, 3 s for a whole search without them.

## Decision
- **One endpoint, the workspace's**: `GET /api/v1/workspaces/{workspace}/search?q=`. The
  workspace resolves to an `AccessScope` first (404 before the query is even validated).
  No admin-only search.
- **Each module searches its own records** (`leads/pipeline/activities.selectors.search`),
  with its own match rule and the scope applied before any word is matched; `arkray.search`
  only composes them. The match rule is the Leads list's, generalised: every word a trigram
  index can look up (3 letters or digits in a row, a letter's own marks counting with it;
  at most 5 different ones; other words only rank) must occur, case-insensitively, in the
  record's search text: a lead's names, organisation, email and phone digits (Phase 2's
  column); an opportunity's title; a task's title; a meeting's title and location; a
  note's body. One partial trigram index per searched expression; for tasks, meetings and
  notes the owner comes first (`btree_gin`), so one person's workspace looks up only their
  records (leads and opportunities have short texts and keep trigram-only indexes).
- **Grouped results, not one cross-kind score**: at most 5 per kind, each kind ranked the
  same way (`core.ranking`): among the **100 most recent matches**, match tier against the
  title (exact, prefix, word prefix, elsewhere), then most recent, then id. "Recent" is the
  record's own date: creation for leads and opportunities; due date, start or creation for
  tasks, meetings and notes (so each type's schedule index serves it).
- **The 100 newest matches are found in one statement with two passes**, composed from the
  modules' compiled querysets (every value a bound parameter) into `MATERIALIZED` CTEs:
  a *recent pass* reads the scope's 5,000 most recent records of the kind in index order
  and checks each, its text computed once (fixed cost), and an *older pass*, behind a
  one-time gate, collects older matches through the trigram index only when fewer than
  100 recent records match *or would be returned by the index for the words* (every
  fragment it looks up, as PostgreSQL splits the words), and PostgreSQL can narrow by a
  searched word. The index then returns about 2 % of the older records at most; otherwise
  the words are matched among the recent records only.
- **Read-only and forgetful**: one `REPEATABLE READ, READ ONLY` transaction; nothing is
  written (no history, analytics or per-query audit); the application never logs the query
  (PostgreSQL's own log of a failed statement can hold it: R63).

## Consequences
- Every search costs five statements whatever the data (eight queries per request with the
  session and the snapshot; nine in a selected user's workspace). Benchmarked groups take
  2-30 ms of SQL at 1M leads and 2M activities, 24-120 ms per whole search
  (docs/search.md#performance).
- Results never depend on the plan: the window is the 100 newest matches whichever pass
  finds them, among the 5,000 most recent records when the gate says the index can't narrow
  by the words (checked against a brute-force reference that models the gate).
- Words whose fragments are common ("the-", "station": 2 % or more of recent records hold
  what the index would look up) are matched among the recent records only: a recall limit,
  traded for a bound (they read nearly every record organisation-wide: 0.3-1.2 s).
- Exact or prefix matches older than the 100 newest matches aren't ranked; more specific
  words find them. Words of 1-2 letters don't narrow a global search (a trigram index can't
  look them up), though they still rank. The Leads list keeps its own 2-letter rule.
- A word common long ago but rare recently makes the older pass read every old match (the
  one case the gate can't bound): about 2.3 µs each, 0.44 s for 190,000 organisation-wide,
  54 ms in one user's workspace; documented as a risk (R59).
- The new indexes cost writes: `description` and `location` edits are no longer HOT updates
  and write 2-5 times the WAL, and GIN indexes grow under churn (REINDEX CONCURRENTLY); R64.
- `core.ranking` writes SQL text (constants and quoted identifiers around Django-compiled
  SQL), which the rest of the code base avoids; it is the one place, pinned by tests that
  the searched words reach the driver only as parameters (the driver quotes them into the
  statement: Django's client-side binding, R63).
- Words pg_trgm can't index ("---", "★★★", "a-b", "on" with a combining accent) don't
  narrow a search (the review found them reading every record: 0.8-19 s).

## Alternatives considered
- **Search everything, filter afterwards**: forbidden (authorisation before matching).
- **ORM-only, one query per group, the planner choosing**: measured unbounded both ways.
- **Full-text search (tsvector)**: word-level, language-dependent stemming, no substring
  match for names, emails and phone digits; inconsistent with the Leads list.
- **A global relevance score across kinds**: a lead's and a note's scores aren't comparable;
  grouped results are simpler to explain and to bound.
- **pgvector / embeddings**: semantic retrieval is Phase 8 (Ask Arkray), not search.
- **An owner-first index for opportunities too**: PostgreSQL also chose it for 17 of 67
  pipeline list and board queries (a GIN index's owner key serves any owner filter), with
  no measured gain for search (short titles); the activity ones changed none of 86 activity
  list queries or the dashboard's.
