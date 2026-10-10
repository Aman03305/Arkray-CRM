"""Activity and timeline serializers.

Output: explicit fields. People are {id, full_name, is_active}. A related lead or
opportunity is shown only if it is visible in the same workspace; otherwise (a completed
meeting whose lead has since been reassigned, an open task on a won opportunity its closer
kept) it is {"id": null, "restricted": true} (docs/authorization.md#related-records-and-
timelines). Lists and timelines carry a bounded preview of the text (240 characters),
never a whole note; `is_overdue` is computed when read.

Input: strict (undeclared keys are a 400). Nobody sends an owner, status, created_by,
completed/cancelled fields, archive state or version bumps: ownership follows the lead,
and completing, cancelling, reopening and archiving are their own audited operations.
Which of the type-specific fields a request may carry depends on its type (validation.py).
"""

from __future__ import annotations

import unicodedata
from datetime import date
from typing import Any
from uuid import UUID

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from arkray.core.access import AccessScope
from arkray.core.api import AwareDateTimeField, OpaqueIdField, StrictInputSerializer
from arkray.leads.api.serializers import UserRefSerializer
from arkray.leads.models import Lead
from arkray.pipeline import customer
from arkray.pipeline.models import Opportunity, StageCategory

from .. import attachments, storage
from .. import models as m
from ..selectors import DEFAULT_ORDERING, ORDERINGS, PREVIEW_LENGTH
from ..timeline import USER_KEYS

EARLIEST_DATE, LATEST_DATE = date(2000, 1, 1), date(2099, 12, 31)


# --- related records ---------------------------------------------------------------------------
class ActivityLeadRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(allow_null=True)
    display_name = serializers.CharField(required=False)
    organization_name = serializers.CharField(required=False)
    restricted = serializers.BooleanField()


class ActivityOpportunityRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(allow_null=True)
    title = serializers.CharField(required=False)
    status = serializers.ChoiceField(choices=StageCategory.choices, required=False)
    restricted = serializers.BooleanField()


def lead_ref(lead: Lead, scope: AccessScope) -> dict[str, Any]:
    if not scope.permits_owner(lead.owner_id):
        return {"id": None, "restricted": True}
    return {
        "id": lead.pk,
        "display_name": lead.display_name,
        "organization_name": lead.organization_name,
        "restricted": False,
    }


def opportunity_ref(
    opportunity: Opportunity | None, scope: AccessScope, lead_owner_id: UUID
) -> dict[str, Any] | None:
    """A linked deal, if visible; named without its customer when the viewer no longer
    sees the deal's lead (pipeline.customer)."""
    if opportunity is None:
        return None
    if not scope.permits_owner(opportunity.owner_id):
        return {"id": None, "restricted": True}
    return {
        "id": opportunity.pk,
        "title": customer.title_for(opportunity, scope, lead_owner_id),
        "status": opportunity.status,
        "restricted": False,
    }


_JOINERS = frozenset({chr(0x200C), chr(0x200D)})


def _continues(char: str) -> bool:
    """Does `char` belong to the character before it (a combining mark, a joiner)?"""
    return char in _JOINERS or unicodedata.category(char).startswith("M")


def preview(text: str | None, *, starts_mid_text: bool = False) -> tuple[str, bool]:
    """A bounded preview: at most PREVIEW_LENGTH characters, and whether text was cut. A cut
    never splits a character from its combining marks (a Devanagari vowel sign shown on its
    own, Phase 7 review): the end steps back to a whole character, and a preview that
    starts mid-text drops marks left over from the character before it."""
    text = text or ""
    truncated = len(text) > PREVIEW_LENGTH
    if starts_mid_text:
        while text and _continues(text[0]):
            text = text[1:]
    if len(text) <= PREVIEW_LENGTH and not truncated:
        return text, False
    end = min(len(text), PREVIEW_LENGTH)
    while 0 < end < len(text) and _continues(text[end]):
        end -= 1
    while end > 0 and text[end - 1] in _JOINERS:
        end -= 1
    return text[:end], truncated


# --- activities --------------------------------------------------------------------------------
_SHARED_FIELDS = [
    "id",
    "type",
    "title",
    "status",
    "priority",
    "due_at",
    "starts_at",
    "ends_at",
    "is_overdue",
    "completable",
    "lead",
    "opportunity",
    "owner",
    "created_by",
    "completed_at",
    "cancelled_at",
    "archived_at",
    "version",
    "created_at",
    "updated_at",
]


