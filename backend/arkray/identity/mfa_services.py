"""Two-step verification use cases (ADR-0033; the primitives are in identity.mfa).

- **State**: a user has a second factor once their device is confirmed. Super Admins must
  have one when MFA_REQUIRED_FOR_ADMINS is on (`enrollment_required`): until then they reach
  only the views marked `allowed_before_mfa_enrollment` (identity.permissions).
- **Step-up**: the session records when a code was last verified (`SESSION_VERIFIED_AT`):
  at the second step of a sign-in, at confirmation, at POST /auth/mfa/verify. Views marked
  `requires_recent_mfa` need it within MFA_STEP_UP_TTL_S from a user with a device.
- **Codes** are checked under the device's row lock; the accepted time step is stored there,
  so the same code (or an older one) is refused afterwards, on any server. A recovery code
  is used up by a conditional update (two requests can't both use it).
- **Guessing is throttled** by the durable sign-in throttle (identity.throttling): every
  wrong code at sign-in, at step-up, when replacing recovery codes or disabling counts as a
  failed sign-in of the account from that browser.
- **Guessing is bounded** too (deep security audit M-4): the throttle's window only slows a
  password thief down (about 550 codes a day), so wrong codes also accumulate on the device
  (`consecutive_failures`, under its row lock, across sign-in attempts, browsers and
  step-ups; a right code resets it). From MFA_MAX_CONSECUTIVE_FAILURES on, every code is
  refused (`mfa_locked`), the right one included, until the password is reset by email or
  set by an administrator (identity.services), or the factor is reset. Only someone past
  the password (a challenge) or holding a signed-in session can add to it, so it is no lever
  for locking out an account whose password is safe. The owner is emailed about a wrong
  code at most once per MFA_FAILURE_NOTICE_INTERVAL_S, and when it locks (`mfa.locked`,
  audited, alerted on: identity.metrics).
- **Over a long window too** (R133): the same wrong codes accumulate in
  `cumulative_failures`, which only a right code (or an MFA reset, which deletes the device)
  forgets; a new password doesn't. From MFA_MAX_FAILURES_BEFORE_RESET on, every code is
  refused (`mfa_locked_until_reset`) until an MFA reset: someone holding the password and the
  mailbox can't buy more guesses with each emailed reset. Audited as `mfa.locked` with
  `until: mfa_reset`, emailed, alerted on (identity.metrics).
- **Changes end other sessions**: confirming, disabling, resetting a device and replacing
  recovery codes increment `session_epoch`; the session making the change is kept, re-hashed.
- **Setups expire**: a started setup must be confirmed within MFA_SETUP_TTL_S and with
  fewer than MFA_SETUP_MAX_ATTEMPTS wrong codes (each also a failed sign-in in the durable
  throttle); otherwise it is deleted and must start again. The hourly housekeeping deletes
  the ones nobody finished.
- **Audit** (never a secret, a code or a hash): mfa.enabled, mfa.disabled, mfa.reset,
  mfa.recovery_codes_replaced, mfa.recovery_code_used, mfa.locked (and mfa.unlocked,
  identity.services). Each change also emails the account's own address (no code, no
  secret): an owner learns of a change they didn't make.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.contrib.auth import update_session_auth_hash
from django.contrib.sessions.backends.base import SessionBase
from django.core.checks import Error, register
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from arkray.audit import services as audit
from arkray.core import outbox
from arkray.core.errors import (
    BusinessRuleViolation,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
)

from . import emails, mfa, selectors, throttling
from .models import MfaDevice, MfaLock, MfaRecoveryCode, Role, User, normalize_email
from .policy import administrator_roles
from .services import (
    SESSION_ENDED,
    _acting_manager,
    _acting_super_administrator,
    _lock_reachable_user,
    _refuse_if_recently_demoted,
    refuse_if_pseudonymised,
    serialise_user_administration,
)
from .sessions import session_matches

SESSION_VERIFIED_AT = "_arkray_mfa_at"  # epoch seconds of the last verified code
SESSION_CHALLENGE = "_arkray_mfa_challenge"  # the pending second step of a sign-in
_CLOCK_SKEW_S = 60
_CACHE = "_arkray_mfa_confirmed"  # memo on a User instance, for one request

AUDIT_ENABLED = "mfa.enabled"
AUDIT_DISABLED = "mfa.disabled"
AUDIT_RESET = "mfa.reset"
AUDIT_CODES_REPLACED = "mfa.recovery_codes_replaced"
AUDIT_CODE_USED = "mfa.recovery_code_used"
AUDIT_LOCKED = "mfa.locked"

TOPIC_MFA_CHANGED = "identity.notify_mfa_changed"
# What the owner is told about (identity.emails.send_mfa_notice).
NOTICES = (
    "enabled",
    "disabled",
    "reset",
    "recovery_codes_replaced",
    "recovery_code_used",
    "wrong_code",
    "locked",
    "locked_until_reset",
)

FACTOR_TOTP = "totp"
FACTOR_RECOVERY_CODE = "recovery_code"

REQUIRED_FOR_ROLE = (
    "Two-step verification is required for super administrators, so it can't be turned off."
)
ADMINISTRATOR_MFA_REFUSED = (
    "Administrators manage their own two-step verification. If one is locked out, an operator"
    " resets it on the server."
)


class InvalidMfaCodeError(DomainError):
    code = "invalid_mfa_code"
    http_status = 400
    default_message = "That code didn't work. Check your authenticator app and try again."


class MfaLockedError(DomainError):
    """MFA_MAX_CONSECUTIVE_FAILURES wrong codes in a row (deep security audit M-4): no code
    is accepted, the right one included, until a password reset or an MFA reset."""

    code = "mfa_locked"
    http_status = 403
    default_message = (
        "Too many wrong codes were entered for your account, so two-step verification is"
        " locked. Reset your password (Forgot password on the sign-in page) or ask an"
        " administrator for help."
    )


class MfaLockedUntilResetError(MfaLockedError):
    """MFA_MAX_FAILURES_BEFORE_RESET wrong codes since the last right one, new passwords
    notwithstanding (R133): no code is accepted until an MFA reset. Handled wherever
    MfaLockedError is (a failure; the sign-in challenge ends). Its own code, so clients show
    this message: `mfa_locked`'s advice (reset your password) no longer helps."""

    code = "mfa_locked_until_reset"
    default_message = (
        "Too many wrong codes were entered for your account, so two-step verification is"
        " locked until it is reset. Resetting your password won't unlock it: ask an"
        " administrator to reset your two-step verification."
    )


