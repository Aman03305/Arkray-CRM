"""Pipeline and opportunity serializers.

Output: explicit fields. Money and probabilities are decimal strings with two places
("1250000.00", "75.00"), never JSON numbers. People are {id, full_name, is_active}. An
opportunity's lead is shown only if the lead is visible in the same workspace; otherwise
(a closed opportunity whose lead has since been reassigned) it is
{"id": null, "restricted": true} (docs/authorization.md#related-records-and-timelines).

Input: strict (undeclared keys are a 400). Nobody sends an owner, status, closed_at,
created_by or archive state: ownership follows the lead, status follows the stage, and
stage changes, archiving and conversion are their own audited operations.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from arkray.core.access import AccessScope
from arkray.core.api import ExactDecimalField, OpaqueIdField, StrictInputSerializer
from arkray.leads.api.serializers import LeadSerializer, UserRefSerializer

from .. import configuration
from .. import models as m
from ..selectors import BOARD_CARDS_DEFAULT, BOARD_CARDS_MAX, DEFAULT_ORDERING, ORDERINGS


def money(**kwargs: Any) -> serializers.DecimalField:
    return serializers.DecimalField(
        max_digits=None,
        decimal_places=2,
        coerce_to_string=True,
        rounding=ROUND_HALF_UP,
        read_only=True,
        **kwargs,
    )


def percentage(**kwargs: Any) -> serializers.DecimalField:
    return serializers.DecimalField(
        max_digits=m.PROBABILITY_DIGITS,
        decimal_places=m.PROBABILITY_PLACES,
        coerce_to_string=True,
        read_only=True,
        **kwargs,
    )


# --- output ------------------------------------------------------------------------------------
class StageSerializer(serializers.ModelSerializer[m.Stage]):
    probability = percentage()
    type = serializers.ChoiceField(
        source="stage_type",
        choices=m.StageType.choices,
        read_only=True,
        help_text="open, negotiation, won or lost: what the stage means (never its name).",
    )

    class Meta:
        model = m.Stage
        fields = [
            "id",
            "key",
            "name",
            "position",
            "probability",
            "category",
            "type",
            "is_negotiation",
            "is_active",
        ]
        read_only_fields = fields


class PipelineRefSerializer(serializers.ModelSerializer[m.Pipeline]):
    class Meta:
        model = m.Pipeline
        fields = ["id", "key", "name"]
        read_only_fields = fields


class FieldOptionSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    # DRF pops declared fields off the class; the base Field attribute is unaffected.
    label = serializers.CharField()  # type: ignore[assignment]


class CustomFieldSerializer(serializers.ModelSerializer[m.CustomField]):
    type = serializers.ChoiceField(source="field_type", choices=m.FieldType.choices, read_only=True)
    options = FieldOptionSerializer(many=True, read_only=True)

    class Meta:
        model = m.CustomField
        fields = ["id", "name", "type", "required", "options", "position"]
        read_only_fields = fields


class PipelineSerializer(serializers.ModelSerializer[m.Pipeline]):
    """A pipeline with its stages in order (retired ones flagged) and its active custom
    fields: the board columns, stage choices and form fields come from here, never from
    hard-coded lists. `owner` is null for a shared (organisation) pipeline; `can_manage`
    says whether the caller may configure it in this workspace."""

    stages = StageSerializer(many=True, read_only=True)
    custom_fields = CustomFieldSerializer(source="fields", many=True, read_only=True)
    owner = UserRefSerializer(read_only=True, allow_null=True)
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = m.Pipeline
        fields = [
            "id",
            "key",
            "name",
            "owner",
            "is_default",
            "is_active",
            "version",
            "can_manage",
            "stages",
            "custom_fields",
        ]
        read_only_fields = fields

    def get_can_manage(self, pipeline: m.Pipeline) -> bool:
        actor, scope = self.context.get("actor"), self.context.get("scope")
        if actor is None or scope is None:
            return False
        return configuration.can_manage(actor, scope, pipeline)


class PipelineListSerializer(serializers.Serializer[Any]):
    results = PipelineSerializer(many=True)


class LeadRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(allow_null=True)
    display_name = serializers.CharField(required=False)
    organization_name = serializers.CharField(required=False)
    restricted = serializers.BooleanField()


def lead_ref(opportunity: m.Opportunity, scope: AccessScope) -> dict[str, Any]:
    lead = opportunity.lead
    if not scope.permits_owner(lead.owner_id):
        return {"id": None, "restricted": True}
    return {
        "id": lead.pk,
        "display_name": lead.display_name,
        "organization_name": lead.organization_name,
        "restricted": False,
    }


class _ScopedLead(serializers.Serializer[Any]):
    @extend_schema_field(LeadRefSerializer)
    def get_lead(self, opportunity: m.Opportunity) -> dict[str, Any]:
        return lead_ref(opportunity, self.context["scope"])


CARD_FIELDS = [
    "id",
    "title",
    "account_name",
    "lead",
    "owner",
    "stage_id",
    "status",
    "value",
    "probability",
    "probability_overridden",
    "weighted_value",
    "expected_close_date",
    "negotiated_price",
    "closed_at",
    "archived_at",
    "version",
    "created_at",
    "updated_at",
]


class OpportunityCardSerializer(_ScopedLead, serializers.ModelSerializer[m.Opportunity]):
    """A board card and a list row."""

    lead = serializers.SerializerMethodField()
    owner = UserRefSerializer(read_only=True)
    stage_id = serializers.UUIDField(read_only=True)
    value = money()
    probability = percentage()
    weighted_value = money()
    negotiated_price = money(allow_null=True)

    class Meta:
        model = m.Opportunity
        fields = CARD_FIELDS
        read_only_fields = fields


class OpportunitySerializer(_ScopedLead, serializers.ModelSerializer[m.Opportunity]):
    lead = serializers.SerializerMethodField()
    owner = UserRefSerializer(read_only=True)
    created_by = UserRefSerializer(read_only=True)
    pipeline = PipelineRefSerializer(read_only=True)
    stage = StageSerializer(read_only=True)
    value = money(help_text="The instrument installation price (INR): the deal's value.")
    probability = percentage()
    weighted_value = money()
    negotiated_price = money(allow_null=True, help_text="The latest negotiated price, if any.")
    custom_fields = serializers.DictField(
        read_only=True,
        help_text="Custom field id -> canonical value (strings, booleans or option ids).",
    )

    class Meta:
        model = m.Opportunity
        fields = [
            *(f for f in CARD_FIELDS if f != "stage_id"),
            "pipeline",
            "stage",
            "opportunity_date",
            "customer_name",
            "contact_phone",
            "contact_email",
            "address",
            "instrument_name",
            "work_load",
            "custom_fields",
            "negotiated_at",
            "description",
            "lost_reason",
            "created_by",
        ]
        read_only_fields = fields


class OpportunityPageSerializer(serializers.Serializer[Any]):
    """One keyset-paginated page of opportunities (follow `next` / `previous` as given)."""

    results = OpportunityCardSerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class PipelineTotalsSerializer(serializers.Serializer[Any]):
    """Open opportunities only: won, lost and archived never count."""

    pipeline_value = money(help_text="SUM(value) of open opportunities.")
    weighted_pipeline = money(
        help_text="SUM(value x probability / 100) of open opportunities, rounded once."
    )
    open_count = serializers.IntegerField(read_only=True)


class PipelineSummarySerializer(serializers.Serializer[Any]):
    currency = serializers.CharField()
    totals = PipelineTotalsSerializer()


class BoardColumnSerializer(serializers.Serializer[Any]):
    stage = StageSerializer()
    count = serializers.IntegerField()
    total_value = money()
    weighted_value = money()
    ordering = serializers.ChoiceField(choices=sorted(ORDERINGS))
    cards = OpportunityCardSerializer(many=True)
    next = serializers.CharField(
        allow_null=True,
        help_text="The opportunities list continuing after these cards (same stage, filters "
        "and order), or null when the column shows everything.",
    )


class BoardSerializer(serializers.Serializer[Any]):
    pipeline = PipelineRefSerializer()
    currency = serializers.CharField()
    totals = PipelineTotalsSerializer()
    columns = BoardColumnSerializer(many=True)


class StageHistorySerializer(serializers.ModelSerializer[m.StageHistory]):
    id = OpaqueIdField("stage-history")
    actor = UserRefSerializer(read_only=True)
    value = money()
    probability = percentage()

    class Meta:
        model = m.StageHistory
        fields = [
            "id",
            "from_stage_id",
            "to_stage_id",
            "from_stage_name",
            "to_stage_name",
            "from_status",
            "to_status",
            "value",
            "probability",
            "lost_reason",
            "actor",
            "occurred_at",
        ]
        read_only_fields = fields


class StageHistoryPageSerializer(serializers.Serializer[Any]):
    results = StageHistorySerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class NegotiationPriceSerializer(serializers.ModelSerializer[m.NegotiationPrice]):
    id = OpaqueIdField("negotiation-price")
    price = money()
    actor = UserRefSerializer(read_only=True)

    class Meta:
        model = m.NegotiationPrice
        fields = [
            "id",
            "price",
            "currency",
            "stage_id",
            "stage_name",
            "source",
            "actor",
            "occurred_at",
        ]
        read_only_fields = fields


class NegotiationPricePageSerializer(serializers.Serializer[Any]):
    results = NegotiationPriceSerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class ConversionSerializer(serializers.Serializer[Any]):
    lead = LeadSerializer()
    opportunity = OpportunitySerializer()


# --- input -------------------------------------------------------------------------------------
def _value(**kwargs: Any) -> ExactDecimalField:
    kwargs.setdefault("help_text", 'Amount in the organisation currency (INR), e.g. "1250000.00".')
    return ExactDecimalField(max_whole_digits=m.MONEY_DIGITS - m.MONEY_PLACES, **kwargs)


def _probability(**kwargs: Any) -> ExactDecimalField:
    return ExactDecimalField(
        max_whole_digits=3,
        allow_null=True,
        help_text="A percentage (0-100). Omit (or null) to use the stage's default.",
        **kwargs,
    )


def _text(max_length: int, **kwargs: Any) -> serializers.CharField:
    return serializers.CharField(
        max_length=max_length, allow_blank=True, required=False, trim_whitespace=False, **kwargs
    )


def _custom_values() -> serializers.DictField:
    return serializers.DictField(
        child=serializers.JSONField(allow_null=True),
        required=False,
        help_text="Custom field id -> value (null clears it). Numbers and amounts as strings.",
    )


class DealFieldsMixin(serializers.Serializer[Any]):
    """The opportunity's customer, instrument and custom details (all optional here; the
    account and customer names default to the lead's at creation)."""

    opportunity_date = serializers.DateField(required=False)
    account_name = _text(m.ACCOUNT_NAME_MAX_LENGTH)
    customer_name = _text(m.CUSTOMER_NAME_MAX_LENGTH)
    contact_phone = _text(40)
    contact_email = _text(254)
    address = _text(m.ADDRESS_MAX_LENGTH)
    instrument_name = _text(m.INSTRUMENT_NAME_MAX_LENGTH)
    work_load = _text(m.WORK_LOAD_MAX_LENGTH)
    custom_fields = _custom_values()


