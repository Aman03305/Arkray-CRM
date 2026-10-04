"""Phase 10 tracing: one correlation id from the asking request to the ai worker, and one
safe log line per model call (latency, usage, the provider's request id; never content)."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any
from unittest import mock

import pytest

from arkray.ai import service, tasks
from arkray.ai.llm import Completion, ScriptedProvider, override_provider
from arkray.core.context import (
    ExecutionContext,
    bind_context,
    current_correlation_id,
    reset_context,
)
from tests.ai_fixtures import note, own
from tests.factories import LeadFactory
from tests.helpers import signed_in

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ai_on")]


def test_the_asking_requests_id_travels_with_the_question(user_a):
    with mock.patch("arkray.ai.tasks.answer_question.apply_async") as publish:
        response = signed_in(user_a).post(
            "/api/v1/workspaces/me/ask",
            {"question": "What did the customer say about the price?"},
            format="json",
        )
    assert response.status_code in (200, 201, 202), response.content
    request_id = response["X-Request-ID"]
    (call,) = publish.call_args_list
    assert call.kwargs["kwargs"] == {"correlation_id": request_id}


def test_the_worker_binds_it_for_every_line_of_the_question(user_a):
    question = service.submit(user_a, own(user_a), "What did the customer say?", None)
    seen: list[str] = []

    def answer(question_id: Any) -> None:
        seen.append(current_correlation_id())

    token = bind_context(ExecutionContext(correlation_id="celery-task-id"))
    try:
        with mock.patch("arkray.ai.service.answer", side_effect=answer):
            tasks.answer_question(str(question.pk), correlation_id="req-1234abcd")
    finally:
        reset_context(token)
    assert seen == ["req-1234abcd"]


class Usage(ScriptedProvider):
    """The scripted model, with usage and a provider request id on every completion."""

    def complete(self, **kwargs: Any) -> Completion:
        return replace(
            super().complete(**kwargs),
            input_tokens=1200,
            output_tokens=80,
            provider_request_id="req_011CTESTPROVIDER",
        )


def test_each_model_call_is_logged_without_content(user_a, caplog):
    lead = LeadFactory(owner=user_a)
    secret = note(user_a, lead, "MODEL-CALL-SECRET-5521 was agreed.")
    provider = Usage(steps=[[("get_record", {"ref": f"note:{secret.pk}"})], "Done."])
    with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
        question = service.submit(user_a, own(user_a), "What was agreed?", None)
        service.dispatch(question)
    with override_provider(provider), caplog.at_level(logging.INFO, logger="arkray.ai.service"):
        service.answer(question.pk)
    calls = [r for r in caplog.records if r.getMessage() == "ai_model_call"]
    assert [r.round for r in calls] == [0, 1]
    assert {r.provider_request_id for r in calls} == {"req_011CTESTPROVIDER"}
    assert all(r.input_tokens == 1200 and r.latency_ms >= 0 for r in calls)
    logged = json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert "MODEL-CALL-SECRET-5521" not in logged
    assert "What was agreed" not in logged


def test_a_failed_model_call_is_logged_too(user_a, caplog):
    """Phase 10 review: only successful calls were logged, so a 20 s timeout left no
    latency behind and the slowest calls were missing from the measurements."""
    from arkray.ai.llm import ProviderError

    class TimingOut(ScriptedProvider):
        def complete(self, **kwargs: Any) -> Completion:
            raise ProviderError("timeout", counts=True)

    with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
        question = service.submit(user_a, own(user_a), "Why did we lose the tender?", None)
        service.dispatch(question)
    with (
        override_provider(TimingOut(steps=[])),
        caplog.at_level(logging.INFO, logger="arkray.ai.service"),
    ):
        service.answer(question.pk)
    calls = [r for r in caplog.records if r.getMessage() == "ai_model_call"]
    assert [(r.outcome, r.kind) for r in calls] == [("failed", "timeout")]
    assert calls[0].latency_ms >= 0
    assert "Why did we lose" not in json.dumps([r.__dict__ for r in caplog.records], default=str)