class MfaChallengeExpiredError(DomainError):
    code = "mfa_challenge_expired"
    http_status = 400
    default_message = "Your sign-in timed out. Enter your email and password again."


class MfaSetupRequiredError(DomainError):
    code = "mfa_setup_required"
    http_status = 400
    default_message = "Start setting up two-step verification first."


class MfaAlreadyEnabledError(DomainError):
    code = "mfa_already_enabled"
    http_status = 409
    default_message = "Two-step verification is already on for your account."


# --- configuration ----------------------------------------------------------------------------
@register()
def check_failure_limits(**_: Any) -> list[Error]:
    """The cap a new password doesn't clear (R133) must leave room for the lock a new
    password does: at or below MFA_MAX_CONSECUTIVE_FAILURES, every lock would need an MFA
    reset. (Registered from IdentityConfig.ready, which imports the handlers.)"""
    consecutive = settings.MFA_MAX_CONSECUTIVE_FAILURES
    if 0 < consecutive < settings.MFA_MAX_FAILURES_BEFORE_RESET:
        return []
    return [
        Error(
            "MFA_MAX_FAILURES_BEFORE_RESET must be more than MFA_MAX_CONSECUTIVE_FAILURES,"
            " and both positive (R133).",
            id="arkray.E030",
        )
    ]


def _now() -> float:
    return time.time()


def _moment() -> datetime:
    return datetime.fromtimestamp(_now(), tz=UTC)


# --- state ------------------------------------------------------------------------------------
def has_confirmed_device(user: Any) -> bool:
    """Whether `user` has a confirmed second factor (memoised on the instance)."""
    if not isinstance(user, User) or user.pk is None:
        return False
    cached = user.__dict__.get(_CACHE)
    if cached is None:
        cached = MfaDevice.objects.filter(user_id=user.pk, confirmed_at__isnull=False).exists()
        user.__dict__[_CACHE] = cached
    return bool(cached)