class OpportunityFieldsSerializer(DealFieldsMixin, StrictInputSerializer):
    title = serializers.CharField(max_length=m.TITLE_MAX_LENGTH)
    value = _value()
    probability = _probability(required=False)
    expected_close_date = serializers.DateField(allow_null=True, required=False)
    description = serializers.CharField(
        max_length=m.DESCRIPTION_MAX_LENGTH, allow_blank=True, required=False, trim_whitespace=False
    )
    negotiated_price = _value(
        required=False,
        allow_null=True,
        help_text="Required when the stage is a negotiation stage; refused otherwise.",
    )


class OpportunityCreateSerializer(OpportunityFieldsSerializer):
    lead = serializers.UUIDField(help_text="The lead (in this workspace) the opportunity is for.")
    pipeline = serializers.UUIDField(required=False, help_text="The default pipeline if omitted.")
    stage = serializers.UUIDField(
        required=False, help_text="A stage of the pipeline; its first open stage if omitted."
    )
    lost_reason = serializers.CharField(
        max_length=m.LOST_REASON_MAX_LENGTH, allow_blank=True, required=False
    )


class OpportunityUpdateSerializer(DealFieldsMixin, StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=m.TITLE_MAX_LENGTH, required=False)
    value = _value(required=False)
    probability = _probability(required=False)
    expected_close_date = serializers.DateField(allow_null=True, required=False)
    description = serializers.CharField(
        max_length=m.DESCRIPTION_MAX_LENGTH, allow_blank=True, required=False, trim_whitespace=False
    )
    lost_reason = serializers.CharField(
        max_length=m.LOST_REASON_MAX_LENGTH, allow_blank=True, required=False
    )


