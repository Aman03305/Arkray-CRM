"""Adversarial review of 73ff1e3, M1: deleting an Ask Arkray conversation told the asker
whether a legal hold kept it (docs/privacy.md#legal-holds).

`DELETE .../ask/conversations/{id}` answered 409 `legal_hold` when the conversation's text
named any held customer in the whole organisation (the hold match reads every hold, and
matches the caller's own question text), else 204: asking about candidate names and
deleting told a sales user whether a colleague's customer exists and is under a legal or
data-request hold, and told a held user that they are held.

Deleting one's own conversation now always answers 204, and the conversation is gone for
them everywhere (list, detail, questions, continuing it, a second deletion), whatever the
holds. One that no hold keeps is deleted at once, as before; one a hold keeps (its asker's or
its workspace user's hold, or a held customer it touches: ai.subjects, the erasure's own
matching, unchanged) is kept as a tombstone (`forgotten_at`), held evidence for subject-access
exports and erasures, until the hourly housekeeping finds no hold keeping it any more and
deletes it. The audit's probes asserted the oracle; these are those probes inverted, plus the
edges the tombstone has to hold. Races with placing a hold: tests/security/test_hold_races.py.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest import mock
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.ai import service
from arkray.ai.models import Conversation, Question, QuestionStatus, WorkspaceKind
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.models import HoldSubject, LegalHold
from arkray.identity import services as identity_services
from arkray.privacy import exports, services, staff
from tests.factories import LeadFactory, OpportunityFactory
from tests.helpers import signed_in

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ai_on")]

ZELDA = "Do we have a customer called Zelda Quillfeather?"
YOLANDA = "Do we have a customer called Yolanda Fernsby?"
# Headers that differ between any two responses (the request's own id, and its time).
PER_REQUEST = {"x-request-id", "date", "expires"}


@pytest.fixture(autouse=True)
def publish():
    with mock.patch("arkray.ai.tasks.answer_question.apply_async") as publish:
        yield publish


# --- helpers ---------------------------------------------------------------------------------
def hold(subject_type: HoldSubject, subject_id: Any, by: Any) -> LegalHold:
    return LegalHold.objects.create(
        subject_type=subject_type, subject_id=subject_id, reference="CASE-M1", placed_by=by.pk
    )


def release(legal_hold: LegalHold, by: Any) -> None:
    LegalHold.objects.filter(pk=legal_hold.pk).update(released_at=timezone.now(), released_by=by.pk)


def ask_url(workspace: str = "me", tail: str = "") -> str:
    return f"/api/v1/workspaces/{workspace}/ask" + (f"/{tail}" if tail else "")


def conversation_url(conversation_id: Any, workspace: str = "me") -> str:
    return ask_url(workspace, f"conversations/{conversation_id}")


def asked(client: Any, text: str, workspace: str = "me", **extra: Any) -> dict[str, Any]:
    """Ask over the API: the question is stored (pending: no worker runs here)."""
    response = client.post(ask_url(workspace), {"question": text, **extra}, format="json")
    assert response.status_code == 201, response.content
    body: dict[str, Any] = response.json()
    return body


def stored(
    actor: Any,
    text: str,
    answer: dict[str, Any] | None = None,
    *,
    subject: Any = None,
    kind: str = WorkspaceKind.SELF,
    days_ago: int = 0,
    conversation: Conversation | None = None,
) -> Question:
    """An answered question, finished `days_ago` days ago, in a new conversation (or in
    `conversation`)."""
    when = timezone.now() - timedelta(days=days_ago)
    if conversation is None:
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


def as_seen(response: Any) -> tuple[int, bytes, tuple[tuple[str, str], ...]]:
    """Everything a caller sees of a response but what differs between any two requests."""
    headers = sorted(
        (name.lower(), value)
        for name, value in response.headers.items()
        if name.lower() not in PER_REQUEST
    )
    return response.status_code, response.content, tuple(headers)


def error(response: Any) -> tuple[int, dict[str, Any]]:
    """An error response without its request id."""
    body = response.json()["error"]
    body.pop("request_id", None)
    return response.status_code, body


def tombstoned(conversation_id: Any) -> bool:
    return Conversation.objects.filter(pk=conversation_id, forgotten_at__isnull=False).exists()


def gone(conversation_id: Any) -> bool:
    return not Conversation.objects.filter(pk=conversation_id).exists()


def forgotten_for(client: Any, body: dict[str, Any], workspace: str = "me") -> None:
    """The conversation is gone for its asker everywhere, as a deleted one is."""
    conversation_id = body["conversation_id"]
    listed = client.get(ask_url(workspace, "conversations")).json()
    assert conversation_id not in [c["id"] for c in listed]
    assert client.get(conversation_url(conversation_id, workspace)).status_code == 404
    assert client.get(ask_url(workspace, f"questions/{body['id']}")).status_code == 404
    continued = client.post(
        ask_url(workspace),
        {"question": "And what else?", "conversation_id": conversation_id},
        format="json",
    )
    assert continued.status_code == 404


# --- the audit's scenario ----------------------------------------------------------------------
class TestNoOracle:
    def test_a_colleagues_held_customer_and_a_stranger_answer_alike(self, user_a, user_b, admin):
        """The reviewer's probe: Zelda Quillfeather is user B's customer, under a hold; user
        A can't see her. A asks about her and about a name nobody holds, then deletes both."""
        zelda = LeadFactory(
            owner=user_b, first_name="Zelda", last_name="Quillfeather", email="zelda.q@example.org"
        )
        hold(HoldSubject.LEAD, zelda.pk, admin)
        client = signed_in(user_a)
        held = asked(client, ZELDA)
        control = asked(client, YOLANDA)

        deleted = [client.delete(conversation_url(q["conversation_id"])) for q in (held, control)]
        assert deleted[0].status_code == 204, deleted[0].content
        assert as_seen(deleted[0]) == as_seen(deleted[1])
        # Gone for A everywhere, both of them, and a second deletion is the same 404.
        for body in (held, control):
            forgotten_for(client, body)
        again = [client.delete(conversation_url(q["conversation_id"])) for q in (held, control)]
        assert error(again[0]) == error(again[1]) == error(client.delete(conversation_url(uuid4())))
        assert again[0].status_code == 404

        # Internally: the held one is kept as evidence, the other deleted.
        assert tombstoned(held["conversation_id"])
        assert Question.objects.get(pk=held["id"]).text == ZELDA
        assert gone(control["conversation_id"])

    def test_the_identifiers_the_probe_used_answer_alike_too(self, user_a, user_b, admin):
        zelda = LeadFactory(
            owner=user_b,
            first_name="Zelda",
            last_name="Quillfeather",
            email="zelda.q@example.org",
            phone="+91 98765 43210",
        )
        hold(HoldSubject.LEAD, zelda.pk, admin)
        client = signed_in(user_a)
        seen = set()
        for text in (
            "Any history with zelda.q@example.org please",
            "Who has the number +91 98765 43210?",
            "Compare Asha Verma, Zelda Quillfeather, Ravi Nair and Joy Das",
            "Compare Asha Verma, Ravi Nair and Joy Das",
        ):
            body = asked(client, text)
            seen.add(as_seen(client.delete(conversation_url(body["conversation_id"]))))
        assert len(seen) == 1
        assert next(iter(seen))[0] == 204

    def test_deleting_does_the_same_work_whether_or_not_a_hold_keeps_it(
        self, user_a, user_b, admin
    ):
        """No extra or missing step on either path (the time a deletion takes is no oracle
        either): the same statements, a tombstone written where a row is deleted. Also for a
        held asker, whose own hold used to skip matching the held customers."""
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        hold(HoldSubject.USER, user_b.pk, admin)
        warm_up = stored(user_a, "How many overdue tasks?")
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), warm_up.conversation_id)
        counts = []
        for actor, text in ((user_a, ZELDA), (user_a, YOLANDA), (user_b, YOLANDA)):
            question = stored(actor, text)
            with CaptureQueriesContext(connection) as queries:
                service.forget_conversation(
                    actor, AccessScope.own(actor.pk), question.conversation_id
                )
            counts.append(len(queries.captured_queries))
        assert counts[0] == counts[1] == counts[2], counts

    def test_another_persons_conversation_stays_a_404_held_or_not(self, user_a, user_b, admin):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        live = stored(user_a, ZELDA)
        forgotten = stored(user_a, "What did Zelda Quillfeather order?")
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), forgotten.conversation_id)
        other = signed_in(user_b)
        missing = error(other.delete(conversation_url(uuid4())))
        for question in (live, forgotten):
            assert error(other.delete(conversation_url(question.conversation_id))) == missing
        assert not tombstoned(live.conversation_id)
        assert tombstoned(forgotten.conversation_id)