def _forget(user: Any) -> None:
    if isinstance(user, User):
        user.__dict__.pop(_CACHE, None)


def required_for(user: Any) -> bool:
    """Whether the account must have a second factor: Super Admins only (ADR-0033)."""
    # Only organisation-wide super administrators are subject to the second factor.
    # Do not silently broaden this to TEAM_ADMIN, even when an old deployment environment
    # still contains the retired MFA_REQUIRED_FOR_ALL_USERS flag.
    return bool(settings.MFA_REQUIRED_FOR_ADMINS) and getattr(user, "role", None) == Role.ADMIN


def required_message() -> str:
    return REQUIRED_FOR_ROLE


def enrollment_required(user: Any) -> bool:
    return required_for(user) and not has_confirmed_device(user)


def mark_verified(session: SessionBase) -> None:
    session[SESSION_VERIFIED_AT] = _now()


def recently_verified(request: Any, user: Any) -> bool:
    """A code verified in this session within MFA_STEP_UP_TTL_S, or no device to ask for."""
    if not required_for(user):
        return True  # Non-super-admin accounts never require an MFA step-up.
    at = request.session.get(SESSION_VERIFIED_AT)
    if isinstance(at, int | float) and not isinstance(at, bool):
        age = _now() - at
        if -_CLOCK_SKEW_S <= age < settings.MFA_STEP_UP_TTL_S:
            return True
    return not has_confirmed_device(user)


@dataclass(frozen=True, slots=True)
class Status:
    enabled: bool
    confirmed_at: Any
    recovery_codes_remaining: int
    required: bool
    can_disable: bool
    # What unlocks it when wrong codes locked it (R133: was in security events only).
    lock: MfaLock | None


def status(user: User) -> Status:
    device = MfaDevice.objects.filter(user_id=user.pk, confirmed_at__isnull=False).first()
    remaining = (
        MfaRecoveryCode.objects.filter(user_id=user.pk, used_at__isnull=True).count()
        if device is not None
        else 0
    )
    required = required_for(user)
    return Status(
        enabled=device is not None,
        confirmed_at=device.confirmed_at if device else None,
        recovery_codes_remaining=remaining,
        required=required,
        can_disable=device is not None and not required,
        lock=device.lock if device else None,
    )


# --- verifying a factor -------------------------------------------------------------------------
# What checking a code under the device's lock found.
_RIGHT, _WRONG, _LOCKED = "right", "wrong", "locked"
_LOCKED_UNTIL_RESET = "locked_until_reset"  # R133: past what a new password unlocks


def _confirmed_device_locked(user_id: UUID) -> MfaDevice | None:
    """The confirmed device under its row lock (inside the caller's transaction)."""
    return (
        MfaDevice.objects.select_for_update()
        .filter(user_id=user_id, confirmed_at__isnull=False)
        .first()
    )


def _lock_outcome(device: MfaDevice) -> str:
    return _LOCKED_UNTIL_RESET if device.locked_until_reset else _LOCKED


