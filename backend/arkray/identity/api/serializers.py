"""Identity serializers.

Output serializers list their fields explicitly: password hashes, token digests, session
epochs and other security metadata are never serialised. Input serializers are strict:
undeclared keys (`is_superuser`, `capabilities`, `status`, ...) are rejected with a 400,
and only the fields a given operation may change are declared at all.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from arkray.core.api import OpaqueIdField, StrictInputSerializer
from arkray.core.text import SEARCH_MAX_LENGTH, SEARCH_MIN_LENGTH

from ..emails import validate_ascii_email
from ..models import NAME_MAX_LENGTH, Role, SupportSession, User, UserStatus
from ..policy import capabilities_for
from ..support import REASON_MAX_LENGTH

EMAIL_MAX_LENGTH = 254
# Generous parse limit; the password policy itself caps new passwords at 128 characters.
PASSWORD_INPUT_MAX_LENGTH = 1024
TOKEN_INPUT_MAX_LENGTH = 128


def _password_field() -> serializers.CharField:
    # Never trim: leading/trailing spaces are part of a password.
    return serializers.CharField(
        max_length=PASSWORD_INPUT_MAX_LENGTH,
        trim_whitespace=False,
        style={"input_type": "password"},
    )


def _email_field() -> serializers.EmailField:
    return serializers.EmailField(max_length=EMAIL_MAX_LENGTH, validators=[validate_ascii_email])


# --- output -----------------------------------------------------------------------------------
class ViewerFeaturesSerializer(serializers.Serializer[Any]):
    """Deployment-wide features the UI should offer (configuration, not permissions)."""

    ask = serializers.BooleanField(help_text="Ask Arkray is turned on for this CRM.")


class PersonSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    full_name = serializers.CharField()


class SupportSessionSerializer(serializers.ModelSerializer[SupportSession]):
    """A live support session: whose CRM, since when, until when (never extended)."""

    target = PersonSerializer(read_only=True)

    class Meta:
        model = SupportSession
        fields = ["id", "target", "reason", "started_at", "expires_at"]
        read_only_fields = fields


class ViewerSerializer(serializers.ModelSerializer[User]):
    """The signed-in user (GET /auth/me and the sign-in response). Always the person signed
    in, also during a support session (which is described separately)."""

    full_name = serializers.CharField(read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)
    capabilities = serializers.SerializerMethodField()
    features = serializers.SerializerMethodField()
    support_session = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "role",
            "role_label",
            "capabilities",
            "features",
            "password_change_required",
            "support_session",
        ]
        read_only_fields = fields

    @extend_schema_field(SupportSessionSerializer(allow_null=True))
    def get_support_session(self, user: User) -> dict[str, Any] | None:
        session = self.context.get("support_session")
        return None if session is None else dict(SupportSessionSerializer(session).data)

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_capabilities(self, user: User) -> list[str]:
        return sorted(capability.value for capability in capabilities_for(user))

    @extend_schema_field(ViewerFeaturesSerializer)
    def get_features(self, user: User) -> dict[str, bool]:
        return {"ask": bool(settings.AI_ENABLED)}


class InvitationStateSerializer(serializers.Serializer[Any]):
    expires_at = serializers.DateTimeField()
    sent_at = serializers.DateTimeField(allow_null=True)
    expired = serializers.BooleanField()


class AdminUserSerializer(serializers.ModelSerializer[User]):
    """A user as administrators see it. Expects selectors.admin_user_* querysets."""

    full_name = serializers.CharField(read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    invitation = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "role",
            "role_label",
            "status",
            "status_label",
            "last_login",
            "created_at",
            "activated_at",
            "deactivated_at",
            "invitation",
            "password_change_required",
            "password_changed_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(InvitationStateSerializer(allow_null=True))
    def get_invitation(self, user: User) -> dict[str, Any] | None:
        expires_at = getattr(user, "invitation_expires_at", None)
        if user.status != UserStatus.INVITED or expires_at is None:
            return None
        return InvitationStateSerializer(
            {
                "expires_at": expires_at,
                "sent_at": getattr(user, "invitation_sent_at", None),
                "expired": expires_at <= timezone.now(),
            }
        ).data


class AdminUserPageSerializer(serializers.Serializer[Any]):
    """One keyset-paginated page of users (follow `next` / `previous` as given)."""

    results = AdminUserSerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class WorkspaceSubjectSerializer(serializers.ModelSerializer[User]):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "full_name", "status"]
        read_only_fields = fields


class WorkspaceSerializer(serializers.Serializer[Any]):
    kind = serializers.ChoiceField(choices=["self", "user", "organization"])
    subject = WorkspaceSubjectSerializer(allow_null=True)


class AssigneeSerializer(serializers.ModelSerializer[User]):
    """A user CRM records can be assigned to (for owner pickers)."""

    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "full_name", "email"]
        read_only_fields = fields


class AssigneePageSerializer(serializers.Serializer[Any]):
    results = AssigneeSerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class InvitationPreviewSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()
    first_name = serializers.CharField()


class DetailSerializer(serializers.Serializer[Any]):
    detail = serializers.CharField()


# --- input ------------------------------------------------------------------------------------
class LoginSerializer(StrictInputSerializer):
    # Not an EmailField: a malformed address must fail exactly like an unknown one.
    email = serializers.CharField(max_length=EMAIL_MAX_LENGTH)
    password = _password_field()


class PasswordChangeSerializer(StrictInputSerializer):
    current_password = _password_field()
    new_password = _password_field()


class PasswordResetRequestSerializer(StrictInputSerializer):
    email = serializers.CharField(max_length=EMAIL_MAX_LENGTH)


class PasswordResetConfirmSerializer(StrictInputSerializer):
    token = serializers.CharField(max_length=TOKEN_INPUT_MAX_LENGTH)
    new_password = _password_field()


class InvitationTokenSerializer(StrictInputSerializer):
    token = serializers.CharField(max_length=TOKEN_INPUT_MAX_LENGTH)


class InvitationAcceptSerializer(StrictInputSerializer):
    token = serializers.CharField(max_length=TOKEN_INPUT_MAX_LENGTH)
    password = _password_field()


class UserListQuerySerializer(StrictInputSerializer):
    q = serializers.CharField(
        max_length=SEARCH_MAX_LENGTH, min_length=SEARCH_MIN_LENGTH, required=False, allow_blank=True
    )
    status = serializers.ChoiceField(choices=UserStatus.choices, required=False)
    role = serializers.ChoiceField(choices=Role.choices, required=False)
    cursor = serializers.CharField(max_length=500, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False)


class AssigneeQuerySerializer(StrictInputSerializer):
    q = serializers.CharField(
        max_length=SEARCH_MAX_LENGTH, min_length=SEARCH_MIN_LENGTH, required=False, allow_blank=True
    )
    cursor = serializers.CharField(max_length=1000, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False, default=100)


class UserCreateSerializer(StrictInputSerializer):
    first_name = serializers.CharField(max_length=NAME_MAX_LENGTH)
    last_name = serializers.CharField(
        max_length=NAME_MAX_LENGTH, required=False, allow_blank=True, default=""
    )
    email = _email_field()
    role = serializers.ChoiceField(choices=Role.choices)
    password = serializers.CharField(
        max_length=PASSWORD_INPUT_MAX_LENGTH,
        trim_whitespace=False,
        required=False,
        style={"input_type": "password"},
        help_text="An initial password (the user must change it when they first sign in). "
        "Omit it to email an invitation instead. Stored only as a hash; never returned.",
    )


class SetPasswordSerializer(StrictInputSerializer):
    version = serializers.IntegerField(min_value=1)
    new_password = _password_field()


class SupportSessionStartSerializer(StrictInputSerializer):
    user = serializers.UUIDField(help_text="The user whose CRM to open.")
    reason = serializers.CharField(
        max_length=REASON_MAX_LENGTH, required=False, allow_blank=True, default=""
    )


class SecurityEventQuerySerializer(StrictInputSerializer):
    cursor = serializers.CharField(max_length=1000, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=50, required=False, default=20)


class SecurityEventSerializer(serializers.Serializer[Any]):
    """One account or security event (identity.selectors.SECURITY_ACTIONS): who did what to
    which account, and when. Never a password, a hash, a token or a link."""

    id = OpaqueIdField("security-event")
    action = serializers.CharField()
    occurred_at = serializers.DateTimeField()
    actor = PersonSerializer(allow_null=True, help_text="Null when the system acted.")
    user = PersonSerializer(allow_null=True, help_text="The account concerned.")
    details = serializers.DictField(child=serializers.CharField(), help_text="Allowlisted.")
    in_support_session = serializers.BooleanField()


class SecurityEventPageSerializer(serializers.Serializer[Any]):
    results = SecurityEventSerializer(many=True)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)


class UserUpdateSerializer(StrictInputSerializer):
    first_name = serializers.CharField(max_length=NAME_MAX_LENGTH, required=False)
    last_name = serializers.CharField(max_length=NAME_MAX_LENGTH, required=False, allow_blank=True)
    role = serializers.ChoiceField(choices=Role.choices, required=False)
    version = serializers.IntegerField(min_value=1)


class EmailChangeSerializer(StrictInputSerializer):
    email = _email_field()
    version = serializers.IntegerField(min_value=1)
    # Required only to change one's own email (re-authentication).
    current_password = serializers.CharField(
        max_length=PASSWORD_INPUT_MAX_LENGTH,
        trim_whitespace=False,
        required=False,
        style={"input_type": "password"},
    )
