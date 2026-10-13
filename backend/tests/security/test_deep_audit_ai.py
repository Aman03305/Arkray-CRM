"""Deep security audit: Ask Arkray findings (M-5, L-5, L-6, L-10, L-11, L-12, L-13).

The audit's probes asserted the vulnerable behaviour; these are those probes inverted, plus
the edges each fix has to hold (release of a hold, the other workspaces, the queued path).
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest import mock
from uuid import uuid4

import pytest
from django.db import DatabaseError, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.ai import service, subjects, tasks, tools
from arkray.ai.models import Conversation, Question, QuestionStatus, WorkspaceKind
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.errors import RateLimitedError
from arkray.core.models import HoldSubject, LegalHold
from arkray.identity import services as identity_services
from arkray.identity.workspaces import resolve_workspace
from arkray.pipeline.models import Pipeline, Stage
from arkray.privacy import exports, staff
from tests.ai_fixtures import own
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory, UserFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _ai(ai_on: None) -> None:
    """AI on for every test here (tests.ai_fixtures.ai_on)."""


@pytest.fixture
def publish():
    with mock.patch("arkray.ai.tasks.answer_question.apply_async") as publish:
        yield publish


def hold(subject_type: HoldSubject, subject_id: Any, by: Any) -> LegalHold:
    return LegalHold.objects.create(
        subject_type=subject_type, subject_id=subject_id, reference="CASE-1", placed_by=by.pk
    )


def release(legal_hold: LegalHold, by: Any) -> None:
    LegalHold.objects.filter(pk=legal_hold.pk).update(released_at=timezone.now(), released_by=by.pk)


def stored(
    actor: Any,
    text: str,
    answer: dict[str, Any] | None = None,
    *,
    subject: Any = None,
    kind: str = WorkspaceKind.SELF,
    days_ago: int = 0,
) -> Question:
    """An answered question in a new conversation, finished `days_ago` days ago."""
    when = timezone.now() - timedelta(days=days_ago)
    conversation = Conversation.objects.create(
        actor=actor,
        workspace_kind=kind,
        subject=actor if kind == WorkspaceKind.SELF else subject,
        created_at=when,
        updated_at=when,
    )
    return Question.objects.create(
        conversation=conversation,
        actor=actor,
        text=text,
        status=QuestionStatus.ANSWERED,
        mode="llm",
        answer=answer or {"blocks": [], "sources": []},
        created_at=when,
        finished_at=when,
        expires_at=when + timedelta(minutes=2),
    )


def conversation_url(question: Question, workspace: str = "me") -> str:
    return f"/api/v1/workspaces/{workspace}/ask/conversations/{question.conversation_id}"


# --- M-5 (a): deleting a conversation under a legal hold ------------------------------------------
class TestDeletingUnderAHold:
    def test_the_held_users_conversation_is_kept_until_the_hold_is_released(self, user_a, admin):
        """The deletion answers 204 like any other (adversarial review M1: a 409 told the user
        they were held; tests/security/test_ask_hold_oracle.py): the conversation is kept,
        hidden from them, until the hold is released and housekeeping completes it."""
        question = stored(user_a, "What is my pipeline value?")
        legal_hold = hold(HoldSubject.USER, user_a.pk, admin)
        client = signed_in(user_a)

        assert client.delete(conversation_url(question)).status_code == 204
        assert client.get(conversation_url(question)).status_code == 404
        assert Question.objects.filter(pk=question.pk).exists()
        event = AuditEvent.objects.get(action=service.CONVERSATION_DELETED)
        assert (event.actor_id, event.target_type, event.target_id) == (
            user_a.pk,
            "ai_conversation",
            str(question.conversation_id),
        )
        # Ids and counts only, never the text; and that a hold kept it, never which.
        assert event.metadata == {"questions": 1, "retained": True}
        assert event.subject_user_id is None

        release(legal_hold, admin)
        service.housekeeping()
        assert not Conversation.objects.filter(pk=question.conversation_id).exists()

    def test_a_conversation_about_a_held_customer_is_kept(self, user_a, admin):
        lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
        named = stored(user_a, "What did Sunita Rao say about reagents?")
        deal = OpportunityFactory(lead=lead)
        cited = stored(
            user_a,
            "Which deal is closest to closing?",
            {
                "blocks": [{"type": "text", "parts": [{"ref": f"opportunity:{deal.pk}"}]}],
                "sources": [{"ref": f"opportunity:{deal.pk}", "label": "Analyser"}],
            },
        )
        unrelated = stored(user_a, "How many overdue tasks do I have?")
        legal_hold = hold(HoldSubject.LEAD, lead.pk, admin)
        client = signed_in(user_a)
        for question in (named, cited):
            # 204 as any deletion (adversarial review M1), and kept, hidden, as evidence.
            assert client.delete(conversation_url(question)).status_code == 204
            assert Question.objects.filter(pk=question.pk).exists()
        assert client.delete(conversation_url(unrelated)).status_code == 204
        assert not Question.objects.filter(pk=unrelated.pk).exists()

        release(legal_hold, admin)
        service.housekeeping()
        assert not Question.objects.filter(pk__in=[named.pk, cited.pk]).exists()

    def test_an_administrators_conversation_in_a_held_users_workspace_is_kept(self, user_a, admin):
        question = stored(admin, "How many overdue tasks?", subject=user_a, kind=WorkspaceKind.USER)
        hold(HoldSubject.USER, user_a.pk, admin)
        response = signed_in(admin).delete(conversation_url(question, str(user_a.pk)))
        assert response.status_code == 204  # as any deletion (adversarial review M1)
        assert Question.objects.filter(pk=question.pk).exists()

    def test_another_persons_conversation_is_still_a_404(self, user_a, user_b, admin):
        question = stored(user_a, "What is my pipeline value?")
        assert signed_in(user_b).delete(conversation_url(question)).status_code == 404
        assert Question.objects.filter(pk=question.pk).exists()


# --- M-5 (b): housekeeping and held customers -----------------------------------------------------
class TestHousekeepingAndHeldCustomers:
    def test_an_old_question_about_a_held_customer_survives_until_the_release(
        self, settings, user_a, admin
    ):
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
        named = stored(user_a, "What did Sunita Rao say?", days_ago=31)
        note = NoteFactory(lead=lead, created_by=user_a)
        cited = stored(
            user_a,
            "What was discussed last week?",
            {"blocks": [], "sources": [{"ref": f"note:{note.pk}", "label": "Visit"}]},
            days_ago=31,
        )
        unrelated = stored(user_a, "How many overdue tasks?", days_ago=31)
        legal_hold = hold(HoldSubject.LEAD, lead.pk, admin)

        assert service.housekeeping()["deleted_conversations"] == 1
        assert Question.objects.filter(pk__in=[named.pk, cited.pk]).count() == 2
        assert not Question.objects.filter(pk=unrelated.pk).exists()

        release(legal_hold, admin)
        service.housekeeping()
        assert not Question.objects.filter(pk__in=[named.pk, cited.pk]).exists()

    def test_an_old_question_in_a_recent_conversation_is_kept_too(self, settings, user_a, admin):
        """Questions are deleted one by one once older than the retention, even in a
        conversation still in use: the held customer's are kept all the same."""
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
        old = stored(user_a, "Tell me about Sunita Rao", days_ago=31)
        Conversation.objects.filter(pk=old.conversation_id).update(updated_at=timezone.now())
        legal_hold = hold(HoldSubject.LEAD, lead.pk, admin)
        assert service.housekeeping()["deleted_questions"] == 0
        assert Question.objects.filter(pk=old.pk).exists()
        release(legal_hold, admin)
        assert service.housekeeping()["deleted_questions"] == 1

    def test_a_held_users_workspace_is_kept_for_the_administrator_too(
        self, settings, user_a, admin
    ):
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        question = stored(
            admin, "Overdue tasks?", subject=user_a, kind=WorkspaceKind.USER, days_ago=31
        )
        hold(HoldSubject.USER, user_a.pk, admin)
        service.housekeeping()
        assert Question.objects.filter(pk=question.pk).exists()

    def test_held_conversations_reads_only_the_conversations_asked_about(self, user_a, admin):
        lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
        first = stored(user_a, "Sunita Rao?")
        second = stored(user_a, "Sunita Rao again?")
        hold(HoldSubject.LEAD, lead.pk, admin)
        assert subjects.held_conversations() == {first.conversation_id, second.conversation_id}
        assert subjects.held_conversations(among=[first.conversation_id]) == {first.conversation_id}
        assert subjects.held_conversations(among=[]) == set()


