"""Sign-in, sign-out, password change and re-authentication (docs/authorization.md).

Every failed sign-in (unknown email, wrong password, invited or deactivated account) gets
the same response, and the password hasher runs in every case, so neither the response
nor its timing reveals whether an account exists or what state it is in. Throttling
happens before the password is checked and is keyed by the submitted email, so a
throttled attempt reveals nothing either.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from functools import lru_cache

from django.contrib.auth import HASH_SESSION_KEY, update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from arkray.audit import services as audit
from arkray.core.errors import (
    DomainError,
    InvalidInputError,
    PermissionDeniedError,
    RateLimitedError,
)
from arkray.core.middleware import client_ip

from . import throttling
from .models import AccountToken, TokenPurpose, TokenStatus, User, normalize_email
from .passwords import check_new_password
from .services import SESSION_ENDED
from .sessions import end_session, start_session

logger = logging.getLogger(__name__)

AUDIT_LOGIN = "auth.login"
AUDIT_LOGOUT = "auth.logout"
AUDIT_PASSWORD_CHANGED = "auth.password_changed"  # noqa: S105 — an event name
CURRENT_PASSWORD_WRONG = "Your current password is incorrect."  # noqa: S105 — a message


class InvalidCredentialsError(DomainError):
    code = "invalid_credentials"
    http_status = 400
    default_message = "Invalid email or password."


@dataclass(frozen=True, slots=True)
class SignIn:
    user: User
    identifier: str  # for the trusted-device cookie
    device_id: str
    device_trust: str  # the account's credentials as the cookie trusts them


@lru_cache(maxsize=1)
def _decoy_hash() -> str:
    """A real hash in the current default format, verified when there is no usable one."""
    return make_password(secrets.token_urlsafe(16))


def _verify_credentials(email: str, password: str) -> User | None:
    user = User.objects.filter(email=normalize_email(email)).first()
    if user is None or not user.has_usable_password():
        check_password(password, _decoy_hash())  # same work as a real check: no timing oracle
        return None
    if not user.check_password(password):  # may transparently upgrade the hash
        return None
    return user if user.is_active else None


def _throttled(denied: throttling.Denied) -> RateLimitedError:
    minutes = max(1, round(denied.retry_after / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return RateLimitedError(
        f"Too many sign-in attempts. Try again in {minutes} {unit}.", retry_after=denied.retry_after
    )


def _throttle_for(request: HttpRequest, email: str) -> throttling.LoginThrottle:
    identifiers = throttling.login_identifiers(email)
    device_id = throttling.read_device(request, identifiers, email)
    return throttling.LoginThrottle(identifiers, device_id, client_ip(request))


def sign_in(request: HttpRequest, email: str, password: str) -> SignIn:
    throttle = _throttle_for(request, email)
    denied = throttle.reserve()
    if denied is not None:
        logger.info(
            "login_throttled", extra={"reason": denied.reason, "login": throttle.identifier[:12]}
        )
        raise _throttled(denied)

    user = _verify_credentials(email, password)  # outside any transaction
    if user is None:
        throttle.failed()
        logger.info("login_failed", extra={"login": throttle.identifier[:12]})
        raise InvalidCredentialsError()

    throttle.succeeded()
    with transaction.atomic():
        start_session(request, user)
        audit.record(
            AUDIT_LOGIN,
            actor_id=user.pk,
            target_type="user",
            target_id=user.pk,
            metadata={"trusted_device": bool(throttle.device_id)},
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
    throttle.succeeded()


def session_still_current(request: HttpRequest, user: User) -> bool:
    """Whether this request's session still belongs to `user` as it is now (read under its
    row lock). The session hash covers the password and `session_epoch`, so a password reset,
    a password or email change, or "sign out everywhere" committed while this request was
    running makes it false (Phase 9 review, P1: a password change in flight must not undo a
    reset that ended its session)."""
    stored = request.session.get(HASH_SESSION_KEY)
    return isinstance(stored, str) and constant_time_compare(stored, user.get_session_auth_hash())


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
        user.save(update_fields=["password", "updated_at"])
        AccountToken.objects.filter(
            user=user, purpose=TokenPurpose.PASSWORD_RESET, status=TokenStatus.PENDING
        ).update(status=TokenStatus.REVOKED, revoked_at=timezone.now())
        audit.record(
            AUDIT_PASSWORD_CHANGED, actor_id=user.pk, target_type="user", target_id=user.pk
        )
        update_session_auth_hash(request, user)
