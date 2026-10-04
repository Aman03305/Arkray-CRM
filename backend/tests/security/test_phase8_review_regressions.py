"""Phase 8 review regressions (Ask Arkray): each test reproduces a finding of the security
or the backend review and pins its fix."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.ai import answers, breaker, indexing, router, service, tools
from arkray.ai.indexing import Outcome
from arkray.ai.llm import AnthropicProvider, ProviderError
from arkray.ai.models import Conversation, KnowledgeChunk, Question, QuestionStatus, WorkspaceKind
from arkray.ai.sources import SourceModule
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import RateLimitedError
from arkray.leads import services as lead_services
from tests.ai_fixtures import index, note, own
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    OpportunityFactory,
    UserFactory,
    default_stage,
)
from tests.helpers import run_concurrently, signed_in

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ai_on")]


def ask(actor: Any, scope: AccessScope, text: str, conversation: Any = None) -> Question:
    with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
        question = service.submit(actor, scope, text, conversation)
        return service.dispatch(question)


def run(scope: AccessScope, name: str, args: dict[str, Any]) -> tuple[Any, tools.ToolContext]:
    ctx = tools.ToolContext(scope=scope, now=timezone.now())
    outcome = tools.execute(ctx, name, args)
    return json.loads(outcome.content), ctx


# --- security review ---------------------------------------------------------------------------
class TestAnswersFollowAccess:
    """S1: a stored answer is shown, and replayed to the model, only while its records are
    still visible to the reader."""

    def test_after_a_reassignment_the_old_answer_is_hidden_and_not_replayed(
        self, admin, user_a, user_b, scripted
    ):
        lead = LeadFactory(owner=user_a, description="RAHUL-LEAD-DESC-SECRET-QXZ")
        scripted.steps += [
            [("get_record", {"ref": f"lead:{lead.pk}"})],
            f"The lead says RAHUL-LEAD-DESC-SECRET-QXZ, see [[lead:{lead.pk}]].",
        ]
        first = ask(user_a, own(user_a), "What does my lead's file say?")
        service.answer(first.pk)
        client = signed_in(user_a)
        url = f"/api/v1/workspaces/me/ask/conversations/{first.conversation_id}"
        assert "RAHUL-LEAD-DESC-SECRET-QXZ" in json.dumps(client.get(url).json())

        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        body = json.dumps(client.get(url).json())
        assert "RAHUL-LEAD-DESC-SECRET-QXZ" not in body
        assert str(lead.pk) not in body
        polled = client.get(f"/api/v1/workspaces/me/ask/questions/{first.pk}").json()
        assert polled["answer"]["notices"][-1] == "records_hidden"

        scripted.steps += ["Nothing more."]
        follow_up = ask(user_a, own(user_a), "And what else?", first.conversation_id)
        service.answer(follow_up.pk)
        assert "RAHUL-LEAD-DESC-SECRET-QXZ" not in json.dumps(scripted.requests[-1]["messages"])

    def test_a_conversation_view_checks_visibility_in_constant_queries(self, user_a):
        first = ask(user_a, own(user_a), "pipeline value")
        for _ in range(5):
            ask(user_a, own(user_a), "lead count", first.conversation_id)
        client = signed_in(user_a)
        url = f"/api/v1/workspaces/me/ask/conversations/{first.conversation_id}"
        client.get(url)
        with CaptureQueriesContext(connection) as few:
            client.get(url)
        for _ in range(10):
            ask(user_a, own(user_a), "how many overdue tasks", first.conversation_id)
        with CaptureQueriesContext(connection) as many:
            client.get(url)
        assert len(many) == len(few)

    def test_the_conversation_list_costs_the_same_however_many_conversations(self, user_a):
        """Phase 10 N+1 audit: the list's titles come from one subquery, not one query per
        conversation."""
        client = signed_in(user_a)
        url = "/api/v1/workspaces/me/ask/conversations"
        ask(user_a, own(user_a), "pipeline value")
        client.get(url)
        with CaptureQueriesContext(connection) as few:
            listed = client.get(url).json()
        for _ in range(12):
            ask(user_a, own(user_a), "lead count")
        with CaptureQueriesContext(connection) as many:
            listed_more = client.get(url).json()
        assert (len(listed), len(listed_more)) == (1, 13)
        assert len(many) == len(few)


class TestGrounding:
    """S2 / B5 / B11: ids, offsets and scores ground nothing; words and currency prefixes are
    numbers; the context's date and the history's figures are given."""

    def allowed(self, *texts: str) -> set[Decimal]:
        result = json.dumps(
            {
                "ref": "task:aaaaaaaa-4321-4777-8530-0123456789ab",
                "id": "aaaaaaaa-4321-4777-8530-0123456789ab",
                "due": {"iso": "2026-10-03T10:30+05:30", "display": "3 Oct 2026, 10:30 AM"},
                "relevance": 0.7123,
                "total_matching": 3,
            }
        )
        return answers.allowed_numbers([result], *texts)

    @pytest.mark.parametrize(
        "narrative",
        [
            "You have 7 overdue tasks.",  # a digit of a uuid
            "You have 4,321 open deals.",
            "5 overdue and 30 open.",  # the +05:30 offset
            "You have seventeen overdue tasks.",
            "Rs.99,00,000 in total.",
            "INR 99,00,000 in total.",
            "Relevance 7123.",
        ],
    )
    def test_invented_figures_are_caught(self, narrative):
        assert answers.ungrounded_numbers(narrative, self.allowed("q")) != set()

    @pytest.mark.parametrize(
        "narrative",
        ["You have 3 overdue tasks.", "three overdue tasks", "due 3 Oct 2026 at 10:30"],
    )
    def test_real_figures_pass(self, narrative):
        assert answers.ungrounded_numbers(narrative, self.allowed("q")) == set()

    def test_the_contexts_date_and_the_history_are_given(self):
        context = "<context>\nToday: 4 Oct 2026 (Asia/Kolkata)\n</context>"
        allowed = self.allowed("q", context, "Your pipeline value is ₹10,00,000.")
        assert answers.ungrounded_numbers("As of 4 Oct 2026: ₹10,00,000.", allowed) == set()


def test_the_kill_switch_stops_questions_already_queued(settings, user_a, scripted):
    """S3."""
    scripted.steps += ["should never be asked"]
    question = ask(user_a, own(user_a), "What concerns came up?")
    settings.AI_ENABLED = False
    service.answer(question.pk)
    question.refresh_from_db()
    assert (question.status, question.error_code) == (QuestionStatus.FAILED, "ai_disabled")
    assert scripted.requests == []


def test_locations_and_links_never_reach_the_model(user_a):
    """S4."""
    lead = LeadFactory(owner=user_a)
    meeting = MeetingFactory(
        lead=lead,
        location="Zoom https://zoom.us/j/9988776655?pwd=LOCPASS123",
        description="Dial in at https://meet.example/abc?pwd=DESCPASS456 then discuss pricing.",
    )
    body, _ = run(own(user_a), "get_record", {"ref": f"meeting:{meeting.pk}"})
    sent = json.dumps(body)
    assert "LOCPASS123" not in sent
    assert "DESCPASS456" not in sent
    assert "[link]" in sent


def test_retrieved_text_and_tool_output_are_budgeted_per_question(settings, user_a):
    """S5."""
    settings.AI_CONTEXT_MAX_CHARS = 2_000
    settings.AI_TOOL_RESULTS_MAX_CHARS = 6_000
    lead = LeadFactory(owner=user_a)
    for i in range(8):
        note(user_a, lead, f"Pricing concern number {i}. " * 40)
    index()
    ctx = tools.ToolContext(scope=own(user_a), now=timezone.now())
    sent = []
    for _ in range(12):
        outcome = tools.execute(ctx, "search_notes", {"query": "pricing concern"})
        sent.append(outcome.content)
    retrieved = sum(
        len(p["untrusted_text"]) for c in sent for p in json.loads(c).get("passages", [])
    )
    assert retrieved <= 2_000
    assert sum(len(c) for c in sent if "budget" not in c) <= 6_000


def test_a_closing_window_means_open_deals(user_a):
    """S6 / B6."""
    lead = LeadFactory(owner=user_a)
    today = timezone.localdate()
    for key in ("new", "won", "lost"):
        OpportunityFactory(lead=lead, stage=default_stage(key), expected_close_date=today)
    body, _ = run(own(user_a), "list_opportunities", {"closing": "this_month"})
    assert [o["status"] for o in body["opportunities"]] == ["open"]


def test_a_passage_found_twice_is_quoted_once(user_a):
    """S7."""
    note(user_a, LeadFactory(owner=user_a), "Customer worried about the analyser price.")
    index()
    ctx = tools.ToolContext(scope=own(user_a), now=timezone.now())
    tools.execute(ctx, "search_notes", {"query": "analyser price"})
    tools.execute(ctx, "search_notes", {"query": "analyser price"})
    assert len(ctx.citations) == len({(c.ref, c.snippet) for c in ctx.citations}) == 1


def test_a_question_that_expires_while_answered_is_audited_as_expired(user_a, scripted):
    """S9 / B8."""
    question = ask(user_a, own(user_a), "Slow one?")

    def expire_meanwhile(**_: Any) -> Any:
        Question.objects.filter(pk=question.pk).update(
            status=QuestionStatus.FAILED, error_code="timeout", finished_at=timezone.now()
        )
        return mock.DEFAULT

    with mock.patch.object(
        scripted, "complete", wraps=scripted.complete, side_effect=expire_meanwhile
    ):
        scripted.steps += ["late answer"]
        service.answer(question.pk)
    question.refresh_from_db()
    assert (question.status, question.error_code) == (QuestionStatus.FAILED, "timeout")
    event = AuditEvent.objects.get(action="ai.question", metadata__question_id=str(question.pk))
    assert event.metadata["outcome"] == "expired"


def test_a_question_with_too_little_time_left_is_not_started(user_a, scripted):
    question = ask(user_a, own(user_a), "Nearly expired?")
    Question.objects.filter(pk=question.pk).update(expires_at=timezone.now() + timedelta(seconds=2))
    service.answer(question.pk)
    assert scripted.requests == []


# --- backend review ----------------------------------------------------------------------------
def test_one_broker_failure_doesnt_keep_the_broker_away(user_a, monkeypatch):
    """B1 (P1): the fail-fast breaker never re-trips itself; after the cool-down the broker
    is tried again (questions every 20 s, a 30 s cool-down)."""
    clock = [1_000.0]

    class Clock:
        @staticmethod
        def monotonic() -> float:
            return float(clock[0])

    monkeypatch.setattr("arkray.core.redis.time", Clock)
    service.dispatch_breaker.reset()
    publish = mock.Mock(side_effect=[ConnectionError("blip"), None, None, None])
    with mock.patch("arkray.ai.tasks.answer_question.apply_async", publish):
        outcomes = []
        for step in range(4):
            question = service.submit(user_a, own(user_a), f"Question {step}?", None)
            outcomes.append(service.dispatch(question).status)
            Question.objects.filter(pk=question.pk).update(
                status=QuestionStatus.FAILED, error_code="x", finished_at=timezone.now()
            )  # not pending any more: the bulkhead stays out of this test
            clock[0] += 20  # 20 s between questions
    assert outcomes[0] == QuestionStatus.FAILED  # the blip
    assert outcomes[1] == QuestionStatus.FAILED  # within the cool-down: fail fast
    assert outcomes[2:] == [QuestionStatus.PENDING, QuestionStatus.PENDING]
    assert publish.call_count == 3
    service.dispatch_breaker.reset()


class TestIndexingDecidesFromTheLiveRecord:
    """B2: a slow handler acting on an old document can't undo a newer change."""

    def test_an_old_owner_doesnt_overwrite_a_newer_reassignment(self, admin, user_a, user_b):
        """Indexed under A; the note moved on to B; a slow handler still holds a document
        from an intermediate move (owner: admin). It must write the live owner, B."""
        lead = LeadFactory(owner=user_a)
        created = note(user_a, lead, "Moved twice.")
        index()
        (document,) = indexing.LOADERS[SourceModule.ACTIVITY].for_indexing([created.pk])
        type(lead).objects.filter(pk=lead.pk).update(owner_id=user_b.pk)
        type(created).objects.filter(pk=created.pk).update(owner_id=user_b.pk)
        stale = replace(document, owner_id=admin.pk)
        assert indexing.sync(SourceModule.ACTIVITY, created.pk, stale) == Outcome.METADATA
        owners = {c.owner_id for c in KnowledgeChunk.objects.filter(source_id=created.pk)}
        assert owners == {user_b.pk}

    def test_an_old_archived_event_doesnt_delete_a_restored_sources_chunks(self, user_a):
        created = note(user_a, LeadFactory(owner=user_a), "Archived then restored.")
        index()
        assert indexing.sync(SourceModule.ACTIVITY, created.pk, None) == Outcome.UNCHANGED
        assert KnowledgeChunk.objects.filter(source_id=created.pk).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_concurrent_submissions_cant_pass_the_pending_limit(settings):
    """S8 / B3."""
    settings.AI_MAX_PENDING_PER_USER = 1
    user_a = UserFactory()

    def submit() -> str:
        try:
            service.submit(user_a, own(user_a), "Why did we lose?", None)
            return "ok"
        except RateLimitedError:
            return "limited"

    results = run_concurrently(*[submit] * 4)
    assert sorted(results) == ["limited", "limited", "limited", "ok"]
    assert Question.objects.filter(status=QuestionStatus.PENDING).count() == 1