# --- L-5: pseudonymising a staff member -----------------------------------------------------------
class TestPseudonymisationRemovesConversationsAboutThem:
    def test_conversations_about_them_go_and_held_ones_stay(self, user_a, user_b, admin):
        in_their_workspace = service.submit(
            admin, resolve_workspace(admin, str(user_a.pk)), "How many overdue tasks?", None
        )
        assert in_their_workspace.status == "answered"
        naming = stored(
            admin,
            "How is the team doing?",
            {"blocks": [{"type": "text", "parts": [{"text": "Rahul Sharma leads."}]}]},
            kind=WorkspaceKind.ORGANIZATION,
        )
        asked_by_name = stored(user_b, "Is Rahul Sharma back from leave?")
        unrelated = stored(user_b, "How many overdue tasks?")
        lead = LeadFactory(owner=user_b, first_name="Sunita", last_name="Rao")
        kept = stored(user_b, "Did Rahul Sharma meet Sunita Rao?")
        hold(HoldSubject.LEAD, lead.pk, admin)
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)

        assert staff.preview(user_a.pk, operator_id=admin.pk).conversations == 3
        result = staff.pseudonymise(user_a.pk, operator_id=admin.pk)

        assert result.conversations == 3
        gone = [in_their_workspace, naming, asked_by_name]
        assert not Question.objects.filter(pk__in=[q.pk for q in gone]).exists()
        assert Question.objects.filter(pk__in=[unrelated.pk, kept.pk]).count() == 2
        left = json.dumps(list(Question.objects.values("text", "answer")), ensure_ascii=False)
        assert "Rahul Sharma" in left  # only in the conversation the hold keeps
        assert left.count("Rahul Sharma") == 1


