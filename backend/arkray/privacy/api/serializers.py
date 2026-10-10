"""Data-subject export and staff pseudonymisation API (privacy.exports, privacy.staff)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from arkray.core.api import StrictInputSerializer

from ..models import DataExport, ExportStatus, SubjectType

REFERENCE_HELP = (
    "The request's ticket or case reference (letters, digits and . _ / # -): evidence of the "
    "identity check, never a description of the person."
)


class ExportRequestSerializer(StrictInputSerializer):
    subject_type = serializers.ChoiceField(choices=SubjectType.choices)
    subject_id = serializers.UUIDField()
    reference = serializers.RegexField(
        r"^[A-Za-z0-9][A-Za-z0-9._/#-]{0,63}$", max_length=64, help_text=REFERENCE_HELP
    )
    identity_verified = serializers.BooleanField(
        help_text="Must be true: you have verified that the requester is the person (or acts "
        "for them) before exporting their data."
    )

    def validate_identity_verified(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError("Verify the requester's identity first.")
        return value


class DataExportSerializer(serializers.ModelSerializer[DataExport]):
    status = serializers.ChoiceField(choices=ExportStatus.choices, read_only=True)

    class Meta:
        model = DataExport
        fields = [
            "id",
            "subject_type",
            "subject_id",
            "reference",
            "status",
            "created_at",
            "ready_at",
            "expires_at",
            "size",
            "sha256",
            "files_included",
            "files_omitted",
            "error_code",
            "downloads",
        ]
        read_only_fields = fields


class DataExportListSerializer(serializers.Serializer[Any]):
    results = DataExportSerializer(many=True)


class PseudonymiseSerializer(StrictInputSerializer):
    confirm_email = serializers.CharField(
        max_length=254, help_text="The account's current email address, to confirm."
    )


class PseudonymiseResultSerializer(serializers.Serializer[Any]):
    conversations = serializers.IntegerField()
    audit_details = serializers.IntegerField()
    support_reasons = serializers.IntegerField()
    tokens_revoked = serializers.IntegerField()
    support_sessions_ended = serializers.IntegerField()
    throttle_events = serializers.IntegerField()
    queued_payloads = serializers.IntegerField()
    access_windows = serializers.IntegerField()
