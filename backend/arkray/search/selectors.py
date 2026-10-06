"""Global search: one bounded, read-only look across a workspace's CRM records
(docs/search.md).

Nothing here decides what a record matches or who may see it. Each kind of record is
searched by its own module, with the request's AccessScope applied before any word is
matched, and ranked the same way (core.ranking):

    leads                    leads.selectors.search       (the Leads list's own search rule)
    opportunities            pipeline.selectors.search    (title)
    tasks, meetings, notes   activities.selectors.search  (title; title and location; body)

Results stay grouped by kind, at most LIMIT each, with a flag when more matched. Archived
records are left out; won, lost, completed and cancelled ones are history and stay in.
Nothing is written: the five queries run in one READ ONLY transaction (PostgreSQL refuses
any write inside it), so search can never change CRM state, record history, or remember
what was searched.

Each of the five statements may run for at most STATEMENT_TIMEOUT_MS
(docs/search.md#resource-protection), so one search holds a connection for seconds at worst,
never the 50 s that five statements under the connection's own 10 s timeout would allow. A
search the database gives up on is SearchBusy (503, "try again"), not a server error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
from django.db import OperationalError, connection, transaction

from arkray.activities import selectors as activity_selectors
from arkray.activities.models import Activity, ActivityType
from arkray.core.access import AccessScope
from arkray.core.errors import ServiceUnavailableError
from arkray.core.ranking import Matches, SearchQuery
from arkray.leads import selectors as lead_selectors
from arkray.leads.models import Lead
from arkray.pipeline import selectors as pipeline_selectors
from arkray.pipeline.models import Opportunity

logger = logging.getLogger(__name__)

LIMIT = 5  # results shown per kind of record (docs/search.md#result-limits)
# Per statement, five statements per search. Every measured statement takes under 0.5 s at
# 1M leads / 2M activities (the worst, R59's old matches, 441-706 ms alone); 2 s leaves room
# for contention while five of them stay under the web app's 15 s request timeout.
STATEMENT_TIMEOUT_MS = 2_000


def _begin(timeout_ms: int) -> str:
    """The transaction's first statements, in one round trip: REPEATABLE READ and READ ONLY,
    and the search's statement timeout for the rest of the transaction, never above the
    connection's own (DB_STATEMENT_TIMEOUT_MS may be lower; 0 there means none, so the
    search's applies). `timeout_ms` is this module's constant, never input."""
    return (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY; "
        "SELECT set_config('statement_timeout', CASE WHEN "
        "current_setting('statement_timeout')::interval "
        f"BETWEEN interval '1 ms' AND interval '{int(timeout_ms)} ms' "
        f"THEN current_setting('statement_timeout') ELSE '{int(timeout_ms)}' END, true)"
    )


class SearchBusy(ServiceUnavailableError):
    """The database gave up on a search (its statement timeout): too busy right now, or a
    search too broad to finish in time. Nothing about the query is kept."""

    code = "search_busy"
    default_message = "Search is busy right now. Try again in a moment, or add another word."


@dataclass(frozen=True, slots=True)
class SearchResults:
    query: SearchQuery
    leads: Matches[Lead]
    opportunities: Matches[Opportunity]
    tasks: Matches[Activity]
    meetings: Matches[Activity]
    notes: Matches[Activity]


def global_search(scope: AccessScope, query: SearchQuery) -> SearchResults:
    """See _search. Its queries run in one REPEATABLE READ, READ ONLY transaction: every
    group describes the same moment, and nothing can be written; each statement within
    STATEMENT_TIMEOUT_MS, or SearchBusy. Inside a caller's transaction (tests) that
    transaction's rules apply."""
    if connection.in_atomic_block:
        return _search(scope, query)
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(_begin(STATEMENT_TIMEOUT_MS))
            return _search(scope, query)
    except OperationalError as exc:
        # Only the statement timeout (or an operator's pg_cancel_backend): the transaction
        # has been rolled back and the connection is fine. Anything else stays an error
        # (a lost connection is core.views' 503).
        if not isinstance(exc.__cause__, psycopg.errors.QueryCanceled):
            raise
        logger.warning("search_timed_out", extra={"workspace_kind": scope.kind.value})
        raise SearchBusy() from None


def _search(scope: AccessScope, query: SearchQuery) -> SearchResults:
    """Five bounded queries whatever the data: one per kind of record, each reading at most
    core.ranking.WINDOW candidates and returning at most LIMIT + 1 rows with what they
    display joined (no query per result)."""
    return SearchResults(
        query=query,
        leads=lead_selectors.search(scope, query, limit=LIMIT),
        opportunities=pipeline_selectors.search(scope, query, limit=LIMIT),
        tasks=activity_selectors.search(scope, query, ActivityType.TASK, limit=LIMIT),
        meetings=activity_selectors.search(scope, query, ActivityType.MEETING, limit=LIMIT),
        notes=activity_selectors.search(scope, query, ActivityType.NOTE, limit=LIMIT),
    )