# --- what a hold keeps -------------------------------------------------------------------------
class TestWhatAHoldKeeps:
    def test_a_conversation_about_the_askers_own_held_customer(self, user_a, admin):
        lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
        deal = OpportunityFactory(lead=lead)
        hold(HoldSubject.LEAD, lead.pk, admin)
        client = signed_in(user_a)
        named = asked(client, "What did Sunita Rao say about reagents?")
        cited = stored(
            user_a,
            "Which deal is closest to closing?",
            {
                "blocks": [{"type": "paragraph", "parts": [{"ref": f"opportunity:{deal.pk}"}]}],
                "sources": [{"ref": f"opportunity:{deal.pk}", "label": "Analyser"}],
            },
        )
        for conversation_id in (named["conversation_id"], cited.conversation_id):
            assert client.delete(conversation_url(conversation_id)).status_code == 204
            assert tombstoned(conversation_id)
        forgotten_for(client, named)

    def test_a_held_asker_is_not_told_they_are_held(self, user_a, admin):
        hold(HoldSubject.USER, user_a.pk, admin)
        client = signed_in(user_a)
        body = asked(client, "How many overdue tasks do I have, and why?")
        response = client.delete(conversation_url(body["conversation_id"]))
        assert (response.status_code, response.content) == (204, b"")
        forgotten_for(client, body)
        assert tombstoned(body["conversation_id"])

    def test_an_administrators_conversation_in_a_held_users_workspace(self, user_a, admin):
        hold(HoldSubject.USER, user_a.pk, admin)
        client = signed_in(admin)
        workspace = str(user_a.pk)
        body = asked(client, "Which of these leads went cold, and why?", workspace)
        assert (
            client.delete(conversation_url(body["conversation_id"], workspace)).status_code == 204
        )
        forgotten_for(client, body, workspace)
        assert tombstoned(body["conversation_id"])

    def test_a_conversation_about_a_held_and_an_unheld_customer_is_kept_whole(
        self, user_a, user_b, admin
    ):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        LeadFactory(owner=user_a, first_name="Yolanda", last_name="Fernsby")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        client = signed_in(user_a)
        first = asked(client, YOLANDA)
        second = stored(
            user_a,
            ZELDA,
            conversation=Conversation.objects.get(pk=first["conversation_id"]),
        )
        assert client.delete(conversation_url(first["conversation_id"])).status_code == 204
        assert tombstoned(first["conversation_id"])
        kept = Question.objects.filter(conversation_id=first["conversation_id"])
        assert {str(pk) for pk in kept.values_list("pk", flat=True)} == {
            first["id"],
            str(second.pk),
        }

    def test_without_a_hold_it_is_deleted_at_once(self, user_a, admin):
        lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
        released = hold(HoldSubject.LEAD, lead.pk, admin)
        release(released, admin)
        client = signed_in(user_a)
        body = asked(client, "What did Sunita Rao say about reagents?")
        assert client.delete(conversation_url(body["conversation_id"])).status_code == 204
        assert gone(body["conversation_id"])
        assert not Question.objects.filter(pk=body["id"]).exists()