# --- L-6: a staff member's own-data export --------------------------------------------------------
def test_a_staff_export_never_names_the_customers_their_answers_cited(user_a):
    lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
    stored(
        user_a,
        "Who should I call first?",
        {
            "blocks": [
                {"type": "text", "parts": [{"text": "Call "}, {"ref": f"lead:{lead.pk}"}]},
                {"type": "text", "parts": [{"text": "Her number is +91 98765 43210."}]},
            ],
            "sources": [{"ref": f"lead:{lead.pk}", "label": "Sunita Rao"}],
        },
    )
    document = exports.user_document(user_a.pk)
    [row] = document["ask_arkray"]
    assert row["question"] == "Who should I call first?"  # their own words
    assert "Call [another record]" in row["answer"]
    assert "Sunita" not in json.dumps(document)
    assert "98765" not in row["answer"]


def test_a_customer_export_still_names_the_customers_own_records(user_a):
    lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
    other = LeadFactory(owner=user_a, first_name="Other", last_name="Person")
    stored(
        user_a,
        "Sunita Rao or the other one?",
        {
            "blocks": [
                {
                    "type": "text",
                    "parts": [
                        {"ref": f"lead:{lead.pk}"},
                        {"text": ", "},
                        {"ref": f"lead:{other.pk}"},
                    ],
                }
            ],
            "sources": [
                {"ref": f"lead:{lead.pk}", "label": "Sunita Rao"},
                {"ref": f"lead:{other.pk}", "label": "Other Person"},
            ],
        },
    )
    document, _ = exports.lead_document(lead.pk)
    assert [row["answer"] for row in document["ask_arkray"]] == ["Sunita Rao, [another record]"]


# --- L-10: an archived pipeline with the name of an active one -----------------------------------
def pipeline(owner: Any, name: str, *, active: bool, key: str) -> Pipeline:
    found = Pipeline.objects.create(key=key, name=name, owner=owner, is_active=active)
    Stage.objects.create(
        pipeline=found,
        key="s1",
        name="Bid",
        position=10,
        probability=Decimal("20"),
        category="open",
    )
    return found


