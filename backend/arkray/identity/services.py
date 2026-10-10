"""Identity use cases: user administration and the emailed account-token flows.

Concurrency rules (docs/authorization.md#concurrency):
- Every user-administration write takes one transaction-scoped advisory lock first. Admin
  writes are rare, and serialising them makes cross-checks (duplicate emails, "is there
  another administrator left?", an admin being deactivated mid-request) trivially correct.
  The acting administrator is re-read *under* that lock: a manager who was deactivated or
  demoted a moment ago can no longer act.
- Account-token flows lock the user row, then the token row (always in that order), so a
  resend racing an activation, or two confirmations of one link, serialise cleanly.
- Database constraints (unique email, one pending token per user and purpose, lifecycle
  CHECKs) remain the final arbiter.

Secrets never pass through here in plaintext except `deliver_account_token`, which mints
the one-time secret, stores its digest and emails the link, all inside the email job, and an
administrator-chosen password (a new user's initial one, or a reset), which exists only in
the request that sets it: it is hashed at once, never logged, audited, queued or returned,
and the user must replace it at their first sign-in (docs/authorization.md#admin-set-passwords).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import outbox
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)
from arkray.core.text import TextRejected, clean_line

from . import emails, selectors, throttling, tokens
from .models import (
    AccountToken,
    Role,
    TokenPurpose,
    TokenStatus,
    User,
    UserStatus,
    normalize_email,
)
from .passwords import check_new_password
from .policy import Capability, has_capability, roles_with

logger = logging.getLogger(__name__)

TOPIC_DELIVER_ACCOUNT_TOKEN = "identity.deliver_account_token"  # noqa: S105 — topic
TOPIC_PASSWORD_RESET_REQUESTED = "identity.password_reset_requested"  # noqa: S105
TOPIC_EMAIL_CHANGED = "identity.notify_email_changed"

AUDIT_USER_CREATED = "user.created"
AUDIT_INVITATION_CREATED = "user.invitation_created"
AUDIT_INVITATION_RESENT = "user.invitation_resent"
AUDIT_USER_ACTIVATED = "user.activated"
AUDIT_USER_DEACTIVATED = "user.deactivated"
AUDIT_USER_REACTIVATED = "user.reactivated"
AUDIT_ROLE_CHANGED = "user.role_changed"
AUDIT_PROFILE_UPDATED = "user.profile_updated"
AUDIT_EMAIL_CHANGED = "user.email_changed"
AUDIT_PASSWORD_RESET_ISSUED = "auth.password_reset_issued"  # noqa: S105
AUDIT_PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"  # noqa: S105
AUDIT_PASSWORD_SET_BY_ADMIN = "auth.password_set_by_admin"  # noqa: S105
AUDIT_PASSWORD_RESET_SUPPRESSED = "auth.password_reset_suppressed"  # noqa: S105

EDITABLE_FIELDS = frozenset({"first_name", "last_name", "role"})
EMAIL_TAKEN = "A user with this email address already exists."
# Advisory lock (namespace "ARK2", key 1) serialising user-administration writes.
_ADMIN_LOCK = (0x41524B32, 1)


class InvalidTokenError(DomainError):
    """Unknown, used, superseded, revoked or expired link: deliberately one answer."""

    code = "invalid_token"
    http_status = 400
    default_message = "This link is invalid or has expired. Ask for a new one."


# --- helpers ---------------------------------------------------------------------------------
def _serialise_user_administration() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(_ADMIN_LOCK))


def _acting_manager(actor_id: UUID) -> User:
    """The acting administrator, re-read under the admin lock."""
    actor = User.objects.filter(pk=actor_id).first()
    if actor is None or not has_capability(actor, Capability.USERS_MANAGE):
        raise PermissionDeniedError()
    return actor


def _lock_user(user_id: UUID) -> User:
    user = User.objects.select_for_update().filter(pk=user_id).first()
    if user is None:
        raise NotFoundError()
    return user


def _manages_users(user: User) -> bool:
    return user.status == UserStatus.ACTIVE and user.role in roles_with(Capability.USERS_MANAGE)


def _is_administrator(user: User) -> bool:
    """An administrator account whatever its status: an invited or deactivated one is
    protected like an active one, or deactivating it would unlock its credentials."""
    return user.role in roles_with(Capability.USERS_MANAGE)


def _refuse_for_another_administrator(actor: User, user: User, message: str) -> None:
    """An administrator's credentials and standing are changed only by that administrator
    (risk R100: email change, then Forgot password at the new address, was a takeover).
    Called with the target's *locked* row, under the admin lock, so a concurrent promotion
    or demotion can't slip between the check and the write."""
    if user.pk != actor.pk and _is_administrator(user):
        raise BusinessRuleViolation(message)