# --- what the asker can still tell apart: nothing ------------------------------------------------
class TestNothingLingers:
    def test_a_kept_conversations_pending_question_no_longer_counts(
        self, settings, user_a, user_b, admin
    ):
        """A deleted conversation's pending question goes with it: one kept by a hold must
        not keep counting against the asker's pending questions (a 429 on the next question
        would tell them a hold kept it)."""
        settings.AI_MAX_PENDING_PER_USER = 1
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        client = signed_in(user_a)
        for text in (ZELDA, YOLANDA):
            body = asked(client, text)
            assert client.delete(conversation_url(body["conversation_id"])).status_code == 204
        asked(client, "What did Dr Mehta say about pricing?")

    def test_the_worker_never_answers_a_kept_conversations_question(self, user_a, user_b, admin):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        client = signed_in(user_a)
        body = asked(client, ZELDA)
        client.delete(conversation_url(body["conversation_id"]))
        service.answer(body["id"])
        question = Question.objects.get(pk=body["id"])
        assert (question.status, question.answer) == (QuestionStatus.FAILED, {})

    def test_a_question_written_into_a_kept_conversation_later_is_failed_by_the_worker(
        self, settings, user_a, user_b, admin
    ):
        """Independent review of the M1 fix, AI-3: a pod of the previous release (which
        doesn't know tombstones) can still add a question to one during a rolling deploy.
        The worker fails it `forgotten`, as forget_conversation fails the ones it finds: left
        pending, it counted against the asker's pending questions until it expired (a 429
        on their next question)."""
        settings.AI_MAX_PENDING_PER_USER = 1
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        kept = stored(user_a, ZELDA)
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), kept.conversation_id)
        now = timezone.now()
        late = Question.objects.create(  # as the previous release's submit writes it
            conversation_id=kept.conversation_id,
            actor=user_a,
            text="And her last order?",
            created_at=now,
            expires_at=now + timedelta(seconds=settings.AI_QUESTION_TIMEOUT_S),
        )
        service.answer(late.pk)
        late.refresh_from_db()
        assert (late.status, late.error_code, late.answer) == (
            QuestionStatus.FAILED,
            service.FORGOTTEN,
            {},
        )
        assert late.finished_at is not None
        asked(signed_in(user_a), "What did Dr Mehta say about pricing?")  # not a 429

    def test_both_paths_are_audited_with_ids_only(self, user_a, user_b, admin):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        client = signed_in(user_a)
        held, control = asked(client, ZELDA), asked(client, YOLANDA)
        for body in (held, control):
            client.delete(conversation_url(body["conversation_id"]))
        events = {
            event.target_id: event
            for event in AuditEvent.objects.filter(action=service.CONVERSATION_DELETED)
        }
        assert set(events) == {held["conversation_id"], control["conversation_id"]}
        for body, retained in ((held, True), (control, False)):
            event = events[body["conversation_id"]]
            assert (event.actor_id, event.target_type) == (user_a.pk, "ai_conversation")
            # For the compliance trail (no screen shows this action's metadata): whether a
            # hold kept it, never which hold, customer or words.
            assert event.metadata == {"questions": 1, "retained": retained}
            assert event.subject_user_id is None