class TestAPipelineNameAnArchivedOneShares:
    def test_the_routed_question_is_answered_for_the_active_one(self, user_a):
        pipeline(user_a, "Government Tender", active=False, key="old1")
        pipeline(user_a, "Government Tender", active=True, key="new1")
        question = service.submit(
            user_a, own(user_a), "What is the value of my Government Tender pipeline?", None
        )
        assert (question.status, question.mode) == ("answered", "router")
        assert "Government Tender" in json.dumps(question.answer)

    def test_a_failing_routed_call_queues_the_question_instead_of_a_500(self, user_a, publish):
        failed = service.RoutedCallFailed("Routed tool get_pipeline_summary failed.")
        with mock.patch.object(service, "_run", side_effect=failed):
            response = signed_in(user_a).post(
                "/api/v1/workspaces/me/ask", {"question": "What is my pipeline value?"}
            )
            assert response.status_code == 201
            assert response.json()["status"] == "pending"
            assert publish.call_count == 1
            service.answer(response.json()["id"])  # the worker: the routed call fails again
        question = Question.objects.get(pk=response.json()["id"])
        assert (question.status, question.mode) == ("answered", "retrieval")


# --- L-11: tool output dropped by the budget is never a source ------------------------------------
@pytest.mark.django_db(transaction=True)
def test_a_result_the_budget_drops_leaves_no_citation(ai_on, crm_configuration, user_a, settings):
    from tests.ai_fixtures import index, note

    lead = LeadFactory(owner=user_a)
    note(user_a, lead, "The customer raised concerns about analyser calibration. " * 5)
    index()
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    found = tools.execute(ctx, "search_notes", {"query": "concerns about calibration"})
    assert not found.is_error  # the search finds it
    assert ctx.citations
    assert ctx.records

    settings.AI_TOOL_RESULTS_MAX_CHARS = 200  # smaller than one search_notes result
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    out = tools.execute(ctx, "search_notes", {"query": "concerns about calibration"})
    assert out.is_error
    assert (ctx.citations, ctx.records, ctx.facts, ctx.retrieved_chars) == ([], {}, [], 0)
    assert ctx.tools_used == ["search_notes"]  # the call itself is still recorded


def test_a_failing_tool_leaves_no_records_behind(user_a):
    lead = LeadFactory(owner=user_a)
    ctx = tools.ToolContext(scope=own(user_a), now=timezone.now())
    ctx.cite("lead", lead.pk, "Before")

    def cites_then_fails(context: tools.ToolContext, _: dict[str, Any]) -> Any:
        context.cite("lead", uuid4(), "Half done")
        context.count("Half", 1)
        raise RuntimeError("boom")

    broken = tools.Tool("broken", "", {}, cites_then_fails)
    with (
        mock.patch.object(tools, "TOOLS", (*tools.TOOLS, broken)),
        mock.patch.dict(tools._BY_NAME, {"broken": broken}),
    ):
        out = tools.execute(ctx, "broken", {})
    assert out.is_error
    assert list(ctx.records) == [f"lead:{lead.pk}"]
    assert ctx.facts == []


