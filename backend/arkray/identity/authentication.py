"""Sign-in, sign-out, password change and re-authentication (docs/authorization.md).

Every failed sign-in (unknown email, wrong password, invited or deactivated account) gets
the same response, and the password hasher runs in every case, so neither the response
nor its timing reveals whether an account exists or what state it is in. Throttling
happens before the password is checked and is keyed by the submitted email, so a
throttled attempt reveals nothing either. A failed sign-in of an existing account is
audited (`auth.login_failed`, shown in security events; bounded by the throttle); one of an
unknown email is a log line only, so an anonymous caller can't create audit rows at will.
It does the same database work all the same (adversarial review of 73ff1e3, AUTH-4: the
real account's two committed audit inserts were 3 to 7 ms a few hundred samples could
measure, R135): the same inserts in a savepoint that is rolled back, and one commit either
way. An email that isn't ASCII never names an account (models.normalize_email; AUTH-7).

With a second factor (ADR-0033) the sign-in takes two requests: the right password gives a
202 and a pending challenge in a fresh anonymous session (identity.mfa_services), and
`sign_in_second_step` with a code completes it. The password alone forgives no earlier
failure: only the second factor does. Wrong codes also accumulate on the device until it
locks (deep security audit M-4, identity.mfa_services).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from functools import lru_cache
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core.errors import (
    BusinessRuleViolation,
    DomainError,
    InvalidInputError,
    PermissionDeniedError,
    RateLimitedError,
)

from . import mfa_services, support, throttling
from .models import AccountToken, SupportEnd, TokenStatus, User, normalize_email
from .passwords import check_new_password
from .services import CREDENTIAL_LINKS, SESSION_ENDED
from .sessions import end_session, session_matches, start_session

logger = logging.getLogger(__name__)

AUDIT_LOGIN = "auth.login"
AUDIT_LOGIN_TEMPORARY = "auth.login_with_temporary_password"
AUDIT_LOGIN_FAILED = "auth.login_failed"
# `auth.login` records the factor: this, mfa_services.FACTOR_TOTP or FACTOR_RECOVERY_CODE.
FACTOR_PASSWORD_ONLY = "password_only"  # noqa: S105 — a label
AUDIT_LOGOUT = "auth.logout"
AUDIT_PASSWORD_CHANGED = "auth.password_changed"  # noqa: S105 — an event name
CURRENT_PASSWORD_WRONG = "Your current password is incorrect."  # noqa: S105 — a message


class InvalidCredentialsError(DomainError):
    code = "invalid_credentials"
    http_status = 400
    default_message = "Invalid email or password."


class TemporaryPasswordExpiredError(DomainError):
    """The right temporary password (chosen by an administrator), too late: only someone
    who knows it sees this."""

    code = "temporary_password_expired"
    http_status = 400
    default_message = "This temporary password has expired. Ask an administrator to set a new one."


def temporary_password_expired(user: User) -> bool:
    if not user.password_change_required or user.password_changed_at is None:
        return False
    age = timezone.now() - user.password_changed_at
    return bool(age.total_seconds() > settings.TEMPORARY_PASSWORD_TTL_S)


@dataclass(frozen=True, slots=True)
class SignIn:
    user: User
    identifier: str  # for the trusted-device cookie
    device_id: str
    device_trust: str  # the account's credentials as the cookie trusts them


@dataclass(frozen=True, slots=True)
class SecondFactorNeeded:
    """The password was right; a code must follow within `expires_in` seconds."""

    expires_in: int


@lru_cache(maxsize=1)
def _decoy_hash() -> str:
    """A real hash in the current default format, verified when there is no usable one."""
    return make_password(secrets.token_urlsafe(16))


def _verify_credentials(email: str, password: str) -> tuple[User | None, User | None]:
    """(the user signing in, or None; the account the email names, if any)."""
    account = User.objects.filter(email=normalize_email(email)).first()
    if account is None or not account.has_usable_password():
        check_password(password, _decoy_hash())  # same work as a real check: no timing oracle
        return None, account
    if not account.check_password(password):  # may transparently upgrade the hash
        return None, account
    return (account if account.is_active else None), account


def _throttled(denied: throttling.Denied) -> RateLimitedError:
    return throttling.rate_limited(denied)


def _throttle_for(request: HttpRequest, email: str) -> throttling.LoginThrottle:
    return throttling.for_request(request, email)


def _audit_failure(account: User, reason: str, throttle: throttling.LoginThrottle) -> None:
    """A failed sign-in of an existing account, for administrators' security events: the
    reason, never what was typed. Bounded by the throttle (refused attempts aren't recorded)."""
    audit.record(
        AUDIT_LOGIN_FAILED,
        actor_id=None,
        target_type="user",
        target_id=account.pk,
        subject_user_id=account.pk,
        metadata={"reason": reason, "trusted_device": bool(throttle.device_id)},
    )


def _record_failure(account: User | None, throttle: throttling.LoginThrottle) -> None:
    """A wrong password: counted (and, from an unrecognised browser, checked against the
    daily cap), and audited for an existing account, in one transaction that does the same
    work for an unknown email (AUTH-4): its audit rows are written in a savepoint and rolled
    back, so the statements, the tables and indexes they touch and the commit are the same,
    and still no audit row an anonymous caller chose remains."""
    with transaction.atomic():
        throttle.failed()
        savepoint = transaction.savepoint()
        if account is not None:
            reason = "wrong_password" if account.is_active else "account_inactive"
            _audit_failure(account, reason, throttle)
            transaction.savepoint_commit(savepoint)
        else:
            _audit_failure(User(pk=uuid4(), is_active=True), "wrong_password", throttle)
            transaction.savepoint_rollback(savepoint)


def sign_in(request: HttpRequest, email: str, password: str) -> SignIn | SecondFactorNeeded:
    throttle = _throttle_for(request, email)
    denied = throttle.reserve()
    if denied is not None:
        logger.info(
            "login_throttled", extra={"reason": denied.reason, "login": throttle.identifier[:12]}
        )
        raise _throttled(denied)

    user, account = _verify_credentials(email, password)  # outside any transaction
    if user is None:
        _record_failure(account, throttle)
        logger.info("login_failed", extra={"login": throttle.identifier[:12]})
        raise InvalidCredentialsError()
    if temporary_password_expired(user):
        logger.info("login_temporary_password_expired", extra={"login": throttle.identifier[:12]})
        raise TemporaryPasswordExpiredError()

    # This browser's session is about to be replaced (a new key, or a fresh challenge): a
    # support session bound to it ends now, not when it expires (deep security audit I-1).
    # After the password, so a mistyped one leaves a live support session alone.
    support.end_on_sign_out(request, SupportEnd.SESSION_CHANGED)

    if mfa_services.has_confirmed_device(user):
        # Not a failure, but nothing is forgiven until the second factor is proved.
        throttle.release()
        return SecondFactorNeeded(expires_in=mfa_services.begin_challenge(request, user))

    throttle.succeeded()
    return _complete_sign_in(request, user, throttle, factor=FACTOR_PASSWORD_ONLY)


def sign_in_second_step(
    request: HttpRequest, *, code: str | None = None, recovery_code: str | None = None
) -> SignIn:
    """The second request of a sign-in with a second factor. Each wrong code counts as a
    failed sign-in in the durable throttle; MFA_CHALLENGE_MAX_ATTEMPTS of them void the
    challenge (the password is asked again)."""
    challenge = mfa_services.read_challenge(request)
    user = challenge.user
    throttle = _throttle_for(request, user.email)
    denied = throttle.reserve()
    if denied is not None:
        logger.info(
            "login_throttled", extra={"reason": denied.reason, "login": throttle.identifier[:12]}
        )
        raise _throttled(denied)
    try:
        factor = mfa_services.verify_factor(user, code=code, recovery_code=recovery_code)
    except mfa_services.MfaLockedError:
        # Too many wrong codes in a row (deep security audit M-4): this challenge is over,
        # and so is every later one until the password is reset or the factor is (only the
        # factor, past MFA_MAX_FAILURES_BEFORE_RESET: MfaLockedUntilResetError, R133).
        throttle.failed()
        mfa_services.end_challenge(request)
        logger.info("login_mfa_locked", extra={"login": throttle.identifier[:12]})
        _audit_failure(user, "mfa_locked", throttle)
        raise
    if factor is None:
        throttle.failed()
        mfa_services.record_wrong_code(request, challenge)
        logger.info("login_mfa_failed", extra={"login": throttle.identifier[:12]})
        _audit_failure(user, "wrong_recovery_code" if code is None else "wrong_code", throttle)
        raise mfa_services.InvalidMfaCodeError()
    throttle.succeeded()
    mfa_services.end_challenge(request)
    return _complete_sign_in(request, user, throttle, factor=factor)


def _complete_sign_in(
    request: HttpRequest, user: User, throttle: throttling.LoginThrottle, *, factor: str
) -> SignIn:
    with transaction.atomic():
        start_session(request, user)
        if factor != FACTOR_PASSWORD_ONLY:
            mfa_services.mark_verified(request.session)
        audit.record(
            AUDIT_LOGIN,
            actor_id=user.pk,
            target_type="user",
            target_id=user.pk,
            metadata={"trusted_device": bool(throttle.device_id), "factor": factor},
        )
        if user.password_change_required:
            # Shown in administrators' security events (unlike every sign-in): whoever
            # signs in with a password an administrator chose, before the user's own
            # change, is visible there (R92; enhancement security review).
            audit.record(
                AUDIT_LOGIN_TEMPORARY,
                actor_id=user.pk,
                target_type="user",
                target_id=user.pk,
                subject_user_id=user.pk,
            )
    return SignIn(
        user=user,
        identifier=throttle.identifier,
        device_id=throttle.device_id or secrets.token_hex(16),
        device_trust=throttling.device_trust(user.password, user.session_epoch),
    )


def sign_out(request: HttpRequest) -> None:
    user = request.user
    if isinstance(user, User):
        support.end_on_sign_out(request)
        audit.record(AUDIT_LOGOUT, actor_id=user.pk, target_type="user", target_id=user.pk)
    end_session(request)


def _signed_in_user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User) or not user.is_active:
        raise PermissionDeniedError()
    return user