# --- housekeeping ------------------------------------------------------------------------------
class TestHousekeeping:
    def test_a_released_hold_completes_the_deletion_at_the_next_run(self, user_a, user_b, admin):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        legal_hold = hold(HoldSubject.LEAD, zelda.pk, admin)
        question = stored(user_a, ZELDA)
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), question.conversation_id)

        service.housekeeping()
        assert tombstoned(question.conversation_id)  # still held: kept

        release(legal_hold, admin)
        assert service.housekeeping()["deleted_conversations"] == 1
        assert gone(question.conversation_id)
        assert not Question.objects.filter(pk=question.pk).exists()

    def test_a_tombstone_a_user_hold_keeps_goes_once_released(self, user_a, admin):
        legal_hold = hold(HoldSubject.USER, user_a.pk, admin)
        question = stored(user_a, "How many overdue tasks?")
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), question.conversation_id)
        service.housekeeping()
        assert tombstoned(question.conversation_id)
        release(legal_hold, admin)
        service.housekeeping()
        assert gone(question.conversation_id)

    def test_retention_keeps_a_held_tombstone_whole_and_purges_the_rest(
        self, settings, user_a, user_b, admin
    ):
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        legal_hold = hold(HoldSubject.LEAD, zelda.pk, admin)
        own = AccessScope.own(user_a.pk)
        old_held = stored(user_a, ZELDA, days_ago=40)
        newer = stored(user_a, "And her last order?", conversation=old_held.conversation)
        old_unheld = stored(user_a, YOLANDA, days_ago=40)
        recent_unheld = stored(user_a, "How many overdue tasks?")
        for question in (old_held, recent_unheld):
            service.forget_conversation(user_a, own, question.conversation_id)
        assert tombstoned(old_held.conversation_id)
        assert gone(recent_unheld.conversation_id)  # no hold: deleted at once

        result = service.housekeeping()
        assert result["deleted_conversations"] == 1  # the old conversation nobody deleted
        assert gone(old_unheld.conversation_id)
        # Held: kept whole, its 40-day-old question included, past the retention period.
        assert Question.objects.filter(pk__in=[old_held.pk, newer.pk]).count() == 2

        release(legal_hold, admin)
        service.housekeeping()
        assert gone(old_held.conversation_id)

    def test_a_recently_forgotten_conversation_goes_without_waiting_for_the_retention(
        self, settings, user_a, user_b, admin
    ):
        settings.AI_CONVERSATION_RETENTION_DAYS = 30
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        legal_hold = hold(HoldSubject.LEAD, zelda.pk, admin)
        question = stored(user_a, ZELDA)
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), question.conversation_id)
        release(legal_hold, admin)
        service.housekeeping()
        assert gone(question.conversation_id)