# --- L-12: a daily cap on the questions a worker answers ------------------------------------------
class TestDailyCap:
    def test_the_cap_refuses_queued_questions_not_routed_ones(self, settings, user_a, publish):
        settings.AI_MAX_QUESTIONS_PER_USER_PER_DAY = 2
        settings.AI_MAX_PENDING_PER_USER = 10
        for _ in range(2):
            assert (
                service.submit(user_a, own(user_a), "What did Mehta say?", None).status == "pending"
            )
        with pytest.raises(RateLimitedError) as refused:
            service.submit(user_a, own(user_a), "What did Mehta say?", None)
        assert 60 <= refused.value.retry_after <= 24 * 3600
        routed = service.submit(user_a, own(user_a), "What is my pipeline value?", None)
        assert routed.status == "answered"
        response = signed_in(user_a).post(
            "/api/v1/workspaces/me/ask", {"question": "What did Mehta say?"}
        )
        assert response.status_code == 429
        assert int(response["Retry-After"]) >= 60

    def test_questions_older_than_a_day_no_longer_count(self, settings, user_a):
        settings.AI_MAX_QUESTIONS_PER_USER_PER_DAY = 1
        settings.AI_MAX_PENDING_PER_USER = 10
        service.submit(user_a, own(user_a), "What did Mehta say?", None)
        with pytest.raises(RateLimitedError):
            service._check_daily_cap(user_a, timezone.now())
        service._check_daily_cap(user_a, timezone.now() + timedelta(days=1, minutes=1))

    def test_forgetting_the_conversations_does_not_reset_it(self, settings, user_a, publish):
        """Review P3-1 (the reviewer's probe, inverted): the cap counted stored questions,
        so deleting the conversations reset it."""
        settings.AI_MAX_QUESTIONS_PER_USER_PER_DAY = 2
        settings.AI_MAX_PENDING_PER_USER = 10
        asked = [service.submit(user_a, own(user_a), "What did Mehta say?", None) for _ in "ab"]
        client = signed_in(user_a)
        for question in asked:
            assert client.delete(conversation_url(question)).status_code == 204
        with pytest.raises(RateLimitedError):
            service.submit(user_a, own(user_a), "What did Mehta say?", None)
        events = AuditEvent.objects.filter(action=service.QUEUED_ACTION, actor_id=user_a.pk)
        assert sorted(e.metadata["question_id"] for e in events) == sorted(
            str(q.pk) for q in asked
        )  # ids only

    def test_zero_turns_it_off(self, settings, user_a):
        settings.AI_MAX_QUESTIONS_PER_USER_PER_DAY = 0
        settings.AI_MAX_PENDING_PER_USER = 10
        for _ in range(3):
            service.submit(user_a, own(user_a), "What did Mehta say?", None)


# --- L-13: the worker's audit event carries the support session ----------------------------------
class TestSupportSessionReachesTheWorker:
    def test_dispatch_sends_it_and_the_worker_records_it(self, admin, user_a, publish):
        session_id = uuid4()
        token = bind_context(
            ExecutionContext(correlation_id="req-1", support_session_id=str(session_id))
        )
        try:
            question = service.dispatch(
                service.submit(admin, resolve_workspace(admin, str(user_a.pk)), "Concerns?", None)
            )
        finally:
            reset_context(token)
        kwargs = publish.call_args.kwargs["kwargs"]
        assert kwargs == {"correlation_id": "req-1", "support_session_id": str(session_id)}

        worker = bind_context(ExecutionContext(correlation_id="task-1"))  # celery's prerun
        try:
            tasks.answer_question(str(question.pk), **kwargs)
        finally:
            reset_context(worker)
        event = AuditEvent.objects.get(action="ai.question", metadata__question_id=str(question.pk))
        assert event.support_session_id == session_id

    def test_without_a_session_nothing_new_is_sent_and_old_messages_still_run(
        self, user_a, publish
    ):
        question = service.dispatch(service.submit(user_a, own(user_a), "Concerns?", None))
        assert set(publish.call_args.kwargs["kwargs"]) == {"correlation_id"}
        tasks.answer_question(str(question.pk), "")  # a message from the previous release
        event = AuditEvent.objects.get(action="ai.question", metadata__question_id=str(question.pk))
        assert event.support_session_id is None