def verify_current_password(request: HttpRequest, password: str) -> None:
    """Re-authentication for sensitive changes to one's own account. Guesses are throttled
    exactly like sign-in, so a hijacked session can't be used to find the password."""
    actor = _signed_in_user(request)
    throttle = _throttle_for(request, actor.email)
    denied = throttle.reserve()
    if denied is not None:
        raise _throttled(denied)
    if not User.objects.get(pk=actor.pk).check_password(password):
        throttle.failed()
        raise InvalidInputError(
            CURRENT_PASSWORD_WRONG, details={"current_password": [CURRENT_PASSWORD_WRONG]}
        )
    if mfa_services.has_confirmed_device(actor):
        # The bucket also holds wrong second-factor codes (step-up, disabling, new recovery
        # codes): a password proves nothing about those, so only this attempt is withdrawn.
        # Only a right code forgives them (review P2-1).
        throttle.release()
    else:
        throttle.succeeded()


def session_still_current(request: HttpRequest, user: User) -> bool:
    """Whether this request's session still belongs to `user` as it is now (read under its
    row lock). The session hash covers the password and `session_epoch`, so a password reset,
    a password or email change, or "sign out everywhere" committed while this request was
    running makes it false (Phase 9 review, P1: a password change in flight must not undo a
    reset that ended its session)."""
    return session_matches(request, user)