class _ActivityBase(serializers.ModelSerializer[m.Activity]):
    lead = serializers.SerializerMethodField()
    opportunity = serializers.SerializerMethodField()
    owner = UserRefSerializer(read_only=True)
    created_by = UserRefSerializer(read_only=True, help_text="Who created it (a note's author).")
    is_overdue = serializers.SerializerMethodField(
        help_text="An open task past its due time, or a scheduled meeting past its end."
    )
    completable = serializers.SerializerMethodField(
        help_text="Can be completed now: an open task, or a scheduled meeting that has started "
        "(the caller's permissions aside)."
    )

    @extend_schema_field(ActivityLeadRefSerializer)
    def get_lead(self, activity: m.Activity) -> dict[str, Any]:
        return lead_ref(activity.lead, self.context["scope"])

    @extend_schema_field(ActivityOpportunityRefSerializer(allow_null=True))
    def get_opportunity(self, activity: m.Activity) -> dict[str, Any] | None:
        # An activity's lead is its opportunity's lead (database-enforced).
        return opportunity_ref(activity.opportunity, self.context["scope"], activity.lead.owner_id)

    def get_is_overdue(self, activity: m.Activity) -> bool:
        return activity.is_overdue(self.context["now"])

    def get_completable(self, activity: m.Activity) -> bool:
        return activity.is_completable(self.context["now"])


class ActivityListItemSerializer(_ActivityBase):
    """A list row: the text only as a bounded preview."""

    preview = serializers.SerializerMethodField(help_text=f"At most {PREVIEW_LENGTH} characters.")
    preview_truncated = serializers.SerializerMethodField()

    class Meta:
        model = m.Activity
        fields = [*_SHARED_FIELDS[:3], "preview", "preview_truncated", *_SHARED_FIELDS[3:]]
        read_only_fields = fields

    def get_preview(self, activity: m.Activity) -> str:
        return preview(getattr(activity, "text_preview", ""))[0]

    def get_preview_truncated(self, activity: m.Activity) -> bool:
        return preview(getattr(activity, "text_preview", ""))[1]