class OpportunityMoveSerializer(StrictInputSerializer):
    stage = serializers.UUIDField(help_text="An active stage of the opportunity's pipeline.")
    version = serializers.IntegerField(min_value=1)
    lost_reason = serializers.CharField(
        max_length=m.LOST_REASON_MAX_LENGTH,
        allow_blank=True,
        required=False,
        help_text="Optional, only when moving to a lost stage.",
    )
    negotiated_price = _value(
        required=False,
        allow_null=True,
        help_text="Required when moving into a negotiation stage; refused otherwise.",
    )


class NegotiatedPriceInputSerializer(StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)
    price = _value(help_text='The negotiated price (INR), e.g. "1050000.00".')


# --- input: configuration -------------------------------------------------------------------
class StageInputSerializer(StrictInputSerializer):
    id = serializers.UUIDField(required=False, help_text="An existing stage; omit for a new one.")
    name = serializers.CharField(max_length=m.STAGE_NAME_MAX_LENGTH, trim_whitespace=False)
    type = serializers.ChoiceField(choices=m.StageType.choices)
    probability = ExactDecimalField(
        max_whole_digits=3,
        required=False,
        allow_null=True,
        help_text="0-100 for open and negotiation stages; won is 100 and lost 0.",
    )


class FieldOptionInputSerializer(StrictInputSerializer):
    id = serializers.CharField(max_length=10, required=False, help_text="An existing choice.")
    label = serializers.CharField(  # type: ignore[assignment]
        max_length=m.FIELD_OPTION_MAX_LENGTH, trim_whitespace=False
    )