def _count_failure(device: MfaDevice) -> str:
    """A wrong code, under the device's row lock (deep security audit M-4), on a device that
    isn't locked. Counted across sign-in attempts, browsers and step-ups, twice: in a row
    (`consecutive_failures`, which a new password forgets) and since the last right code
    (`cumulative_failures`, which it doesn't: R133). The MFA_MAX_FAILURES_BEFORE_RESET-th of
    the latter locks the factor until an MFA reset, else the MFA_MAX_CONSECUTIVE_FAILURES-th
    in a row locks it until a new password: either is audited (`mfa.locked`, ids and counts
    only) and emailed to the owner, once, since a locked device counts nothing more.
    Otherwise the owner is emailed about the first wrong code in
    MFA_FAILURE_NOTICE_INTERVAL_S: codes are asked for only after the right password (or in
    a signed-in session), so one they didn't type means someone else has it. _WRONG, or
    the lock this one caused."""
    device.consecutive_failures += 1
    device.cumulative_failures += 1
    fields = ["consecutive_failures", "cumulative_failures", "updated_at"]
    outcome = _WRONG
    now = timezone.now()
    interval = timedelta(seconds=settings.MFA_FAILURE_NOTICE_INTERVAL_S)
    if device.locked_until_reset:
        outcome = _LOCKED_UNTIL_RESET
        audit.record(
            AUDIT_LOCKED,
            actor_id=None,
            target_type="user",
            target_id=device.user_id,
            subject_user_id=device.user_id,
            metadata={"failures": device.cumulative_failures, "until": MfaLock.MFA_RESET.value},
        )
        _notify_owner(device.user_id, "locked_until_reset")
    elif device.locked:
        outcome = _LOCKED
        audit.record(
            AUDIT_LOCKED,
            actor_id=None,
            target_type="user",
            target_id=device.user_id,
            subject_user_id=device.user_id,
            metadata={"failures": device.consecutive_failures},
        )
        _notify_owner(device.user_id, "locked")
    elif device.failure_notified_at is None or now - device.failure_notified_at >= interval:
        device.failure_notified_at = now
        fields.append("failure_notified_at")
        _notify_owner(device.user_id, "wrong_code")
    device.save(update_fields=fields)
    return outcome


def _verify_totp(user_id: UUID, code: str) -> str:
    """Check a code against the confirmed device under its row lock; accept each time step
    once (replay prevention), re-encrypt the secret under the current key, and count wrong
    codes (a locked device checks nothing)."""
    if mfa.normalise_code(code) is None:
        return _WRONG  # can't be anyone's code: nothing was guessed
    with transaction.atomic():
        device = _confirmed_device_locked(user_id)
        if device is None:
            return _WRONG
        if device.locked:
            return _lock_outcome(device)
        try:
            secret = mfa.decrypt(device.secret)
        except InvalidToken:
            return _WRONG  # encrypted under a key no longer configured: an operator resets it
        step = mfa.matching_step(secret, code, at=_now(), after_step=device.last_used_step)
        if step is None:
            return _count_failure(device)
        device.last_used_step = step
        device.secret = mfa.rotate(device.secret)
        device.consecutive_failures = device.cumulative_failures = 0
        device.save(
            update_fields=[
                "last_used_step",
                "secret",
                "consecutive_failures",
                "cumulative_failures",
                "updated_at",
            ]
        )
    return _RIGHT


def _use_recovery_code(user: User, raw: str) -> str:
    """Use up a recovery code (audited with how many remain). Wrong ones count like wrong
    authenticator codes."""
    if mfa.normalise_recovery_code(raw) is None:
        return _WRONG
    with transaction.atomic():
        # The device's lock serialises this with a replacement of the codes, and the count.
        device = _confirmed_device_locked(user.pk)
        if device is None:
            return _WRONG
        if device.locked:
            return _lock_outcome(device)
        unused = dict(
            MfaRecoveryCode.objects.filter(user_id=user.pk, used_at__isnull=True).values_list(
                "code_hash", "pk"
            )
        )
        found = mfa.matching_hash(raw, list(unused))
        if found is None:
            return _count_failure(device)
        used = MfaRecoveryCode.objects.filter(pk=unused[found], used_at__isnull=True).update(
            used_at=timezone.now()
        )
        if not used:
            return _WRONG
        if device.consecutive_failures or device.cumulative_failures:
            device.consecutive_failures = device.cumulative_failures = 0
            device.save(update_fields=["consecutive_failures", "cumulative_failures", "updated_at"])
        audit.record(
            AUDIT_CODE_USED,
            actor_id=user.pk,
            target_type="user",
            target_id=user.pk,
            subject_user_id=user.pk,
            metadata={"remaining": len(unused) - 1},
        )
        _notify(user, "recovery_code_used")
    return _RIGHT


def verify_factor(
    user: User, *, code: str | None = None, recovery_code: str | None = None
) -> str | None:
    """FACTOR_TOTP or FACTOR_RECOVERY_CODE when it is right (and now used), else None (a
    wrong code: counted). MfaLockedError when the factor is locked, or this wrong code
    locked it (M-4): the caller counts it as a failure, like a wrong code. Its subclass
    MfaLockedUntilResetError when only an MFA reset unlocks it (R133)."""
    if code is not None:
        factor, outcome = FACTOR_TOTP, _verify_totp(user.pk, code)
    elif recovery_code is not None:
        factor, outcome = FACTOR_RECOVERY_CODE, _use_recovery_code(user, recovery_code)
    else:
        return None
    if outcome == _LOCKED_UNTIL_RESET:
        raise MfaLockedUntilResetError()
    if outcome == _LOCKED:
        raise MfaLockedError()
    return factor if outcome == _RIGHT else None


