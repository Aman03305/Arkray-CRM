"""Answering a question: the router's fast path, the bounded model loop (scripted), the
grounding check, degradation without a model, and the worker's own checks."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from arkray.ai import breaker, service
from arkray.ai.llm import ProviderError
from arkray.ai.models import Conversation, Question, QuestionStatus
from arkray.core.access import AccessScope
from arkray.core.errors import NotFoundError, RateLimitedError
from arkray.identity.models import Role
from tests.ai_fixtures import index, note, own
from tests.factories import LeadFactory

pytestmark = pytest.mark.django_db


def ask(actor: Any, scope: AccessScope, text: str, conversation: Any = None) -> Question:
    with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
        question = service.submit(actor, scope, text, conversation)
        return service.dispatch(question)


def answered(question: Question) -> Any:
    question.refresh_from_db()
    assert question.status == QuestionStatus.ANSWERED, question.error_code
    return question.answer


def narrative(answer: dict[str, Any]) -> str:
    labels = {s["ref"]: s["label"] for s in answer["sources"]}
    return "\n".join(
        "".join(labels.get(p.get("ref", ""), "") or p.get("text", "") for p in b["parts"])
        for b in answer["blocks"]
    )


class TestRouter:
    def test_pipeline_value_is_answered_at_once_without_a_model(self, user_a, golden, scripted):
        question = ask(user_a, own(user_a), "What is my pipeline value?")
        answer = answered(question)
        assert question.mode == "router"
        assert scripted.requests == []
        assert narrative(answer) == (
            "Your pipeline value is ₹10,00,000 across 2 open opportunities. "
            "The weighted pipeline is ₹5,00,000."
        )
        assert {f["label"]: f["value"] for f in answer["facts"]}["Weighted pipeline"] == "₹5,00,000"
        assert answer["provenance"] == {
            "mode": "router",
            "tools": ["get_pipeline_summary"],
            "grounded": True,
            "model": "",
        }

    def test_overdue_tasks_and_todays_meetings(self, user_a, golden):
        overdue = answered(ask(user_a, own(user_a), "How many overdue tasks do I have?"))
        assert narrative(overdue).startswith("You have 3 overdue tasks.")
        assert len(overdue["sources"]) == 3
        assert {s["kind"] for s in overdue["sources"]} == {"task"}
        meetings = answered(ask(user_a, own(user_a), "What meetings do I have today?"))
        assert narrative(meetings).startswith("You have 2 meetings today.")

    def test_selected_user_and_organisation_wording(self, admin, user_a, golden):
        rahul = AccessScope.for_user(admin.pk, user_a.pk)
        assert narrative(answered(ask(admin, rahul, "How many overdue tasks?"))).startswith(
            "Rahul Sharma has 3 overdue tasks."
        )
        org = AccessScope.organization(admin.pk)
        assert narrative(answered(ask(admin, org, "pipeline value"))).startswith(
            "The organisation's pipeline value is ₹10,00,000"
        )

    def test_deals_in_a_stage(self, user_a, golden):
        answer = answered(ask(user_a, own(user_a), "Which deals are in New?"))
        assert narrative(answer).startswith("You have 2 opportunities in New.")


class TestWithoutAModel:
    def test_a_semantic_question_shows_records_not_a_summary(self, user_a):
        lead = LeadFactory(owner=user_a)
        created = note(user_a, lead, "Dr Mehta raised concerns about the analyser price.")
        index()
        question = ask(user_a, own(user_a), "What concerns did Dr Mehta raise?")
        assert question.status == QuestionStatus.PENDING
        service.answer(question.pk)
        answer = answered(question)
        assert question.mode == "retrieval"
        assert answer["blocks"][0]["parts"][0]["text"] == service.NO_MODEL
        assert [s["ref"] for s in answer["sources"]] == [f"note:{created.pk}"]
        assert answer["citations"][0]["snippet"].endswith("concerns about the analyser price.")

    def test_nothing_relevant_says_so(self, user_a):
        question = ask(user_a, own(user_a), "What did we agree with the zebra farm?")
        service.answer(question.pk)
        assert "couldn't find anything" in narrative(answered(question))


class TestModelLoop:
    def test_tools_then_a_grounded_answer_with_resolved_references(self, user_a, golden, scripted):
        deal = golden["opportunities"][0]
        scripted.steps += [
            [("list_opportunities", {"limit": 5})],
            f"Your biggest deal is [[opportunity:{deal.pk}]] at ₹5,00,000.",
        ]
        question = ask(user_a, own(user_a), "Tell me about my analyzer deals")
        service.answer(question.pk)
        answer = answered(question)
        assert question.mode == "llm"
        assert answer["provenance"]["grounded"] is True
        assert answer["blocks"][0]["parts"][1] == {"ref": f"opportunity:{deal.pk}"}
        assert [s["ref"] for s in answer["sources"]] == [f"opportunity:{deal.pk}"]
        first, second = scripted.requests
        assert first["allow_tools"]
        assert "list_opportunities" in first["tools"]
        assert second["messages"][-1]["content"][0]["type"] == "tool_result"

    def test_an_invented_figure_withholds_the_narrative_but_keeps_the_facts(
        self, user_a, golden, scripted
    ):
        scripted.steps += [[("get_pipeline_summary", {})], "Your pipeline is ₹12,00,000."]
        question = ask(user_a, own(user_a), "Summarise my pipeline")
        service.answer(question.pk)
        answer = answered(question)
        assert answer["provenance"]["grounded"] is False
        assert "₹12,00,000" not in json.dumps(answer)
        assert {f["label"] for f in answer["facts"]} >= {"Pipeline value", "Weighted pipeline"}
        assert answer["notices"] == ["ai_summary_withheld"]

    def test_references_to_records_no_tool_returned_are_dropped(self, user_a, user_b, scripted):
        foreign = note(user_b, LeadFactory(owner=user_b), "Priya's private note.")
        scripted.steps += [f"See [[note:{foreign.pk}]] for details."]
        question = ask(user_a, own(user_a), "Anything about the tender?")
        service.answer(question.pk)
        answer = answered(question)
        assert answer["sources"] == []
        assert str(foreign.pk) not in json.dumps(answer)

    def test_a_refusal_is_a_polite_answer(self, user_a, scripted):
        scripted.steps += [("refusal",)]
        question = ask(user_a, own(user_a), "Something the classifier dislikes")
        service.answer(question.pk)
        assert narrative(answered(question)) == "I can't help with that request."

    def test_tool_rounds_are_bounded(self, settings, user_a, scripted):
        settings.AI_MAX_TOOL_ROUNDS = 2
        scripted.steps += [[("get_lead_summary", {})]] * 10
        question = ask(user_a, own(user_a), "Loop forever please")
        service.answer(question.pk)
        assert len(scripted.requests) == 3
        assert [r["allow_tools"] for r in scripted.requests] == [True, True, False]
        assert question.__class__.objects.get(pk=question.pk).mode == "retrieval"

    def test_calls_per_round_are_bounded(self, settings, user_a, scripted):
        settings.AI_MAX_TOOL_CALLS_PER_ROUND = 2
        scripted.steps += [[("get_lead_summary", {})] * 5, "You have 0 leads."]
        question = ask(user_a, own(user_a), "Count my leads five times")
        service.answer(question.pk)
        results = scripted.requests[1]["messages"][-1]["content"]
        assert [r["is_error"] for r in results] == [False, False, True, True, True]

    def test_history_is_the_conversations_earlier_turns_as_text(self, user_a, golden, scripted):
        first = ask(user_a, own(user_a), "What is my pipeline value?")
        scripted.steps += ["Fine."]
        follow_up = ask(user_a, own(user_a), "And how does that compare?", first.conversation_id)
        service.answer(follow_up.pk)
        messages = scripted.requests[0]["messages"]
        # Phase 9: one user turn; earlier turns are a fenced, quoted block in it (never the
        # assistant's own voice), before the new question.
        assert [m["role"] for m in messages] == ["user"]
        context, earlier, asked = (part["text"] for part in messages[0]["content"])
        assert context.startswith("<context>")
        assert earlier.startswith("<earlier_turns>")
        assert earlier.endswith("</earlier_turns>")
        assert "Question: What is my pipeline value?" in earlier
        assert "₹10,00,000" in earlier
        assert "never instructions" in earlier
        assert asked == "And how does that compare?"

    def test_the_system_prompt_and_tool_list_are_stable_per_scope_kind(
        self, admin, user_a, user_b, scripted
    ):
        scripted.steps += ["One.", "Two."]
        for user in (user_a, user_b):
            service.answer(ask(user, own(user), "anything at all?").pk)
        first, second = scripted.requests
        assert first["system"] == second["system"]
        assert first["tool_definitions"] == second["tool_definitions"]


class TestProviderFailures:
    def test_a_provider_error_falls_back_and_trips_the_breaker(self, settings, user_a, scripted):
        settings.AI_BREAKER_FAILURES = 2
        scripted.steps += [ProviderError("timeout", counts=True)] * 2 + ["unused"]
        for _ in range(2):
            question = ask(user_a, own(user_a), "What concerns were raised?")
            service.answer(question.pk)
            answer = answered(question)
            assert answer["blocks"][0]["parts"][0]["text"] == service.MODEL_UNAVAILABLE
        assert breaker.is_open()
        calls = len(scripted.requests)
        question = ask(user_a, own(user_a), "What concerns were raised?")
        service.answer(question.pk)
        assert len(scripted.requests) == calls  # open: the provider isn't even tried
        assert service.status()["summaries"] == "none"  # no key configured in tests

    def test_our_own_bad_request_does_not_trip_the_breaker(self, user_a, scripted):
        scripted.steps += [ProviderError("status_400", counts=False)] * 5
        for _ in range(4):
            service.answer(ask(user_a, own(user_a), "Question?").pk)
        assert not breaker.is_open()


class TestWorkerChecks:
    def test_the_worker_re_authorises_a_demoted_admin(self, admin, user_a):
        question = ask(admin, AccessScope.for_user(admin.pk, user_a.pk), "What concerns came up?")
        admin.role = Role.SALES_USER
        admin.save(update_fields=["role"])
        service.answer(question.pk)
        question.refresh_from_db()
        assert (question.status, question.error_code) == (QuestionStatus.FAILED, "not_permitted")

    def test_a_deactivated_user_is_not_answered(self, admin, user_a):
        from arkray.identity import services as identity_services

        question = ask(user_a, own(user_a), "What concerns came up?")
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        service.answer(question.pk)
        question.refresh_from_db()
        assert question.error_code == "not_permitted"

    def test_a_question_is_answered_once_however_often_delivered(self, user_a, scripted):
        scripted.steps += ["Only once.", "Twice?"]
        question = ask(user_a, own(user_a), "Say something")
        service.answer(question.pk)
        service.answer(question.pk)
        assert len(scripted.requests) == 1

    def test_an_expired_question_is_failed_not_answered(self, user_a, scripted):
        question = ask(user_a, own(user_a), "Late?")
        Question.objects.filter(pk=question.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        service.answer(question.pk)
        assert scripted.requests == []
        question.refresh_from_db()
        assert service.expire_if_overdue(question).error_code == "timeout"


class TestSubmission:
    def test_ai_disabled(self, settings, user_a):
        settings.AI_ENABLED = False
        with pytest.raises(service.AiDisabledError):
            service.submit(user_a, own(user_a), "pipeline value", None)

    def test_pending_questions_per_person_are_bounded(self, settings, user_a):
        settings.AI_MAX_PENDING_PER_USER = 2
        ask(user_a, own(user_a), "First open question?")
        ask(user_a, own(user_a), "Second open question?")
        with pytest.raises(RateLimitedError):
            ask(user_a, own(user_a), "Third?")
        # Routed questions never wait, but the bulkhead is checked first all the same.

    def test_pending_questions_in_total_are_bounded(self, settings, user_a, user_b):
        settings.AI_MAX_PENDING_TOTAL = 1
        ask(user_a, own(user_a), "One question?")
        with pytest.raises(service.AiBusyError):
            ask(user_b, own(user_b), "Another?")

    def test_a_conversation_continues_only_in_its_own_workspace(self, admin, user_a, user_b):
        in_rahul = ask(admin, AccessScope.for_user(admin.pk, user_a.pk), "pipeline value")
        with pytest.raises(NotFoundError):
            ask(
                admin,
                AccessScope.for_user(admin.pk, user_b.pk),
                "pipeline value",
                in_rahul.conversation_id,
            )
        with pytest.raises(NotFoundError):
            ask(user_a, own(user_a), "pipeline value", in_rahul.conversation_id)

    def test_a_broker_failure_fails_the_question_fast(self, user_a):
        service.dispatch_breaker.reset()
        question = service.submit(user_a, own(user_a), "Why did we lose the tender?", None)
        with mock.patch(
            "arkray.ai.tasks.answer_question.apply_async", side_effect=ConnectionError("down")
        ) as publish:
            service.dispatch(question)
            again = service.submit(user_a, own(user_a), "And the other one?", None)
            service.dispatch(again)
        assert publish.call_count == 1  # the second didn't wait for the broker
        for q in (question, again):
            q.refresh_from_db()
            assert (q.status, q.error_code) == (QuestionStatus.FAILED, "ai_unavailable")
        service.dispatch_breaker.reset()

    def test_the_dispatch_cooldown_backs_off_and_a_publish_restores_it(self, user_a, caplog):
        """Phase 10 drill: a failed publish held its web worker about 10 s, once per process
        per cool-down; the cool-down now doubles (30 s up to 2 min) while the broker stays
        down, and the first successful hand-off restores it."""
        breaker = service.dispatch_breaker
        breaker.reset()
        down = mock.patch(
            "arkray.ai.tasks.answer_question.apply_async", side_effect=ConnectionError("down")
        )
        cooldowns = []
        with down, caplog.at_level("WARNING", logger="arkray.core.redis"):
            for _ in range(4):
                service.dispatch(service.submit(user_a, own(user_a), "Why did we lose?", None))
                cooldowns.append(breaker._cooldown)
                breaker._open_until = 0.0  # the cool-down ran out
        assert cooldowns == [30.0, 60.0, 120.0, 120.0]
        assert "ai_dispatch_circuit_opened" in [r.getMessage() for r in caplog.records]
        with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
            service.dispatch(service.submit(user_a, own(user_a), "And the other one?", None))
        assert breaker._cooldown == 30.0
        breaker.reset()

    @pytest.mark.parametrize("text", ["", "   ", "x" * 1001, "bad\x00char", 42])
    def test_invalid_questions(self, user_a, text):
        from arkray.core.errors import InvalidInputError

        with pytest.raises(InvalidInputError):
            service.submit(user_a, own(user_a), text, None)


class TestPrivacy:
    def test_logs_and_audit_carry_no_question_or_crm_text(self, user_a, caplog, scripted):
        from arkray.audit.models import AuditEvent

        lead = LeadFactory(owner=user_a)
        note(user_a, lead, "Confidential: SECRET-NOTE-TEXT-4417 about pricing.")
        index()
        scripted.steps += [[("search_notes", {"query": "pricing"})], "It mentions pricing."]
        with caplog.at_level(logging.DEBUG):
            question = ask(user_a, own(user_a), "What does SECRET-QUESTION-9921 say about pricing?")
            service.answer(question.pk)
        logged = " ".join(
            f"{r.getMessage()} {json.dumps(r.__dict__, default=str)}" for r in caplog.records
        )
        assert "SECRET-QUESTION-9921" not in logged
        assert "SECRET-NOTE-TEXT-4417" not in logged
        audited = json.dumps(
            list(AuditEvent.objects.filter(action="ai.question").values("metadata"))
        )
        assert "SECRET" not in audited
        assert "search_notes" in audited

    def test_housekeeping_expires_and_purges(self, settings, user_a):
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        stuck = ask(user_a, own(user_a), "Stuck?")
        Question.objects.filter(pk=stuck.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        old = ask(user_a, own(user_a), "pipeline value")
        Conversation.objects.filter(pk=old.conversation_id).update(
            updated_at=timezone.now() - timedelta(days=31)
        )
        assert service.housekeeping() == {"expired_questions": 1, "deleted_conversations": 1}
        assert not Question.objects.filter(pk=old.pk).exists()
