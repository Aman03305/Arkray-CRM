"""Write budgets on activities (review of 73ff1e3, M6; docs/reliability.md#write-budgets).

One sales account could post a 10,000-character note every 100 ms within the API's 600
requests a minute, all day: the indexing worker fell hours behind for everyone, and a single
customer could collect so many activities that their data-rights export or erasure failed.
Two budgets, both generous enough that real work never meets them:

- per person, `ACTIVITY_MAX_CREATED_PER_USER_PER_DAY` (1,000) tasks, meetings and notes in
  any 24 hours, counted from the activities they created (never deleted, so archiving gives
  nothing back) through their own index, whatever else they did: 429 `rate_limited` with
  `Retry-After`;
- per customer, `ACTIVITY_MAX_PER_CUSTOMER` (5,000) activities on one lead, archived ones
  included (an erasure and an export handle them all): 422 `activity_limit_reached`.

`0` turns either off. Nothing is written when one refuses.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities import services
from arkray.activities.models import Activity, ActivityType
from arkray.audit import services as audit
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import RateLimitedError
from arkray.core.models import OutboxEvent
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import signed_in

from .conftest import activities_url, meeting_fields, note_fields, task_fields

pytestmark = pytest.mark.django_db

FIELDS = {"task": task_fields, "meeting": meeting_fields, "note": note_fields}


def post(client, lead, kind="note", **extra):
    return client.post(
        activities_url(extra.pop("workspace", "me")),
        {"type": kind, "lead": str(lead.pk), **FIELDS[kind](), **extra},
        format="json",
    )


def create(actor, lead, kind="note", scope=None):
    return services.create_activity(
        actor=actor,
        scope=scope or AccessScope.own(actor.pk),
        activity_type=kind,
        fields=FIELDS[kind](),
        lead_id=lead.pk,
    ).activity


def fill(lead, count: int) -> None:
    """`count` notes on the lead, written directly (as years of real use would leave them:
    created before today, so they don't count against anyone's daily budget)."""
    Activity.objects.bulk_create(
        NoteFactory.build_batch(count, lead=lead, created_at=timezone.now() - timedelta(days=30)),
        batch_size=1000,
    )


def test_the_defaults_are_generous_and_documented():
    assert django_settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY == 1000
    assert django_settings.ACTIVITY_MAX_PER_CUSTOMER == 5000


class TestDailyBudgetPerPerson:
    def test_over_the_budget_is_a_429_with_retry_after_and_nothing_written(self, settings, user_a):
        settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY = 3
        client = signed_in(user_a)
        leads = [LeadFactory(owner=user_a) for _ in range(2)]
        for kind, lead in (("task", leads[0]), ("meeting", leads[1]), ("note", leads[0])):
            assert post(client, lead, kind).status_code == 201
        before = (Activity.objects.count(), AuditEvent.objects.count(), OutboxEvent.objects.count())
        refused = post(client, leads[1], "note")
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "rate_limited"
        assert 60 <= int(refused["Retry-After"]) <= 24 * 3600
        after = (Activity.objects.count(), AuditEvent.objects.count(), OutboxEvent.objects.count())
        assert after == before

    def test_it_is_the_creators_budget_whatever_the_workspace(self, settings, admin, user_a):
        """An administrator adding work in someone's workspace spends their own budget, not
        the workspace owner's."""
        settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY = 2
        lead = LeadFactory(owner=user_a)
        delegated = AccessScope.for_user(admin.pk, user_a.pk)
        create(admin, lead, "task", scope=delegated)
        create(admin, lead, "note", scope=delegated)
        with pytest.raises(RateLimitedError):
            create(admin, lead, "note", scope=delegated)
        create(user_a, lead, "note")  # the owner's own budget is untouched

    def test_archiving_gives_nothing_back(self, settings, user_a):
        settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY = 1
        lead = LeadFactory(owner=user_a)
        first = create(user_a, lead)
        services.archive_activity(
            actor=user_a, scope=AccessScope.own(user_a.pk), activity_id=first.pk, version=1
        )
        with pytest.raises(RateLimitedError):
            create(user_a, lead)

    def test_creations_older_than_a_day_no_longer_count(self, settings, user_a):
        settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY = 1
        create(user_a, LeadFactory(owner=user_a))
        with pytest.raises(RateLimitedError) as refused:
            services._charge_daily_budget(user_a, timezone.now())
        assert refused.value.retry_after >= 60
        services._charge_daily_budget(user_a, timezone.now() + timedelta(days=1, minutes=1))

    def test_retry_after_is_when_the_oldest_counted_creation_is_a_day_old(self, settings, user_a):
        """Review round 1 (TESTS-1): a mutant answering Retry-After 60 whatever the history
        survived the suite. With a fixed clock: the cap-th newest creation (here the oldest of
        three) leaves the window 24 hours after it was made."""
        settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY = 3
        lead = LeadFactory(owner=user_a)
        now = timezone.now()
        made = [create(user_a, lead) for _ in range(3)]
        for activity, age in zip(
            made, (timedelta(hours=23), timedelta(hours=22), timedelta(hours=1)), strict=True
        ):
            Activity.objects.filter(pk=activity.pk).update(created_at=now - age)
        with pytest.raises(RateLimitedError) as refused:
            services._charge_daily_budget(user_a, now)
        assert refused.value.retry_after == 3600  # 23 h old: out of the window in an hour
        Activity.objects.filter(pk=made[0].pk).update(
            created_at=now - timedelta(hours=24) + timedelta(seconds=10)
        )
        with pytest.raises(RateLimitedError) as soon:
            services._charge_daily_budget(user_a, now)
        assert (
            soon.value.retry_after == 60
        )  # 10 s away: never asked to come back sooner than a minute
        Activity.objects.filter(pk=made[0].pk).update(created_at=now - timedelta(hours=25))
        services._charge_daily_budget(user_a, now)  # two in the window: allowed

    def test_other_writes_never_count(self, settings, user_a):
        """Edits, completions and archives aren't creations: only new records are budgeted
        (an edit re-indexes a record that already exists, coalesced while it waits)."""
        settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY = 1
        lead = LeadFactory(owner=user_a)
        task = create(user_a, lead, "task")
        scope = AccessScope.own(user_a.pk)
        services.update_activity(
            actor=user_a, scope=scope, activity_id=task.pk, version=1, changes={"title": "B"}
        )
        services.complete_activity(actor=user_a, scope=scope, activity_id=task.pk, version=2)
        with pytest.raises(RateLimitedError):
            create(user_a, lead)

    def test_zero_turns_it_off(self, settings, user_a):
        settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY = 0
        lead = LeadFactory(owner=user_a)
        for _ in range(3):
            create(user_a, lead)


