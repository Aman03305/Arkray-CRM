"""Ask Arkray API views: thin HTTP adapters over ai.service (docs/rag-architecture.md).

Nested under /api/v1/workspaces/{workspace}/ like every CRM route, and requiring the
`ai.query` capability. The workspace resolves to an AccessScope first (404 for workspaces
the caller may not open, before anything else is read); questions and conversations are
then looked up among *the caller's own* conversations *in that workspace*, so an id from
another person, or from another workspace, is a 404 exactly like an id that doesn't exist.

POST .../ask             ask (201; answered at once by the router, else pending)
GET  .../ask             what Ask Arkray can do right now
GET  .../ask/questions/{id}           one question (poll until answered or failed)
GET  .../ask/conversations            the caller's recent conversations here
GET  .../ask/conversations/{id}       one conversation with its questions
DELETE .../ask/conversations/{id}     forget it (with its questions and answers)

Listed in tests/authz_matrix.py. Questions are rate limited per user (API_THROTTLE_ASK)
on top of the PostgreSQL-counted bulkheads in ai.service.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db.models import OuterRef, Subquery
from django.db.models.functions import Substr
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle, ScopedRateThrottle, UserRateThrottle

from arkray.core.api import ApiView, validated
from arkray.core.errors import NotFoundError, PermissionDeniedError
from arkray.identity.models import User
from arkray.identity.permissions import requires
from arkray.identity.policy import Capability
from arkray.identity.workspaces import resolve_workspace
from arkray.leads.api.views import NOT_FOUND

from .. import service
from ..models import Question
from . import serializers as s

TITLE_LENGTH = 80


def _actor(request: Request) -> User:
    user = request.user
    if not isinstance(user, User):  # unreachable behind the permission classes
        raise PermissionDeniedError()
    return user


class AskView(ApiView):
    """Ask a question about this workspace, or read what Ask Arkray can do right now."""

    permission_classes = [requires(Capability.AI_QUERY)]
    throttle_scope = "ask"

    def get_throttles(self) -> list[BaseThrottle]:
        # Only asking spends the ask budget; reading the status doesn't.
        if self.request.method == "POST":
            return [UserRateThrottle(), ScopedRateThrottle()]
        return [UserRateThrottle()]

    @extend_schema(
        operation_id="ask_status", responses={200: s.AskStatusSerializer, 404: NOT_FOUND}
    )
    def get(self, request: Request, workspace: str) -> Response:
        resolve_workspace(_actor(request), workspace)
        return Response(s.AskStatusSerializer(service.status()).data)

    @extend_schema(
        operation_id="ask",
        request=s.AskInputSerializer,
        responses={
            201: s.QuestionSerializer,
            404: NOT_FOUND,
            429: OpenApiResponse(description="Too many questions, or one is still pending."),
            503: OpenApiResponse(description="Ask Arkray is turned off or busy."),
        },
    )
    def post(self, request: Request, workspace: str) -> Response:
        actor = _actor(request)
        scope = resolve_workspace(actor, workspace)
        data = validated(s.AskInputSerializer, request.data)
        question = service.submit(actor, scope, data["question"], data.get("conversation_id"))
        service.dispatch(question)
        return Response(s.QuestionSerializer(question).data, status=status.HTTP_201_CREATED)


class QuestionView(ApiView):
    """One of the caller's questions in this workspace (poll until answered or failed)."""

    permission_classes = [requires(Capability.AI_QUERY)]

    @extend_schema(
        operation_id="ask_question", responses={200: s.QuestionSerializer, 404: NOT_FOUND}
    )
    def get(self, request: Request, workspace: str, question_id: UUID) -> Response:
        actor = _actor(request)
        scope = resolve_workspace(actor, workspace)
        question = (
            Question.objects.filter(
                pk=question_id,
                actor_id=actor.pk,
                conversation__in=service.conversations_in(actor, scope),
            )
            .only(*_QUESTION_FIELDS)
            .first()
        )
        if question is None:
            raise NotFoundError()
        service.expire_if_overdue(question)
        question.answer = service.visible_answer(question.answer, scope)
        return Response(s.QuestionSerializer(question).data)


_QUESTION_FIELDS = (
    "id",
    "conversation_id",
    "text",
    "status",
    "error_code",
    "answer",
    "created_at",
    "finished_at",
    "expires_at",
)


class ConversationListView(ApiView):
    """The caller's most recent conversations in this workspace (at most 20)."""

    permission_classes = [requires(Capability.AI_QUERY)]

    @extend_schema(
        operation_id="ask_conversations",
        responses={200: s.ConversationSummarySerializer(many=True), 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str) -> Response:
        actor = _actor(request)
        scope = resolve_workspace(actor, workspace)
        first_question = Question.objects.filter(conversation_id=OuterRef("pk")).order_by(
            "created_at"
        )
        conversations = (
            service.conversations_in(actor, scope)
            .annotate(title=Substr(Subquery(first_question.values("text")[:1]), 1, TITLE_LENGTH))
            .order_by("-updated_at", "-id")[: service.CONVERSATION_LIST_LIMIT]
        )
        return Response(s.ConversationSummarySerializer(conversations, many=True).data)


class ConversationView(ApiView):
    """One of the caller's conversations in this workspace, or forget it."""

    permission_classes = [requires(Capability.AI_QUERY)]

    def _conversation(self, request: Request, workspace: str, conversation_id: UUID) -> Any:
        actor = _actor(request)
        scope = resolve_workspace(actor, workspace)
        conversation = service.conversations_in(actor, scope).filter(pk=conversation_id).first()
        if conversation is None:
            raise NotFoundError()
        conversation.scope = scope
        return conversation

    @extend_schema(
        operation_id="ask_conversation", responses={200: s.ConversationSerializer, 404: NOT_FOUND}
    )
    def get(self, request: Request, workspace: str, conversation_id: UUID) -> Response:
        conversation = self._conversation(request, workspace, conversation_id)
        questions = [
            service.expire_if_overdue(q)
            for q in conversation.questions.only(*_QUESTION_FIELDS).order_by("created_at", "id")[
                : service.CONVERSATION_QUESTION_LIMIT
            ]
        ]
        # One visibility check for the whole conversation (a query per kind of record).
        basis = {ref for question in questions for ref in service.answer_basis(question.answer)}
        visible = service.visible_refs(conversation.scope, basis)
        current = service.current_hashes(conversation.scope, [q.answer for q in questions])
        for question in questions:
            question.answer = service.visible_answer(
                question.answer, conversation.scope, visible=visible, current=current
            )
        data = {
            "id": conversation.pk,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
            "questions": s.QuestionSerializer(questions, many=True).data,
        }
        return Response(data)

    @extend_schema(operation_id="ask_conversation_delete", responses={204: None, 404: NOT_FOUND})
    def delete(self, request: Request, workspace: str, conversation_id: UUID) -> Response:
        self._conversation(request, workspace, conversation_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
