"""Erasing a customer at the per-customer activity budget (review of 73ff1e3, M6;
docs/privacy.md#erasure).

Nothing capped how many activities one customer could collect, so an erasure (one
transaction, every statement under a 120 s timeout) could be made to fail by whoever had
written enough notes on the customer. Now a customer holds at most ACTIVITY_MAX_PER_CUSTOMER
activities (activities.services), and the erasure redacts them in bounded batches: at the
budget it completes well inside the time a worker job has (the automatic ledger replay runs
it under Celery's 100 s soft limit), whatever else is going on.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities.models import Activity, ActivityType
from arkray.ai.models import Conversation, KnowledgeChunk, Question, QuestionStatus, WorkspaceKind
from arkray.audit.models import AuditEvent
from arkray.leads.models import Lead
from arkray.privacy import services
from tests.factories import AdminFactory, LeadFactory, NoteFactory, OpportunityFactory, TaskFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ai_on")]

AT_CAP = 5000
NOTE = "Rahul Sharma asked for the HbA1c analyser demo after 5pm. " * 30  # about 1,800 chars
WORKER_BUDGET_S = 100  # CELERY_TASK_SOFT_TIME_LIMIT: the automatic replay runs erase() there


def chunk(source_id: Any, lead: Any, index: int) -> KnowledgeChunk:
    return KnowledgeChunk(
        source_type="note",
        source_id=source_id,
        chunk_index=index,
        owner_id=lead.owner_id,
        lead_id=lead.pk,
        source_hash="0" * 64,
        char_start=0,
        char_end=10,
        embedding=[0.0] * 383 + [1.0],
        embedding_model="hashing-v1",
        source_updated_at=timezone.now(),
    )


@pytest.fixture
def capped(user_a: Any) -> Any:
    """A customer at the budget: AT_CAP notes and tasks with text, two index chunks each, a
    conversation citing them."""
    lead: Any = LeadFactory(owner=user_a, first_name="Rahul", last_name="Sharma")
    deal = OpportunityFactory(lead=lead, customer_name="Rahul Sharma")
    Activity.objects.bulk_create(
        [
            *NoteFactory.build_batch(AT_CAP - 1000, lead=lead, description=NOTE),
            *TaskFactory.build_batch(1000, lead=lead, opportunity=deal, title="Call Rahul Sharma"),
        ],
        batch_size=1000,
    )
    notes = list(
        Activity.objects.filter(lead=lead, type=ActivityType.NOTE).values_list("pk", flat=True)
    )
    KnowledgeChunk.objects.bulk_create(
        [chunk(note, lead, i) for note in notes for i in range(2)], batch_size=2000
    )
    conversation = Conversation.objects.create(
        actor=user_a, workspace_kind=WorkspaceKind.SELF, subject=user_a
    )
    now = timezone.now()
    Question.objects.create(
        conversation=conversation,
        actor=user_a,
        text="What did Rahul Sharma ask?",
        status=QuestionStatus.ANSWERED,
        mode="retrieval",
        answer={"sources": [{"ref": f"note:{notes[0]}", "label": "Visit"}], "blocks": []},
        finished_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    return lead


def timeouts(captured: CaptureQueriesContext) -> list[str]:
    prefix = "SET LOCAL statement_timeout = "
    return [
        q["sql"].removeprefix(prefix).strip("'")
        for q in captured.captured_queries
        if q["sql"].startswith(prefix)
    ]


def test_the_budget_is_the_one_tested_here():
    assert settings.ACTIVITY_MAX_PER_CUSTOMER == AT_CAP


def test_a_customer_at_the_budget_is_erased_completely_within_a_worker_jobs_time(capped):
    operator = AdminFactory()
    started = time.monotonic()
    with CaptureQueriesContext(connection) as captured:
        done = services.erase(capped.pk, operator_id=operator.pk)
    elapsed = time.monotonic() - started
    assert elapsed < WORKER_BUDGET_S / 4, f"{elapsed:.1f} s"
    # Bounded batches: no statement grows with the customer.
    slowest = max(captured.captured_queries, key=lambda query: float(query["time"]))
    assert float(slowest["time"]) < 5, slowest["sql"][:200]
    redactions = [
        q["sql"]
        for q in captured.captured_queries
        if q["sql"].startswith('UPDATE "activities_activity"')
    ]
    # Notes and other work, each in statements of at most 1,000 rows (services.ERASE_BATCH).
    assert len(redactions) == 2 * AT_CAP // 1000
    assert timeouts(captured) == ["120s"]  # an operator's command
    assert done.activities == AT_CAP
    assert done.chunks == 2 * (AT_CAP - 1000)
    assert done.conversations == 1
    lead = Lead.objects.get(pk=capped.pk)
    assert lead.erased_at is not None
    left = Activity.objects.filter(lead=capped)
    assert not left.filter(description__contains="Rahul").exists()
    assert not left.filter(title__contains="Rahul").exists()
    assert not KnowledgeChunk.objects.filter(lead_id=capped.pk).exists()
    assert AuditEvent.objects.filter(action="lead.erased", target_id=str(capped.pk)).count() == 1
    with pytest.raises(services.AlreadyErased):
        services.erase(capped.pk, operator_id=operator.pk)


def test_the_automatic_replay_runs_under_a_worker_jobs_statement_timeout(user_a):
    """privacy.reconcile replays an erasure in a worker job (no operator): each statement
    fails on its own before Celery's soft limit stops the job."""
    lead = LeadFactory(owner=user_a, first_name="Rahul", last_name="Sharma")
    NoteFactory(lead=lead, description=NOTE)
    with CaptureQueriesContext(connection) as captured:
        services.erase(lead.pk, operator_id=None, replay=True, unless_held=True)
    assert timeouts(captured) == ["60s"]
    assert not Activity.objects.filter(lead=lead, description__contains="Rahul").exists()