class AttachmentSerializer(serializers.ModelSerializer[m.Attachment]):
    """A file's metadata (never its storage key or bytes). `downloadable` follows the virus
    scan policy; `previewable` images may be shown inline (the preview route)."""

    name = serializers.CharField(source="original_name", read_only=True)
    uploaded_by = UserRefSerializer(read_only=True)
    downloadable = serializers.SerializerMethodField()
    previewable = serializers.SerializerMethodField()

    class Meta:
        model = m.Attachment
        fields = [
            "id",
            "name",
            "extension",
            "content_type",
            "size",
            "scan_status",
            "downloadable",
            "previewable",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = fields

    def get_downloadable(self, attachment: m.Attachment) -> bool:
        return attachments.downloadable(attachment)

    def get_previewable(self, attachment: m.Attachment) -> bool:
        return storage.CATALOG[attachment.extension].previewable and attachments.downloadable(
            attachment
        )


class ActivitySerializer(_ActivityBase):
    completed_by = UserRefSerializer(read_only=True, allow_null=True)
    cancelled_by = UserRefSerializer(read_only=True, allow_null=True)
    edited_by = UserRefSerializer(read_only=True, allow_null=True)
    attachments = serializers.SerializerMethodField(help_text="A note's files (others: none).")

    class Meta:
        model = m.Activity
        fields = [
            *_SHARED_FIELDS,
            "description",
            "location",
            "meeting_url",
            "completed_by",
            "cancelled_by",
            "edited_at",
            "edited_by",
            "attachments",
        ]
        read_only_fields = fields

    @extend_schema_field(AttachmentSerializer(many=True))
    def get_attachments(self, activity: m.Activity) -> list[dict[str, Any]]:
        if activity.type != m.ActivityType.NOTE:
            return []
        files = attachments.for_notes([activity.pk])[activity.pk]
        return list(AttachmentSerializer(files, many=True).data)


class NoteSerializer(serializers.ModelSerializer[m.Activity]):
    """A note on the deal page: the whole text, its author, when it was written and last
    edited (and by whom), its files, and whether the caller may change it."""

    created_by = UserRefSerializer(read_only=True, help_text="The author.")
    edited_by = UserRefSerializer(read_only=True, allow_null=True)
    attachments = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()

    class Meta:
        model = m.Activity
        fields = [
            "id",
            "description",
            "created_by",
            "created_at",
            "edited_at",
            "edited_by",
            "version",
            "attachments",
            "can_edit",
        ]
        read_only_fields = fields

    @extend_schema_field(AttachmentSerializer(many=True))
    def get_attachments(self, note: m.Activity) -> list[dict[str, Any]]:
        files = self.context["attachments"].get(note.pk, [])
        return list(AttachmentSerializer(files, many=True).data)

    def get_can_edit(self, note: m.Activity) -> bool:
        actor, scope = self.context.get("actor"), self.context["scope"]
        return (
            actor is not None
            and self.context["writable"]
            and attachments.may_change_note(actor, scope, note)
        )


class NotePageSerializer(serializers.Serializer[Any]):
    results = NoteSerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class ActivityPageSerializer(serializers.Serializer[Any]):
    """One keyset-paginated page of activities (follow `next` / `previous` as given)."""

    results = ActivityListItemSerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class ActivitySummarySerializer(serializers.Serializer[Any]):
    """The authoritative activity figures for this workspace (archived activities never
    count; "today" is the business day in Asia/Kolkata)."""

    open_tasks = serializers.IntegerField()
    tasks_due_today = serializers.IntegerField(help_text="Earlier today included.")
    overdue_tasks = serializers.IntegerField()
    meetings_today = serializers.IntegerField(help_text="Scheduled or completed.")
    upcoming_meetings = serializers.IntegerField(help_text="Scheduled, from now on.")


# --- timeline ----------------------------------------------------------------------------------
class TimelineActivitySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    type = serializers.ChoiceField(choices=m.ActivityType.choices)
    title = serializers.CharField()
    preview = serializers.CharField()
    preview_truncated = serializers.BooleanField()
    status = serializers.ChoiceField(choices=m.ActivityStatus.choices, allow_null=True)
    due_at = serializers.DateTimeField(allow_null=True)
    starts_at = serializers.DateTimeField(allow_null=True)
    ends_at = serializers.DateTimeField(allow_null=True)


class TimelineEntrySerializer(serializers.Serializer[Any]):
    """One event. `details` is the event's snapshot (status and stage names as they were,
    people as {id, full_name, is_active}, a meeting's times when it was scheduled); the
    activity and opportunity are the live records, shown only because they are visible."""

    id = OpaqueIdField("timeline")
    kind = serializers.ChoiceField(choices=m.TimelineKind.choices)
    occurred_at = serializers.DateTimeField()
    actor = UserRefSerializer(allow_null=True, help_text="Null when the system acted.")
    details = serializers.SerializerMethodField()
    activity = serializers.SerializerMethodField()
    opportunity = serializers.SerializerMethodField()

    @extend_schema_field({"type": "object", "additionalProperties": True})
    def get_details(self, entry: m.TimelineEntry) -> dict[str, Any]:
        people = self.context["people"]
        rendered: dict[str, Any] = {}
        for key, value in (entry.data or {}).items():
            if key in USER_KEYS:
                person = people.get(str(value))
                rendered[key.removesuffix("_id")] = (
                    UserRefSerializer(person).data if person is not None else None
                )
            else:
                rendered[key] = value
        return rendered

    @extend_schema_field(TimelineActivitySerializer(allow_null=True))
    def get_activity(self, entry: m.TimelineEntry) -> dict[str, Any] | None:
        activity = entry.activity
        if activity is None:
            return None
        text, truncated = preview(getattr(entry, "text_preview", ""))
        return {
            "id": activity.pk,
            "type": activity.type,
            "title": activity.title,
            "preview": text,
            "preview_truncated": truncated,
            "status": activity.status,
            "due_at": activity.due_at,
            "starts_at": activity.starts_at,
            "ends_at": activity.ends_at,
        }

    @extend_schema_field(ActivityOpportunityRefSerializer(allow_null=True))
    def get_opportunity(self, entry: m.TimelineEntry) -> dict[str, Any] | None:
        opportunity = entry.opportunity
        if opportunity is None:
            return None
        return opportunity_ref(opportunity, self.context["scope"], opportunity.lead.owner_id)


class TimelinePageSerializer(serializers.Serializer[Any]):
    results = TimelineEntrySerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


# --- input -------------------------------------------------------------------------------------
def _text(max_length: int, **kwargs: Any) -> serializers.CharField:
    return serializers.CharField(max_length=max_length, allow_blank=True, required=False, **kwargs)


class _ActivityFieldsSerializer(StrictInputSerializer):
    """Every type-specific field (each type accepts only its own: validation.py)."""

    title = _text(m.TITLE_MAX_LENGTH, help_text="Subject. Tasks and meetings.")
    description = _text(
        m.DESCRIPTION_MAX_LENGTH,
        trim_whitespace=False,
        help_text="A task's description, a meeting's agenda, a note's text.",
    )
    priority = serializers.ChoiceField(
        choices=m.Priority.choices, required=False, help_text="Tasks; normal if omitted."
    )
    due_at = AwareDateTimeField(required=False, allow_null=True, help_text="Tasks, optional.")
    starts_at = AwareDateTimeField(required=False, help_text="Meetings.")
    ends_at = AwareDateTimeField(
        required=False, help_text="Meetings: after the start, within 24 hours."
    )
    location = _text(m.LOCATION_MAX_LENGTH, help_text="Meetings.")
    meeting_url = _text(m.MEETING_URL_MAX_LENGTH, help_text="Meetings: an https:// link.")


class ActivityCreateSerializer(_ActivityFieldsSerializer):
    type = serializers.ChoiceField(choices=m.ActivityType.choices)
    lead = serializers.UUIDField(
        required=False, help_text="The lead (in this workspace) it is about."
    )
    opportunity = serializers.UUIDField(
        required=False,
        help_text="An opportunity (in this workspace) it is about; its lead is implied.",
    )


class ActivityUpdateSerializer(_ActivityFieldsSerializer):
    version = serializers.IntegerField(min_value=1)


class ActivityVersionSerializer(StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)


class ActivityListQuerySerializer(StrictInputSerializer):
    type = serializers.ChoiceField(choices=m.ActivityType.choices, required=False)
    status = serializers.ChoiceField(choices=m.ActivityStatus.choices, required=False)
    lead = serializers.UUIDField(required=False)
    opportunity = serializers.UUIDField(required=False)
    owner = serializers.UUIDField(required=False, help_text="Organisation-wide workspace only.")
    date_from = serializers.DateField(
        required=False, help_text="Inclusive business date of the due time / start / creation."
    )
    date_to = serializers.DateField(required=False, help_text="Inclusive.")
    overdue = serializers.BooleanField(required=False, default=False)
    current = serializers.BooleanField(
        required=False, default=False, help_text="Open tasks and scheduled meetings."
    )
    upcoming = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Open tasks and scheduled meetings due or starting from now on.",
    )
    # Unset is None (a query string's missing boolean would otherwise read as false).
    cancelled = serializers.BooleanField(
        required=False,
        allow_null=True,
        default=None,
        help_text="false leaves cancelled tasks and meetings out; true keeps only them.",
    )
    archived = serializers.BooleanField(required=False, default=False)
    ordering = serializers.ChoiceField(
        choices=sorted(ORDERINGS), required=False, default=DEFAULT_ORDERING
    )
    cursor = serializers.CharField(max_length=1000, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False, default=25)

    def _bounded(self, value: date) -> date:
        if not EARLIEST_DATE <= value <= LATEST_DATE:
            raise serializers.ValidationError("Enter a date between 2000 and 2099.")
        return value

    def validate_date_from(self, value: date) -> date:
        return self._bounded(value)

    def validate_date_to(self, value: date) -> date:
        return self._bounded(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start, end = attrs.get("date_from"), attrs.get("date_to")
        if start and end and start > end:
            raise serializers.ValidationError(
                {"date_to": ["The end date must be on or after the start date."]}
            )
        kind, status = attrs.get("type"), attrs.get("status")
        allowed = {
            m.ActivityType.TASK: m.TASK_STATUSES,
            m.ActivityType.MEETING: m.MEETING_STATUSES,
            m.ActivityType.NOTE: (),
        }
        if kind and status and status not in allowed[kind]:
            raise serializers.ValidationError({"status": ["This type doesn't have that status."]})
        if attrs.get("overdue") and (
            kind not in (None, m.ActivityType.TASK) or status not in (None, m.ActivityStatus.OPEN)
        ):
            raise serializers.ValidationError({"overdue": ["Only open tasks can be overdue."]})
        for flag in ("current", "upcoming"):
            if attrs.get(flag) and status is not None:
                raise serializers.ValidationError(
                    {flag: ["Choose either a status or current work, not both."]}
                )
        if attrs.get("upcoming") and attrs.get("overdue"):
            raise serializers.ValidationError(
                {"upcoming": ["Nothing can be both overdue and upcoming."]}
            )
        if attrs.get("cancelled") is not None and status is not None:
            raise serializers.ValidationError(
                {"cancelled": ["Choose either a status or the cancelled filter, not both."]}
            )
        return attrs


class TimelineQuerySerializer(StrictInputSerializer):
    cursor = serializers.CharField(max_length=1000, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=50, required=False, default=20)


class NoteQuerySerializer(StrictInputSerializer):
    cursor = serializers.CharField(max_length=1000, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=50, required=False, default=20)
