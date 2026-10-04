"""Regression tests for the whole-software audit's findings that need no concurrency (the
races are in test_audit_races.py)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, connection, transaction

from arkray.activities import services as activity_services
from arkray.activities.models import ActivityStatus
from arkray.ai import service as ai_service
from arkray.ai import tools
from arkray.core import outbox, privileges
from arkray.core.access import AccessScope
from arkray.core.errors import BusinessRuleViolation
from arkray.core.logging import JsonFormatter
from arkray.leads import selectors as lead_selectors
from arkray.leads.phones import search_digits
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import Pipeline, Stage
from tests.factories import LeadFactory, MeetingFactory, OpportunityFactory, TaskFactory

pytestmark = pytest.mark.django_db

IST = ZoneInfo("Asia/Kolkata")


# --- activities on an archived opportunity (docs/activities.md) -----------------------------
@pytest.mark.parametrize("operation", ["reopen", "restore"])
def test_work_on_an_archived_opportunity_is_not_reopened_or_restored(user_a, operation):
    """The audit: creating new work on an archived opportunity was refused, but reopening
    or restoring its work was accepted, contrary to the documented rule."""
    scope = AccessScope.own(user_a.pk)
    lead = LeadFactory(owner=user_a)
    opportunity = OpportunityFactory(lead=lead)
    task = TaskFactory(lead=lead, opportunity=opportunity)
    if operation == "reopen":
        task = activity_services.complete_activity(
            actor=user_a, scope=scope, activity_id=task.pk, version=1
        )
    else:
        task = activity_services.archive_activity(
            actor=user_a, scope=scope, activity_id=task.pk, version=1
        )
    pipeline_services.archive_opportunity(
        actor=user_a, scope=scope, opportunity_id=opportunity.pk, version=1
    )
    with pytest.raises(BusinessRuleViolation, match="opportunity is archived"):
        getattr(activity_services, f"{operation}_activity")(
            actor=user_a, scope=scope, activity_id=task.pk, version=task.version
        )


# --- Ask Arkray --------------------------------------------------------------------------------
def test_upcoming_meetings_are_the_dashboards_scheduled_ones(user_a):
    """The audit: "upcoming" counted this morning's meetings, held or missed, and disagreed
    with the dashboard (scheduled, from now on)."""
    now = datetime(2026, 10, 4, 15, 0, tzinfo=IST)
    lead = LeadFactory(owner=user_a)
    MeetingFactory(  # held this morning
        lead=lead,
        status=ActivityStatus.COMPLETED,
        starts_at=now - timedelta(hours=5),
        ends_at=now - timedelta(hours=4),
    )
    MeetingFactory(lead=lead, starts_at=now - timedelta(hours=3))  # missed this morning
    MeetingFactory(lead=lead, starts_at=now + timedelta(days=1))  # upcoming
    MeetingFactory(lead=lead, starts_at=now + timedelta(days=10))  # beyond the week
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=now)
    result = json.loads(tools.execute(ctx, "list_meetings", {"range": "upcoming"}).content)
    assert result["total_matching"] == 1


@pytest.mark.usefixtures("ai_on")
@pytest.mark.parametrize(
    ("question", "fact"),
    [
        ("How many open opportunities do I have?", "Open opportunities"),
        ("Which deals are closing this month?", "Open opportunities expected to close this month"),
    ],
)
def test_common_opportunity_questions_are_answered_from_the_crm(user_a, question, fact):
    """Without a model these fell through to note search (the audit's own finding)."""
    OpportunityFactory(lead=LeadFactory(owner=user_a), value=Decimal("1500.00"))
    asked = ai_service.submit(user_a, AccessScope.own(user_a.pk), question, None)
    assert asked.mode == "router"
    assert fact in [f["label"] for f in asked.answer["facts"]]


def test_the_ai_stage_breakdown_adds_up_to_its_totals(user_a):
    """The audit: the totals counted a retired pipeline's opportunities, the breakdown left
    them out."""
    retired = Pipeline.objects.create(key="old", name="Old Pipeline", is_active=False)
    legacy = Stage.objects.create(
        pipeline=retired,
        key="legacy",
        name="Legacy",
        position=1,
        probability=Decimal("10"),
        category="open",
    )
    OpportunityFactory(lead=LeadFactory(owner=user_a), value=Decimal("100000.01"))
    OpportunityFactory(lead=LeadFactory(owner=user_a), value=Decimal("44444.44"), stage=legacy)
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=datetime.now(tz=IST))
    summary = json.loads(tools.execute(ctx, "get_pipeline_summary", {}).content)
    open_stages = sum(
        Decimal(stage["value"]["amount"])
        for pipeline in summary["by_stage"]
        for stage in pipeline["stages"]
        if stage["category"] == "open"
    )
    assert open_stages == Decimal(summary["pipeline_value"]["amount"]) == Decimal("144444.45")
    assert "Old Pipeline (retired)" in [p["pipeline"] for p in summary["by_stage"]]


# --- phone numbers -----------------------------------------------------------------------------
FULL_WIDTH = "".join(
    chr(0xFEE0 + ord(c)) if c.isdigit() or c == "+" else c for c in "+91 98765 43210"
)


def test_full_width_phone_numbers_are_found_like_saved_ones(user_a):
    """The audit: saving normalised full-width digits (NFKC), the duplicate check and the
    lead search didn't, so the same number typed the same way wasn't found."""
    lead = LeadFactory(owner=user_a, phone="+91 98765 43210")
    scope = AccessScope.own(user_a.pk)
    found = lead_selectors.possible_duplicates(scope, phones=[FULL_WIDTH])
    assert [match.pk for match, _ in found] == [lead.pk]
    assert search_digits(FULL_WIDTH.replace(" ", "")[3:8]) == "98765"
    listed = lead_selectors.lead_list(scope, lead_selectors.LeadFilters(q=FULL_WIDTH[4:9]))
    assert list(listed.values_list("pk", flat=True)) == [lead.pk]


# --- database errors in logs (R63) ---------------------------------------------------------
def test_database_errors_are_logged_without_the_values_they_quote(user_a):
    """The audit: PostgreSQL's error text quotes the failing row ("Failing row contains
    (...)"); an unexpected IntegrityError's traceback put it in the application's log."""
    secret = "Secret.Person@Example.com"
    update = "UPDATE identity_user SET email = %s WHERE id = %s"
    with (
        pytest.raises(IntegrityError) as caught,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(update, [secret, user_a.pk])
    assert secret in str(caught.value)  # what the driver hands back
    exc_info: Any = (caught.type, caught.value, caught.tb)
    record = logging.LogRecord(
        "django.request", logging.ERROR, __file__, 1, "Internal Server Error", (), exc_info
    )
    line = JsonFormatter().format(record)
    assert secret.lower() not in line.lower()
    payload = json.loads(line)
    assert payload["exc_type"] == "IntegrityError"
    assert (payload["db_sqlstate"], payload["db_constraint"]) == (
        "23514",
        "identity_user_email_lowercase",
    )
    assert "Traceback" in payload["exc"]
    assert secret.lower() not in outbox._describe(caught.value).lower()


def test_the_release_check_warns_when_postgresql_would_log_row_values():
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL log_min_error_statement = panic")
        cursor.execute("SET LOCAL log_error_verbosity = terse")
        assert not privileges.logs_failed_statements(cursor)
        cursor.execute("SET LOCAL log_error_verbosity = 'default'")  # quoted: the value
        assert privileges.logs_failed_statements(cursor)
