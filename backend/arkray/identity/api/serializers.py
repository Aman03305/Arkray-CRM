"""Identity serializers.

Output serializers list their fields explicitly: password hashes, token digests, session
epochs and other security metadata are never serialised. Input serializers are strict:
undeclared keys (`is_superuser`, `capabilities`, `status`, ...) are rejected with a 400,
and only the fields a given operation may change are declared at all.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from arkray.core.api import OpaqueIdField, StrictInputSerializer
from arkray.core.text import SEARCH_MAX_LENGTH, SEARCH_MIN_LENGTH

from .. import mfa_services, throttling
from ..emails import validate_ascii_email
from ..models import (
    NAME_MAX_LENGTH,
    MfaDevice,
    MfaLock,
    Role,
    SupportSession,
    User,
    UserStatus,
)
from ..policy import capabilities_for
from ..selectors import NO_MANAGER
from ..support import REASON_MAX_LENGTH

EMAIL_MAX_LENGTH = 254
_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# Generous parse limit; the password policy itself caps new passwords at 128 characters.
PASSWORD_INPUT_MAX_LENGTH = 1024
TOKEN_INPUT_MAX_LENGTH = 128
MFA_CODE_INPUT_MAX_LENGTH = 64
MFA_LOCK_HELP = (
    "Null while codes are accepted. Otherwise too many wrong codes locked the second factor"
    " (no code is accepted, the right one included) and this says what unlocks it:"
    " `password_reset` (a password reset by email, a password set by an administrator, or an"
    " MFA reset) or `mfa_reset` (only an MFA reset: too many wrong codes since the last right"
    " one, across new passwords; R133)."
)


def _mfa_lock_field() -> serializers.ChoiceField:
    return serializers.ChoiceField(
        choices=MfaLock.choices, allow_null=True, help_text=MFA_LOCK_HELP
    )


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
            "mfa_enabled",
            "mfa_enrollment_required",
        ]
        read_only_fields = fields

    mfa_enabled = serializers.SerializerMethodField(
        help_text="The account has a confirmed second factor (two-step verification)."
    )
    mfa_enrollment_required = serializers.SerializerMethodField(
        help_text="The account must set up two-step verification before anything else "
        "(administrators, when required): only /auth/me, sign-out, the password change and "
        "the /auth/mfa setup answer until then."
    )

    @extend_schema_field(serializers.BooleanField())
    def get_mfa_enabled(self, user: User) -> bool:
        return mfa_services.has_confirmed_device(user)

    @extend_schema_field(serializers.BooleanField())
    def get_mfa_enrollment_required(self, user: User) -> bool:
        return mfa_services.enrollment_required(user)

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
    manager = serializers.SerializerMethodField(
        help_text="The Admin (role team_admin) whose team this user is in, or null: a user in"
        " no team (managed by super admins) or an administrator (ADR-0035)."
    )

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
            "manager",
            "password_change_required",
            "password_changed_at",
            "mfa_enabled",
            "mfa_lock",
            "sign_in_locked_at",
            "pseudonymised_at",
            "version",
        ]
        read_only_fields = fields

    mfa_enabled = serializers.SerializerMethodField(
        help_text="The user has a confirmed second factor."
    )
    mfa_lock = serializers.SerializerMethodField(help_text=MFA_LOCK_HELP)
    sign_in_locked_at = serializers.SerializerMethodField(
        help_text="Null, or when too many failed sign-ins in a day locked this account's"
        " sign-in from browsers it hasn't been used in (those it has keep working). The owner"
        " was emailed an unlock link; a password reset or POST .../clear-sign-in-lock lifts it."
    )
    pseudonymised_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Null, or when this former user was pseudonymised: then nothing about the"
        " account can be changed any more (422 `account_pseudonymised`).",
    )

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_sign_in_locked_at(self, user: User) -> str | None:
        # A list passes them all at once (one query); a single user is looked up.
        locks = self.context.get("sign_in_locks")
        locked_at: datetime | None = (
            locks.get(user.email)
            if isinstance(locks, dict)
            else throttling.sign_in_locked_at(user.email)
        )
        return (
            None if locked_at is None else serializers.DateTimeField().to_representation(locked_at)
        )

    @extend_schema_field(serializers.BooleanField())
    def get_mfa_enabled(self, user: User) -> bool:
        annotated = getattr(user, "mfa_enabled", None)  # selectors.admin_user_*
        return bool(annotated) if annotated is not None else mfa_services.has_confirmed_device(user)

    @extend_schema_field(_mfa_lock_field())
    def get_mfa_lock(self, user: User) -> str | None:
        annotated = user.__dict__
        if "mfa_consecutive_failures" in annotated:  # selectors.admin_user_*
            lock = MfaDevice.lock_for(
                annotated["mfa_consecutive_failures"], annotated["mfa_cumulative_failures"]
            )
        else:
            device = MfaDevice.objects.filter(user_id=user.pk, confirmed_at__isnull=False).first()
            lock = device.lock if device is not None else None
        return None if lock is None else lock.value

    @extend_schema_field(PersonSerializer(allow_null=True))
    def get_manager(self, user: User) -> dict[str, Any] | None:
        if user.manager_id is None:
            return None
        annotated = user.__dict__
        if "manager_first_name" in annotated:  # selectors.admin_user_*: no query per row
            first, last = annotated["manager_first_name"], annotated["manager_last_name"]
        else:
            first, last = User.objects.filter(pk=user.manager_id).values_list(
                "first_name", "last_name"
            )[0]
        full_name = f"{first} {last}".strip()
        return dict(PersonSerializer({"id": user.manager_id, "full_name": full_name}).data)

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
    kind = serializers.ChoiceField(choices=["self", "user", "team", "organization"])
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


class MfaChallengeSerializer(serializers.Serializer[Any]):
    """202 from /auth/login: the password was right; send a code to /auth/login/mfa."""

    mfa_required = serializers.BooleanField()
    expires_in = serializers.IntegerField(help_text="Seconds left for the second step.")


class MfaStatusSerializer(serializers.Serializer[Any]):
    """The two-step verification state of one's own account."""

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        # Built here: a declared field named `required` would shadow Field.required.
        return {
            "enabled": serializers.BooleanField(),
            "confirmed_at": serializers.DateTimeField(allow_null=True),
            "recovery_codes_remaining": serializers.IntegerField(),
            "required": serializers.BooleanField(help_text="Required for this account's role."),
            "can_disable": serializers.BooleanField(),
            "lock": _mfa_lock_field(),
        }