def _either(code: str) -> dict[str, str]:
    """A typed `code` as what it is: an authenticator code or a recovery code."""
    return {"code": code} if mfa.normalise_code(code) is not None else {"recovery_code": code}


def _throttled_verify(
    request: HttpRequest, user: User, *, code: str | None = None, recovery_code: str | None = None
) -> str:
    """Verify a factor of the signed-in user, throttled exactly like a sign-in: a hijacked
    session can't be used to guess the second factor."""
    throttle = throttling.for_request(request, user.email)
    denied = throttle.reserve()
    if denied is not None:
        raise throttling.rate_limited(denied)
    try:
        factor = verify_factor(user, code=code, recovery_code=recovery_code)
    except MfaLockedError:
        throttle.failed()
        raise
    if factor is None:
        throttle.failed()
        raise InvalidMfaCodeError()
    throttle.succeeded()
    return factor


def _locked_signed_in(request: HttpRequest, user_id: UUID) -> User:
    user = User.objects.select_for_update(no_key=True).filter(pk=user_id).first()
    if user is None or not user.is_active:
        raise PermissionDeniedError()
    if not session_matches(request, user):
        raise PermissionDeniedError(SESSION_ENDED)
    return user


def _end_other_sessions(request: HttpRequest, user: User) -> None:
    """Every other session of the user ends; this one is kept (re-hashed, new key)."""
    user.session_epoch += 1
    user.save(update_fields=["session_epoch", "updated_at"])
    update_session_auth_hash(request, user)


def _issue_recovery_codes(user: User) -> list[str]:
    MfaRecoveryCode.objects.filter(user_id=user.pk).delete()
    codes = mfa.generate_recovery_codes()
    now = timezone.now()
    MfaRecoveryCode.objects.bulk_create(
        [
            MfaRecoveryCode(user_id=user.pk, code_hash=digest, created_at=now)
            for digest in mfa.hash_recovery_codes(codes)
        ]
    )
    return codes


def _notify(user: User, change: str) -> None:
    """Email the account's own address about a change to its second factor (outbox, in the
    caller's transaction: no change without its notice, no notice without its change)."""
    _notify_owner(user.pk, change, user.email)


def _notify_owner(user_id: UUID, change: str, email: str | None = None) -> None:
    """The address is the account's now, in the payload (review round 1, IDENTITY-3: read when
    the job ran, a deactivation or an email change before then dropped or redirected it);
    personal data, blanked by housekeeping."""
    if email is None:
        email = User.objects.filter(pk=user_id).values_list("email", flat=True).first()
    outbox.enqueue(
        TOPIC_MFA_CHANGED, {"user_id": str(user_id), "change": change, "email": email or ""}
    )


def notify_change(user_id: UUID, change: str, email: str | None = None) -> None:
    """Outbox job (queue "email"): to the address the account had when it happened (the
    current one for a notice queued before it was recorded), whatever its status now (an
    administrator's reset followed by a deactivation must still reach the owner). Skipped for
    an unknown change or a pseudonymised account."""
    user = User.objects.filter(pk=user_id).only("email", "first_name", "pseudonymised_at").first()
    if user is None or user.is_pseudonymised or change not in NOTICES:
        return
    emails.send_mfa_notice(to=email or user.email, first_name=user.first_name, change=change)


def _audit(action: str, actor_id: UUID | None, user: User, **metadata: object) -> None:
    audit.record(
        action,
        actor_id=actor_id,
        target_type="user",
        target_id=user.pk,
        subject_user_id=user.pk,
        metadata=metadata or None,
    )


# --- enrolment ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Setup:
    secret: str
    otpauth_uri: str
    qr: mfa.QrMatrix