def test_router_answers_are_never_refused_by_the_bulkheads(settings, user_a, user_b, golden):
    """B4."""
    settings.AI_MAX_PENDING_TOTAL = 1
    ask(user_b, own(user_b), "Something open-ended?")
    question = ask(user_a, own(user_a), "What is my pipeline value?")
    assert question.status == QuestionStatus.ANSWERED


def test_an_opportunitys_passages_are_found_through_its_leads_index(admin, user_a):
    """B7: organisation-wide search_notes(about=opportunity) narrows by the lead."""
    opportunity = OpportunityFactory(lead=LeadFactory(owner=user_a))
    with CaptureQueriesContext(connection) as queries:
        run(
            AccessScope.organization(admin.pk),
            "search_notes",
            {"query": "x", "about": f"opportunity:{opportunity.pk}"},
        )
    chunk_sql = [q["sql"] for q in queries.captured_queries if "ai_knowledge_chunk" in q["sql"]]
    assert chunk_sql
    assert all("lead_id =" in sql for sql in chunk_sql)


def test_model_retries_stay_inside_the_deadline(settings, monkeypatch):
    """B9."""
    from arkray.ai import llm

    monkeypatch.setattr(llm, "RETRY_BACKOFF_S", 0.0)  # the attempts' own time, as before
    settings.AI_LLM_MAX_RETRIES = 3
    provider = AnthropicProvider.__new__(AnthropicProvider)
    attempts = []

    def slow_failure(**kwargs: Any) -> Any:
        attempts.append(kwargs["timeout"])
        time.sleep(min(kwargs["timeout"], 0.4))
        raise ProviderError("timeout", counts=True)

    provider._complete_once = slow_failure
    started = time.monotonic()
    with pytest.raises(ProviderError):
        provider.complete(system="", tools=[], messages=[], timeout=1.6, allow_tools=True)
    assert time.monotonic() - started < 1.6 + 0.2
    assert all(t <= 1.6 + 1e-6 for t in attempts)  # never more than the time left
    assert len(attempts) == 2  # the third would have had under MIN_ATTEMPT_S left


