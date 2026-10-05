"""Identity tables: user accounts, one-time account tokens and the login-throttle log.

Account lifecycle (docs/authorization.md#account-lifecycle):

    invited --accept invitation--> active --deactivate--> deactivated --reactivate--> active
       |                                                        |          (or invited, if the
       +-----------------------deactivate-----------------------+           user never activated)

Users are never hard-deleted: deactivation blocks sign-in and ends every session (via
`session_epoch`) while preserving all CRM history they own. Every state rule below is a
database constraint, so no code path (ORM, raw SQL, a future bug) can store an account in
an impossible state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import F, Q, TextField
from django.db.models.functions import Cast, Lower, Upper
from django.utils import timezone
from django.utils.crypto import salted_hmac

from arkray.core.models import TimeStampedModel, UUIDPrimaryKeyModel

NAME_MAX_LENGTH = 100


def _trigram_index(field: str, name: str) -> GinIndex:
    """Supports `field__icontains` (Django compiles it to UPPER(field::text) LIKE ...)."""
    return GinIndex(
        OpClass(Upper(Cast(field, output_field=TextField())), name="gin_trgm_ops"), name=name
    )


class Role(models.TextChoices):
    ADMIN = "admin", "Admin"
    SALES_USER = "sales_user", "User"


class UserStatus(models.TextChoices):
    INVITED = "invited", "Invited"
    ACTIVE = "active", "Active"
    DEACTIVATED = "deactivated", "Deactivated"


def normalize_email(email: str) -> str:
    """The canonical login identity: surrounding whitespace removed, lower-cased.

    Arkray treats the whole address as case-insensitive (User@Example.com and
    user@example.com are one identity) and deliberately does nothing provider-specific
    (no Gmail dot or +tag stripping). Only ASCII addresses are accepted at the API
    (identity.emails), which rules out look-alike Unicode identities.
    """
    return email.strip().lower()


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def get_by_natural_key(self, username: str | None) -> User:
        return self.get(email=normalize_email(username or ""))

    def create_user(
        self,
        email: str,
        first_name: str,
        last_name: str = "",
        *,
        role: str = Role.SALES_USER,
        password: str | None = None,
        **extra_fields: Any,
    ) -> User:
        """Low-level constructor (bootstrap, fixtures). Product flows use identity.services.

        With a password the account is active; without one it is invited (it cannot sign in
        until the invitation is accepted).
        """
        user = self.model(
            email=normalize_email(email),
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            role=role,
            **extra_fields,
        )
        if password:
            user.set_password(password)
            user.status = UserStatus.ACTIVE
            user.is_active = True
            user.activated_at = timezone.now()
        else:
            user.set_unusable_password()
            user.status = UserStatus.INVITED
            user.is_active = False
        user.save(using=self._db)
        return user

    def create_superuser(
        self,
        email: str,
        first_name: str,
        last_name: str = "",
        password: str | None = None,
        **extra_fields: Any,
    ) -> User:
        """Backs `manage.py createsuperuser`, used only to bootstrap the first admin."""
        if not password:
            raise ValueError("The first administrator must be created with a password.")
        return self.create_user(
            email, first_name, last_name, role=Role.ADMIN, password=password, **extra_fields
        )


class User(UUIDPrimaryKeyModel, TimeStampedModel, AbstractBaseUser):
    # Unique + the lower-case check constraint below = case-insensitive uniqueness that
    # holds even for writes that bypass the ORM.
    email = models.EmailField(max_length=254, unique=True)
    first_name = models.CharField(max_length=NAME_MAX_LENGTH)
    last_name = models.CharField(max_length=NAME_MAX_LENGTH, blank=True, default="")
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.SALES_USER)
    status = models.CharField(max_length=16, choices=UserStatus.choices, default=UserStatus.INVITED)
    # Kept as a column because Django's auth backend (and DRF) check it on every request.
    # A constraint pins it to `status`, so the two can never disagree.
    is_active = models.BooleanField(default=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    # Part of the session auth hash: incrementing it ends every existing session at once
    # (deactivation, email change), including after a later reactivation.
    session_epoch = models.PositiveIntegerField(default=1)
    # Optimistic concurrency for admin edits (PATCH requires the current version).
    version = models.PositiveIntegerField(default=1)
    # Set when an administrator chose the password (an initial or a reset one): until the
    # user picks their own, they can only sign out or change it (docs/authorization.md).
    password_change_required = models.BooleanField(default=False)
    # When the password was last set (by the user or an administrator): shown to
    # administrators instead of anything about the password itself. NULL: never set here.
    password_changed_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    objects = UserManager()

    class Meta:
        db_table = "identity_user"
        indexes = [
            # Admin user list: default order and cursor.
            models.Index(fields=["-created_at", "-id"], name="identity_user_created_idx"),
            # Admin user search (substring match on name and email).
            _trigram_index("first_name", "identity_user_first_trgm"),
            _trigram_index("last_name", "identity_user_last_trgm"),
            _trigram_index("email", "identity_user_email_trgm"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(email=Lower("email")), name="identity_user_email_lowercase"
            ),
            models.CheckConstraint(
                condition=Q(role__in=Role.values), name="identity_user_role_valid"
            ),
            models.CheckConstraint(
                condition=~Q(first_name=""), name="identity_user_first_name_present"
            ),
            models.CheckConstraint(
                condition=Q(status__in=UserStatus.values), name="identity_user_status_valid"
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=UserStatus.ACTIVE, is_active=True)
                    | (~Q(status=UserStatus.ACTIVE) & Q(is_active=False))
                ),
                name="identity_user_is_active_matches_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=UserStatus.INVITED, activated_at__isnull=True)
                    | Q(status=UserStatus.ACTIVE, activated_at__isnull=False)
                    | Q(status=UserStatus.DEACTIVATED)
                ),
                name="identity_user_activated_at_matches_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=UserStatus.DEACTIVATED, deactivated_at__isnull=False)
                    | (~Q(status=UserStatus.DEACTIVATED) & Q(deactivated_at__isnull=True))
                ),
                name="identity_user_deactivated_at_matches_status",
            ),
            # An active account always has a usable password (Django marks unusable ones
            # with a leading "!"): nobody can be active without having set a password.
            models.CheckConstraint(
                condition=~Q(status=UserStatus.ACTIVE) | ~Q(password__startswith="!"),  # noqa: S106
                name="identity_user_active_has_password",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    def _get_session_auth_hash(self, secret: str | None = None) -> str:
        """Bind sessions to the password *and* the session epoch.

        Django stores this HMAC in the session at login and re-checks it on every request.
        Changing the password or bumping `session_epoch` therefore ends every session.
        """
        return salted_hmac(
            "arkray.identity.User.session_auth_hash",
            f"{self.password}\x00{self.session_epoch}",
            secret=secret,
            algorithm="sha256",
        ).hexdigest()


class TokenPurpose(models.TextChoices):
    INVITATION = "invitation", "Invitation"
    PASSWORD_RESET = "password_reset", "Password reset"


class TokenStatus(models.TextChoices):
    PENDING = "pending", "Pending"  # usable until expires_at (if issued)
    USED = "used", "Used"
    REVOKED = "revoked", "Revoked"  # superseded, cancelled by deactivation, ...


class AccountToken(UUIDPrimaryKeyModel):
    """A one-time credential emailed to a user: an invitation or a password reset.

    Only the SHA-256 digest of the secret is stored. The secret itself is minted by the
    email job at send time (identity.services.deliver_account_token), so it exists only in
    that worker's memory and in the email: never in a web request, the outbox or logs. A
    database leak therefore yields nothing usable (the secret has 256 bits of entropy).

    `token_hash` stays NULL until the email job mints the secret; a retry after a failed
    send mints a new one (the undelivered secret is simply overwritten).
    """

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="account_tokens")
    purpose = models.CharField(max_length=24, choices=TokenPurpose.choices)
    status = models.CharField(
        max_length=16, choices=TokenStatus.choices, default=TokenStatus.PENDING
    )
    token_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    issued_at = models.DateTimeField(null=True, blank=True)  # secret minted
    sent_at = models.DateTimeField(null=True, blank=True)  # email accepted by the server
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    # The administrator who sent an invitation; NULL for self-service password resets.
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "identity_account_token"
        indexes = [
            # Latest invitation / reset for a user; per-account reset-request cap.
            models.Index(fields=["user", "purpose", "-created_at"], name="identity_token_user_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(purpose__in=TokenPurpose.values),
                name="identity_account_token_purpose_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=TokenStatus.values),
                name="identity_account_token_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__gt=models.F("created_at")),
                name="identity_account_token_expires_after_creation",
            ),
            models.CheckConstraint(
                condition=(
                    Q(token_hash__isnull=True) | Q(token_hash__regex=r"^[0-9a-f]{64}$")  # noqa: S106 — a format rule
                ),
                name="identity_account_token_hash_format",
            ),
            models.CheckConstraint(
                condition=(
                    Q(token_hash__isnull=True, issued_at__isnull=True)
                    | Q(token_hash__isnull=False, issued_at__isnull=False)
                ),
                name="identity_account_token_issued_iff_hashed",
            ),
            models.CheckConstraint(
                condition=Q(sent_at__isnull=True) | Q(issued_at__isnull=False),
                name="identity_account_token_sent_after_issue",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=TokenStatus.USED, used_at__isnull=False, token_hash__isnull=False)
                    | (~Q(status=TokenStatus.USED) & Q(used_at__isnull=True))
                ),
                name="identity_account_token_used_at_matches_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=TokenStatus.REVOKED, revoked_at__isnull=False)
                    | (~Q(status=TokenStatus.REVOKED) & Q(revoked_at__isnull=True))
                ),
                name="identity_account_token_revoked_at_matches_status",
            ),
            # At most one live token per user and purpose: issuing a new invitation or
            # reset must revoke the previous one first (supersession is DB-enforced).
            models.UniqueConstraint(
                fields=["user", "purpose"],
                condition=Q(status=TokenStatus.PENDING),
                name="identity_account_token_one_pending",
            ),
        ]

    def __str__(self) -> str:
        return f"AccountToken({self.purpose}, {self.status})"

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or timezone.now())


class ThrottleKind(models.TextChoices):
    LOGIN_FAILURE = "login_failure", "Failed sign-in"
    PASSWORD_RESET_REQUEST = "password_reset_request", "Password reset request"


class AuthThrottleEvent(models.Model):
    """Durable evidence for authentication throttling (identity.throttling).

    Lives in PostgreSQL, not Redis, so brute-force protection survives a cache outage.
    Stores no email addresses: `identifier_hash` is a keyed HMAC of the normalised email
    that was *submitted* (whether or not such an account exists). Purged hourly.
    """

    id = models.BigAutoField(primary_key=True)
    kind = models.CharField(max_length=32, choices=ThrottleKind.choices)
    occurred_at = models.DateTimeField(default=timezone.now)
    identifier_hash = models.CharField(max_length=64, blank=True, default="")
    # "" = an unrecognised browser; otherwise the id from a trusted-device cookie.
    device_id = models.CharField(max_length=32, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "identity_auth_throttle_event"
        indexes = [
            models.Index(
                fields=["identifier_hash", "device_id", "-occurred_at"],
                condition=Q(kind=ThrottleKind.LOGIN_FAILURE),
                name="auth_throttle_account_idx",
            ),
            models.Index(
                fields=["kind", "ip_address", "-occurred_at"], name="auth_throttle_source_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=ThrottleKind.values), name="auth_throttle_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(identifier_hash="") | Q(identifier_hash__regex=r"^[0-9a-f]{64}$"),
                name="auth_throttle_identifier_format",
            ),
        ]

    def __str__(self) -> str:
        return f"AuthThrottleEvent({self.kind}, {self.occurred_at:%Y-%m-%d %H:%M:%S})"


class WorkspaceAccessWindow(models.Model):
    """That an actor's viewing of a workspace (a user's, or "all") has been audited in one
    window of WORKSPACE_ACCESS_AUDIT_WINDOW_S (identity.workspaces).

    In PostgreSQL, written in the audit row's own transaction: a rolled-back request leaves
    neither, concurrent first requests write exactly one audit row (the unique constraint),
    and nothing outside the database can suppress auditing (Phase 9 review: the marker was
    a cache key, and a write to Redis hid access indefinitely). Purged hourly."""

    id = models.BigAutoField(primary_key=True)
    actor_id = models.UUIDField()
    workspace = models.CharField(max_length=36)  # a user id, or "all"
    window_start = models.DateTimeField()

    class Meta:
        db_table = "identity_workspace_access_window"
        constraints = [
            models.UniqueConstraint(
                fields=["actor_id", "workspace", "window_start"],
                name="workspace_access_window_unique",
            )
        ]
        indexes = [models.Index(fields=["window_start"], name="workspace_access_window_idx")]

    def __str__(self) -> str:
        return f"WorkspaceAccessWindow({self.workspace}, {self.window_start:%Y-%m-%d %H:%M})"


class SupportEnd(models.TextChoices):
    """Why a support session ended."""

    EXITED = "exited", "Exited"
    EXPIRED = "expired", "Expired"
    SIGNED_OUT = "signed_out", "Signed out"
    # The user was deactivated, became an administrator, or the administrator lost the
    # capability: the session can't continue (checked on every request).
    NOT_ALLOWED = "not_allowed", "No longer allowed"
    # The administrator's browser session changed (a new sign-in elsewhere replaced it, or
    # the session it was bound to ended).
    SESSION_CHANGED = "session_changed", "Browser session changed"


class SupportSession(UUIDPrimaryKeyModel):
    """An administrator's time-limited, audited support access to one user's CRM
    (docs/admin-user-workspace.md#support-sessions). Not impersonation: the administrator
    stays signed in as themselves (their own credentials, their own session), never learns
    or uses the user's password, and every change they make is recorded with them as the
    actor and the user as the subject (plus this session's id).

    Bound to the administrator's browser session (`session_digest`, a SHA-256 of its key)
    and short-lived (`expires_at`); at most one live session per administrator."""

    admin = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    target = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", db_index=False)
    reason = models.CharField(max_length=200, blank=True, default="")
    session_digest = models.CharField(max_length=64)
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=16, choices=SupportEnd.choices, blank=True, default="")

    class Meta:
        db_table = "identity_support_session"
        indexes = [
            # Sessions to close when they expire (the hourly sweep) and a user's history.
            models.Index(
                fields=["expires_at"],
                condition=Q(ended_at__isnull=True),
                name="identity_support_live_idx",
            ),
            models.Index(fields=["target", "-started_at"], name="identity_support_target_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(admin=F("target")), name="identity_support_not_self"
            ),
            models.CheckConstraint(
                condition=Q(expires_at__gt=F("started_at")),
                name="identity_support_expires_after_start",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True, end_reason="")
                | (Q(ended_at__isnull=False) & Q(end_reason__in=SupportEnd.values)),
                name="identity_support_end_complete",
            ),
            models.CheckConstraint(
                condition=Q(session_digest__regex=r"^[0-9a-f]{64}$"),
                name="identity_support_digest_format",
            ),
            models.UniqueConstraint(
                fields=["admin"],
                condition=Q(ended_at__isnull=True),
                name="identity_support_one_live_per_admin",
            ),
        ]

    def __str__(self) -> str:
        return f"SupportSession({self.pk})"

    def is_live(self, now: datetime) -> bool:
        return self.ended_at is None and now < self.expires_at