def begin_setup(user: User) -> Setup:
    """A new unconfirmed device (replacing an earlier unconfirmed one). The caller checked
    the current password. The secret is returned here, once, and never again."""
    with transaction.atomic():
        locked = User.objects.select_for_update(no_key=True).get(pk=user.pk)
        device = MfaDevice.objects.select_for_update().filter(user_id=locked.pk).first()
        if device is not None and device.confirmed_at is not None:
            raise MfaAlreadyEnabledError()
        secret = mfa.generate_secret()
        deadline = _moment() + timedelta(seconds=settings.MFA_SETUP_TTL_S)
        if device is None:
            MfaDevice.objects.create(
                user_id=locked.pk, secret=mfa.encrypt(secret), pending_until=deadline
            )
        else:
            device.secret, device.last_used_step = mfa.encrypt(secret), None
            device.pending_until, device.setup_attempts = deadline, 0
            device.save(
                update_fields=[
                    "secret",
                    "last_used_step",
                    "pending_until",
                    "setup_attempts",
                    "updated_at",
                ]
            )
    uri = mfa.otpauth_uri(secret, locked.email)
    return Setup(secret=secret, otpauth_uri=uri, qr=mfa.qr_matrix(uri))


def confirm(request: HttpRequest, user_id: UUID, code: str) -> list[str]:
    """Activate the pending device with a code from it: ten recovery codes (shown once),
    this session counts as verified now, every other session ends.

    Throttled like a sign-in (a session taken before enrolment can't guess its way in), and
    the setup itself expires: MFA_SETUP_MAX_ATTEMPTS wrong codes or MFA_SETUP_TTL_S after
    it started, it is deleted (`mfa_setup_required`: start again, with the password)."""
    owner = User.objects.filter(pk=user_id).only("email").first()
    if owner is None:
        raise PermissionDeniedError()
    throttle = throttling.for_request(request, owner.email)
    denied = throttle.reserve()
    if denied is not None:
        raise throttling.rate_limited(denied)
    try:
        codes = _confirm_locked(request, user_id, code)
    except InvalidMfaCodeError:
        throttle.failed()
        raise
    except DomainError:
        throttle.release()  # nothing was guessed
        raise
    throttle.succeeded()
    _forget(request.user)
    return codes


def _confirm_locked(request: HttpRequest, user_id: UUID, code: str) -> list[str]:
    """The confirmation under the user's and the device's locks. A wrong code's count (or the
    setup's deletion) commits before the error is raised."""
    outcome = "confirmed"
    codes: list[str] = []
    with transaction.atomic():
        user = _locked_signed_in(request, user_id)
        device = MfaDevice.objects.select_for_update().filter(user_id=user.pk).first()
        if device is None:
            raise MfaSetupRequiredError()
        if device.confirmed_at is not None:
            raise MfaAlreadyEnabledError()
        now = _moment()
        try:
            secret: str | None = mfa.decrypt(device.secret)
        except InvalidToken:
            secret = None
        step = None if secret is None else mfa.matching_step(secret, code, at=_now())
        if secret is None or device.pending_until is None or device.pending_until <= now:
            device.delete()  # expired (or under a retired key): start again
            outcome = "expired"
        elif step is None:
            device.setup_attempts += 1
            if device.setup_attempts >= settings.MFA_SETUP_MAX_ATTEMPTS:
                device.delete()
            else:
                device.save(update_fields=["setup_attempts", "updated_at"])
            outcome = "wrong"
        else:
            device.confirmed_at, device.pending_until = timezone.now(), None
            device.last_used_step = step
            device.secret = mfa.rotate(device.secret)
            device.save(
                update_fields=[
                    "confirmed_at",
                    "pending_until",
                    "last_used_step",
                    "secret",
                    "updated_at",
                ]
            )
            codes = _issue_recovery_codes(user)
            _end_other_sessions(request, user)
            _audit(AUDIT_ENABLED, user.pk, user)
            _notify(user, "enabled")
            mark_verified(request.session)
    if outcome == "expired":
        raise MfaSetupRequiredError()
    if outcome == "wrong":
        raise InvalidMfaCodeError()
    return codes


