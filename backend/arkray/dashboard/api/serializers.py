"""Dashboard serializers. Output only. The pipeline totals and the activity summary are
rendered by the pipeline's and the activities' own serializers (one schema, one TypeScript
type each). The rows of the short lists carry only what the dashboard shows: no text
preview, no author, no version (the full activity is a click away; Phase 5 security review).

Money is a decimal string with two places ("1500000.00"), never a JSON number. People are
{id, full_name, is_active}: a name is all the dashboard shows of anyone.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from arkray.activities.api.serializers import (
    ActivityLeadRefSerializer,
    ActivitySummarySerializer,
    lead_ref,
)
from arkray.activities.models import Activity
from arkray.leads.api.serializers import UserRefSerializer
from arkray.leads.models import Lead
from arkray.pipeline.api.serializers import PipelineTotalsSerializer


class LeadSummarySerializer(serializers.Serializer[Any]):
    """Archived leads never count; "today" is the business day in Asia/Kolkata."""

    total = serializers.IntegerField()
    new_today = serializers.IntegerField(help_text="Created during today's business day.")


class DashboardLeadSerializer(serializers.ModelSerializer[Lead]):
    """One of today's new leads: its name, who it is assigned to, and when it was created."""

    owner = UserRefSerializer(read_only=True, help_text="The assigned user.")

    class Meta:
        model = Lead
        fields = ["id", "display_name", "organization_name", "owner", "created_at"]
        read_only_fields = fields


class DashboardActivitySerializer(serializers.ModelSerializer[Activity]):
    """A meeting or task in a short list: what it is, when, about which lead, whose."""

    lead = serializers.SerializerMethodField()
    owner = UserRefSerializer(read_only=True)
    is_overdue = serializers.SerializerMethodField(
        help_text="An open task past its due time, or a scheduled meeting past its end."
    )

    class Meta:
        model = Activity
        fields = [
            "id",
            "type",
            "title",
            "status",
            "due_at",
            "starts_at",
            "is_overdue",
            "lead",
            "owner",
        ]
        read_only_fields = fields

    @extend_schema_field(ActivityLeadRefSerializer)
    def get_lead(self, activity: Activity) -> dict[str, Any]:
        return lead_ref(activity.lead, self.context["scope"])

    def get_is_overdue(self, activity: Activity) -> bool:
        return activity.is_overdue(self.context["now"])


class DashboardSerializer(serializers.Serializer[Any]):
    """The workspace's figures and its short lists, all read at the same moment."""

    currency = serializers.CharField()
    time_zone = serializers.CharField(help_text='The business time zone that defines "today".')
    business_date = serializers.DateField(help_text="Today in the business time zone.")
    leads = LeadSummarySerializer()
    pipeline = PipelineTotalsSerializer()
    activities = ActivitySummarySerializer()
    new_leads = DashboardLeadSerializer(
        many=True, help_text="Today's newest leads (at most 5): the ones `leads.new_today` counts."
    )
    upcoming_meetings = DashboardActivitySerializer(
        many=True, help_text="The next scheduled meetings from now on (at most 5)."
    )
    next_tasks = DashboardActivitySerializer(
        many=True, help_text="Open tasks, soonest due first: overdue ones first (at most 5)."
    )