def _another_manager_remains(excluding: UUID) -> bool:
    return (
        User.objects.filter(status=UserStatus.ACTIVE, role__in=roles_with(Capability.USERS_MANAGE))
        .exclude(pk=excluding)
        .exists()
    )


def _require_version(user: User, version: int) -> None:
    if user.version != version:
        raise ConflictError()


def _email_conflict(email: str, *, excluding: UUID | None = None) -> ConflictError | None:
    others = User.objects.filter(email=email)
    if excluding is not None:
        others = others.exclude(pk=excluding)
    return ConflictError(EMAIL_TAKEN, details={"email": [EMAIL_TAKEN]}) if others.exists() else None


def _revoke_pending(
    user: User, now: datetime, purposes: Collection[TokenPurpose] | None = None
) -> int:
    pending = AccountToken.objects.filter(user=user, status=TokenStatus.PENDING)
    if purposes is not None:
        pending = pending.filter(purpose__in=purposes)
    return pending.update(status=TokenStatus.REVOKED, revoked_at=now)


def _revoke_invitations_sent_by(manager: User, now: datetime) -> int:
    """A manager who is removed or demoted takes their outstanding invitations with them,
    so they cannot leave a pre-approved account (possibly an admin) behind."""
    return AccountToken.objects.filter(
        created_by=manager, purpose=TokenPurpose.INVITATION, status=TokenStatus.PENDING
    ).update(status=TokenStatus.REVOKED, revoked_at=now)