def purge_expired_setups(now: datetime) -> int:
    """Delete setups nobody confirmed in time (hourly, identity.housekeeping)."""
    deleted, _ = MfaDevice.objects.filter(
        confirmed_at__isnull=True, pending_until__lte=now
    ).delete()
    return deleted


def replace_recovery_codes(request: HttpRequest, user: User, code: str) -> list[str]:
    """New recovery codes for a code from the authenticator (or an unused recovery code);
    the old ones stop working and every other session ends."""
    if not has_confirmed_device(user):
        raise MfaSetupRequiredError()
    _throttled_verify(request, user, **_either(code))
    with transaction.atomic():
        locked = _locked_signed_in(request, user.pk)
        if not (
            MfaDevice.objects.select_for_update()
            .filter(user_id=locked.pk, confirmed_at__isnull=False)
            .exists()
        ):
            raise MfaSetupRequiredError()
        codes = _issue_recovery_codes(locked)
        _end_other_sessions(request, locked)
        _audit(AUDIT_CODES_REPLACED, locked.pk, locked)
        _notify(locked, "recovery_codes_replaced")
        mark_verified(request.session)
    return codes


def disable(request: HttpRequest, user: User, code: str) -> None:
    """Turn the second factor off (the caller checked the current password): refused where
    the role requires it. Every other session ends."""
    if required_for(user):
        raise BusinessRuleViolation(required_message())
    if not has_confirmed_device(user):
        raise MfaSetupRequiredError()
    _throttled_verify(request, user, **_either(code))
    with transaction.atomic():
        locked = _locked_signed_in(request, user.pk)
        if required_for(locked):  # promoted meanwhile
            raise BusinessRuleViolation(required_message())
        _remove_factor(locked)
        _end_other_sessions(request, locked)
        _audit(AUDIT_DISABLED, locked.pk, locked)
        _notify(locked, "disabled")
        request.session.pop(SESSION_VERIFIED_AT, None)
    _forget(request.user)


def step_up(
    request: HttpRequest, user: User, *, code: str | None = None, recovery_code: str | None = None
) -> None:
    """Renew this session's verification (POST /auth/mfa/verify)."""
    if not has_confirmed_device(user):
        raise MfaSetupRequiredError()
    _throttled_verify(request, user, code=code, recovery_code=recovery_code)
    mark_verified(request.session)


def _remove_factor(user: User) -> bool:
    deleted, _ = MfaDevice.objects.filter(user_id=user.pk).delete()
    MfaRecoveryCode.objects.filter(user_id=user.pk).delete()
    return deleted > 0


# --- resets ------------------------------------------------------------------------------------
def reset_for_user(*, actor_id: UUID, user_id: UUID) -> User:
    """An administrator removes an ordinary user's second factor (lost phone and codes): the
    user signs in with their password and may set up a new one; their sessions end. Never an
    administrator's of either kind, their own included (ADR-0030/0033/0035): an operator runs
    `reset_mfa`. Within the actor's reach (404 outside it, identical to no such account; a
    team administrator: their team's users), and not for an account another administrator
    took out of an administrator's role within the cooling-off (ADR-0035)."""
    with transaction.atomic():
        serialise_user_administration()
        actor = _acting_manager(actor_id)
        user = _lock_reachable_user(actor, user_id)
        refuse_if_pseudonymised(user)
        if user.role in administrator_roles():
            raise BusinessRuleViolation(ADMINISTRATOR_MFA_REFUSED)
        _refuse_if_recently_demoted(actor, user)
        if _remove_factor(user):
            user.session_epoch += 1
            user.version += 1
            user.save(update_fields=["session_epoch", "version", "updated_at"])
            _audit(AUDIT_RESET, actor.pk, user, by="administrator")
            _notify(user, "reset")
    return selectors.admin_user_detail(user.pk)


