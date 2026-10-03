"""Migrations that rewrite, backfill or index whole tables run under the application's
statement timeout (DB_STATEMENT_TIMEOUT_MS, 10 s, which `migrate` inherits). Each lifts it
for its own transaction first, or it would fail part-way at production volume (Phase 4
review: the timeline backfill passes 10 s around 740,000 history rows). The activities'
0001 and 0002 create and constrain tables that are empty at that point.

Index changes on tables in use are made CONCURRENTLY (Phase 5 review: a plain DROP INDEX
holds ACCESS EXCLUSIVE, so one long reader stalls every request on the table). Those
migrations can't be atomic, so they lift the timeouts for their session and restore them
at the end, in both directions.
"""

from __future__ import annotations

import importlib

import pytest
from django.contrib.postgres.operations import AddIndexConcurrently, RemoveIndexConcurrently
from django.db import migrations

TABLE_WIDE = [
    "activities.0003_backfill_timeline",
    "activities.0005_review_integrity",
    "activities.0006_review_performance",
    "activities.0007_current_work_indexes",
]
CONCURRENT = ["leads.0005_owner_index_covers_archive"]  # Phase 5

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


def load(name):
    app, migration_name = name.split(".")
    return importlib.import_module(f"arkray.{app}.migrations.{migration_name}").Migration


def sql(operation, reverse=False):
    statement = operation.reverse_sql if reverse else operation.sql
    return (statement if isinstance(statement, str) else statement[0]).strip()


@pytest.mark.parametrize("name", TABLE_WIDE)
def test_table_wide_migrations_lift_the_statement_timeout_first(name):
    migration = load(name)
    first = migration.operations[0]
    assert isinstance(first, migrations.RunSQL)
    assert sql(first) == "SET LOCAL statement_timeout = 0;"
    # SET LOCAL lasts until the migration's own transaction ends, and no longer.
    assert migration.atomic


@pytest.mark.parametrize("name", CONCURRENT)
def test_concurrent_index_migrations_lift_and_restore_the_timeouts_both_ways(name):
    migration = load(name)
    assert not migration.atomic  # CONCURRENTLY can't run inside a transaction
    first, *middle, last = migration.operations
    assert isinstance(first, migrations.RunSQL)
    assert isinstance(last, migrations.RunSQL)
    # Forwards: lift first, restore last. Backwards the operations run in reverse order, so
    # the last one lifts and the first one restores (the domain review found a reverse that
    # rebuilt the index before lifting the statement timeout).
    assert (sql(first), sql(last)) == (LIFT, RESTORE)
    assert (sql(last, reverse=True), sql(first, reverse=True)) == (LIFT, RESTORE)
    # No index operation that would lock the table for reads.
    for operation in middle:
        assert not isinstance(
            operation, migrations.AddIndex | migrations.RemoveIndex
        ) or isinstance(operation, AddIndexConcurrently | RemoveIndexConcurrently), operation
