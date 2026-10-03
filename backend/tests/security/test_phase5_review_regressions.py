"""Regressions for what the Phase 5 (dashboard) adversarial review confirmed. Each test
fails if its defect returns."""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from tests.factories import LeadFactory, MeetingFactory, TaskFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

DASHBOARD = "/api/v1/workspaces/me/dashboard"
ROW_FIELDS = {"id", "type", "title", "status", "due_at", "starts_at", "is_overdue", "lead", "owner"}


def test_activity_rows_carry_only_what_the_dashboard_shows(user_a, admin):
    """Security review, P3 (data minimisation): the meeting and task rows were full activity
    list rows, sending up to 240 characters of each description or agenda and its author
    (after a reassignment, the previous owner's name and whether they are still active) on
    every dashboard refresh, although the dashboard shows neither."""
    lead = LeadFactory(owner=user_a)
    secret = "Call Dr Mehta on 98200 12345 about the confidential discount"
    TaskFactory(lead=lead, created_by=admin, description=secret, due_at=timezone.now())
    start = timezone.now() + timedelta(days=1)
    MeetingFactory(
        lead=lead,
        created_by=admin,
        description=secret,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
    )
    response = signed_in(user_a).get(DASHBOARD)
    body = response.json()
    rows = [*body["next_tasks"], *body["upcoming_meetings"]]
    assert len(rows) == 2
    for row in rows:
        assert set(row) == ROW_FIELDS
        assert set(row["owner"]) == {"id", "full_name", "is_active"}
    raw = response.content.decode()
    assert "98200" not in raw
    assert "confidential" not in raw
    assert admin.full_name not in raw


def test_the_lead_index_is_swapped_without_blocking_reads_or_writes():
    """Security review (P3) then performance and domain reviews (P2/P3), availability: the
    migration first dropped the old index (ACCESS EXCLUSIVE on leads_lead for the whole
    build), then built before dropping (still ACCESS EXCLUSIVE for the drop: one long reader
    stalled every lead request for the 5 s lock timeout and failed the deploy). Now: build
    CONCURRENTLY, drop CONCURRENTLY, rename (SHARE UPDATE EXCLUSIVE)."""
    migration = importlib.import_module(
        "arkray.leads.migrations.0005_owner_index_covers_archive"
    ).Migration
    kinds = [type(operation).__name__ for operation in migration.operations]
    assert kinds == [
        "RunSQL",
        "AddIndexConcurrently",
        "RemoveIndexConcurrently",
        "RenameIndex",
        "RunSQL",
    ]
    assert not migration.atomic
    built, dropped, renamed = migration.operations[1:4]
    assert dropped.name == renamed.new_name == "leads_owner_created_idx"
    assert built.index.name == renamed.old_name
    assert built.index.include == ("archived_at",)


def test_leads_are_vacuumed_and_analysed_often_enough_for_index_only_counts():
    """Performance review, P1: every lead edit clears visibility-map bits (lead updates are
    never HOT: updated_at is indexed). With the defaults (20 % / 10 % of the table) the
    planner kept choosing the "index-only" count while it fetched most heap rows: the
    organisation's lead figures up to ~1 s at 1,000,000 leads, 60 ms when fresh. The table
    is now vacuumed and analysed after 1 % changes (migration leads.0006)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT reloptions FROM pg_class WHERE relname = 'leads_lead'")
        (options,) = cursor.fetchone()
    assert sorted(options) == [
        "autovacuum_analyze_scale_factor=0.01",
        "autovacuum_vacuum_insert_scale_factor=0.01",
        "autovacuum_vacuum_scale_factor=0.01",
    ]


def test_application_connections_never_jit_compile():
    """Performance review, P3: the dashboard's aggregates sit near the JIT cost threshold;
    JIT added 10-30 ms each (290-430 ms above the inlining threshold) to queries that take
    milliseconds without it."""
    with connection.cursor() as cursor:
        cursor.execute("SHOW jit")
        assert cursor.fetchone() == ("off",)
