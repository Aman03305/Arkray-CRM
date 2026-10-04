"""Ask Arkray's tables (docs/rag-architecture.md).

- `KnowledgeChunk` (ai_knowledge_chunk): the semantic index. Derived data only: every row is
  rebuilt from the authoritative CRM records (`manage.py ai_reindex`), so deleting the
  whole table loses nothing. A chunk stores **no text**: only where its source is
  (`source_type`, `source_id`), which slice of the source's knowledge document it embeds
  (`char_start`, `char_end`), the hash of that document (`source_hash`) and the vector.
  Retrieval re-reads the live record through the caller's scope and uses the live text,
  and only if its hash still matches, so a stale or reassigned chunk can never surface
  text the caller may not see now.
- `Conversation` and `Question`: one person's questions in one workspace. Bound to the
  actor *and* the workspace (own, one user's, or the organisation's): a conversation
  started in Rahul's workspace is never shown, continued or answered in Priya's.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone
from pgvector.django import HnswIndex, VectorField

from arkray.core.knowledge import SourceType
from arkray.core.models import UUIDPrimaryKeyModel
from arkray.identity.models import User
from arkray.leads.models import Lead

# The embedding model's output size (BAAI/bge-small-en-v1.5). Changing the model or its
# dimension is a new column, index and backfill (docs/rag-architecture.md#indexing-pipeline).
EMBEDDING_DIMENSIONS = 384
QUESTION_MAX_LENGTH = 1000
SOURCE_TYPES = [source_type.value for source_type in SourceType]


class KnowledgeChunk(models.Model):
    id = models.BigAutoField(primary_key=True)
    source_type = models.CharField(max_length=16, choices=[(t, t) for t in SOURCE_TYPES])
    source_id = models.UUIDField()
    chunk_index = models.PositiveSmallIntegerField()
    # Authorization metadata as of indexing: retrieval's pre-filter. Never trusted alone:
    # every hit is re-verified against the live record (see the module docstring).
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+", db_index=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="+", db_index=False)
    opportunity_id = models.UUIDField(null=True, blank=True)
    source_hash = models.CharField(max_length=64)
    char_start = models.PositiveIntegerField()
    char_end = models.PositiveIntegerField()
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS)
    embedding_model = models.CharField(max_length=100)
    source_updated_at = models.DateTimeField()
    indexed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "ai_knowledge_chunk"
        constraints = [
            # Idempotent indexing: a source's chunks are replaced, never duplicated.
            models.UniqueConstraint(
                fields=["source_type", "source_id", "chunk_index"], name="ai_chunk_source_unique"
            ),
            models.CheckConstraint(
                condition=Q(char_end__gt=models.F("char_start")), name="ai_chunk_range_valid"
            ),
            models.CheckConstraint(
                condition=Q(source_type__in=SOURCE_TYPES), name="ai_chunk_source_type_valid"
            ),
            models.CheckConstraint(
                condition=Q(source_hash__regex=r"^[0-9a-f]{64}$"), name="ai_chunk_hash_valid"
            ),
        ]
        indexes = [
            # Organisation-wide semantic search (approximate nearest neighbours, cosine).
            HnswIndex(
                name="ai_chunk_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
            # One person's (or one lead's) chunks: exact search over the scope's rows.
            models.Index(fields=["owner", "source_type"], name="ai_chunk_owner_idx"),
            models.Index(fields=["lead"], name="ai_chunk_lead_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.source_type}:{self.source_id}#{self.chunk_index}"


class WorkspaceKind(models.TextChoices):
    # The AccessScope kinds (core.access.ScopeKind), stored with the conversation.
    SELF = "self"
    USER = "user"
    ORGANIZATION = "organization"


class Conversation(UUIDPrimaryKeyModel):
    actor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+", db_index=False)
    workspace_kind = models.CharField(max_length=16, choices=WorkspaceKind.choices)
    # The workspace's user: the actor for their own workspace, the selected user for an
    # administrator's view of one user, NULL for the organisation.
    subject = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="+", null=True, blank=True, db_index=False
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "ai_conversation"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(workspace_kind=WorkspaceKind.ORGANIZATION, subject__isnull=True)
                    | Q(
                        workspace_kind=WorkspaceKind.SELF,
                        subject__isnull=False,
                        subject=models.F("actor"),
                    )
                    | (
                        Q(workspace_kind=WorkspaceKind.USER, subject__isnull=False)
                        & ~Q(subject=models.F("actor"))
                    )
                ),
                name="ai_conversation_workspace_valid",
            ),
        ]
        indexes = [
            # A person's recent conversations in one workspace; retention purges by age.
            models.Index(
                fields=["actor", "workspace_kind", "subject", "-updated_at"],
                name="ai_conversation_actor_idx",
            ),
            models.Index(fields=["updated_at"], name="ai_conversation_updated_idx"),
        ]

    def __str__(self) -> str:
        return f"Conversation {self.pk}"


class QuestionStatus(models.TextChoices):
    PENDING = "pending"  # waiting for (or being answered by) the ai worker
    ANSWERED = "answered"
    FAILED = "failed"


class Question(UUIDPrimaryKeyModel):
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="questions", db_index=False
    )
    actor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+", db_index=False)
    text = models.TextField(max_length=QUESTION_MAX_LENGTH)
    status = models.CharField(
        max_length=16, choices=QuestionStatus.choices, default=QuestionStatus.PENDING
    )
    # The typed answer (narrative blocks, facts, sources, provenance); see ai.answers.
    answer = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=40, blank=True, default="")
    # How it was answered: "router" (deterministic), "llm", "retrieval" (no model).
    mode = models.CharField(max_length=16, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "ai_question"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(status=QuestionStatus.PENDING, finished_at__isnull=True)
                    | Q(
                        status__in=[QuestionStatus.ANSWERED, QuestionStatus.FAILED],
                        finished_at__isnull=False,
                    )
                ),
                name="ai_question_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(status=QuestionStatus.FAILED) | Q(error_code=""),
                name="ai_question_error_only_when_failed",
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "created_at"], name="ai_question_conv_idx"),
            # The metrics endpoint's questions of the last hour (Phase 10 review).
            models.Index(fields=["finished_at"], name="ai_question_finished_idx"),
            # Bulkheads: pending questions per person and in total.
            models.Index(
                fields=["actor"],
                condition=Q(status=QuestionStatus.PENDING),
                name="ai_question_pending_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Question {self.pk} ({self.status})"