class TestBudgetPerCustomer:
    def test_the_5001st_activity_on_one_customer_is_refused(self, user_a):
        """The reviewer's regression test, at the default: an export or an erasure of the
        customer then has a known worst case (tests/security/test_write_budgets.py)."""
        lead = LeadFactory(owner=user_a)
        fill(lead, 4999)
        client = signed_in(user_a)
        assert post(client, lead, "note").status_code == 201  # the 5,000th
        before = (Activity.objects.count(), AuditEvent.objects.count(), OutboxEvent.objects.count())
        refused = post(client, lead, "task")
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "activity_limit_reached"
        assert "5,000" in refused.json()["error"]["message"]
        after = (Activity.objects.count(), AuditEvent.objects.count(), OutboxEvent.objects.count())
        assert after == before
        # The same person's other customers are untouched.
        assert post(client, LeadFactory(owner=user_a), "note").status_code == 201

    def test_archived_and_deal_activities_count(self, settings, user_a):
        settings.ACTIVITY_MAX_PER_CUSTOMER = 2
        lead = LeadFactory(owner=user_a)
        deal = OpportunityFactory(lead=lead)
        NoteFactory(lead=lead, opportunity=deal)
        NoteFactory(lead=lead, archived_at=timezone.now())
        response = signed_in(user_a).post(
            activities_url(),
            {"type": "note", "opportunity": str(deal.pk), **note_fields()},
            format="json",
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "activity_limit_reached"

    def test_zero_turns_it_off(self, settings, user_a):
        settings.ACTIVITY_MAX_PER_CUSTOMER = 0
        lead = LeadFactory(owner=user_a)
        fill(lead, 3)
        assert create(user_a, lead).type == ActivityType.NOTE


class TestDailyBudgetCost:
    """Review round 1 (QUEUES-3): the budget was counted from the creator's audit events, so
    its cost grew with everything else they did in the day (400,000 audited edits: every one
    read and filtered out, 66-85 ms per creation), and a refusal read them twice. Now one
    statement over the creator's own activities, through `activities_created_by_idx`, reads
    at most the budget's worth of index entries whatever else they wrote."""

    def edits(self, user: Any, count: int) -> None:
        audit.record_many(
            [audit.Entry("task.updated", user.pk, "activity", uuid4()) for _ in range(count)]
        )

    def budget_statements(self, user: Any) -> list[str]:
        with CaptureQueriesContext(connection) as captured, contextlib.suppress(RateLimitedError):
            services._charge_daily_budget(user, timezone.now())
        return [
            q["sql"]
            for q in captured.captured_queries
            if q["sql"].lstrip().upper().startswith("SELECT") and "advisory" not in q["sql"]
        ]

    def plan(self, sql: str, *, forced: bool = False) -> list[dict[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE activities_activity")
            cursor.execute("ANALYZE audit_event")
            if forced:
                cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql)
            (found,) = cursor.fetchone()
            if forced:
                cursor.execute("SET LOCAL enable_seqscan = on")
        nodes: list[dict[str, Any]] = []
        pending = [found[0]["Plan"]]
        while pending:
            node = pending.pop()
            nodes.append(node)
            pending.extend(node.get("Plans", []))
        return nodes

    @pytest.mark.parametrize("created", [3, 5], ids=["allowed", "refused"])
    def test_one_bounded_statement_that_never_reads_the_creators_other_events(
        self, settings, user_a, created
    ):
        settings.ACTIVITY_MAX_CREATED_PER_USER_PER_DAY = 5
        lead = LeadFactory(owner=user_a)
        for _ in range(created):
            create(user_a, lead)
        self.edits(user_a, 3000)
        (statement,) = self.budget_statements(user_a)  # a refusal no longer reads twice
        nodes = self.plan(statement)
        assert not any(node.get("Relation Name") == "audit_event" for node in nodes)
        read = sum(
            node.get("Rows Removed by Filter", 0) + node.get("Actual Rows", 0) for node in nodes
        )
        assert read <= 4 * 5, read  # the budget's worth, not the 3,000 edits
        # The index serves it (forced: on a test's handful of rows the planner rightly
        # prefers to read the table; at scale the index is the only bounded path).
        forced = self.plan(statement, forced=True)
        assert any(node.get("Index Name") == "activities_created_by_idx" for node in forced)
