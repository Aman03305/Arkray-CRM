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
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import connection, transaction

from arkray.activities import selectors as activity_selectors
from arkray.activities.models import Activity, ActivityType
from arkray.core.access import AccessScope
from arkray.core.ranking import Matches, SearchQuery
from arkray.leads import selectors as lead_selectors
from arkray.leads.models import Lead
from arkray.pipeline import selectors as pipeline_selectors
from arkray.pipeline.models import Opportunity

LIMIT = 5  # results shown per kind of record (docs/search.md#result-limits)


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
    group describes the same moment, and nothing can be written. Inside a caller's
    transaction (tests) that transaction's rules apply."""
    if connection.in_atomic_block:
        return _search(scope, query)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _search(scope, query)


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