def change_password(request: HttpRequest, current_password: str, new_password: str) -> None:
    """Keep this session (with a new key); end every other session of the user; void any
    outstanding password-reset link (it may be why the user is changing the password)."""
    actor = _signed_in_user(request)
    verify_current_password(request, current_password)
    if new_password == current_password:
        raise InvalidInputError(
            details={"new_password": ["Choose a password you haven't used here."]}
        )
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=actor.pk)
        if not user.is_active:
            raise PermissionDeniedError()
        if not session_still_current(request, user):
            raise PermissionDeniedError(SESSION_ENDED)
        check_new_password(new_password, user, field="new_password")
        user.set_password(new_password)
        # Their own choice now: a temporary (administrator-set) password is replaced.
        user.password_change_required = False
        user.password_changed_at = timezone.now()
        user.save(
            update_fields=[
                "password",
                "password_change_required",
                "password_changed_at",
                "updated_at",
            ]
        )
        # And any unlock link (review round 1, IDENTITY-2): it would trust a browser for the
        # new password.
        AccountToken.objects.filter(
            user=user, purpose__in=CREDENTIAL_LINKS, status=TokenStatus.PENDING
        ).update(status=TokenStatus.REVOKED, revoked_at=timezone.now())
        # The safe event administrators see (who and when; never the password, its hash or
        # any token): docs/authorization.md#password-change-notification.
        audit.record(
            AUDIT_PASSWORD_CHANGED,
            actor_id=user.pk,
            target_type="user",
            target_id=user.pk,
        )
        update_session_auth_hash(request, user)


# --- two-step verification of one's own account (ADR-0033) -----------------------------------
def begin_mfa_setup(request: HttpRequest, current_password: str) -> mfa_services.Setup:
    """Re-authenticate (throttled like a sign-in), then start a setup."""
    actor = _signed_in_user(request)
    verify_current_password(request, current_password)
    return mfa_services.begin_setup(actor)


def disable_mfa(request: HttpRequest, current_password: str, code: str) -> None:
    """The current password and a code (or an unused recovery code), unless the role
    requires a second factor (checked first: nothing to guess then)."""
    actor = _signed_in_user(request)
    if mfa_services.required_for(actor):
        raise BusinessRuleViolation(mfa_services.required_message())
    verify_current_password(request, current_password)
    mfa_services.disable(request, actor, code)