def test_the_breaker_counts_failures_in_a_row_across_workers(settings):
    """B10: another worker's success resets the count."""
    settings.AI_BREAKER_FAILURES = 3
    breaker.reset()
    breaker.record_failure("timeout")
    breaker.record_failure("timeout")
    breaker.record_success()  # elsewhere
    breaker.record_failure("timeout")
    assert not breaker.is_open()
    breaker.reset()


@pytest.mark.parametrize(
    "question",
    [
        "What meetings do I have next week?",
        "What's my next meeting?",
        "How many active leads do I have?",
        "How many new deals do I have?",
        "List all my leads",
        "Show me my leads",
    ],
)
def test_the_router_doesnt_answer_a_different_question(question):
    """B12."""
    assert router.route(question, stage_names=lambda: ["New", "Negotiation", "Won", "Lost"]) is None


def test_a_self_conversation_needs_its_subject(user_a):
    """B13."""
    with pytest.raises(IntegrityError), transaction.atomic():
        Conversation.objects.create(
            actor_id=user_a.pk, workspace_kind=WorkspaceKind.SELF, subject_id=None
        )


def test_a_bold_reference_is_just_a_reference(user_a):
    """B14."""
    lead = LeadFactory(owner=user_a)
    records = {f"lead:{lead.pk}": tools.RecordRef("lead", lead.pk, "Dr Mehta")}
    blocks, _ = answers.to_blocks(f"See **[[lead:{lead.pk}]]** now.", records)
    assert "**" not in json.dumps(blocks)
