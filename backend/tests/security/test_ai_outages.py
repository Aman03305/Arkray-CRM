"""Ask Arkray's failures stay Ask Arkray's (Phase 8 brief, 30): with the language model,
the embedding model, the broker, the ai workers or Redis down, the CRM and global search
keep working and Ask Arkray degrades to what still works."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from arkray.ai import breaker, service
from arkray.ai.embeddings import EmbeddingUnavailable
from arkray.ai.llm import ProviderError
from arkray.ai.models import QuestionStatus
from arkray.core.models import OutboxEvent, OutboxStatus
from tests.ai_fixtures import index, note
from tests.factories import LeadFactory
from tests.helpers import signed_in

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ai_on")]


def broken_embedder() -> Any:
    raise EmbeddingUnavailable("model files missing")


@pytest.fixture
def no_embeddings(monkeypatch):
    monkeypatch.setattr("arkray.ai.indexing.get_embedder", broken_embedder)
    monkeypatch.setattr("arkray.ai.retrieval.get_embedder", broken_embedder)


def ask(client: Any, text: str) -> dict[str, Any]:
    with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
        response = client.post("/api/v1/workspaces/me/ask", {"question": text}, format="json")
    assert response.status_code == 201, response.content
    body: dict[str, Any] = response.json()
    return body


def test_embedding_model_down(no_embeddings, user_a, golden):
    client = signed_in(user_a)
    # CRM writes commit; their indexing waits in the outbox.
    response = client.post(
        "/api/v1/workspaces/me/activities",
        {
            "type": "note",
            "lead": str(golden["leads"][0].pk),
            "description": "Written during the outage.",
        },
        format="json",
    )
    assert response.status_code == 201
    index()
    event = OutboxEvent.objects.get(topic="ai.index_source", payload__id=response.json()["id"])
    assert event.status == OutboxStatus.PENDING
    assert event.attempts == 1
    # Structured questions are still answered; semantic ones say note search is down.
    assert ask(client, "What is my pipeline value?")["status"] == "answered"
    pending = ask(client, "What did the customer say about pricing?")
    service.answer(pending["id"])
    answer = client.get(f"/api/v1/workspaces/me/ask/questions/{pending['id']}").json()
    assert "can't search notes right now" in answer["answer"]["blocks"][0]["parts"][0]["text"]
    # Global search doesn't use AI at all.
    assert client.get("/api/v1/workspaces/me/search?q=Written").status_code == 200


def test_language_model_down_then_breaker_open(settings, user_a, scripted):
    settings.AI_BREAKER_FAILURES = 1
    scripted.steps += [ProviderError("server_error", counts=True)]
    client = signed_in(user_a)
    first = ask(client, "Why did we lose the hospital deal?")
    service.answer(first["id"])
    assert breaker.is_open()
    second = ask(client, "Why did we lose the hospital deal?")
    service.answer(second["id"])
    for body in (first, second):
        polled = client.get(f"/api/v1/workspaces/me/ask/questions/{body['id']}").json()
        assert polled["status"] == "answered"
        assert polled["answer"]["provenance"]["mode"] == "retrieval"
    assert len(scripted.requests) == 1
    assert client.get("/api/v1/workspaces/me/dashboard").status_code == 200


def test_broker_down(user_a, golden):
    service.dispatch_breaker.reset()
    client = signed_in(user_a)
    with mock.patch("arkray.ai.tasks.answer_question.apply_async", side_effect=OSError("refused")):
        failed = client.post(
            "/api/v1/workspaces/me/ask", {"question": "Why did we lose?"}, format="json"
        ).json()
        routed = client.post(
            "/api/v1/workspaces/me/ask", {"question": "How many overdue tasks?"}, format="json"
        ).json()
        lead = client.post(
            "/api/v1/workspaces/me/leads",
            {"first_name": "Still", "last_name": "Works"},
            format="json",
        )
    assert (failed["status"], failed["error"]) == ("failed", "ai_unavailable")
    assert routed["status"] == "answered"  # the router never needs the broker
    assert lead.status_code == 201
    service.dispatch_breaker.reset()


def test_ai_workers_down(user_a):
    """Nobody consumes the ai queue: the question times out; nothing else waits."""
    from datetime import timedelta

    from django.utils import timezone

    from arkray.ai.models import Question

    client = signed_in(user_a)
    body = ask(client, "Summarise my week")
    Question.objects.filter(pk=body["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    polled = client.get(f"/api/v1/workspaces/me/ask/questions/{body['id']}").json()
    assert (polled["status"], polled["error"]) == ("failed", "timeout")
    assert Question.objects.get(pk=body["id"]).status == QuestionStatus.FAILED


def test_redis_down_the_breaker_still_works_in_process(settings, user_a, scripted):
    settings.AI_BREAKER_FAILURES = 2
    broken = mock.Mock(side_effect=ConnectionError("redis down"))
    with (
        mock.patch("arkray.ai.breaker.cache.get", broken),
        mock.patch("arkray.ai.breaker.cache.add", broken),
        mock.patch("arkray.ai.breaker.cache.set", broken),
        mock.patch("arkray.ai.breaker.cache.delete", broken),
    ):
        assert not breaker.is_open()
        breaker.record_failure("timeout")
        breaker.record_failure("timeout")
        assert breaker.is_open()


def test_ai_disabled_entirely_leaves_the_crm_alone(settings, user_a):
    settings.AI_ENABLED = False
    settings.AI_INDEXING_ENABLED = False
    client = signed_in(user_a)
    lead = LeadFactory(owner=user_a)
    note(user_a, lead, "No AI anywhere.")
    assert not OutboxEvent.objects.filter(topic__startswith="ai.").exists()
    refused = client.post("/api/v1/workspaces/me/ask", {"question": "x"}, format="json")
    assert refused.status_code == 503
    assert client.get("/api/v1/workspaces/me/search?q=Apollo").status_code == 200
