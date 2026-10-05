# Global search (Phase 7)

One search box in the shell finds the workspace's leads, opportunities, tasks, meetings and
notes. It is **deterministic lexical search in PostgreSQL**: no AI, no embeddings, no
external service (semantic retrieval is Ask Arkray, Phase 8). The design decision is
[ADR-0022](adr/0022-global-search.md).

> **Since [ADR-0027](adr/0027-leads-removed-from-the-ui.md)** the UI has no Leads: the dialog
> shows no Leads group (the API still returns one, unchanged), and an opportunity also
> matches on its own **account and customer names**, which its results now carry. That is how
> a customer is found: through their deals.

Code: [`backend/arkray/search/`](../backend/arkray/search/) (module beside `dashboard`
above `activities` in the [layering](architecture.md#dependency-rules)),
[`backend/arkray/core/ranking.py`](../backend/arkray/core/ranking.py) (ranking and the
window), each module's `selectors.search`, and
[`frontend/src/features/search/`](../frontend/src/features/search/).

## What is searched

| Kind | A word matches when it occurs in | Never searched | Result shows |
|---|---|---|---|
| Leads | first and last name, organisation, email, phone digits (the Leads list's own rule, `leads.selectors.match_condition`) | description, address, city, amounts | name, organisation, status, owner (organisation workspace) |
| Opportunities | title, account name, customer name (the opportunity's own snapshot; ADR-0027) | the lead's name, description, amounts, history | title, account and customer names (the lead's name when both are empty), stage, Open/Won/Lost, owner (organisation workspace) |
| Tasks | title (subject) | description, the lead's name | title, status, due date, overdue, lead, owner (organisation workspace) |
| Meetings | title and location | agenda, meeting link, the lead's name | title, start, location, status, lead, owner (organisation workspace) |
| Notes | body | the lead's name, the author | a 240-character preview around the first search word, the lead, when it was added |

- **Every searched word must match**, case-insensitively, anywhere in the record's search
  text (a substring: "quot" finds "quotation", "98765" finds "+91 98765 43210").
  Wildcards (`%`, `_`), quotes, backslashes and SQL or regex syntax are just characters.
- **Archived records are left out.** Won and lost opportunities, completed and cancelled
  tasks and meetings are history and are found, with their status written out.
- **Not by the lead's name.** An opportunity, task or meeting is found by its own text,
  never its lead's name: a closed deal or completed task its owner kept after the lead was
  reassigned shows that lead as "Customer in another workspace" (Phases 3-4), and matching
  on the lead's name would reveal whose lead it is. An opportunity's account and customer
  names are its own fields, shown to whoever sees it, so matching them reveals nothing
  (ADR-0027).
- No Companies or Products exist; users, audit logs, timelines and configuration are not
  searched.

## Input

`q`: 2-100 characters (also after normalisation: NFC can lengthen text), cleaned exactly
like stored text and the Leads list's search (`core.text`): NFC (an "e" plus a combining
accent finds "é"), whitespace collapsed, and **invisible or control characters refused**
(400): control characters, bidi overrides and isolates, zero-width spaces, BOM, tag
characters, private use, lone surrogates, noncharacters. Zero-width joiners (ZWJ, ZWNJ)
are allowed: Indic scripts need them.

**Searched words** are the ones a trigram index can look up: with **3 letters or digits in
a row**, a letter's own marks counting with it (`core.ranking.indexable`: "Rahul", "98765",
"राम", "शर्मा" (its vowel sign and virama count), "ab12"; not "Om", "---", "★★★", "a-b",
"😀😀😀", nor "on" followed by a combining accent NFC can't merge into it). Generic combining
marks (the blocks every script borrows), enclosing marks and variation selectors don't
count; joiners (ZWJ, ZWNJ) neither count nor break a run. The first 5 different ones are
searched (a repeated word counts once); the other words don't narrow the search but still
rank ("Om Prakash" puts Om Prakash first). A query with no searchable word is a 400 ("Search
for a word with at least 3 letters or digits in a row."). Words pg_trgm can't index can't
be searched in bounded time: punctuation and symbols give it nothing, one-letter fragments
like the "b" of "a-b" match almost everything (review, P1: 1-19 s at 2M activities), and
"on" plus an accent was looked up as "every word ending in on" (review, P2: 0.8-1.9 s). The
Leads list keeps its Phase 2 rule (2-character words narrow it too).

Errors never echo the query. Any other parameter is a 400.

Phase 7 fixed two Phase 2 edge cases on the way (they apply to the Leads list too): the
5-word cap was applied before short words were dropped, so "a b c d e Rahul" searched
nothing (the Leads list showed every lead); and a repeated word used up the cap, so
"rahul rahul rahul rahul rahul sharma" never searched "sharma".

## Workspaces and authorization

`GET /api/v1/workspaces/{workspace}/search?q=` is the only search endpoint:

| Workspace | Searches | Needs |
|---|---|---|
| `me` | the caller's own records | `crm.access_own` |
| `{userId}` | that user's records only (deactivated and invited users included: their history stays readable) | `workspace.view_any` (404 otherwise, like a missing user) |
| `all` | every user's records | `crm.view_all` (404 otherwise) |

- **Authorization comes first.** The workspace resolves to an `AccessScope` before the query
  is looked at (an unopenable workspace is 404 even for an invalid query). Each module then
  applies `scope.apply()` to its records **before any word is matched**: both passes of the
  window carry the scope, so a record outside it is never matched, counted, ranked or read.
- **The selected workspace decides**, not the actor's rights: an administrator searching
  Rahul's workspace for "Priya" finds only Rahul's records, although the same administrator
  could find Priya's organisation-wide.
- **Actor and subject** are Phase 6's: the administrator stays signed in as themselves; no
  impersonation. Opening a user's workspace is audited (`workspace.accessed`, once per 15
  minutes) by `resolve_workspace`; individual searches are not audited and the query never
  reaches the audit trail.
- **Results grant nothing.** A result is a snapshot: opening it re-authorises (a lead
  reassigned meanwhile is 404 in the old workspace).

## Ranking

Per kind of record, deterministic (`core.ranking`):

1. **Window**: the 100 most recent matches (most recent first, then id).
2. **Match tier** against the title (a lead's name; a note's text):
   0 the title *is* the query; 1 it *starts with* the query; 2 a word of it (after a space)
   starts with the query; 3 the words occur elsewhere (an email, inside a word, a meeting's
   location). The whole cleaned query is compared, short words included.
3. **Most recent first, then id.** No ties, so the same data always gives the same order.

"Most recent" is each record's own date: a lead's or an opportunity's creation; a task's
due date (tasks without one first: open-ended work), a meeting's start (upcoming meetings
before past ones), a note's creation: the date the Activities page shows. Activities rank
this way because each type's schedule index then holds exactly that type's records in that
order, so the recent pass reads exactly that type's newest records however rare the type is
(review: a created-order walk of all activities read every other type's rows first, and an
organisation that hardly uses meetings would have read its whole history).

Kinds are **not** scored against each other: a lead's tier and a note's tier aren't
comparable, so results stay grouped, always in the order Leads, Opportunities, Tasks,
Meetings, Notes.

## Result limits

- **5 results per kind** (25 at most), and `has_more` per kind when more matched. No counts
  are computed or returned (a count is both an oracle risk and an unbounded aggregate).
- **No pagination** and no full results page: the dialog is a quick finder; every kind's
  full list, with its own filters and keyset pagination, is a click away. Nothing about a
  search is put in a URL, history or storage.
- **The window is the 100 newest matches** of each kind. An exact match older than the 100
  newest matches of a very common word isn't ranked; adding a word finds it.
- **Words the index can't narrow by** are matched among the **5,000 most recent records** of
  each kind only: words whose fragments at least 2 % of the recent records contain ("the-",
  "station"), and words PostgreSQL splits into fragments too short to look up (below).
  Adding a more distinctive word searches all records again.

## How a search runs

`search.selectors.global_search` runs five statements (one per kind) in one
`REPEATABLE READ, READ ONLY` transaction (every group describes the same moment, and
PostgreSQL refuses any write). Each statement finds its window in two passes, composed by
`core.ranking` from the module's own compiled querysets into `MATERIALIZED` CTEs:

```
recent pass  the scope's 5,000 most recent records of the kind, read in index order; each
             one's text is computed once and checked twice as it is read: does it match,
             and would the trigram index return it for the words (fixed cost; no plan
             choice)
older pass   only if the recent pass read all 5,000, fewer than 100 of them match or would
             be returned by the index, and PostgreSQL can narrow by a searched word (a
             one-time gate: otherwise "never executed"): every older match, through the
             kind's trigram index (in one user's workspace, looked up with the owner)
window       the most recent 100 of both, then tiers, recency, id; the 6 best rows are
             read again with what they display joined (lead, owner, stage, status)
```

Why: a substring search's frequency can't be estimated, and the two obvious plans are each
unbounded one way (walking the newest records never ends for a rare word; collecting every
match never ends for a common one; both measured below). The recent pass ends a common
word's search at a fixed cost. The older pass costs about 2.3 µs per record the trigram
index returns (each is rechecked), so the gate bounds *that*: it counts, among the recent
records, those the index would return for the words, and runs the older pass only when
they are under 2 % (then the index returns about that share of the older records too).
Counting matches alone wasn't enough: "the-" matches almost nothing, but pg_trgm looks it
up as "the" plus "words ending in he", which nearly every note has (1.2 s organisation-wide;
"ationthe", rare as a whole but made of common trigrams, 0.46 s; both found re-measuring
after the performance review; 14-16 ms of SQL for the notes group now). A record the index would return contains
every fragment it looks up, as PostgreSQL splits the words (`[[:alnum:]]`, pg_trgm's own
word characters): 2-letter fragments whole, longer ones as trigrams, the first 8
alphabetically (`core.ranking._looked_up`). The window is the newest 100 matches whichever
pass finds them, among the recent records when the gate says the index can't narrow
(`test_window.py` compares it with a brute-force reference that models the gate, for 64
combinations; the review compared the earlier gate for 432 more across every kind and
scope).

The gate also asks PostgreSQL whether its index can narrow by a word at all:
`word ~ '[[:alnum:]]{3}|(?=[[:alnum:]]{2})[...]{2}'`, three alphanumerics in a row (a trigram
that isn't padded) or two in a row in an Indian script (U+0900 to U+0DFF, Devanagari to
Sinhala, whose marks split pg_trgm's words: "शर्मा" is looked up as "शर" and "मा", padded;
4-15 ms per group measured). `[[:alnum:]]` is the character table pg_trgm uses (equal for
every character, pinned by `test_phase7_review_regressions.py`). Python's `indexable`
approximates it; where they disagree (letters newer than the server's Unicode data, a
Devanagari mark after Latin letters) such a word is searched among the 5,000 most recent
records only.

**Each recent record's text is read once.** The recent pass computes the searched text
(for a note, `upper(description)`: up to 10,000 characters) in an inner query and checks it
with one `LIKE ALL` per question. One `LIKE` per word read and upper-cased a long note once
per word (review, P2-4: 0.9-2.4 s for 5,000 maximum-length notes and five words; 0.19-0.44 s
now, Limits below).

**The composed SQL holds no searched text**: words, scope ids and limits are parameters of
Django-compiled querysets, and `core.ranking` adds only constants and identifiers quoted
from model metadata (`test_window.py::test_the_sql_holds_no_searched_text`). The database
driver then quotes them into the statement it sends (Django's default client-side
binding), so a statement PostgreSQL logs, for example one cancelled by the statement
timeout, shows them (R63). The older pass excludes the recent records by id, not by a
recency boundary, so the planner can't choose to walk the recency index through every
older record.

Notes: the body is matched and previewed inside PostgreSQL. The preview is
`substring(body from max(position of the first word - 60, 1) for 241)`: the Activities list's
240-character preview (`activities.api.serializers.preview`), started 60 characters before
the match so the match is in view, with flags for text cut before and after. A cut never
splits a character from its combining marks (a Devanagari vowel sign on its own): the end
steps back to a whole character, and a preview starting mid-text drops leftover marks. The
match tier compares the body without selecting it, so a whole note never leaves the database
(`test_ranking.py::test_the_whole_body_never_leaves_the_database`).

## Database and indexes

Phase 2's `leads_search_trgm` serves leads. Phase 7 adds one partial GIN index per other
searched expression (migrations `pipeline.0005` and `activities.0008`); the activity ones
put **the owner first, then the trigrams** (`btree_gin`, a contrib extension PostgreSQL
marks trusted, created by `activities.0008` and kept on rollback like `pg_trgm`):

| Index | Columns | Rows (benchmark) | Size | Build (CONCURRENTLY) |
|---|---|---|---|---|
| `pipeline_opp_text_trgm` (was `pipeline_opp_search_trgm`, title only, until `pipeline.0007`) | `upper(title \|\| ' ' \|\| account_name \|\| ' ' \|\| customer_name)` where not archived | 300,000 opportunities | 17 MB (title only; larger with the names, not re-measured) | 4.1-4.5 s (title only) |
| `activities_task_search_trgm` | `owner_id`, `upper(title)` where task, not archived | 900,000 tasks | 47 MB | |
| `activities_meeting_search_trgm` | `owner_id`, `upper(coalesce(title, '') \|\| coalesce(' ' \|\| coalesce(location, ''), ''))` where meeting, not archived | 600,000 meetings | 36 MB | 46-73 s for the three (four runs) |
| `activities_note_search_trgm` | `owner_id`, `upper(description)` where note, not archived | 500,000 notes (avg 300, max 3,000 characters) | 119-122 MB | |

- **The owner first**: in one user's workspace PostgreSQL looks the trigrams up together
  with the owner, so it rechecks only that user's records. With the trigrams alone it
  rechecked every owner's candidates and filtered by owner afterwards: 260-460 ms for a word
  made of common trigrams in an ordinary owner's workspace (review, P2-2; 14-55 ms now).
  Organisation-wide searches use the same indexes without the owner.
- **Leads and opportunities stay trigram-only**: their search text is short, so rechecking
  every owner's candidates costs little (2-21 ms per group in every shape and workspace).
  An owner-first opportunity index was tried and dropped: PostgreSQL also chose it for 17
  of 67 pipeline list and board queries (its owner key serves any owner filter), trading
  their measured indexes for a search index at no gain. With the final indexes the
  Activities (86 queries), pipeline (67) and dashboard benchmarks use the same indexes as
  without Phase 7.
- One index per activity type, on exactly that type's search text
  (`activities.models.SEARCH_TEXT`), so a task search never reads a note's postings; each
  is partial (archived records are never searched).
- **Migration locking**: both migrations use `CREATE INDEX CONCURRENTLY` (non-atomic; the
  statement and lock timeouts are lifted for the session and restored in both directions;
  `tests/architecture/test_migrations.py` pins it). Reads and writes continue: a probe
  writing to activities and opportunities every 100 ms throughout the 2M-row builds (four
  runs, 1,862-2,462 writes each; p50 2.3-2.6 ms and p99 6-15 ms in the two runs that
  recorded them) never waited for a build: sampled every 50 ms in the last run, the writes
  only ever waited on CPU or a WAL flush, never a lock (a blocking build would have held
  every write for the whole minute). While the note index was written out,
  a few commits waited 0.1-1.5 s for the WAL flush on this Docker-on-Windows disk (the
  review saw such spikes after the builds too). A concurrent build waits for every
  transaction older than it, so a long one (a dump, a report) delays the deploy. A failed
  build leaves an INVALID index, still maintained on every write: drop it and rerun.
- **Write cost** (review, P3-5): `description` and `location` are now indexed columns, so
  changing them is never a HOT update, for any activity type (PostgreSQL ignores the
  indexes' type conditions when deciding): 0 of 3,000 note, task-description or
  meeting-location edits were HOT, against 1,349-2,341 without the indexes. WAL per 5,000
  rows: note edits 522 MB against 111 MB, task-description edits 331 MB against 189 MB,
  note inserts 163 MB against 70 MB. Storage: 205 MB of activity search indexes beside a
  590 MB activity heap at 2M activities, and 17 MB for opportunities.
- **Churn** (review, P3-6): GIN indexes grow under updates and VACUUM doesn't shrink them.
  Appending a word to 240,000 notes grew the note index from 119 MB to 205 MB;
  `REINDEX INDEX CONCURRENTLY activities_note_search_trgm` (34 s, no write blocked) brought
  it back to 122 MB. Pending lists stay under 4 MB; flushing a full one took 165-191 ms,
  paid by the writer that fills it or by autovacuum. Monitoring index bloat is Phase 10/11
  (R64).
- The meeting index uses exactly the expression Django generates for the query
  (`SEARCH_TEXT`, a `Concat` with `COALESCE`s), so PostgreSQL matches them; the plan tests pin
  it.
- The recent pass reads existing indexes: `leads_owner_created_idx` / `leads_created_idx`,
  `pipeline_opp_owner_created_idx` / `pipeline_opp_created_idx`, and for activities the
  type's schedule indexes `activities_owner_type_when_idx` / `activities_type_when_idx`.

## Performance

### Benchmark

`backend/tests/performance/bench_search.py` on `arkray_bench_search`: a copy of the Phase 5
growth database (501 users; **1,000,000 leads**, 66,700 for the heaviest owner, 1,726 for a
typical one; **300,000 opportunities**; **2,000,000 activities**: 900,000 tasks, 600,000
meetings, 500,000 notes; 137,000 for the heaviest owner) whose text columns `--seed-text`
rewrote with a skewed vocabulary: frequent and rare first names (5 % Devanagari, 2 %
accented), organisations, cities, 40 % of leads with a phone number, templated task and
meeting titles and locations, notes of 1-8 sentences (5 % of 20-59 sentences, up to
3,000 characters; 3 % with a Hindi sentence), planted words at known frequencies
(`xylograph` in 10 notes, `zebrafish` 1,000, `periwinkle` 20,000), and words only in the
oldest notes (`legacymany` in 190,000, `legacyfifty` in 50,000; none after March). PostgreSQL
16 in Docker, `jit=off`, parallel workers off. Numbers are for the final code (owner-first
activity indexes, the candidate gate, the single-read recent pass).

**SQL per group** (EXPLAIN ANALYZE, best of 3; L leads, O opportunities, T tasks, M meetings,
N notes):

| Scope | Common (`follow`, `the`) | Frequent name / prefix (`Rahul`, `Rah`) | Rare / no match (`xylograph`, `qqxzvbn`) | Phrase (`spoke with about the analyser`) | Devanagari (`राहुल`) |
|---|---|---|---|---|---|
| Heavy owner (own) | L 6-15, O 6-7, T 8, M 7-8, N 13-16 | L 7, O 6, T 8, M 11-12, N 16-17 | L 6, O 7-8, T 7-9, M 7-8, N 14-20 | L 7, O 7, T 8, M 8, N 20 | L 14, O 6, T 7, M 7, N 16 |
| Typical owner (own) | L 3, O 1, T/M/N 0.1 | L 3-4, O 1, T/M/N 0.1 | L 2-3, O 1, T/M/N 0.1 | L 3, O 1, T/M/N 0.1 | L 3, O 1, T/M/N 0.1 |
| Admin, heavy user selected | L 6-15, O 6-7, T 8, M 7-8, N 13-16 | L 7, O 7, T 8, M 10-11, N 17 | L 6-7, O 6, T 7, M 7, N 15 | L 7, O 7, T 8, M 7, N 20 | L 14, O 6, T 7, M 8, N 16 |
| Admin, organisation | L 6-20, O 4-5, T 8, M 7-8, N 12-15 | L 7, O 4-5, T 7-8, M 24, N 16 | L 6, O 4, T 6, M 6-9, N 11-14 | L 6, O 5, T 7, M 7, N 19 | L 14, O 4, T 7, M 8, N 31 |

Shapes the review found, notes group (the costly one) in ms:

| Shape | Organisation | Heavy owner | Admin, heavy user |
|---|---|---|---|
| word and punctuation (`the-`) | 14 (1,008 before the candidate gate) | 14 (121 before) | 14 (133 before) |
| rare word of common trigrams (`ationthe`, `station`) | 16 (282-408 before) | 16 (34-45 before; 260-400 before the owner-first indexes) | 15-16 |
| Latin letters and an Indic mark (`on` + nukta), Kawi letters | 11-12 (gate closed) | 11-12 | 10-12 |
| old matches only (`legacyfifty`: 50,000; `legacymany`: 190,000) | 132; 441 (R59) | 29; 54 | 31; 55 |
| 5,000 maximum-length notes (10,000 characters), 1-5 words | | English 187-227, Hindi 402-435 (0.2-2.4 s before) | |

**Whole search** (wall clock, best of 7, including Django building five queries and eight
round trips through Docker's port forwarding on Windows; a quiet run, as other load on
this machine inflates it by up to 40 %): typical owner **24-30 ms**; heavy owner
**81-120 ms**; an administrator in the heavy user's workspace **83-115 ms**; the
organisation **53-119 ms**; old-matches-only words organisation-wide 188 ms (50,000) and
489 ms (190,000), in the heavy owner's workspace 104-144 ms. Planning takes under 1.5 ms per
statement.

Every query shape was also checked at this size with `bench_search.py --check-plans`
(4 scopes × 26 queries × 5 groups: wherever the older pass ran it read the kind's trigram
index, or in a user's workspace their own records' index; in a user's workspace the
activity trigram indexes were looked up with the owner; no sequential scan; no sort of
records over more than one user's): PASS.

### Before and after

| | Without the Phase 7 indexes and window (one ORM query per group) | Indexes, one plan chosen by PostgreSQL | Final (indexes + two-pass window) |
|---|---|---|---|
| Organisation, rare or no match | 1.8-1.9 s (sequential scans of opportunities and activities) | 0.1-0.3 ms per group | 4-14 ms per group |
| Organisation, common word | 0.2-1.9 s | `follow`: notes 336 ms, tasks 163 ms (every match sorted) | 4-20 ms per group |
| Organisation, phrase | 3.0 s | notes 1.4 s | notes 19 ms |
| Organisation, short rare word (`Zoë`, `Qz`) | 2.6-3.0 s | 0.6-1.1 s per group (the walk never ends) | `Zoë` 4-11 ms per group; `Qz` alone is refused |
| Heavy owner, common word | 0.1-0.6 s | notes 335 ms | 6-16 ms per group |

### Tuning

`RECENT` (newest records read by the recent pass) trades the fixed cost of every search
against the older pass's worst case; measured at 2,000 and 5,000 when it was chosen (before
the review's changes, which only lowered the worst cases):

| `RECENT` | Rare-word search | Worst case found |
|---|---|---|
| 2,000 | 36-44 ms | 256 ms (organisation-wide `Rah`: tasks and meetings just under the gate's 5 %) |
| **5,000** (chosen) | 54-67 ms | 70-113 ms |

`WINDOW` (100) is how many newest matches are ranked; `LIMIT` (5) how many are shown.
`CANDIDATE_PATTERNS` (8) caps the fragments the candidate count looks for, so a long query
doesn't make the recent pass scan each text dozens of times: fewer fragments count more
records (never fewer), which only closes the gate earlier.

### Limits

- **Words common long ago but rare recently** make the older pass read every old match:
  the gate sees few candidates among the recent records, but the index returns all the old
  ones (risk R59): about 2.3 µs each, 132 ms of SQL for 50,000 and 441 ms for 190,000
  organisation-wide (29-55 ms in one user's workspace, where only their records are
  looked up). The 10 s statement timeout would be reached at about 4 million.
- **Words just under the gate's threshold** (2 % of recent records) make the older pass
  collect up to 2 % of the kind's records (at 900,000 tasks, up to 18,000 rows): a planted
  1.8 % word measured 34-38 ms (notes) and 22-25 ms (tasks) in every scope (review).
- **Long notes**: the recent pass's cost follows the bytes it reads. 5,000 notes of the
  maximum 10,000 characters cost 187-227 ms (English) and 402-435 ms (Devanagari: 3 bytes a
  character) per notes group; typical notes 12-20 ms.
- **Indian scripts**: their marks split pg_trgm's words into short fragments, so a word with
  only 1-letter fragments ("क्क") is matched among the recent records only, and on real
  Hindi text 2-letter fragments may be common enough to close the gate; the benchmark's
  three Hindi sentences can't show how often (review, unconfirmed).
- A **disk-cold** first search after a restart reads index pages from disk and is slower;
  production memory sizing is Phase 10/11 (as R53).

## Query counts

Per request, pinned exactly in `arkray/search/tests/test_query_counts.py` (identical at 3 and
30 records per kind, at 2 and 12 users, with and without matches):

| Workspace | Queries | Made of |
|---|---|---|
| Own (`me`) | **8** | session, user, `SET TRANSACTION`, 5 searches |
| A selected user (`{userId}`) | **9** | + the subject user's existence check (+1 audit insert once per 15-minute window) |
| Organisation (`all`) | **8** | (+1 audit insert once per window) |

An invalid query costs 2 (session and user): nothing is searched. Inside a test transaction
the `SET TRANSACTION` isn't sent (7 / 8 / 7).

## Privacy

- **Nothing is written**: no search history, recent searches, ranking feedback or analytics;
  the READ ONLY transaction makes PostgreSQL refuse a write (`test_privacy.py` slips one in
  and it fails).
- **The application never logs the query**: the access log records the path without the
  query string (`core.middleware`), errors never echo it, and the audit trail never holds it
  (`test_privacy.py` checks every log record, formatted as in production, for 200, 400, 404
  and a real statement timeout). Telemetry is the access log's route and duration.
- **PostgreSQL's own log** is another matter (review, P3; R63): Django sends statements with
  their values quoted in (client-side binding, its default, for every query of the app since
  Phase 0), and PostgreSQL logs the text of a failed statement, for example one cancelled by
  the statement timeout. Production must keep the database's statement logging off and set
  `log_min_error_statement = panic` (Phase 11), or the app must move to server-side binding
  (Phase 9, after a full regression run).
- **Note bodies** reach the browser only as the bounded preview, never logs, audit metadata,
  exceptions or events. Results name no note author: the workspace's owner didn't
  necessarily write the note; its page says who did.
- **In transit and at the edge**: the query is in the request URL (`GET`, as the Leads
  search since Phase 2). The browser keeps it out of history (the dialog never changes the
  page URL); the production reverse proxy must not log query strings (Phase 11, risk R61).

## Frontend

- **Entry**: a search field-like button in the top bar on every page ("Search deals,
  customers, activities", `Ctrl K` / `⌘K` shown from tablet width up), always visible as a
  touch target;
  `Ctrl+K` / `Cmd+K` opens it too, except while another dialog or the mobile menu is open,
  while typing in a multi-line field, or during IME composition. A URL that names no
  workspace (a malformed user id) disables it.
- **Dialog**: the shared modal (focus moves in and is trapped, the page behind is inert,
  Escape closes, focus returns to the opener) with a combobox: focus stays in the input,
  Up/Down move through results (`aria-activedescendant`), Enter or a click opens one,
  Ctrl/⌘-click opens a new tab. The dialog says whose records it searches ("Searching Rahul
  Sharma's records.", from the banner's own request).
- **Results**: a listbox with one group per kind under its written-out heading; each option
  has an explicit label ("Opportunity: Analyser upgrade, Apollo Diagnostics, Proposal,
  Open"); there is no Leads group (ADR-0027); "Top 5 shown" when
  more matched. A polite status line announces "Searching…" (also while results update, so
  every finished search is announced), "5 results, more match", "No matching CRM records",
  or why the query can't be searched (with `aria-invalid`). Nothing suggests hidden results
  exist. The focus trap skips the options (they are never focused; the listbox isn't a tab
  stop either), so Tab cycles between the box and Close.
- **When search is available**: only once the workspace is certain: after the signed-in
  viewer has loaded (never a guessed "me"), not for a malformed user id, and not in a
  user's workspace the viewer may not open. `Ctrl+K` also works on non-Latin keyboard
  layouts (`KeyK`).
- **States**: idle (a hint), invalid (the rule, without a request), loading, results,
  updating (previous results dimmed while a longer query loads), no results, error with
  "Try again" (a 400 shows the API's reason).
- **Debounce and races**: a request 250 ms after typing pauses (never for text still being
  composed with an input method); each query is keyed `["search", workspace, query]`,
  superseded requests are cancelled, and a late answer lands in its own key. Previous
  results stay on screen, dimmed, while a longer or shorter version of the same query
  loads, only if they were the same workspace's; an unrelated query shows "Searching…".
- **Enter opens a result of what is in the box**: while results are catching up with the
  typing, Enter searches at once and opens the first result of *that* query when it
  arrives, never a result of the previous query still on screen (review, P2).
- **Workspace switch**: the launcher is mounted per workspace; a switch (Back, Forward, a
  link) closes the dialog and drops what was typed and found. The test watches every text
  node and link that reaches the DOM while a delayed Rahul search finishes after the switch
  to Priya: nothing of Rahul's appears.
- **Links stay in the workspace**: every result opens through the Phase 6 route builders
  (`opportunityHref`, `activityHref`): `/pipeline/{id}` in one's own or the organisation's
  workspace, `/admin/users/{id}/pipeline/{id}` in a user's.
- **Cache**: TanStack Query in memory only (15 s fresh, gone a minute after its last use);
  nothing in browser storage; sign-out reloads the page, dropping all of it.
- **XSS**: everything is rendered as React text. Highlighting wraps the searched words in
  `<mark>` elements built from text slices, never HTML strings, and is skipped where
  upper-casing would change the text's length (ß → SS).
- **Mobile**: the dialog is full width at 320 px; titles truncate, notes wrap at any
  character, so long unbroken strings never widen the page.

## Reliability

Search reads PostgreSQL only. Redis down: it works (the cache isn't used; the throttle and
audit window fail open towards more auditing). Broker down: irrelevant (no task is
enqueued). AI or embedding provider down: irrelevant (never called; `test_privacy.py`
checks that nothing on the search path imports an AI, HTTP or task library). The 10 s
statement timeout is the hard guardrail: a timeout is a generic 500, with no SQL or query
text (`test_privacy.py` provokes a real one).

Rate limits: besides the global per-user limit (600 requests a minute), search has its own
scope, **120 searches a minute per user** (`API_THROTTLE_SEARCH`, DRF's scoped throttle as
for sign-in): search is the most expensive read per request and is sent as people type, so
a runaway client or script is held to about two searches a second, far above what a person
typing produces (a 250 ms debounce). A 429 shows in the dialog as an error with "Try
again". Like every throttle it lives in the cache: with Redis down it doesn't apply and
search still answers. Phase 9 tunes rate limits.

## Security tests

| What | Where |
|---|---|
| Marked records (`RAHUL-OMEGA-LEAD` … `PRIYA-ZETA-NOTE-SECRET`) searched from Rahul's, Priya's, the organisation's and each selected workspace: exact results, no trace of the other user in the bytes | `tests/security/test_search_cross_user.py` |
| Another workspace's exact secret looks like nothing at all (same body, size and flags as a secret that doesn't exist); another user's records never move a workspace's ranking, window or flags; scope widening attempts; 404 identical for missing and forbidden users | same |
| Restricted leads aren't revealed or matched; results re-authorise when opened; deactivated users' workspaces | `arkray/search/tests/test_api.py` |
| Input bounds, malformed encodings, lone surrogates, bidi and control characters, every script, SQL and regex syntax, wildcards | `arkray/search/tests/test_input.py` |
| Read-only, nothing logged or audited, statement timeout, no AI or network imports | `arkray/search/tests/test_privacy.py` |
| Redis and broker outages | `tests/security/test_outages.py::TestSearchKeepsWorking` |
| Frontend: entry, shortcut rules, debounce, grouping, keyboard, states, links in all three workspaces, the slow-response race, workspace switch, cache keys, storage, XSS payloads, highlighting | `frontend/src/features/search/search.test.tsx` |
| Review regressions: words an index can't look up (punctuation, symbols, generic combining marks), PostgreSQL's word table vs `[[:alnum:]]` for every character, the gate (words, candidates), length after NFC, repeated words, messages, cluster-safe previews, activity recency, one read of each recent text; Enter on stale results, focus trap, announcements, guessed workspace, client rules, layout, IME, non-Latin Ctrl+K | `backend/tests/security/test_phase7_review_regressions.py`, `frontend/src/features/search/review-regressions.test.tsx` |
| Query shapes: each pass's index in every scope, the gate, owner-first trigram lookups (and at 1M/2M: `bench_search.py --check-plans`) | `backend/tests/performance/test_search_query_plans.py` |

## Side channels

- **Results, counts, sizes, "more" flags and ranking** depend only on records inside the
  scope (pinned above).
- **Timing** is not constant. Within a user's workspace the recent pass reads only their
  records, and the older pass looks the activity trigrams up with the owner, rechecking only
  their records; but it still reads every user's posting lists for the trigrams (and the
  trigram-only lead and opportunity indexes recheck every owner's candidates: short texts). Measured: a word in 187,328 other
  users' notes and none of the user's took 21 ms against 14 ms for a word found nowhere
  (about 380 ms before the owner-first indexes, review). It reveals at most that some record
  somewhere contains the words, never which or whose (risk R60).

## Defects found in Phase 7

Found by the independent review (four reviewers: API security, frontend, backend domain,
PostgreSQL performance), by re-measuring after their fixes, and by the live walkthrough.
Each was reproduced, fixed, pinned by a regression test, and shown to fail without its fix
(the backend ones by reverting each fix in turn: 12 of 12 caught; the frontend review's 13
of 13; `test_phase7_review_regressions.py`, `test_search_query_plans.py`,
`review-regressions.test.tsx`, the Leads and Activities list tests):

| Severity | Defect | Fix |
|---|---|---|
| P1 | **Words pg_trgm can't index made the older pass read every record**: punctuation and symbols ("---", "★★★", emoji) give it no trigrams, "a-b" only one-letter fragments; a sequential scan or a full index scan of every activity, 1-19 s per search organisation-wide, 500s at the statement timeout (security and domain reviews) | Only words with 3 letters or digits in a row are searched (`indexable`); others only rank; nothing searchable is a 400. The gate also asks PostgreSQL whether its index can narrow by a word |
| P2 | Two letters and a combining mark NFC can't merge ("on" + U+0308) counted as 3 characters; pg_trgm looked up only "words ending in on": 0.8-1.9 s (performance review) | Generic combining marks, enclosing marks, variation selectors and joiners don't count; the SQL gate asks for an unpadded trigram (or an Indian script's 2-letter fragment) |
| P2 | In one user's workspace the trigram lookup rechecked every owner's candidates before filtering by owner: 260-460 ms for a word of common trigrams; the plan checks accepted it (performance review) | Owner-first activity trigram indexes (`btree_gin`); plan tests and `--check-plans` require the owner in the lookup. Opportunities stay trigram-only: an owner-first index there was chosen for 17 pipeline queries (caught by the full suite's dashboard plan test, then compared across the Phase 3-5 benchmarks) |
| P2 | A rare word whose index lookup isn't ("the-": "the" plus "words ending in he"; "ationthe") made the older pass recheck nearly every note: 1.2 s / 0.46 s organisation-wide (found re-measuring after the fixes above; the review's P2-3 measured the same class at 0.25-0.46 s) | The gate counts the recent records the index would return, not only the matches |
| P2 | The recent pass read and upper-cased each note once per word: 0.9-2.4 s for 5,000 maximum-length notes and five words (performance review) | The text is computed once per record and checked with one `LIKE ALL` |
| P2 | Enter opened a result of the previous query, not the one typed (frontend review) | Enter while results are catching up searches at once and opens the first result of what was typed |
| P2 | The focus trap leaked once results were shown (a result link counted as the last stop; Chrome made the list a tab stop) (frontend review) | The trap skips elements with a negative tab index; the listbox isn't a tab stop |
| P3 | Searched words reached PostgreSQL inside the statement text (Django's client-side binding), so a failed search's words could land in PostgreSQL's log (security review) | Documented with the production settings it needs (R63); the docs no longer claim otherwise |
| P3 | The 100-character limit was checked before NFC, which can lengthen text (security review) | Checked again after normalisation (Leads list too) |
| P3 | Single letters got the Leads list's 2-character message; a repeated word used up the 5-word cap (domain review) | Global search's own message; repeated words count once (Leads list too) |
| P3 | A note preview could start or end inside a character cluster (a Devanagari vowel sign on its own) (domain review) | Cuts step to whole clusters |
| P3 | A rare activity type's newest records were found by walking all activities in creation order (domain review, unconfirmed; fixed on reasoning) | Activities rank by their own date through each type's schedule index |
| P3 | strict mypy failed (unused ignores, untyped test helpers) (domain review) | Fixed |
| P3 | Finished searches with the same count weren't announced; the invalid state wasn't exposed; the launcher guessed "me" before the viewer loaded; tabs and Unicode spaces were refused or mis-split by the client; a long name pushed Close off a 320 px screen; sticky headings covered the active option; Ctrl+K failed on non-Latin layouts; Escape and Enter misfired during IME composition (frontend review) | Each fixed as described under Frontend |
| P3 | (Since Phases 2 and 4) At 1024-1440 px a Leads or Activities table wide enough to scroll made the whole page scroll sideways: its screen-reader-only "Actions" header label, absolutely positioned, escaped the table's scroll container (found by the Phase 7 walkthrough at 1280 px) | The scroll containers are positioned; a regression test per list, failing without it |
| P3 | The docs understated the older pass's cost, the write cost of the new indexes (no HOT updates) and GIN growth under churn (performance review) | Measured and documented here (Limits, Database and indexes; R59, R64) |
