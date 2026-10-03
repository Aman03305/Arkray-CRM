"""What each activity type is: its fields, statuses, lifecycle and timeline vocabulary.

One spec per type, read by validation (which fields a payload may carry), the services
(initial status, which transitions exist, what the timeline records) and the API. The
database states the same rules as CHECK constraints (models.Activity); the spec exists so
the services can refuse with a helpful message before the database would.

Adding a type (call, email, WhatsApp) means: an ActivityType value, its columns and CHECKs
in one migration, and a TypeSpec here. Nothing else changes shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ActivityStatus, ActivityType, TimelineKind


@dataclass(frozen=True, slots=True)
class TypeSpec:
    type: ActivityType
    label: str
    # Fields a create may carry (besides the lead/opportunity link) and an edit may change.
    fields: frozenset[str]
    required: frozenset[str]
    # None: the type has no status and no lifecycle (notes).
    initial_status: ActivityStatus | None
    created_kind: TimelineKind
    completed_kind: TimelineKind | None = None
    cancelled_kind: TimelineKind | None = None
    reopened_kind: TimelineKind | None = None

    @property
    def has_lifecycle(self) -> bool:
        return self.initial_status is not None


SPECS: dict[str, TypeSpec] = {
    ActivityType.TASK: TypeSpec(
        type=ActivityType.TASK,
        label="Task",
        fields=frozenset({"title", "description", "priority", "due_at"}),
        required=frozenset({"title"}),
        initial_status=ActivityStatus.OPEN,
        created_kind=TimelineKind.TASK_CREATED,
        completed_kind=TimelineKind.TASK_COMPLETED,
        cancelled_kind=TimelineKind.TASK_CANCELLED,
        reopened_kind=TimelineKind.TASK_REOPENED,
    ),
    ActivityType.MEETING: TypeSpec(
        type=ActivityType.MEETING,
        label="Meeting",
        fields=frozenset(
            {"title", "description", "starts_at", "ends_at", "location", "meeting_url"}
        ),
        required=frozenset({"title", "starts_at", "ends_at"}),
        initial_status=ActivityStatus.SCHEDULED,
        created_kind=TimelineKind.MEETING_SCHEDULED,
        completed_kind=TimelineKind.MEETING_COMPLETED,
        cancelled_kind=TimelineKind.MEETING_CANCELLED,
        reopened_kind=TimelineKind.MEETING_REOPENED,
    ),
    ActivityType.NOTE: TypeSpec(
        type=ActivityType.NOTE,
        label="Note",
        fields=frozenset({"description"}),
        required=frozenset({"description"}),
        initial_status=None,
        created_kind=TimelineKind.NOTE_ADDED,
    ),
}

# Every type-specific field any type has (the API's input serializers declare these).
ALL_FIELDS = frozenset().union(*(spec.fields for spec in SPECS.values()))