def _issue_token(
    user: User, purpose: TokenPurpose, *, created_by: User | None, now: datetime
) -> AccountToken:
    """Supersede the user's live token of this purpose and queue a new one for email."""
    _revoke_pending(user, now, [purpose])
    ttl = (
        settings.ACCOUNT_INVITATION_TTL_S
        if purpose == TokenPurpose.INVITATION
        else settings.PASSWORD_RESET_TTL_S
    )
    token = AccountToken.objects.create(
        user=user,
        purpose=purpose,
        created_by=created_by,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    outbox.enqueue(TOPIC_DELIVER_ACCOUNT_TOKEN, {"token_id": str(token.pk)})
    return token


def _audit_user(action: str, actor_id: UUID | None, user: User, **metadata: object) -> None:
    audit.record(
        action, actor_id=actor_id, target_type="user", target_id=user.pk, metadata=metadata
    )


def _audit_user_sensitive(
    action: str,
    actor_id: UUID | None,
    user: User,
    sensitive: dict[str, object] | None,
    **metadata: object,
) -> None:
    """With personal details kept only for the audit detail's retention (audit.services)."""
    audit.record(
        action,
        actor_id=actor_id,
        target_type="user",
        target_id=user.pk,
        metadata=metadata,
        sensitive=sensitive,
    )


# --- user administration --------------------------------------------------------------------
def _clean_name(value: str, field: str) -> str:
    """A person's name, under the same text rules as every CRM field (Phase 9 review: names
    took control, bidi, zero-width and tag characters and line breaks, which reached the
    owner pickers and the lines of invitation emails)."""
    try:
        return clean_line(value)
    except TextRejected as exc:
        raise InvalidInputError(details={field: [str(exc)]}) from None


def create_user(
    *,
    actor_id: UUID,
    email: str,
    first_name: str,
    last_name: str,
    role: str,
    password: str | None = None,
) -> User:
    """Create a user. With `password` (the administrator enters an initial password) the
    account is active at once and the user must choose their own password when they first
    sign in; only the hash is stored. Without it, the user is invited: the email job
    delivers a one-time activation link and the user sets their password themselves."""
    email = normalize_email(email)
    first_name = _clean_name(first_name, "first_name")
    last_name = _clean_name(last_name, "last_name")
    if role not in Role.values:
        raise InvalidInputError(details={"role": ["Choose a valid role."]})
    if not first_name:
        raise InvalidInputError(details={"first_name": ["This field may not be blank."]})
    if password is not None and role in roles_with(Capability.USERS_MANAGE):
        # An administrator choosing another administrator's first password could sign in as
        # them before they do (enhancement security review): administrators are invited.
        raise InvalidInputError(details={"password": [ADMINISTRATOR_INVITED]})
    try:
        with transaction.atomic():
            _serialise_user_administration()
            actor = _acting_manager(actor_id)
            if conflict := _email_conflict(email):
                raise conflict
            now = timezone.now()
            user = User(
                email=email,
                first_name=first_name,
                last_name=last_name,
                role=role,
                status=UserStatus.INVITED,
                is_active=False,
            )
            if password is not None:
                # The policy (length, similarity to the name and email, common passwords).
                check_new_password(password, user, field="password")
                user.set_password(password)
                user.status, user.is_active, user.activated_at = UserStatus.ACTIVE, True, now
                user.password_change_required = True
                user.password_changed_at = now
                user.save()
                _audit_user(
                    AUDIT_USER_CREATED, actor.pk, user, role=role, activation="set_by_admin"
                )
                return selectors.admin_user_detail(user.pk)
            user.set_unusable_password()
            user.save()
            invitation = _issue_token(user, TokenPurpose.INVITATION, created_by=actor, now=now)
            _audit_user(AUDIT_USER_CREATED, actor.pk, user, role=role, activation="invitation")
            _audit_user(
                AUDIT_INVITATION_CREATED,
                actor.pk,
                user,
                expires_at=invitation.expires_at.isoformat(),
            )
    except IntegrityError:
        # The unique index is the final arbiter (e.g. a CLI bootstrap racing the API).
        if conflict := _email_conflict(email):
            raise conflict from None
        raise
    return selectors.admin_user_detail(user.pk)


def update_user(*, actor_id: UUID, user_id: UUID, version: int, changes: Mapping[str, str]) -> User:
    """Edit names and role. Email and status have dedicated, audited operations. Another
    administrator's role is not changed here (deactivate them instead): demote, then set a
    password, was a takeover of their account (R100)."""
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise InvalidInputError(details={"non_field_errors": ["These fields cannot be edited."]})
    with transaction.atomic():
        _serialise_user_administration()
        actor = _acting_manager(actor_id)
        user = _lock_user(user_id)
        if changes.get("role", user.role) != user.role:
            # Before the version check: a stale page gets the reason, not a conflict.
            _refuse_for_another_administrator(actor, user, ADMINISTRATOR_ROLE_REFUSED)
        _require_version(user, version)

        changed: list[str] = []
        for field in ("first_name", "last_name"):
            if field in changes:
                value = _clean_name(changes[field], field)
                if field == "first_name" and not value:
                    raise InvalidInputError(details={field: ["This field may not be blank."]})
                if value != getattr(user, field):
                    setattr(user, field, value)
                    changed.append(field)

        previous_role = user.role
        new_role = changes.get("role", previous_role)
        if new_role != previous_role:
            if new_role not in Role.values:
                raise InvalidInputError(details={"role": ["Choose a valid role."]})
            if user.pk == actor.pk:
                raise BusinessRuleViolation("You can't change your own role.")
            if user.password_change_required and new_role in roles_with(Capability.USERS_MANAGE):
                # The password an administrator set is still theirs to know: promoting the
                # account now would hand them an administrator's (demote, set, promote: the
                # enhancement security review's takeover).
                raise BusinessRuleViolation(PROMOTION_REFUSED_UNTIL_OWN_CHOICE)
            if (
                _manages_users(user)
                and new_role not in roles_with(Capability.USERS_MANAGE)
                and not _another_manager_remains(user.pk)
            ):
                raise BusinessRuleViolation(
                    "Arkray CRM must keep at least one active administrator."
                )
            user.role = new_role
            changed.append("role")

        revoked_invitations = 0
        if "role" in changed and new_role not in roles_with(Capability.USERS_MANAGE):
            revoked_invitations = _revoke_invitations_sent_by(user, timezone.now())
        if not changed:
            return selectors.admin_user_detail(user.pk)
        user.version += 1
        user.save(update_fields=[*changed, "version", "updated_at"])
        name_fields = [f for f in changed if f != "role"]
        if name_fields:
            _audit_user(AUDIT_PROFILE_UPDATED, actor.pk, user, fields=name_fields)
        if "role" in changed:
            _audit_user(
                AUDIT_ROLE_CHANGED,
                actor.pk,
                user,
                **{"from": previous_role, "to": new_role},
                revoked_invitations=revoked_invitations,
            )
    return selectors.admin_user_detail(user.pk)


# A change to one's own account refused because the requesting session ended meanwhile.
SESSION_ENDED = "Your session ended while this change was being made. Sign in again."


def change_user_email(
    *,
    actor_id: UUID,
    user_id: UUID,
    version: int,
    email: str,
    still_signed_in: Callable[[User], bool] | None = None,
) -> User:
    """Change a user's sign-in identity. Explicit, audited, and it ends their sessions.
    An administrator's email is changed only by that administrator (R100: a new address
    plus Forgot password would hand their account to whoever chose the address).

    `still_signed_in`: for a change to one's own email, checked against the locked user, so
    a password reset that ended the requesting session meanwhile can't be undone by it."""
    new_email = normalize_email(email)
    try:
        with transaction.atomic():
            _serialise_user_administration()
            actor = _acting_manager(actor_id)
            user = _lock_user(user_id)
            if still_signed_in is not None and not still_signed_in(user):
                raise PermissionDeniedError(SESSION_ENDED)
            # No exception for an invited administrator: the new address would receive the
            # invitation. A mistyped one is deactivated and the right address invited.
            _refuse_for_another_administrator(actor, user, ADMINISTRATOR_EMAIL_REFUSED)
            _require_version(user, version)
            previous_email = user.email
            if new_email == previous_email:
                return selectors.admin_user_detail(user.pk)
            if conflict := _email_conflict(new_email, excluding=user.pk):
                raise conflict
            now = timezone.now()
            user.email = new_email
            user.session_epoch += 1  # signed out everywhere: the login identity changed
            user.version += 1
            user.save(update_fields=["email", "session_epoch", "version", "updated_at"])
            revoked_links = _revoke_pending(user, now, [TokenPurpose.PASSWORD_RESET])
            invitation = None
            if user.status == UserStatus.INVITED:
                # The old link was addressed to the old mailbox: replace it.
                invitation = _issue_token(user, TokenPurpose.INVITATION, created_by=actor, now=now)
            else:
                outbox.enqueue(
                    TOPIC_EMAIL_CHANGED,
                    {"user_id": str(user.pk), "previous_email": previous_email},
                )
            _audit_user_sensitive(
                AUDIT_EMAIL_CHANGED,
                actor.pk,
                user,
                # The addresses expire with the event's detail (audit.retention).
                {"from": previous_email, "to": new_email},
                revoked_links=revoked_links + (1 if invitation else 0),
            )
            if invitation is not None:
                _audit_user(
                    AUDIT_INVITATION_CREATED,
                    actor.pk,
                    user,
                    expires_at=invitation.expires_at.isoformat(),
                    reason="email_changed",
                )
    except IntegrityError:
        if conflict := _email_conflict(new_email, excluding=user_id):
            raise conflict from None
        raise
    return selectors.admin_user_detail(user.pk)


# Why an administrator can't set a password here (messages; the names avoid "password" only
# to keep the hardcoded-secret lint quiet).
OWN_ACCOUNT_REFUSED = "Change your own password in Settings."
INACTIVE_ACCOUNT_REFUSED = (
    "Only active users have a password to set. Resend the invitation instead."
)
ADMINISTRATOR_ACCOUNT_REFUSED = (
    "Administrators set their own passwords: ask them to use Forgot password if they're locked out."
)
ADMINISTRATOR_INVITED = "Administrators choose their own password: send an invitation instead."
# Another administrator's sign-in email and role are theirs alone (R100).
ADMINISTRATOR_EMAIL_REFUSED = "Administrators change their own email address in Settings."
ADMINISTRATOR_ROLE_REFUSED = (
    "An administrator's role can't be changed by another administrator. To remove their"
    " access, deactivate the account."
)
PROMOTION_REFUSED_UNTIL_OWN_CHOICE = (
    "This user must first choose their own password (at their next sign-in): then they can be"
    " made an administrator."
)


def set_user_password(*, actor_id: UUID, user_id: UUID, version: int, new_password: str) -> User:
    """An administrator sets a new (temporary) password for an active user: the user's
    sessions end, outstanding reset links are voided, and the user must choose their own
    password at their next sign-in. Audited as `auth.password_set_by_admin` (who, for whom,
    when; never the password). Not for one's own account (Settings) nor another
    administrator's, whatever its status (that would let one administrator sign in as
    another)."""
    with transaction.atomic():
        _serialise_user_administration()
        actor = _acting_manager(actor_id)
        if user_id == actor.pk:
            raise BusinessRuleViolation(OWN_ACCOUNT_REFUSED)
        user = _lock_user(user_id)
        # By role, before the status: a deactivated or invited administrator is refused too.
        _refuse_for_another_administrator(actor, user, ADMINISTRATOR_ACCOUNT_REFUSED)
        _require_version(user, version)
        if user.status != UserStatus.ACTIVE:
            raise BusinessRuleViolation(INACTIVE_ACCOUNT_REFUSED)
        check_new_password(new_password, user, field="new_password")
        now = timezone.now()
        user.set_password(new_password)
        user.session_epoch += 1  # every session of the user ends
        user.password_change_required = True
        user.password_changed_at = now
        user.version += 1
        user.save(
            update_fields=[
                "password",
                "session_epoch",
                "password_change_required",
                "password_changed_at",
                "version",
                "updated_at",
            ]
        )
        revoked = _revoke_pending(user, now, purposes=[TokenPurpose.PASSWORD_RESET])
        throttling.forget_account_failures(user.email)
        audit.record(
            AUDIT_PASSWORD_SET_BY_ADMIN,
            actor_id=actor.pk,
            target_type="user",
            target_id=user.pk,
            subject_user_id=user.pk,
            metadata={"revoked_links": revoked},
        )
    return selectors.admin_user_detail(user.pk)


def deactivate_user(*, actor_id: UUID, user_id: UUID) -> User:
    """Block sign-in and end every session now. History and references are kept."""
    with transaction.atomic():
        _serialise_user_administration()
        actor = _acting_manager(actor_id)
        user = _lock_user(user_id)
        if user.pk == actor.pk:
            raise BusinessRuleViolation("You can't deactivate your own account.")
        if user.status == UserStatus.DEACTIVATED:
            return selectors.admin_user_detail(user.pk)  # idempotent
        if _manages_users(user) and not _another_manager_remains(user.pk):
            raise BusinessRuleViolation("Arkray CRM must keep at least one active administrator.")
        now = timezone.now()
        previous_status = user.status
        was_manager = _manages_users(user)
        user.status = UserStatus.DEACTIVATED
        user.is_active = False
        user.deactivated_at = now
        user.session_epoch += 1  # stays invalid even if the user is reactivated later
        user.version += 1
        user.save(
            update_fields=[
                "status",
                "is_active",
                "deactivated_at",
                "session_epoch",
                "version",
                "updated_at",
            ]
        )
        revoked_links = _revoke_pending(user, now)
        revoked_invitations = _revoke_invitations_sent_by(user, now) if was_manager else 0
        _audit_user(
            AUDIT_USER_DEACTIVATED,
            actor.pk,
            user,
            previous_status=previous_status,
            revoked_links=revoked_links,
            revoked_invitations=revoked_invitations,
        )
    return selectors.admin_user_detail(user.pk)


def reactivate_user(*, actor_id: UUID, user_id: UUID) -> User:
    """Restore access. A user who never accepted their invitation gets a fresh one."""
    with transaction.atomic():
        _serialise_user_administration()
        actor = _acting_manager(actor_id)
        user = _lock_user(user_id)
        if user.status == UserStatus.ACTIVE:
            return selectors.admin_user_detail(user.pk)  # idempotent
        if user.status == UserStatus.INVITED:
            raise BusinessRuleViolation(
                "This user hasn't accepted their invitation yet. Resend the invitation instead."
            )
        now = timezone.now()
        if user.activated_at is not None and user.has_usable_password():
            user.status, user.is_active = UserStatus.ACTIVE, True
        else:
            user.status, user.is_active, user.activated_at = UserStatus.INVITED, False, None
        user.deactivated_at = None
        user.version += 1
        user.save(
            update_fields=[
                "status",
                "is_active",
                "activated_at",
                "deactivated_at",
                "version",
                "updated_at",
            ]
        )
        _audit_user(AUDIT_USER_REACTIVATED, actor.pk, user, status=user.status)
        if user.status == UserStatus.INVITED:
            invitation = _issue_token(user, TokenPurpose.INVITATION, created_by=actor, now=now)
            _audit_user(
                AUDIT_INVITATION_CREATED,
                actor.pk,
                user,
                expires_at=invitation.expires_at.isoformat(),
                reason="reactivated",
            )
    return selectors.admin_user_detail(user.pk)


def resend_invitation(*, actor_id: UUID, user_id: UUID) -> User:
    """Supersede the current invitation with a new one (the old link stops working)."""
    with transaction.atomic():
        _serialise_user_administration()
        actor = _acting_manager(actor_id)
        user = _lock_user(user_id)
        if user.status != UserStatus.INVITED:
            raise BusinessRuleViolation(
                "Only users who haven't accepted their invitation can be sent one."
            )
        now = timezone.now()
        latest = (
            AccountToken.objects.filter(user=user, purpose=TokenPurpose.INVITATION)
            .order_by("-created_at")
            .values_list("created_at", flat=True)
            .first()
        )
        cooldown = timedelta(seconds=settings.INVITATION_RESEND_COOLDOWN_S)
        if latest is not None and now - latest < cooldown:
            raise RateLimitedError(
                "An invitation was sent moments ago. Wait a minute before sending another.",
                retry_after=int((latest + cooldown - now).total_seconds()) + 1,
            )
        invitation = _issue_token(user, TokenPurpose.INVITATION, created_by=actor, now=now)
        _audit_user(
            AUDIT_INVITATION_RESENT, actor.pk, user, expires_at=invitation.expires_at.isoformat()
        )
    return selectors.admin_user_detail(user.pk)


# --- account tokens: invitation and password reset --------------------------------------------
def _find_token(secret: str, purpose: TokenPurpose) -> AccountToken:
    if not tokens.is_well_formed(secret):
        raise InvalidTokenError()
    token = (
        AccountToken.objects.filter(token_hash=tokens.digest(secret), purpose=purpose)
        .only("id", "user_id")
        .first()
    )
    if token is None:
        raise InvalidTokenError()
    return token


def _lock_live_token(
    secret: str, purpose: TokenPurpose, user_status: UserStatus
) -> tuple[User, AccountToken]:
    """Lock user then token, and re-validate everything under the locks."""
    found = _find_token(secret, purpose)
    user = User.objects.select_for_update().get(pk=found.user_id)
    token = AccountToken.objects.select_for_update().get(pk=found.pk)
    if (
        token.status != TokenStatus.PENDING
        or token.token_hash != tokens.digest(secret)
        or token.is_expired()
        or user.status != user_status
    ):
        raise InvalidTokenError()
    return user, token


def _consume(token: AccountToken, now: datetime) -> None:
    token.status = TokenStatus.USED
    token.used_at = now
    token.save(update_fields=["status", "used_at"])


def inspect_invitation(secret: str) -> User:
    """Who a still-valid invitation link is for (shown on the activation page)."""
    found = _find_token(secret, TokenPurpose.INVITATION)
    token = AccountToken.objects.select_related("user").get(pk=found.pk)
    if (
        token.status != TokenStatus.PENDING
        or token.is_expired()
        or token.user.status != UserStatus.INVITED
    ):
        raise InvalidTokenError()
    return token.user


def accept_invitation(secret: str, password: str) -> User:
    with transaction.atomic():
        user, token = _lock_live_token(secret, TokenPurpose.INVITATION, UserStatus.INVITED)
        check_new_password(password, user, field="password")
        now = timezone.now()
        user.set_password(password)
        user.status, user.is_active, user.activated_at = UserStatus.ACTIVE, True, now
        user.password_changed_at = now
        user.version += 1
        user.save(
            update_fields=[
                "password",
                "status",
                "is_active",
                "activated_at",
                "password_changed_at",
                "version",
                "updated_at",
            ]
        )
        _consume(token, now)
        throttling.forget_account_failures(user.email)
        _audit_user(AUDIT_USER_ACTIVATED, user.pk, user)
    return user


def request_password_reset(email: str, *, ip: str | None) -> None:
    """Constant work whether or not the account exists: record, enqueue, return. The
    email job decides whether anyone is eligible (no enumeration, not even by timing)."""
    with transaction.atomic():
        denied = throttling.reserve_reset_request(ip)
        if denied is None:
            outbox.enqueue(
                TOPIC_PASSWORD_RESET_REQUESTED,
                # Personal data: the payload is blanked by identity.housekeeping once done.
                {"email": normalize_email(email), "requested_from": ip},
            )
    if denied is not None:
        raise RateLimitedError(
            "Too many password reset requests. Try again later.", retry_after=denied.retry_after
        )


def issue_password_reset(email: str, requested_from: str | None = None) -> None:
    """Outbox job: issue a reset link if an active account exists (at most N per hour)."""
    candidate = User.objects.filter(email=normalize_email(email)).only("id").first()
    if candidate is None:
        return
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=candidate.pk)
        if user.status != UserStatus.ACTIVE:
            return
        now = timezone.now()
        issued_last_hour = AccountToken.objects.filter(
            user=user,
            purpose=TokenPurpose.PASSWORD_RESET,
            created_at__gte=now - timedelta(hours=1),
        ).count()
        if issued_last_hour >= settings.PASSWORD_RESET_ACCOUNT_LIMIT_PER_HOUR:
            logger.warning("password_reset_suppressed", extra={"target_user_id": str(user.pk)})
            _audit_user_sensitive(
                AUDIT_PASSWORD_RESET_SUPPRESSED,
                None,
                user,
                {"requested_from": requested_from} if requested_from else None,
            )
            return
        _issue_token(user, TokenPurpose.PASSWORD_RESET, created_by=None, now=now)
        _audit_user_sensitive(
            AUDIT_PASSWORD_RESET_ISSUED,
            None,
            user,
            {"requested_from": requested_from} if requested_from else None,
        )