# --- held evidence: exports, erasure, pseudonymisation -------------------------------------------
class TestHeldEvidence:
    def test_the_held_customers_export_still_holds_the_forgotten_conversation(
        self, user_a, user_b, admin
    ):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        question = stored(user_a, ZELDA)
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), question.conversation_id)
        document, _ = exports.lead_document(zelda.pk)
        assert [row["question"] for row in document["ask_arkray"]] == [ZELDA]

    def test_an_erasure_after_the_release_removes_it(self, user_a, user_b, admin):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        legal_hold = hold(HoldSubject.LEAD, zelda.pk, admin)
        question = stored(user_a, ZELDA)
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), question.conversation_id)
        release(legal_hold, admin)
        assert services.erase(zelda.pk, operator_id=admin.pk).conversations == 1
        assert gone(question.conversation_id)

    def test_an_erasure_of_another_customer_keeps_what_a_hold_keeps(self, user_a, user_b, admin):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        yolanda = LeadFactory(owner=user_a, first_name="Yolanda", last_name="Fernsby")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        question = stored(user_a, "Compare Zelda Quillfeather and Yolanda Fernsby")
        service.forget_conversation(user_a, AccessScope.own(user_a.pk), question.conversation_id)
        assert services.erase(yolanda.pk, operator_id=admin.pk).conversations == 0
        assert tombstoned(question.conversation_id)

    def test_pseudonymising_the_asker_keeps_a_held_tombstone(self, user_a, user_b, admin):
        zelda = LeadFactory(owner=user_b, first_name="Zelda", last_name="Quillfeather")
        hold(HoldSubject.LEAD, zelda.pk, admin)
        held = stored(user_a, ZELDA)
        unheld = stored(user_a, "How many overdue tasks?")
        own = AccessScope.own(user_a.pk)
        service.forget_conversation(user_a, own, held.conversation_id)
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        staff.pseudonymise(user_a.pk, operator_id=admin.pk)
        assert tombstoned(held.conversation_id)
        assert gone(unheld.conversation_id)
