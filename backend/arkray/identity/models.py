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

import string
from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.password_validation import validate_password
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q, TextField, Value
from django.db.models.functions import Cast, Concat, Left, Lower, Replace, Upper
from django.utils import timezone
from django.utils.crypto import salted_hmac

from arkray.core.models import TimeStampedModel, UUIDPrimaryKeyModel

from .emails import validate_ascii_email

NAME_MAX_LENGTH = 100
# A pseudonymised former staff member (privacy.staff; docs/privacy.md#staff): their name and
# email become these, for good (`User.pseudonymised_at`, adversarial review of 73ff1e3, L4).
PSEUDONYM_DOMAIN = "pseudonymised.invalid"
PSEUDONYM_FIRST_NAME = "Former user"
RESERVED_EMAIL = f"Addresses at {PSEUDONYM_DOMAIN} are reserved for former users."


def _trigram_index(field: str, name: str) -> GinIndex:
    """Supports `field__icontains` (Django compiles it to UPPER(field::text) LIKE ...)."""
    return GinIndex(
        OpClass(Upper(Cast(field, output_field=TextField())), name="gin_trgm_ops"), name=name
    )


class Role(models.TextChoices):
    # The organisation-wide administrator. The stored value stays "admin" (ADR-0035): every
    # administrator from before teams keeps exactly the powers they had, with no data
    # migration, and a previous release reading the table during a rolling deploy sees the
    # role it knows.
    ADMIN = "admin", "Super admin"
    # An administrator of their own team: the sales users whose `manager` they are
    # (ADR-0035). A value the previous release doesn't know, so it grants nothing there.
    TEAM_ADMIN = "team_admin", "Admin"
    SALES_USER = "sales_user", "User"


class UserStatus(models.TextChoices):
    INVITED = "invited", "Invited"
    ACTIVE = "active", "Active"
    DEACTIVATED = "deactivated", "Deactivated"


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def normalize_email(email: str) -> str:
    """The canonical login identity: surrounding whitespace removed, lower-cased.

    Arkray treats the whole address as case-insensitive (User@Example.com and
    user@example.com are one identity) and deliberately does nothing provider-specific
    (no Gmail dot or +tag stripping). Only ASCII addresses are accepted at the API
    (identity.emails), which rules out look-alike Unicode identities.

    Only ASCII letters are folded: `str.lower()` also turned a Kelvin sign (U+212A) into an
    ASCII "k", so an address spelled with one signed in to, and reset the password of, the
    account with the plain "k" (adversarial review of 73ff1e3, AUTH-7). A non-ASCII address
    now never equals a stored one: sign-in and reset requests treat it as unknown.
    """
    return email.strip().translate(_ASCII_LOWER)


def is_reserved_email(email: str) -> bool:
    """An address in the pseudonym domain (or under it): only privacy.staff gives one, to a
    former user (review round 1, IDENTITY-5: the API gave another account a former user's
    pseudonym, and pseudonymising the real one then failed)."""
    domain = normalize_email(email).rpartition("@")[2]
    return domain == PSEUDONYM_DOMAIN or domain.endswith(f".{PSEUDONYM_DOMAIN}")


def pseudonym_email(user_id: UUID) -> str:
    return f"former-{user_id.hex}@{PSEUDONYM_DOMAIN}"


def pseudonym_last_name(user_id: UUID) -> str:
    return user_id.hex[:8]


