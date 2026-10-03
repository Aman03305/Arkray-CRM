"""Lead serializers.

Output: explicit fields. The list item is deliberately smaller than the detail (no address,
description or provenance), so list pages stay bounded. People are rendered as
{id, full_name, is_active}; no other user data leaves the API here.

Input: strict (undeclared keys are a 400). PATCH accepts profile fields plus `version` only:
`owner`, `status`, `created_by`, `archived_at`, `id`, timestamps and anything else are
refused, because ownership, status and archiving have their own audited operations.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from arkray.core.api import AwareDateTimeField, StrictInputSerializer
from arkray.core.text import TextRejected, clean_line
from arkray.identity.models import User

from .. import models as m
from ..phones import PHONE_MAX_LENGTH
from ..selectors import DEFAULT_ORDERING, ORDERINGS, SEARCH_TERM_MIN_LENGTH

SEARCH_MIN_LENGTH, SEARCH_MAX_LENGTH = 2, 100
EARLIEST_DATE, LATEST_DATE = date(2000, 1, 1), date(2999, 12, 31)


# --- output ------------------------------------------------------------------------------------
class UserRefSerializer(serializers.ModelSerializer[User]):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "full_name", "is_active"]
        read_only_fields = fields


class StatusRefSerializer(serializers.ModelSerializer[m.LeadStatus]):
    class Meta:
        model = m.LeadStatus
        fields = ["key", "name", "category"]
        read_only_fields = fields


class SourceRefSerializer(serializers.ModelSerializer[m.LeadSource]):
    class Meta:
        model = m.LeadSource
        fields = ["key", "name"]
        read_only_fields = fields


LIST_FIELDS = [
    "id",
    "display_name",
    "first_name",
    "last_name",
    "organization_name",
    "job_title",
    "email",
    "phone",
    "mobile",
    "status",
    "source",
    "rating",
    "owner",
    "last_contacted_at",
    "archived_at",
    "created_at",
    "updated_at",
    "version",
]


class LeadListItemSerializer(serializers.ModelSerializer[m.Lead]):
    display_name = serializers.CharField(read_only=True)
    status = StatusRefSerializer(read_only=True)
    source = SourceRefSerializer(read_only=True, allow_null=True)  # type: ignore[assignment]
    owner = UserRefSerializer(read_only=True)

    class Meta:
        model = m.Lead
        fields = LIST_FIELDS
        read_only_fields = fields


class LeadSerializer(serializers.ModelSerializer[m.Lead]):
    display_name = serializers.CharField(read_only=True)
    status = StatusRefSerializer(read_only=True)
    source = SourceRefSerializer(read_only=True, allow_null=True)  # type: ignore[assignment]
    owner = UserRefSerializer(read_only=True)
    created_by = UserRefSerializer(read_only=True)

    class Meta:
        model = m.Lead
        fields = [
            *LIST_FIELDS,
            "alternate_phone",
            "address_line_1",
            "address_line_2",
            "city",
            "state",
            "postal_code",
            "country",
            "description",
            "created_by",
        ]
        read_only_fields = fields


class LeadPageSerializer(serializers.Serializer[Any]):
    """One keyset-paginated page of leads (follow `next` / `previous` as given)."""

    results = LeadListItemSerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class LeadStatusOptionSerializer(serializers.ModelSerializer[m.LeadStatus]):
    class Meta:
        model = m.LeadStatus
        fields = ["key", "name", "category", "is_active", "is_default"]
        read_only_fields = fields


class LeadSourceOptionSerializer(serializers.ModelSerializer[m.LeadSource]):
    class Meta:
        model = m.LeadSource
        fields = ["key", "name", "is_active"]
        read_only_fields = fields


class RatingOptionSerializer(serializers.Serializer[Any]):
    key = serializers.ChoiceField(choices=m.Rating.choices)
    name = serializers.CharField()


class LeadOptionsSerializer(serializers.Serializer[Any]):
    """Choices for lead forms and filters. Inactive statuses and sources are included (leads
    may still carry them) but only active ones may be chosen."""

    statuses = LeadStatusOptionSerializer(many=True)
    sources = LeadSourceOptionSerializer(many=True)
    ratings = RatingOptionSerializer(many=True)
    countries = serializers.ListField(child=serializers.CharField())


class LeadDuplicateSerializer(serializers.ModelSerializer[m.Lead]):
    display_name = serializers.CharField(read_only=True)
    owner = UserRefSerializer(read_only=True)
    matched_on = serializers.SerializerMethodField()

    class Meta:
        model = m.Lead
        fields = ["id", "display_name", "organization_name", "owner", "archived_at", "matched_on"]
        read_only_fields = fields

    @extend_schema_field(
        serializers.ListField(child=serializers.ChoiceField(choices=["email", "phone"]))
    )
    def get_matched_on(self, lead: m.Lead) -> list[str]:
        return list(self.context["matched_on"][lead.pk])


class LeadDuplicateListSerializer(serializers.Serializer[Any]):
    results = LeadDuplicateSerializer(many=True)


# --- input -------------------------------------------------------------------------------------
def _text(max_length: int, **kwargs: Any) -> serializers.CharField:
    return serializers.CharField(max_length=max_length, allow_blank=True, required=False, **kwargs)


class LeadFieldsSerializer(StrictInputSerializer):
    """Profile fields (all optional here; the services enforce what a lead needs)."""

    first_name = _text(m.NAME_MAX_LENGTH)
    last_name = _text(m.NAME_MAX_LENGTH)
    organization_name = _text(m.ORGANIZATION_MAX_LENGTH)
    job_title = _text(m.JOB_TITLE_MAX_LENGTH)
    email = _text(m.EMAIL_MAX_LENGTH)
    phone = _text(PHONE_MAX_LENGTH)
    mobile = _text(PHONE_MAX_LENGTH)
    alternate_phone = _text(PHONE_MAX_LENGTH)
    address_line_1 = _text(m.ADDRESS_LINE_MAX_LENGTH)
    address_line_2 = _text(m.ADDRESS_LINE_MAX_LENGTH)
    city = _text(m.LOCALITY_MAX_LENGTH)
    state = _text(m.LOCALITY_MAX_LENGTH)
    postal_code = _text(m.POSTAL_CODE_MAX_LENGTH)
    country = _text(2, help_text="ISO 3166-1 alpha-2 code, e.g. IN; empty for none.")
    source = serializers.CharField(  # type: ignore[assignment]  # a field named "source"
        max_length=32, allow_null=True, required=False, help_text="A lead source key."
    )
    rating = serializers.ChoiceField(choices=m.Rating.choices, allow_null=True, required=False)
    last_contacted_at = AwareDateTimeField(allow_null=True, required=False)
    description = _text(m.DESCRIPTION_MAX_LENGTH, trim_whitespace=False)


class LeadCreateSerializer(LeadFieldsSerializer):
    status = serializers.CharField(
        max_length=32, required=False, help_text="A lead status key; the default status if omitted."
    )
    owner = serializers.UUIDField(
        required=False,
        help_text="Organisation-wide workspace only (required there, needs crm.assign_any).",
    )


class LeadUpdateSerializer(LeadFieldsSerializer):
    version = serializers.IntegerField(min_value=1)


class LeadStatusChangeSerializer(StrictInputSerializer):
    status = serializers.CharField(max_length=32)
    version = serializers.IntegerField(min_value=1)


class LeadAssignSerializer(StrictInputSerializer):
    owner = serializers.UUIDField()
    version = serializers.IntegerField(min_value=1)


class LeadVersionSerializer(StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)


class LeadListQuerySerializer(StrictInputSerializer):
    q = serializers.CharField(
        min_length=SEARCH_MIN_LENGTH, max_length=SEARCH_MAX_LENGTH, required=False, allow_blank=True
    )
    status = serializers.CharField(max_length=32, required=False)
    source = serializers.CharField(max_length=32, required=False)  # type: ignore[assignment]
    rating = serializers.ChoiceField(choices=m.Rating.choices, required=False)
    owner = serializers.UUIDField(required=False, help_text="Organisation-wide workspace only.")
    created_from = serializers.DateField(required=False, help_text="Inclusive, business time zone.")
    created_to = serializers.DateField(required=False, help_text="Inclusive, business time zone.")
    archived = serializers.BooleanField(required=False, default=False)
    ordering = serializers.ChoiceField(
        choices=sorted(ORDERINGS), required=False, default=DEFAULT_ORDERING
    )
    cursor = serializers.CharField(max_length=1000, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False, default=25)

    def validate_q(self, value: str) -> str:
        try:
            value = clean_line(value)
        except TextRejected as exc:
            raise serializers.ValidationError(str(exc)) from None
        if value and all(len(term) < SEARCH_TERM_MIN_LENGTH for term in value.split()):
            raise serializers.ValidationError(
                f"Use at least {SEARCH_TERM_MIN_LENGTH} characters in a search word."
            )
        return value

    def _bounded(self, value: date) -> date:
        if not EARLIEST_DATE <= value <= LATEST_DATE:
            raise serializers.ValidationError("Enter a date between 2000 and 2999.")
        return value

    def validate_created_from(self, value: date) -> date:
        return self._bounded(value)

    def validate_created_to(self, value: date) -> date:
        return self._bounded(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start, end = attrs.get("created_from"), attrs.get("created_to")
        if start and end and start > end:
            raise serializers.ValidationError(
                {"created_to": ["The end date must be on or after the start date."]}
            )
        return attrs


class LeadDuplicateQuerySerializer(StrictInputSerializer):
    email = serializers.CharField(max_length=m.EMAIL_MAX_LENGTH, required=False, allow_blank=True)
    phone = serializers.ListField(
        child=serializers.CharField(max_length=PHONE_MAX_LENGTH, allow_blank=True),
        required=False,
        max_length=3,
    )
    exclude = serializers.UUIDField(required=False, help_text="A lead to leave out (when editing).")