class QrMatrixSerializer(serializers.Serializer[Any]):
    size = serializers.IntegerField(help_text="Modules per side (no quiet zone).")
    rows = serializers.ListField(
        child=serializers.CharField(), help_text='One string of "1" (dark) and "0" per row.'
    )


class MfaSetupSerializer(serializers.Serializer[Any]):
    """Shown once, at setup: the secret is never returned again."""

    secret = serializers.CharField(help_text="Base32, for typing into an authenticator app.")
    otpauth_uri = serializers.CharField()
    qr = QrMatrixSerializer()


class RecoveryCodesSerializer(serializers.Serializer[Any]):
    recovery_codes = serializers.ListField(
        child=serializers.CharField(), help_text="Ten single-use codes, shown once."
    )


class DetailSerializer(serializers.Serializer[Any]):
    detail = serializers.CharField()


# --- input ------------------------------------------------------------------------------------
class LoginSerializer(StrictInputSerializer):
    # Not an EmailField: a malformed address must fail exactly like an unknown one.
    email = serializers.CharField(max_length=EMAIL_MAX_LENGTH)
    password = _password_field()


def _code_field(**extra: Any) -> serializers.CharField:
    return serializers.CharField(max_length=MFA_CODE_INPUT_MAX_LENGTH, **extra)


class CodeOrRecoveryCodeSerializer(StrictInputSerializer):
    """Exactly one of `code` (the authenticator's six digits) and `recovery_code`."""

    code = _code_field(required=False)
    recovery_code = _code_field(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if ("code" in attrs) == ("recovery_code" in attrs):
            message = "Enter either a code from your authenticator app or a recovery code."
            raise serializers.ValidationError({"non_field_errors": [message]})
        return attrs


class MfaSetupStartSerializer(StrictInputSerializer):
    current_password = _password_field()


class MfaCodeSerializer(StrictInputSerializer):
    code = _code_field(help_text="A code from the authenticator app.")


class MfaDisableSerializer(StrictInputSerializer):
    current_password = _password_field()
    code = _code_field(help_text="A code from the authenticator app, or an unused recovery code.")


class PasswordChangeSerializer(StrictInputSerializer):
    current_password = _password_field()
    new_password = _password_field()


class PasswordResetRequestSerializer(StrictInputSerializer):
    email = serializers.CharField(max_length=EMAIL_MAX_LENGTH)


class SignInUnlockSerializer(StrictInputSerializer):
    token = serializers.CharField(max_length=TOKEN_INPUT_MAX_LENGTH)


class SignInUnlockPreviewSerializer(serializers.Serializer[Any]):
    email = serializers.CharField(help_text="The account's address, masked: r***@example.com.")


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
    manager = serializers.CharField(
        max_length=36,
        required=False,
        help_text="A team: an Admin's id, or `none` for the users in no team (ADR-0035)."
        " Intersected with what the caller administers.",
    )
    cursor = serializers.CharField(max_length=500, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False)

    def validate_manager(self, value: str) -> UUID | str:
        if value == NO_MANAGER:
            return NO_MANAGER
        if not _CANONICAL_UUID.fullmatch(value):
            raise serializers.ValidationError("Enter an Admin's id, or none.")
        return UUID(value)


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
    manager = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="A new user's Admin (an active or invited team_admin), for role sales_user"
        " only (ADR-0035). Super admins only: an Admin's new users are always in their own"
        " team (null or their own id; anything else is 403).",
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
    role = serializers.ChoiceField(
        choices=Role.choices, required=False, help_text="Super admins only (ADR-0035)."
    )
    manager = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Move a user into an Admin's team (an active or invited team_admin), or out"
        " of any (null). Super admins only; the user must be, or become in the same request,"
        " a sales_user (ADR-0035).",
    )
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
