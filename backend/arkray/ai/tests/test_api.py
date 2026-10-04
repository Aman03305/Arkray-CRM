"""The Ask Arkray HTTP API: workspace-scoped, the caller's own questions only, CSRF on
every write, typed answers."""

from __future__ import annotations

from typing import Any
from unittest import mock
from uuid import uuid4

import pytest

from arkray.ai import service
from arkray.ai.models import Question, QuestionStatus
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db


def ask_url(workspace: str = "me", tail: str = "") -> str:
    return f"/api/v1/workspaces/{workspace}/ask" + (f"/{tail}" if tail else "")


@pytest.fixture(autouse=True)
def publish():
    with mock.patch("arkray.ai.tasks.answer_question.apply_async") as publish:
        yield publish


def post(client: Any, question: str, workspace: str = "me", **extra: Any) -> Any:
    return client.post(ask_url(workspace), {"question": question, **extra}, format="json")


class TestAsking:
    def test_a_routed_question_is_answered_in_the_response(self, user_a, golden):
        response = post(signed_in(user_a), "What is my pipeline value?")
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "answered"
        assert body["error"] is None
        assert body["answer"]["provenance"]["mode"] == "router"
        assert body["answer"]["blocks"][0]["parts"][0]["text"].startswith(
            "Your pipeline value is ₹10,00,000"
        )

    def test_an_open_question_is_pending_then_polled(self, user_a, publish):
        client = signed_in(user_a)
        body = post(client, "What did Dr Mehta say about pricing?").json()
        assert (body["status"], body["answer"]) == ("pending", None)
        assert publish.call_count == 1
        service.answer(body["id"])
        polled = client.get(ask_url(tail=f"questions/{body['id']}")).json()
        assert polled["status"] == "answered"
        assert polled["answer"]["provenance"]["mode"] == "retrieval"

    def test_a_follow_up_continues_the_conversation(self, user_a):
        client = signed_in(user_a)
        first = post(client, "pipeline value").json()
        second = post(client, "lead count", conversation_id=first["conversation_id"]).json()
        assert second["conversation_id"] == first["conversation_id"]
        conversation = client.get(ask_url(tail=f"conversations/{first['conversation_id']}")).json()
        assert [q["question"] for q in conversation["questions"]] == [
            "pipeline value",
            "lead count",
        ]
        listed = client.get(ask_url(tail="conversations")).json()
        assert [(c["id"], c["title"]) for c in listed] == [
            (first["conversation_id"], "pipeline value")
        ]

    def test_forgetting_a_conversation(self, user_a):
        client = signed_in(user_a)
        first = post(client, "pipeline value").json()
        url = ask_url(tail=f"conversations/{first['conversation_id']}")
        assert client.delete(url).status_code == 204
        assert client.get(url).status_code == 404
        assert not Question.objects.filter(pk=first["id"]).exists()

    def test_status(self, user_a):
        assert signed_in(user_a).get(ask_url()).json() == {"enabled": True, "summaries": "none"}

    def test_ai_disabled(self, settings, user_a):
        settings.AI_ENABLED = False
        response = post(signed_in(user_a), "pipeline value")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "ai_disabled"
        assert signed_in(user_a).get(ask_url()).json()["enabled"] is False

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"question": ""},
            {"question": "x" * 1001},
            {"question": "pipeline value", "owner_id": str(uuid4())},
            {"question": "pipeline value", "workspace": "all"},
            {"question": ["pipeline value"]},
            {"question": "pipeline value", "conversation_id": "not-a-uuid"},
        ],
    )
    def test_invalid_input_is_a_400(self, user_a, payload):
        response = signed_in(user_a).post(ask_url(), payload, format="json")
        assert response.status_code == 400

    def test_csrf_is_required_to_ask_and_to_forget(self, csrf_client, user_a):
        client = signed_in(user_a, csrf_client)
        assert post(client, "pipeline value").status_code == 403
        assert client.delete(ask_url(tail=f"conversations/{uuid4()}")).status_code == 403

    def test_questions_are_throttled(self, settings, user_a):
        from django.core.cache import cache
        from rest_framework.throttling import ScopedRateThrottle

        rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "ask": "2/min"}
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            client = signed_in(user_a)
            codes = [post(client, "pipeline value").status_code for _ in range(3)]
            assert codes == [201, 201, 429]
            assert client.get(ask_url()).status_code == 200  # reading the status isn't asking


class TestIsolation:
    def test_another_users_question_and_conversation_are_404(self, user_a, user_b):
        mine = post(signed_in(user_a), "pipeline value").json()
        other = signed_in(user_b)
        assert other.get(ask_url(tail=f"questions/{mine['id']}")).status_code == 404
        conversation = ask_url(tail=f"conversations/{mine['conversation_id']}")
        assert other.get(conversation).status_code == 404
        assert other.delete(conversation).status_code == 404
        assert post(other, "lead count", conversation_id=mine["conversation_id"]).status_code == 404
        assert other.get(ask_url(tail="conversations")).json() == []

    def test_a_workspace_switch_never_shows_the_other_workspaces_questions(
        self, admin, user_a, user_b
    ):
        client = signed_in(admin)
        in_rahul = post(client, "pipeline value", workspace=str(user_a.pk)).json()
        priya = str(user_b.pk)
        assert client.get(ask_url(priya, f"questions/{in_rahul['id']}")).status_code == 404
        assert client.get(ask_url(priya, "conversations")).json() == []
        assert client.get(ask_url("me", f"questions/{in_rahul['id']}")).status_code == 404
        assert client.get(ask_url("all", f"questions/{in_rahul['id']}")).status_code == 404
        assert client.get(ask_url(str(user_a.pk), f"questions/{in_rahul['id']}")).status_code == 200

    def test_a_sales_user_cannot_open_another_workspace(self, user_a, user_b):
        client = signed_in(user_a)
        assert post(client, "pipeline value", workspace=str(user_b.pk)).status_code == 404
        assert post(client, "pipeline value", workspace="all").status_code == 404
        assert client.get(ask_url(str(user_b.pk))).status_code == 404

    def test_a_role_without_ai_query_is_refused(self, user_a):
        from arkray.identity.policy import ROLE_CAPABILITIES, Capability

        caps = {
            **ROLE_CAPABILITIES,
            "sales_user": ROLE_CAPABILITIES["sales_user"] - {Capability.AI_QUERY},
        }
        with mock.patch("arkray.identity.policy.ROLE_CAPABILITIES", caps):
            response = post(signed_in(user_a), "pipeline value")
        assert response.status_code == 403

    def test_an_expired_pending_question_reads_as_timed_out(self, user_a):
        from datetime import timedelta

        from django.utils import timezone

        client = signed_in(user_a)
        body = post(client, "Why did we lose?").json()
        Question.objects.filter(pk=body["id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        polled = client.get(ask_url(tail=f"questions/{body['id']}")).json()
        assert (polled["status"], polled["error"]) == ("failed", "timeout")
        assert Question.objects.get(pk=body["id"]).status == QuestionStatus.FAILED


def test_a_deactivated_actor_is_signed_out(admin, user_a):
    from arkray.identity import services as identity_services

    client = signed_in(user_a)
    identity_services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
    assert post(client, "pipeline value").status_code == 401