# --- Independent review of these fixes -----------------------------------------------------------
class TestRoutedCallDatabaseError:
    """Review P3-2: a database error in the routed call (a statement timeout) aborted the
    submitting transaction, and the queueing that followed failed with a 500."""

    def test_a_caught_database_error_still_queues_the_question(self, user_a, publish):
        def times_out(*_: Any, **__: Any) -> Any:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 / 0")  # aborts the transaction, as a timeout does
            except DatabaseError:
                pass  # as tools.execute does: the tool failed
            raise service.RoutedCallFailed("Routed tool get_pipeline_summary failed.")

        with mock.patch.object(service, "answer_routed", side_effect=times_out):
            response = signed_in(user_a).post(
                "/api/v1/workspaces/me/ask", {"question": "What is my pipeline value?"}
            )
        assert response.status_code == 201
        assert response.json()["status"] == "pending"
        assert publish.call_count == 1

    def test_an_uncaught_database_error_queues_it_too(self, user_a, publish):
        def fails(*_: Any, **__: Any) -> Any:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")

        with mock.patch.object(service, "answer_routed", side_effect=fails):
            question = service.submit(user_a, own(user_a), "What is my pipeline value?", None)
        assert question.status == "pending"
        assert Question.objects.filter(pk=question.pk).exists()


class TestStaffNameMatching:
    """Review P3-3: the name was matched as a substring ("anna lee" in "Hanna Leeds"), and
    the 120 s statement timeout outlived the web request that runs it."""

    def test_their_name_is_matched_as_whole_words(self, admin, user_b):
        anna = UserFactory(first_name="Anna", last_name="Lee", email="anna.lee@arkray.example")
        named = stored(user_b, "Did ANNA   LEE call the lab?")
        by_email = stored(user_b, "Write to anna.lee@arkray.example about it")
        other = stored(user_b, "Is Hanna Leeds the new lab manager?")
        other_email = stored(user_b, "Write to joanna.lee@arkray.example.org")
        identity_services.deactivate_user(actor_id=admin.pk, user_id=anna.pk)
        assert staff.pseudonymise(anna.pk, operator_id=admin.pk).conversations == 2
        assert not Question.objects.filter(pk__in=[named.pk, by_email.pk]).exists()
        assert Question.objects.filter(pk__in=[other.pk, other_email.pk]).count() == 2

    def test_regular_expression_characters_in_a_name_are_literal(self):
        pattern = subjects.word_pattern("A.B (x)")
        assert pattern == r"(^|[^[:alnum:]_])A\.B\s+\(x\)([^[:alnum:]_]|$)"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT %s ~* %s, %s ~* %s", ["met a.b (x) today", pattern, "AxB (x)", pattern]
            )
            assert cursor.fetchone() == (True, False)

    def test_the_statement_timeout_fits_in_the_web_request(self):
        assert int(staff.STATEMENT_TIMEOUT.removesuffix("s")) < 30  # gunicorn's timeout


ANSWER_TEXT = "The customer discussed reagent pricing and the service contract. " * 20


def bulk_old_questions(user_id: Any, conversations: int, per: int, days_ago: int) -> None:
    """Answered questions in the user's own conversations, `days_ago` days old (raw SQL)."""
    when = timezone.now() - timedelta(days=days_ago)
    with connection.cursor() as cursor:
        cursor.execute(
            "WITH conv AS ("
            " INSERT INTO ai_conversation"
            "  (id, actor_id, workspace_kind, subject_id, created_at, updated_at)"
            " SELECT gen_random_uuid(), %s, 'self', %s, %s, %s FROM generate_series(1, %s)"
            " RETURNING id)"
            " INSERT INTO ai_question (id, conversation_id, actor_id, text, status, answer,"
            "  error_code, mode, created_at, started_at, finished_at, expires_at)"
            " SELECT gen_random_uuid(), conv.id, %s, 'What did the customer say?', 'answered',"
            "  jsonb_build_object('blocks', jsonb_build_array(jsonb_build_object('type', 'text',"
            "   'parts', jsonb_build_array(jsonb_build_object('text', %s::text)))),"
            "   'sources', jsonb_build_array(jsonb_build_object("
            "    'ref', 'lead:' || gen_random_uuid(), 'label', 'x'))),"
            "  '', 'llm', %s, %s, %s, %s"
            " FROM conv, generate_series(1, %s)",
            [
                user_id,
                user_id,
                when,
                when,
                conversations,
                user_id,
                ANSWER_TEXT,
                when,
                when,
                when,
                when + timedelta(minutes=2),
                per,
            ],
        )


def matching_queries(queries: CaptureQueriesContext) -> list[str]:
    """The queries that matched conversations against held customers (the costly scans)."""
    return [q["sql"] for q in queries.captured_queries if "ILIKE ANY" in q["sql"]]


