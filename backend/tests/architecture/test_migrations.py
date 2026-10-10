"""Migrations that rewrite, backfill or index whole tables run under the application's
statement timeout (DB_STATEMENT_TIMEOUT_MS, 10 s, which `migrate` inherits). Each lifts it
for its own transaction first, or it would fail part-way at production volume (Phase 4
review: the timeline backfill passes 10 s around 740,000 history rows). The activities'
0001 and 0002 create and constrain tables that are empty at that point.

Index changes on tables in use are made CONCURRENTLY (Phase 5 review: a plain DROP INDEX
holds ACCESS EXCLUSIVE, so one long reader stalls every request on the table). Those
migrations can't be atomic, so they lift the timeouts for their session and restore them
at the end, in both directions.

No rollback by migration deletes what people recorded (final audit ARCH-2, R108): every
migration after the supported previous release ends with the reverse guard, and no
backwards plan from the latest schema can drop a table or column, or delete rows, before it
meets one (docs/deployment.md#rollback, docs/database.md#reversibility).
"""

from __future__ import annotations

import importlib
import re

import pytest
from django.contrib.postgres.operations import AddIndexConcurrently, RemoveIndexConcurrently
from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.loader import MigrationLoader

from arkray.core.migrations._reverse_guard import RefuseReverse
from tests.integration.migration_states import RELEASE_CANDIDATE

OURS = {"core", "identity", "audit", "leads", "pipeline", "activities", "ai", "privacy"}

TABLE_WIDE = [
    "activities.0003_backfill_timeline",
    "activities.0005_review_integrity",
    "activities.0006_review_performance",
    "activities.0007_current_work_indexes",
    "pipeline.0006_ownership_negotiation_fields",  # product enhancement phase
]
CONCURRENT = [
    "leads.0005_owner_index_covers_archive",  # Phase 5
    "pipeline.0005_search_indexes",  # Phase 7
    "activities.0008_search_indexes",  # Phase 7
    "core.0005_outbox_dead_index",  # Phase 10
    "ai.0003_question_finished_index",  # Phase 10 review
    "audit.0004_support_session_index",  # product enhancement phase
    "pipeline.0007_search_customer_names",  # ADR-0027: Leads left the UI
]

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


# Every migration after the supported previous release (v1.0 RC). Pinned: a new migration
# is added here, with its guard, on purpose.
REFUSE_REVERSE = {
    "activities.0010_note_edits_and_attachments",
    "audit.0003_support_sessions",
    "audit.0004_support_session_index",
    "audit.0005_audit_details",  # privacy remediation
    "core.0006_privacy_remediation",  # privacy remediation
    "identity.0004_support_sessions",
    "pipeline.0006_ownership_negotiation_fields",
    "pipeline.0007_search_customer_names",
    "pipeline.0008_opportunity_expected_cpt",
    "pipeline.0009_agreed_cpt",
    "privacy.0001_data_exports",  # privacy remediation
}
# Released migrations whose reverse deletes data and that a backwards plan reaches without a
# refusal first: the data is derived or short-lived. ai.0001 (`migrate ai zero`): the notes'
# search index, rebuilt by `ai_reindex`, and Ask Arkray conversations, deleted after 30 days
# anyway. (core.0003/0004 were reachable until core.0006, which refuses first.)
REACHABLE_WITHOUT_REFUSAL = {
    "ai.0001_initial",
}
DELETES = re.compile(r"\b(DROP\s+(TABLE|COLUMN)|DELETE|TRUNCATE)\b", re.IGNORECASE)


def load(name):
    app, migration_name = name.split(".")
    return importlib.import_module(f"arkray.{app}.migrations.{migration_name}").Migration


def sql(operation, reverse=False):
    statement = operation.reverse_sql if reverse else operation.sql
    return (statement if isinstance(statement, str) else statement[0]).strip()


def statements(statement):
    if statement is None:
        return ""
    if isinstance(statement, str):
        return statement
    return " ".join(s if isinstance(s, str) else s[0] for s in statement)


def deletes_when_reversed(migration):
    """Reversing it drops a table or a column, or deletes rows (anything RunPython does
    backwards counts: it can't be inspected)."""
    for operation in migration.operations:
        if isinstance(operation, migrations.CreateModel | migrations.AddField):
            return True
        if isinstance(operation, migrations.RunSQL) and DELETES.search(
            statements(operation.reverse_sql)
        ):
            return True
        if (
            isinstance(operation, migrations.RunPython)
            and not isinstance(operation, RefuseReverse)
            and operation.reverse_code not in (None, migrations.RunPython.noop)
        ):
            return True
    return False


def refuses(migration):
    return isinstance(migration.operations[-1], RefuseReverse)


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
    operations = migration.operations[:-1] if refuses(migration) else migration.operations
    first, *middle, last = operations
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


def test_every_concurrent_index_migration_is_held_to_those_rules():
    """Phase 10 review: core.0005 built an index concurrently without lifting the timeouts
    and wasn't in the list above, so nothing checked it."""
    from django.db.migrations.loader import MigrationLoader

    found = {
        f"{app}.{name}"
        for (app, name), migration in MigrationLoader(None).disk_migrations.items()
        if app in OURS
        and any(
            isinstance(operation, AddIndexConcurrently | RemoveIndexConcurrently)
            for operation in migration.operations
        )
    }
    assert found == set(CONCURRENT)


def test_every_migration_after_the_release_refuses_to_be_reversed():
    loader = MigrationLoader(None)
    released = {node for leaf in RELEASE_CANDIDATE for node in loader.graph.forwards_plan(leaf)}
    later = {
        f"{app}.{name}"
        for app, name in loader.disk_migrations
        if app in OURS and (app, name) not in released
    }
    assert later == REFUSE_REVERSE
    for name in sorted(later):
        *rest, last = load(name).operations
        # Last, so it runs first when reversing: nothing is undone before it refuses.
        assert isinstance(last, RefuseReverse), name
        assert not any(isinstance(operation, RefuseReverse) for operation in rest), name
        assert last.migration == name
        with pytest.raises(IrreversibleError, match=r"restore the database backup"):
            last.reverse_code(None, None)


def test_no_rollback_reaches_recorded_data_before_a_refusal():
    """For each migration whose reverse deletes data, the first migration that undoing it
    would unapply (from the latest schema) is one that refuses."""
    loader = MigrationLoader(None)
    unguarded = set()
    for (app, name), migration in loader.disk_migrations.items():
        if app not in OURS or not deletes_when_reversed(migration):
            continue
        first = loader.graph.backwards_plan((app, name))[0]
        if not refuses(loader.get_migration(*first)):
            unguarded.add(f"{app}.{name}")
    assert unguarded == REACHABLE_WITHOUT_REFUSAL