def confirm_password_reset(secret: str, new_password: str) -> None:
    """Set a new password and end every existing session (the user then signs in again)."""
    with transaction.atomic():
        user, token = _lock_live_token(secret, TokenPurpose.PASSWORD_RESET, UserStatus.ACTIVE)
        check_new_password(new_password, user, field="new_password")
        now = timezone.now()
        user.set_password(new_password)
        user.session_epoch += 1
        # The user chose this one: an administrator-set password no longer needs changing.
        user.password_change_required = False
        user.password_changed_at = now
        user.save(
            update_fields=[
                "password",
                "session_epoch",
                "password_change_required",
                "password_changed_at",
                "updated_at",
            ]
        )
        _consume(token, now)
        throttling.forget_account_failures(user.email)
        _audit_user(AUDIT_PASSWORD_RESET_COMPLETED, user.pk, user)


def deliver_account_token(token_id: UUID) -> None:
    """Outbox job (queue "email"): mint the one-time secret, store its digest, send it.

    Idempotent: a token that is no longer pending, has expired or was already sent is
    skipped. A failed send raises, so the outbox retries; the retry mints a new secret,
    which invalidates the undelivered one.
    """
    now = timezone.now()
    with transaction.atomic():
        token = (
            AccountToken.objects.select_for_update(of=("self",))
            .select_related("user", "created_by")
            .filter(pk=token_id)
            .first()
        )
        if token is None:
            raise outbox.PermanentFailure("Account token not found.")
        required = (
            UserStatus.INVITED if token.purpose == TokenPurpose.INVITATION else UserStatus.ACTIVE
        )
        if (
            token.status != TokenStatus.PENDING
            or token.is_expired(now)
            or token.sent_at is not None
            or token.user.status != required
        ):
            logger.info("account_link_delivery_skipped", extra={"account_token_id": str(token.pk)})
            return
        secret = tokens.generate_secret()
        token.token_hash, token.issued_at = tokens.digest(secret), now
        token.save(update_fields=["token_hash", "issued_at"])
        recipient, first_name = token.user.email, token.user.first_name
        inviter = token.created_by.full_name if token.created_by else None
        purpose, expires_at = token.purpose, token.expires_at

    if purpose == TokenPurpose.INVITATION:
        emails.send_invitation(
            to=recipient,
            first_name=first_name,
            inviter_name=inviter,
            link=emails.account_link("activate", secret),
            expires_at=expires_at,
        )
    else:
        emails.send_password_reset(
            to=recipient,
            first_name=first_name,
            link=emails.account_link("reset-password", secret),
            expires_at=expires_at,
        )
    AccountToken.objects.filter(pk=token_id, token_hash=tokens.digest(secret)).update(
        sent_at=timezone.now()
    )
    logger.info("account_link_sent", extra={"account_token_id": str(token_id), "purpose": purpose})


def notify_email_changed(user_id: UUID, previous_email: str) -> None:
    """Outbox job: tell the previous address that the sign-in email changed."""
    user = User.objects.filter(pk=user_id).only("first_name", "email").first()
    if user is None:
        return
    emails.send_email_changed_notice(
        to=previous_email, first_name=user.first_name, new_email=user.email
    )