class TestHousekeepingStaysBoundedUnderHolds:
    """Review P2-3: every hour, a held user's whole history came back as due and was matched
    against every held customer, until the run hit the statement timeout and deleted
    nothing for anyone."""

    def test_a_held_users_history_is_never_matched_and_the_rest_is_deleted(
        self, settings, user_a, user_b, admin
    ):
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        bulk_old_questions(user_a.pk, 300, 10, 40)  # 3,000 questions a user hold keeps
        bulk_old_questions(user_b.pk, 5, 2, 40)  # due for deletion
        hold(HoldSubject.USER, user_a.pk, admin)
        for i in range(3):
            lead = LeadFactory(owner=admin, first_name=f"Customer{i}", last_name=f"Surname{i}")
            hold(HoldSubject.LEAD, lead.pk, admin)
        about_held = stored(user_b, "What did Customer1 Surname1 say?", days_ago=40)

        started = time.monotonic()
        with CaptureQueriesContext(connection) as queries:
            result = service.housekeeping()
        took = time.monotonic() - started

        assert result["deleted_conversations"] == 5
        assert result["deleted_questions"] == 10
        assert Question.objects.filter(actor_id=user_a.pk).count() == 3000
        assert Question.objects.filter(pk=about_held.pk).exists()
        scans = matching_queries(queries)
        assert len(scans) == 1  # only user B's 6 due conversations, in one batch
        assert took < 5, took  # was linear in the held history (3.98 s at 20,000)

    def test_a_conversation_found_held_is_not_matched_again_while_the_holds_stay(
        self, settings, user_b, admin
    ):
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        lead = LeadFactory(owner=admin, first_name="Sunita", last_name="Rao")
        legal_hold = hold(HoldSubject.LEAD, lead.pk, admin)
        kept = stored(user_b, "What did Sunita Rao say?", days_ago=40)
        service.housekeeping()
        with CaptureQueriesContext(connection) as queries:
            assert service.housekeeping()["deleted_conversations"] == 0
        assert matching_queries(queries) == []  # remembered, not matched again
        assert Question.objects.filter(pk=kept.pk).exists()

        release(legal_hold, admin)  # other holds now: matched afresh, and deleted
        assert service.housekeeping()["deleted_conversations"] == 1

    def test_due_conversations_are_deleted_in_committed_batches(
        self, settings, user_b, admin, monkeypatch
    ):
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        monkeypatch.setattr(service, "HOUSEKEEPING_BATCH", 2)
        lead = LeadFactory(owner=admin, first_name="Sunita", last_name="Rao")
        hold(HoldSubject.LEAD, lead.pk, admin)
        bulk_old_questions(user_b.pk, 5, 1, 40)
        kept = stored(user_b, "What did Sunita Rao say?", days_ago=40)
        with CaptureQueriesContext(connection) as queries:
            assert service.housekeeping()["deleted_conversations"] == 5
        assert len(matching_queries(queries)) == 3  # 6 due conversations, 2 per batch
        savepoints = [q for q in queries.captured_queries if "SAVEPOINT" in q["sql"]]
        assert len(savepoints) >= 3  # each batch in its own transaction (a savepoint in a test)
        assert Question.objects.filter(pk=kept.pk).exists()

    def test_held_conversations_never_matches_what_a_user_hold_keeps(self, user_a, admin):
        """The rule pseudonymising uses: a held user's conversations are kept without matching
        them against the held customers. (Forgetting one conversation matches it all the
        same, ai.subjects.hold_keeps: the same work whatever the answer, adversarial review
        M1.)"""
        lead = LeadFactory(owner=admin, first_name="Sunita", last_name="Rao")
        hold(HoldSubject.LEAD, lead.pk, admin)
        hold(HoldSubject.USER, user_a.pk, admin)
        mine = [stored(user_a, "What did Sunita Rao say?").conversation_id for _ in "abc"]
        with CaptureQueriesContext(connection) as queries:
            assert subjects.held_conversations(among=mine) == set(mine)
        assert matching_queries(queries) == []
