"""Global search serializers (docs/search.md#results).

Input: `q` only (strict: anything else is a 400), cleaned like the Leads list's search
(core.text): 2-100 characters (also after normalisation), no invisible or control
characters. Words a trigram index can look up (3 letters or digits in a row) are searched,
the first 5 different ones (core.ranking), so there must be one; other words only rank.

Output: one small, typed result per record, grouped by kind, never a full record: what a
row needs to be recognised and opened, nothing more. No contact data, amounts, descriptions
or agendas. A note is a bounded preview (240 characters around the first search word,
cut in PostgreSQL), with no author. A related lead is shown only if it is visible in the
same workspace, by the owning module's own rule (otherwise {"id": null, "restricted": true}).
Plain text throughout: no markup, no highlighting markup; the client renders text.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from arkray.activities.api.serializers import ActivityLeadRefSerializer, preview
from arkray.activities.api.serializers import lead_ref as activity_lead_ref
from arkray.activities.models import Activity
from arkray.core.api import StrictInputSerializer
from arkray.core.ranking import SearchQuery
from arkray.core.text import SEARCH_MAX_LENGTH, SEARCH_MIN_LENGTH
from arkray.leads.api.serializers import StatusRefSerializer, UserRefSerializer
from arkray.leads.models import Lead
from arkray.pipeline.api.serializers import LeadRefSerializer
from arkray.pipeline.api.serializers import lead_ref as opportunity_lead_ref
from arkray.pipeline.models import Opportunity, Stage


# --- input -------------------------------------------------------------------------------------
class SearchQuerySerializer(StrictInputSerializer):
    q = serializers.CharField(
        min_length=SEARCH_MIN_LENGTH,
        max_length=SEARCH_MAX_LENGTH,
        help_text="2-100 characters. Every word with 3 letters or digits in a row must match "
        "(the first 5 different such words are searched; other words only rank); "
        "case-insensitive; wildcards are literal.",
    )

    def validate_q(self, value: str) -> SearchQuery:
        try:
            return SearchQuery.parse(value)
        except ValueError as exc:  # TextRejected included
            raise serializers.ValidationError(str(exc)) from None


# --- results -----------------------------------------------------------------------------------
class SearchLeadSerializer(serializers.ModelSerializer[Lead]):
    display_name = serializers.CharField(read_only=True)
    status = StatusRefSerializer(read_only=True)
    owner = UserRefSerializer(read_only=True, help_text="The assigned user.")

    class Meta:
        model = Lead
        fields = ["id", "display_name", "organization_name", "status", "owner"]
        read_only_fields = fields


class SearchStageSerializer(serializers.ModelSerializer[Stage]):
    class Meta:
        model = Stage
        fields = ["id", "name"]
        read_only_fields = fields


class SearchOpportunitySerializer(serializers.ModelSerializer[Opportunity]):
    stage = SearchStageSerializer(read_only=True)
    lead = serializers.SerializerMethodField()
    owner = UserRefSerializer(read_only=True)

    class Meta:
        model = Opportunity
        fields = ["id", "title", "status", "stage", "lead", "owner"]
        read_only_fields = fields

    @extend_schema_field(LeadRefSerializer)
    def get_lead(self, opportunity: Opportunity) -> dict[str, Any]:
        return opportunity_lead_ref(opportunity, self.context["scope"])


class _SearchActivity(serializers.ModelSerializer[Activity]):
    lead = serializers.SerializerMethodField()

    @extend_schema_field(ActivityLeadRefSerializer)
    def get_lead(self, activity: Activity) -> dict[str, Any]:
        return activity_lead_ref(activity.lead, self.context["scope"])


class _SearchWork(_SearchActivity):
    owner = UserRefSerializer(read_only=True)
    is_overdue = serializers.SerializerMethodField(
        help_text="An open task past its due time, or a scheduled meeting past its end."
    )

    def get_is_overdue(self, activity: Activity) -> bool:
        return activity.is_overdue(self.context["now"])


class SearchTaskSerializer(_SearchWork):
    class Meta:
        model = Activity
        fields = ["id", "title", "status", "priority", "due_at", "is_overdue", "lead", "owner"]
        read_only_fields = fields


class SearchMeetingSerializer(_SearchWork):
    class Meta:
        model = Activity
        fields = [
            "id",
            "title",
            "status",
            "starts_at",
            "ends_at",
            "location",
            "is_overdue",
            "lead",
            "owner",
        ]
        read_only_fields = fields


class SearchNoteSerializer(_SearchActivity):
    """A note found by search: a bounded preview of its text, never the whole note, and no
    author (the workspace's owner didn't necessarily write it; the note's page says who did)."""

    preview = serializers.SerializerMethodField(
        help_text="At most 240 characters of the note, starting shortly before the first "
        "search word."
    )
    preview_truncated = serializers.SerializerMethodField(help_text="More text follows.")
    preview_starts_mid_text = serializers.SerializerMethodField(
        help_text="The preview doesn't start at the beginning of the note."
    )

    class Meta:
        model = Activity
        fields = [
            "id",
            "preview",
            "preview_truncated",
            "preview_starts_mid_text",
            "created_at",
            "lead",
        ]
        read_only_fields = fields

    def _preview(self, note: Activity) -> tuple[str, bool]:
        return preview(
            getattr(note, "text_preview", ""),
            starts_mid_text=self.get_preview_starts_mid_text(note),
        )

    def get_preview(self, note: Activity) -> str:
        return self._preview(note)[0]

    def get_preview_truncated(self, note: Activity) -> bool:
        return self._preview(note)[1]

    def get_preview_starts_mid_text(self, note: Activity) -> bool:
        return int(getattr(note, "preview_start", 1)) > 1


class SearchLeadGroupSerializer(serializers.Serializer[Any]):
    results = SearchLeadSerializer(many=True)
    has_more = serializers.BooleanField(help_text="More leads matched than are shown.")


class SearchOpportunityGroupSerializer(serializers.Serializer[Any]):
    results = SearchOpportunitySerializer(many=True)
    has_more = serializers.BooleanField(help_text="More opportunities matched than are shown.")


class SearchTaskGroupSerializer(serializers.Serializer[Any]):
    results = SearchTaskSerializer(many=True)
    has_more = serializers.BooleanField(help_text="More tasks matched than are shown.")


class SearchMeetingGroupSerializer(serializers.Serializer[Any]):
    results = SearchMeetingSerializer(many=True)
    has_more = serializers.BooleanField(help_text="More meetings matched than are shown.")


class SearchNoteGroupSerializer(serializers.Serializer[Any]):
    results = SearchNoteSerializer(many=True)
    has_more = serializers.BooleanField(help_text="More notes matched than are shown.")


class SearchResultsSerializer(serializers.Serializer[Any]):
    """At most 5 results of each kind, best match first (docs/search.md#ranking). Archived
    records are left out. No counts."""

    query = serializers.CharField(help_text="The query as searched (cleaned).")
    terms = serializers.ListField(
        child=serializers.CharField(),
        help_text="The words searched; each one matched.",
    )
    leads = SearchLeadGroupSerializer()
    opportunities = SearchOpportunityGroupSerializer()
    tasks = SearchTaskGroupSerializer()
    meetings = SearchMeetingGroupSerializer()
    notes = SearchNoteGroupSerializer()
