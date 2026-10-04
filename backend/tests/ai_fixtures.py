"""Ask Arkray test fixtures, shared by arkray/ai/tests and tests/security (registered by
the root conftest).

Every test runs with AI on, the deterministic hashing embedder (no model files needed) and
no language model unless a test installs a ScriptedProvider (`scripted`). Records that must
be indexed are created through the CRM services, so the real domain events enqueue the
real outbox work; `index()` then drains the outbox inline.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from arkray.activities import services as activity_services
from arkray.ai import breaker
from arkray.ai.llm import ScriptedProvider, override_provider
from arkray.core.access import AccessScope
from arkray.core.business_time import today_bounds
from arkray.leads import services as lead_services
from tests.factories import LeadFactory, MeetingFactory, OpportunityFactory, TaskFactory
from tests.helpers import drain_outbox

RAHUL_SECRET = "RAG-RAHUL-SECRET-7319"
PRIYA_SECRET = "RAG-PRIYA-SECRET-8842"


@pytest.fixture
def ai_on(settings: Any) -> Iterator[None]:
    settings.AI_ENABLED = True
    settings.AI_INDEXING_ENABLED = True
    settings.AI_EMBEDDING_PROVIDER = "hashing"
    settings.AI_LLM_PROVIDER = "none"
    settings.ANTHROPIC_API_KEY = ""
    # The hashing embedder is lexical: related texts score lower than with the real model.
    settings.AI_RETRIEVAL_MIN_SIMILARITY = 0.05
    breaker.reset()
    yield
    breaker.reset()


def own(user: Any) -> AccessScope:
    return AccessScope.own(user.pk)


def note(actor: Any, lead: Any, text: str, scope: AccessScope | None = None, **extra: Any) -> Any:
    """A note created through the activities service (publishes ActivityCreated)."""
    return activity_services.create_activity(
        actor=actor,
        scope=scope or own(lead.owner),
        activity_type="note",
        fields={"description": text, **extra},
        lead_id=lead.pk,
    ).activity


def described_lead(owner: Any, description: str, first_name: str = "Meera") -> Any:
    """A lead with a description, created through the leads service (LeadCreated)."""
    return lead_services.create_lead(
        actor=owner,
        scope=own(owner),
        fields={"first_name": first_name, "last_name": "Iyer", "description": description},
    ).lead


def index() -> int:
    """Run every due outbox event (indexing included) inline."""
    return drain_outbox(rounds=20)


@pytest.fixture
def scripted() -> Iterator[ScriptedProvider]:
    """A language model that follows a script: append steps to `.steps`."""
    provider = ScriptedProvider(steps=[])
    with override_provider(provider):
        yield provider


@pytest.fixture
def golden(user_a: Any) -> dict[str, Any]:
    """The golden dataset of docs/rag-architecture.md#testing for user A: 10 leads,
    ₹10,00,000 open pipeline, ₹5,00,000 weighted, 2 meetings today, 3 overdue tasks."""
    now = timezone.now()
    leads = LeadFactory.create_batch(10, owner=user_a)
    opportunities = [
        OpportunityFactory(
            lead=lead,
            value=Decimal("500000.00"),
            probability=Decimal("50.00"),
            title=f"Analyzer deal {i}",
        )
        for i, lead in enumerate(leads[:2])
    ]
    for _ in range(3):
        TaskFactory(lead=leads[2], due_at=now - timedelta(days=2))
    TaskFactory(lead=leads[3], due_at=now + timedelta(days=3))
    start, end = today_bounds(now)
    middle = start + (end - start) / 2
    meetings = [
        MeetingFactory(lead=leads[4], starts_at=start + timedelta(minutes=5)),
        MeetingFactory(lead=leads[5], starts_at=middle),
    ]
    return {"leads": leads, "opportunities": opportunities, "meetings": meetings}