class FieldInputSerializer(StrictInputSerializer):
    id = serializers.UUIDField(required=False, help_text="An existing field; omit for a new one.")
    name = serializers.CharField(max_length=m.FIELD_NAME_MAX_LENGTH, trim_whitespace=False)
    type = serializers.ChoiceField(choices=m.FieldType.choices)
    required = serializers.BooleanField(required=False, default=False)  # type: ignore[assignment]
    options = serializers.ListField(
        child=FieldOptionInputSerializer(), required=False, max_length=m.MAX_FIELD_OPTIONS
    )


def _stages() -> serializers.ListField:
    return serializers.ListField(
        child=StageInputSerializer(), max_length=m.MAX_STAGES_PER_PIPELINE, allow_empty=False
    )


def _fields(**kwargs: Any) -> serializers.ListField:
    return serializers.ListField(
        child=FieldInputSerializer(), max_length=m.MAX_FIELDS_PER_PIPELINE, **kwargs
    )


class PipelineCreateSerializer(StrictInputSerializer):
    name = serializers.CharField(max_length=m.PIPELINE_NAME_MAX_LENGTH, trim_whitespace=False)
    stages = _stages()
    custom_fields = _fields(required=False)


class PipelineRenameSerializer(StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)
    name = serializers.CharField(max_length=m.PIPELINE_NAME_MAX_LENGTH, trim_whitespace=False)


class StagesReplaceSerializer(StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)
    stages = _stages()


class FieldsReplaceSerializer(StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)
    custom_fields = _fields(allow_empty=True)


class PipelineListQuerySerializer(StrictInputSerializer):
    archived = serializers.BooleanField(required=False, default=False)


class OpportunityVersionSerializer(StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)


class LeadConvertSerializer(OpportunityFieldsSerializer):
    version = serializers.IntegerField(min_value=1, help_text="The lead's current version.")
    pipeline = serializers.UUIDField(required=False)
    stage = serializers.UUIDField(required=False)


class _FilterSerializer(StrictInputSerializer):
    owner = serializers.UUIDField(required=False, help_text="Organisation-wide workspace only.")
    lead = serializers.UUIDField(required=False)
    expected_close_from = serializers.DateField(required=False, help_text="Inclusive.")
    expected_close_to = serializers.DateField(required=False, help_text="Inclusive.")
    probability_min = ExactDecimalField(max_whole_digits=3, required=False)
    probability_max = ExactDecimalField(max_whole_digits=3, required=False)

    def _date(self, value: date) -> date:
        if not m.EARLIEST_CLOSE_DATE <= value <= m.LATEST_CLOSE_DATE:
            raise serializers.ValidationError("Enter a date between 2000 and 2099.")
        return value

    def validate_expected_close_from(self, value: date) -> date:
        return self._date(value)

    def validate_expected_close_to(self, value: date) -> date:
        return self._date(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start, end = attrs.get("expected_close_from"), attrs.get("expected_close_to")
        if start and end and start > end:
            raise serializers.ValidationError(
                {"expected_close_to": ["The end date must be on or after the start date."]}
            )
        low, high = attrs.get("probability_min"), attrs.get("probability_max")
        for name, bound in (("probability_min", low), ("probability_max", high)):
            if bound is not None and bound > m.CERTAIN:
                raise serializers.ValidationError({name: ["Enter a percentage from 0 to 100."]})
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError(
                {"probability_max": ["The maximum must be at least the minimum."]}
            )
        return attrs


class BoardQuerySerializer(_FilterSerializer):
    pipeline = serializers.UUIDField(required=False, help_text="The default pipeline if omitted.")
    cards_per_stage = serializers.IntegerField(
        min_value=0, max_value=BOARD_CARDS_MAX, required=False, default=BOARD_CARDS_DEFAULT
    )


class SummaryQuerySerializer(_FilterSerializer):
    pipeline = serializers.UUIDField(required=False, help_text="Every pipeline if omitted.")


class OpportunityListQuerySerializer(_FilterSerializer):
    pipeline = serializers.UUIDField(required=False)
    stage = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=m.StageCategory.choices, required=False)
    archived = serializers.BooleanField(required=False, default=False)
    ordering = serializers.ChoiceField(
        choices=sorted(ORDERINGS), required=False, default=DEFAULT_ORDERING
    )
    cursor = serializers.CharField(max_length=1000, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False, default=25)


class HistoryQuerySerializer(StrictInputSerializer):
    cursor = serializers.CharField(max_length=1000, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False, default=50)
