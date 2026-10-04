"""Phase 10 database review regressions (docs/reliability.md#performance-baseline-phase-10)."""

from __future__ import annotations

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


def reloptions(table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT reloptions FROM pg_class WHERE relname = %s", [table])
        (options,) = cursor.fetchone()
    return sorted(options or [])


def test_activities_are_vacuumed_often_enough_for_index_only_dashboard_figures():
    """The dashboard's open-task and meetings-from-today figures are index-only ranges of
    the schedule indexes, over the newest rows, which stay off the visibility map until a
    vacuum. Measured after 5,947 inserts: 4,309 heap fetches for 31,263 rows (14 %); the
    default insert-vacuum waits for 400,000 at 2,000,000 activities (migration
    activities.0009)."""
    assert reloptions("activities_activity") == [
        "autovacuum_analyze_scale_factor=0.02",
        "autovacuum_vacuum_insert_scale_factor=0.02",
        "autovacuum_vacuum_scale_factor=0.02",
    ]


def test_the_tables_without_index_only_reads_keep_the_defaults():
    """Opportunities (organisation figures are sequential scans, the board reads rows),
    timeline entries and knowledge chunks (always read from the heap) don't depend on the
    visibility map; more frequent vacuums there would cost without helping."""
    for table in ("pipeline_opportunity", "activities_timeline_entry", "ai_knowledge_chunk"):
        assert reloptions(table) == [], table