def _id_hex() -> Replace:
    """`id` as the 32 hex digits `UUID.hex` gives, in SQL (for the pseudonym CHECK)."""
    return Replace(
        Cast("id", output_field=TextField()), Value("-"), Value(""), output_field=TextField()
    )


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
        until the invitation is accepted). An address that isn't ASCII is refused, as at the
        API (identity.emails).
        """
        validate_ascii_email(normalize_email(email))
        if is_reserved_email(email):
            raise ValidationError(RESERVED_EMAIL, code="reserved_email")
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
        """Backs `manage.py createsuperuser`, used only to bootstrap the first admin.

        The password policy (AUTH_PASSWORD_VALIDATORS) runs here, raising ValidationError
        (the command reports it and creates nothing): `createsuperuser --noinput`, the
        documented bootstrap, never ran it, so the first administrator could be created with
        the password "a", and the interactive command lets the operator bypass it (adversarial
        review of 73ff1e3, L11)."""
        if not password:
            raise ValueError("The first administrator must be created with a password.")
        validate_ascii_email(normalize_email(email))
        validate_password(
            password,
            self.model(
                email=normalize_email(email),
                first_name=first_name.strip(),
                last_name=last_name.strip(),
                role=Role.ADMIN,
            ),
        )
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
    # When privacy.staff pseudonymised this former staff member. Set once, for good: the
    # account stays deactivated, password-less and under its pseudonym (CHECK below; a
    # trigger refuses clearing the mark), and user administration refuses every change to it
    # (adversarial review of 73ff1e3, L4: it was an email-domain test nothing else asked).
    pseudonymised_at = models.DateTimeField(null=True, blank=True)
    # The team administrator (role team_admin) who manages this sales user, or NULL: managed
    # by the super administrators only (ADR-0035). Only sales users have one (CHECK below);
    # that it names a team administrator is kept by identity.services under the
    # user-administration lock, and every reader re-checks the manager's own role
    # (identity.teams), so a stale reference grants nothing.
    manager = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="team_members",
        db_index=False,  # the partial index below
    )

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
            # A team administrator's team (identity.teams: workspace resolution, the users
            # list, assignment checks).
            models.Index(
                fields=["manager"],
                name="identity_user_manager_idx",
                condition=Q(manager__isnull=False),
            ),
        ]
        constraints = [
            # Only a sales user is managed by a team administrator; administrators of either
            # kind answer to the super administrators (ADR-0035).
            models.CheckConstraint(
                condition=Q(manager__isnull=True) | Q(role="sales_user"),
                name="identity_user_manager_only_for_sales_users",
            ),
            models.CheckConstraint(
                condition=Q(manager__isnull=True) | ~Q(manager=F("id")),
                name="identity_user_manager_not_self",
            ),
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
            # A pseudonymised account is a former one for good: deactivated, no usable
            # password, and its pseudonym's name and email (adversarial review of 73ff1e3, L4).
            models.CheckConstraint(
                condition=Q(pseudonymised_at__isnull=True)
                | Q(
                    status=UserStatus.DEACTIVATED,
                    password__startswith="!",  # noqa: S106 — Django's unusable-password mark
                    first_name=PSEUDONYM_FIRST_NAME,
                    last_name=Left(_id_hex(), 8),
                    email=Concat(
                        Value("former-"),
                        _id_hex(),
                        Value(f"@{PSEUDONYM_DOMAIN}"),
                        output_field=TextField(),
                    ),
                ),
                name="identity_user_pseudonym_final",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_pseudonymised(self) -> bool:
        return self.pseudonymised_at is not None

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
    # Lifts the account's sign-in lock (identity.throttling; adversarial review of 73ff1e3,
    # L1), mailed to the owner when the lock is placed.
    SIGN_IN_UNLOCK = "sign_in_unlock", "Sign-in unlock"


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
    # A failure the right password (and second factor) followed from the same browser: it no
    # longer counts toward that browser's lockout, but still toward the account's daily cap,
    # so the owner signing in from a new browser doesn't forgive an attacker's day
    # (adversarial review of 73ff1e3, L1). db_default: the previous release still inserts.
    forgiven = models.BooleanField(default=False, db_default=False)

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


class LoginLock(UUIDPrimaryKeyModel):
    """An account whose password sign-in from unrecognised browsers is locked: it had
    LOGIN_DAILY_FAILURE_CAP failures from unrecognised browsers in a day (identity.throttling;
    adversarial review of 73ff1e3, L1). Browsers that signed in to it before keep working.

    Keyed like the throttle, by the keyed hash of the *submitted* email, so an unknown email
    is locked exactly like a real one (nothing tells them apart) and no email is stored.
    Lifted by the emailed unlock link, a password reset or an accepted invitation, a password
    set by an administrator, an administrator's "clear" (identity.services), or after
    LOGIN_LOCK_MAX_AGE_S (identity.housekeeping), whichever comes first."""

    identifier_hash = models.CharField(max_length=64, unique=True)
    locked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "identity_login_lock"
        indexes = [models.Index(fields=["locked_at"], name="identity_login_lock_at_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(identifier_hash__regex=r"^[0-9a-f]{64}$"),
                name="identity_login_lock_identifier_format",
            )
        ]

    def __str__(self) -> str:
        return f"LoginLock({self.locked_at:%Y-%m-%d %H:%M:%S})"


class WorkspaceAccessWindow(models.Model):
    """That an actor's viewing of a workspace (a user's, or "all") has been audited in one
    window of WORKSPACE_ACCESS_AUDIT_WINDOW_S (identity.workspaces).

    In PostgreSQL, written in the audit row's own transaction: a rolled-back request leaves
    neither, concurrent first requests write exactly one audit row (the unique constraint),
    and nothing outside the database can suppress auditing (Phase 9 review: the marker was
    a cache key, and a write to Redis hid access indefinitely). Purged hourly."""

    id = models.BigAutoField(primary_key=True)
    actor_id = models.UUIDField()
    # A user id, "all" (the organisation) or "team" (a team administrator's "all", ADR-0035:
    # its own window, so a promotion within one still audits the organisation).
    workspace = models.CharField(max_length=36)
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


class MfaLock(models.TextChoices):
    """What unlocks a second factor locked by wrong codes (deep security audit M-4, R133)."""

    # MFA_MAX_CONSECUTIVE_FAILURES in a row: a password reset by email, a password set by an
    # administrator, or an MFA reset.
    PASSWORD_RESET = "password_reset", "A new password"
    # MFA_MAX_FAILURES_BEFORE_RESET since the last right code, new passwords notwithstanding:
    # only an MFA reset (an administrator for an ordinary user, `reset_mfa` for anyone).
    MFA_RESET = "mfa_reset", "An MFA reset"


class MfaDevice(UUIDPrimaryKeyModel, TimeStampedModel):
    """A user's authenticator (TOTP, RFC 6238; identity.mfa, ADR-0033). At most one per user.

    `secret` is the shared secret encrypted (Fernet, under a key derived from SECRET_KEY and
    its fallbacks): never stored, logged or returned in clear after setup. The device counts
    only once a code from it was confirmed (`confirmed_at`); an unconfirmed one is a setup in
    progress. `last_used_step` is the last accepted 30-second step: a code for it or an
    earlier one is refused (replay), checked and updated under this row's lock.

    A setup in progress lasts until `pending_until` (MFA_SETUP_TTL_S) and allows
    MFA_SETUP_MAX_ATTEMPTS wrong codes (`setup_attempts`); then it is deleted (also by the
    hourly housekeeping) and must start again.

    `consecutive_failures` counts wrong codes (authenticator or recovery) since the last right
    one, across sign-in attempts, browsers and step-ups (deep security audit M-4): from
    MFA_MAX_CONSECUTIVE_FAILURES on, every code is refused until a password reset or an MFA
    reset. `failure_notified_at`: when the owner was last emailed about a wrong code.

    `cumulative_failures` counts the same wrong codes, but only a right code forgets them (or
    an MFA reset, which deletes the device): a new password doesn't (R133). From
    MFA_MAX_FAILURES_BEFORE_RESET on, every code is refused until an MFA reset, so someone
    holding the password and the mailbox can't buy more guesses with each emailed reset."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="mfa_device")
    secret = models.TextField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_step = models.BigIntegerField(null=True, blank=True)
    pending_until = models.DateTimeField(null=True, blank=True)
    setup_attempts = models.PositiveSmallIntegerField(default=0)
    # db_default: the previous release (which never names the column) can still insert.
    consecutive_failures = models.PositiveIntegerField(default=0, db_default=0)
    failure_notified_at = models.DateTimeField(null=True, blank=True)
    # db_default likewise (identity.0007, R133).
    cumulative_failures = models.PositiveIntegerField(default=0, db_default=0)

    class Meta:
        db_table = "identity_mfa_device"
        constraints = [
            # A Fernet token (urlsafe base64, "gAAAAA..."), never a bare base32 secret.
            models.CheckConstraint(
                condition=Q(secret__startswith="gAAAAA"),  # noqa: S106 — a format rule
                name="identity_mfa_secret_encrypted",
            ),
            models.CheckConstraint(
                condition=Q(last_used_step__isnull=True) | Q(last_used_step__gte=0),
                name="identity_mfa_step_nonnegative",
            ),
            # Codes are accepted only from a confirmed device.
            models.CheckConstraint(
                condition=Q(last_used_step__isnull=True) | Q(confirmed_at__isnull=False),
                name="identity_mfa_step_after_confirmation",
            ),
            # A pending setup has a deadline; a confirmed device has none.
            models.CheckConstraint(
                condition=Q(confirmed_at__isnull=True, pending_until__isnull=False)
                | Q(confirmed_at__isnull=False, pending_until__isnull=True),
                name="identity_mfa_pending_has_deadline",
            ),
        ]
        indexes = [
            # Expired setups, purged hourly (identity.housekeeping).
            models.Index(
                fields=["pending_until"],
                condition=Q(confirmed_at__isnull=True),
                name="identity_mfa_pending_idx",
            )
        ]

    def __str__(self) -> str:
        return f"MfaDevice({'confirmed' if self.confirmed_at else 'pending'})"

    @staticmethod
    def lock_for(consecutive: int | None, cumulative: int | None) -> MfaLock | None:
        """What unlocks a factor with these counts, or None when codes are accepted. The one
        rule, for a device and for counts read alongside a user (selectors.admin_user_*)."""
        if (cumulative or 0) >= settings.MFA_MAX_FAILURES_BEFORE_RESET:
            return MfaLock.MFA_RESET
        if (consecutive or 0) >= settings.MFA_MAX_CONSECUTIVE_FAILURES:
            return MfaLock.PASSWORD_RESET
        return None

    @property
    def lock(self) -> MfaLock | None:
        return self.lock_for(self.consecutive_failures, self.cumulative_failures)

    @property
    def locked(self) -> bool:
        """Too many wrong codes: no code is accepted (deep security audit M-4, R133)."""
        return self.lock is not None

    @property
    def locked_until_reset(self) -> bool:
        """Locked past what a new password unlocks: only an MFA reset does (R133)."""
        return self.lock == MfaLock.MFA_RESET


class MfaRecoveryCode(UUIDPrimaryKeyModel):
    """One single-use recovery code (ADR-0033): only its SHA-256 is stored. Ten are issued
    when a device is confirmed or the codes are replaced; replacing deletes the old ones."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mfa_recovery_codes")
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "identity_mfa_recovery_code"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "code_hash"], name="identity_mfa_recovery_code_unique"
            ),
            models.CheckConstraint(
                condition=Q(code_hash__regex=r"^[0-9a-f]{64}$"),
                name="identity_mfa_recovery_code_hash_format",
            ),
        ]

    def __str__(self) -> str:
        return f"MfaRecoveryCode({'used' if self.used_at else 'unused'})"