def operator_reset(email: str, reason: str, *, operator_id: UUID) -> bool:
    """`manage.py reset_mfa`: remove anyone's second factor (administrators included), with
    shell and database access, on behalf of `operator_id` (an active super administrator,
    re-read under the lock: a team administrator administers only their team, ADR-0035; the
    sole administrator may reset their own). Audited with them as the actor and the reason as
    an expiring detail. False when there was nothing to remove."""
    with transaction.atomic():
        serialise_user_administration()
        operator = _acting_super_administrator(operator_id)
        user = (
            User.objects.select_for_update(no_key=True).filter(email=normalize_email(email)).first()
        )
        if user is None:
            raise NotFoundError("No account has this email address.")
        refuse_if_pseudonymised(user)
        if not _remove_factor(user):
            return False
        user.session_epoch += 1
        user.version += 1
        user.save(update_fields=["session_epoch", "version", "updated_at"])
        audit.record(
            AUDIT_RESET,
            actor_id=operator.pk,
            target_type="user",
            target_id=user.pk,
            subject_user_id=user.pk,
            metadata={"by": "operator"},
            sensitive={"reason": reason},
        )
        _notify(user, "reset")
    return True


def reencrypt_all() -> tuple[int, int]:
    """Re-encrypt every device's secret under the current SECRET_KEY (before a fallback key
    is retired). (re-encrypted, unreadable): an unreadable one is under no configured key
    and its owner needs a reset."""
    done = unreadable = 0
    for pk in MfaDevice.objects.order_by("pk").values_list("pk", flat=True):
        with transaction.atomic():
            device = MfaDevice.objects.select_for_update().get(pk=pk)
            try:
                device.secret = mfa.rotate(device.secret)
            except InvalidToken:
                unreadable += 1
                continue
            device.save(update_fields=["secret", "updated_at"])
            done += 1
    return done, unreadable


# --- the sign-in challenge (second step) ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Challenge:
    user: User
    attempts: int


def begin_challenge(request: HttpRequest, user: User) -> int:
    """After the right password of an account with a second factor: a fresh, anonymous
    session holding only the pending sign-in (no signed-in session yet). Its lifetime (s)."""
    request.session.flush()  # nothing of an earlier session carries over
    request.session[SESSION_CHALLENGE] = {
        "user": str(user.pk),
        "hash": user.get_session_auth_hash(),
        "at": _now(),
        "attempts": 0,
    }
    ttl = int(settings.MFA_CHALLENGE_TTL_S)
    request.session.set_expiry(timezone.now() + timedelta(seconds=ttl))
    return ttl


def end_challenge(request: HttpRequest) -> None:
    request.session.pop(SESSION_CHALLENGE, None)


def read_challenge(request: HttpRequest) -> Challenge:
    """The pending sign-in, if still valid: not expired, the account still active, and its
    password and session epoch unchanged since the password was checked."""
    data = request.session.get(SESSION_CHALLENGE)
    if not isinstance(data, dict):
        raise MfaChallengeExpiredError()
    at, attempts, user_id, digest = (
        data.get("at"),
        data.get("attempts"),
        data.get("user"),
        data.get("hash"),
    )
    if (
        not isinstance(at, int | float)
        or not isinstance(attempts, int)
        or not isinstance(user_id, str)
        or not isinstance(digest, str)
    ):
        end_challenge(request)
        raise MfaChallengeExpiredError()
    age = _now() - at
    if not -_CLOCK_SKEW_S <= age < settings.MFA_CHALLENGE_TTL_S or (
        attempts >= settings.MFA_CHALLENGE_MAX_ATTEMPTS
    ):
        end_challenge(request)
        raise MfaChallengeExpiredError()
    try:
        user = User.objects.filter(pk=UUID(user_id)).first()
    except ValueError:
        user = None
    if (
        user is None
        or not user.is_active
        or not session_digest_matches(digest, user)
        or not has_confirmed_device(user)
    ):
        end_challenge(request)
        raise MfaChallengeExpiredError()
    return Challenge(user=user, attempts=attempts)


def session_digest_matches(digest: str, user: User) -> bool:
    return constant_time_compare(digest, user.get_session_auth_hash())


def record_wrong_code(request: HttpRequest, challenge: Challenge) -> None:
    """Count a wrong code; the MFA_CHALLENGE_MAX_ATTEMPTS-th voids the challenge."""
    attempts = challenge.attempts + 1
    if attempts >= settings.MFA_CHALLENGE_MAX_ATTEMPTS:
        end_challenge(request)
        return
    data = dict(request.session[SESSION_CHALLENGE])
    data["attempts"] = attempts
    request.session[SESSION_CHALLENGE] = data
