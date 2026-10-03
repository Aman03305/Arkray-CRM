"""Migrations that rewrite, backfill or index whole activity tables run under the
application's statement timeout (DB_STATEMENT_TIMEOUT_MS, 10 s, which `migrate` inherits).
Each lifts it for its own transaction first, or it would fail part-way at production
volume (Phase 4 review: the timeline backfill passes 10 s around 740,000 history rows).
0001 and 0002 create and constrain tables that are empty at that point.
"""

from __future__ import annotations

import importlib

import pytest
from django.db import migrations

TABLE_WIDE = [
    "0003_backfill_timeline",
    "0005_review_integrity",
    "0006_review_performance",
    "0007_current_work_indexes",
]


@pytest.mark.parametrize("name", TABLE_WIDE)
def test_table_wide_activity_migrations_lift_the_statement_timeout_first(name):
    migration = importlib.import_module(f"arkray.activities.migrations.{name}").Migration
    first = migration.operations[0]
    assert isinstance(first, migrations.RunSQL)
    statement = first.sql if isinstance(first.sql, str) else first.sql[0]
    assert statement.strip() == "SET LOCAL statement_timeout = 0;"
    # SET LOCAL lasts until the migration's own transaction ends, and no longer.
    assert migration.atomic
