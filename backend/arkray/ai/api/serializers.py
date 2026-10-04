"""Ask Arkray serializers (docs/rag-architecture.md#hallucination-protection).

An answer is typed data, never markup: blocks of text parts and record references, facts
formatted by the server, the records cited, quoted passages, notices and provenance. The
client renders text nodes and builds links only from `ref`s (kind + id), inside the
workspace it is showing.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from arkray.core.api import StrictInputSerializer

from ..models import Conversation, Question

REF_KINDS = ["lead", "opportunity", "task", "meeting", "note"]
FACT_KINDS = ["count", "money"]
BLOCK_TYPES = ["paragraph", "bullet"]
ANSWER_MODES = ["router", "llm", "retrieval"]


class AskInputSerializer(StrictInputSerializer):
    question = serializers.CharField(
        max_length=4000,
        trim_whitespace=False,
        help_text="The question, up to 1,000 characters once cleaned.",
    )
    conversation_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Continue this conversation (yours, in this workspace); omit to start one.",
    )


class AnswerPartSerializer(serializers.Serializer[Any]):
    text = serializers.CharField(required=False)
    bold = serializers.BooleanField(required=False)
    ref = serializers.CharField(required=False, help_text="kind:id of a record in `sources`.")


class AnswerBlockSerializer(serializers.Serializer[Any]):
    type = serializers.ChoiceField(choices=BLOCK_TYPES)
    parts = AnswerPartSerializer(many=True)


class AnswerFactSerializer(serializers.Serializer[Any]):
    label = serializers.CharField()  # type: ignore[assignment]  # a field, not Field.label
    value = serializers.CharField(help_text="Formatted by the server, e.g. ₹12,50,000.")
    kind = serializers.ChoiceField(choices=FACT_KINDS)
    raw = serializers.CharField(help_text="The exact value: an integer or a decimal string.")


class AnswerSourceSerializer(serializers.Serializer[Any]):
    ref = serializers.CharField()
    kind = serializers.ChoiceField(choices=REF_KINDS)
    id = serializers.UUIDField()
    label = serializers.CharField()  # type: ignore[assignment]  # a field, not Field.label
    detail = serializers.CharField(allow_blank=True)


class AnswerCitationSerializer(serializers.Serializer[Any]):
    ref = serializers.CharField()
    kind = serializers.ChoiceField(choices=REF_KINDS)
    label = serializers.CharField()  # type: ignore[assignment]  # a field, not Field.label
    snippet = serializers.CharField(help_text="Quoted from the record as it is now.")
    when = serializers.CharField(allow_blank=True)


class AnswerProvenanceSerializer(serializers.Serializer[Any]):
    mode = serializers.ChoiceField(
        choices=ANSWER_MODES,
        help_text="router: CRM figures and a fixed template; llm: written by the AI model "
        "from tool results; retrieval: the most relevant records, without a summary.",
    )
    tools = serializers.ListField(child=serializers.CharField())
    grounded = serializers.BooleanField()
    model = serializers.CharField(allow_blank=True)


class AnswerSerializer(serializers.Serializer[Any]):
    blocks = AnswerBlockSerializer(many=True)
    facts = AnswerFactSerializer(many=True)
    sources = AnswerSourceSerializer(many=True)
    citations = AnswerCitationSerializer(many=True)
    notices = serializers.ListField(child=serializers.CharField())
    provenance = AnswerProvenanceSerializer()


class QuestionSerializer(serializers.ModelSerializer[Question]):
    conversation_id = serializers.UUIDField(read_only=True)
    question = serializers.CharField(source="text", read_only=True)
    error = serializers.SerializerMethodField()
    answer = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            "id",
            "conversation_id",
            "question",
            "status",
            "error",
            "answer",
            "created_at",
            "finished_at",
        ]
        read_only_fields = fields

    def get_error(self, question: Question) -> str | None:
        return question.error_code or None

    @extend_schema_field(AnswerSerializer(allow_null=True))
    def get_answer(self, question: Question) -> dict[str, Any] | None:
        if question.status != "answered" or not question.answer:
            return None
        return dict(AnswerSerializer(question.answer).data)


class ConversationSummarySerializer(serializers.ModelSerializer[Conversation]):
    title = serializers.CharField(read_only=True, help_text="The first question, shortened.")

    class Meta:
        model = Conversation
        fields = ["id", "title", "created_at", "updated_at"]
        read_only_fields = fields


class ConversationSerializer(serializers.ModelSerializer[Conversation]):
    questions = QuestionSerializer(many=True, read_only=True)

    class Meta:
        model = Conversation
        fields = ["id", "created_at", "updated_at", "questions"]
        read_only_fields = fields


class AskStatusSerializer(serializers.Serializer[Any]):
    enabled = serializers.BooleanField()
    summaries = serializers.ChoiceField(
        choices=["available", "unavailable", "none"],
        help_text="available: written answers by the AI model; unavailable: temporarily "
        "down (answers show records instead); none: this CRM runs without a model.",
    )
