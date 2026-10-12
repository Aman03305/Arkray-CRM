"""Wrong second-factor codes accumulate until the factor locks (deep security audit M-4).

The durable sign-in throttle only slows guessing down (its window slides: about 550 codes a
day for someone with the password). Wrong codes now also count on the device, across sign-in
attempts, browsers, step-ups and recovery codes; from MFA_MAX_CONSECUTIVE_FAILURES on no code
is accepted, the right one included, until a password reset or an MFA reset. The owner is
told by email."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.identity import metrics, mfa, mfa_services, selectors
from arkray.identity import services as identity_services
from arkray.identity.models import AuthThrottleEvent, MfaDevice, MfaRecoveryCode
from tests.factories import DEFAULT_PASSWORD
from tests.helpers import drain_outbox, last_secret, signed_in
from tests.mfa_helpers import (
    LOGIN_MFA,
    VERIFY,
    Clock,
    code_for,
    enrol,
    password_step,
    sign_in_with_code,
)

pytestmark = pytest.mark.django_db

LIMIT = 7  # small, and more than one challenge's five attempts: the count crosses challenges
NEW_PASSWORD = "freshly-chosen-passphrase-7"


@pytest.fixture(autouse=True)
def _limit(settings):
    settings.MFA_MAX_CONSECUTIVE_FAILURES = LIMIT


def error_code(response) -> str:
    return str(response.json()["error"]["code"])


def wrong_code(secret: str, clock: Clock) -> str:
    """A well-formed code that matches none of the steps accepted now."""
    accepted = {code_for(secret, clock, steps) for steps in (-1, 0, 1)}
    return next(c for c in (f"{n:06d}" for n in range(1_000_000)) if c not in accepted)


def wrong_recovery_code() -> str:
    return mfa.generate_recovery_codes()[0]  # well-formed, never issued to anyone


def failures(user) -> int:
    return MfaDevice.objects.get(user=user).consecutive_failures


def window_passes() -> None:
    """The durable throttle's window slides on: these tests are about what outlasts it."""
    AuthThrottleEvent.objects.all().delete()


def guess_at_sign_in(user, secret: str, clock: Clock, count: int) -> None:
    """`count` wrong codes after the right password, in fresh browsers and challenges."""
    left = count
    while left:
        window_passes()
        client = APIClient()
        assert password_step(client, user).status_code == 202
        for _ in range(min(left, settings.MFA_CHALLENGE_MAX_ATTEMPTS - 1)):
            response = client.post(LOGIN_MFA, {"code": wrong_code(secret, clock)}, format="json")
            assert response.status_code in (400, 403), response.content
            left -= 1
    window_passes()


def lock(user, secret: str, clock: Clock) -> None:
    guess_at_sign_in(user, secret, clock, LIMIT)
    assert MfaDevice.objects.get(user=user).locked


def notices() -> list[str]:
    drain_outbox()
    return [str(message.subject) for message in mail.outbox]


# --- locking --------------------------------------------------------------------------------------
def test_second_factor_locks_after_max_consecutive_failures(user_a, clock):
    enrolment = enrol(user_a)
    guess_at_sign_in(user_a, enrolment.secret, clock, LIMIT - 1)
    assert failures(user_a) == LIMIT - 1  # across challenges and browsers
    assert not AuditEvent.objects.filter(action=mfa_services.AUDIT_LOCKED).exists()

    client = APIClient()
    assert password_step(client, user_a).status_code == 202
    last = client.post(LOGIN_MFA, {"code": wrong_code(enrolment.secret, clock)}, format="json")
    assert last.status_code == 403
    assert error_code(last) == "mfa_locked"
    assert failures(user_a) == LIMIT
    event = AuditEvent.objects.get(action=mfa_services.AUDIT_LOCKED)
    assert (event.actor_id, event.target_id, event.subject_user_id) == (
        None,
        str(user_a.pk),
        user_a.pk,
    )
    assert event.metadata == {"failures": LIMIT}  # ids and a count only

    # Locked: the right code is refused too, in a new challenge, and nothing more is counted.
    window_passes()
    client = APIClient()
    assert password_step(client, user_a).status_code == 202
    clock.next_step()
    right = client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
    assert right.status_code == 403
    assert error_code(right) == "mfa_locked"
    assert right.json()["error"]["message"] == mfa_services.MfaLockedError.default_message
    assert "_auth_user_id" not in client.session  # not signed in
    assert failures(user_a) == LIMIT
    assert AuditEvent.objects.filter(action=mfa_services.AUDIT_LOCKED).count() == 1
    # The challenge is over: the password is asked again.
    again = client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
    assert error_code(again) == "mfa_challenge_expired"
    # Shown to administrators: the code that locked it, and the attempt refused since.
    failed = AuditEvent.objects.filter(action="auth.login_failed", metadata__reason="mfa_locked")
    assert failed.count() == 2
    assert mfa_services.AUDIT_LOCKED in selectors.SECURITY_ACTIONS


def test_a_password_reset_by_email_unlocks_it(api_client, user_a, clock):
    enrolment = enrol(user_a)
    lock(user_a, enrolment.secret, clock)

    api_client.post("/api/v1/auth/password-reset", {"email": user_a.email}, format="json")
    drain_outbox()
    confirmed = api_client.post(
        "/api/v1/auth/password-reset/confirm",
        {"token": last_secret("reset-password"), "new_password": NEW_PASSWORD},
        format="json",
    )
    assert confirmed.status_code == 204, confirmed.content
    assert failures(user_a) == 0
    unlocked = AuditEvent.objects.get(action=identity_services.AUDIT_MFA_UNLOCKED)
    assert (unlocked.actor_id, unlocked.metadata) == (user_a.pk, {"by": "password_reset"})

    window_passes()
    response = sign_in_with_code(APIClient(), user_a, enrolment.secret, clock, NEW_PASSWORD)
    assert response.status_code == 200, response.content


def test_an_administrators_mfa_reset_unlocks_it(admin, user_a, clock):
    enrolment = enrol(user_a)
    lock(user_a, enrolment.secret, clock)
    reset = signed_in(admin).post(f"/api/v1/admin/users/{user_a.pk}/mfa/reset", format="json")
    assert reset.status_code == 200, reset.content
    assert not MfaDevice.objects.filter(user=user_a).exists()  # the count went with it
    assert password_step(APIClient(), user_a).status_code == 200  # password only, as before


def test_a_password_set_by_an_administrator_unlocks_it(admin, user_a, clock):
    enrolment = enrol(user_a)
    lock(user_a, enrolment.secret, clock)
    user_a.refresh_from_db()
    identity_services.set_user_password(
        actor_id=admin.pk, user_id=user_a.pk, version=user_a.version, new_password=NEW_PASSWORD
    )
    assert failures(user_a) == 0
    unlocked = AuditEvent.objects.get(action=identity_services.AUDIT_MFA_UNLOCKED)
    assert (unlocked.actor_id, unlocked.metadata) == (admin.pk, {"by": "administrator"})
    # The authenticator is kept: the temporary password and a code sign in.
    response = sign_in_with_code(APIClient(), user_a, enrolment.secret, clock, NEW_PASSWORD)
    assert response.status_code == 200, response.content


def test_changing_the_password_in_a_session_does_not_unlock_it(user_a, clock):
    """The session may be the thief's too: only the emailed link or an administrator unlock."""
    enrolment = enrol(user_a)
    lock(user_a, enrolment.secret, clock)
    changed = signed_in(user_a).post(
        "/api/v1/auth/password/change",
        {"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )
    assert changed.status_code == 204, changed.content
    assert MfaDevice.objects.get(user=user_a).locked


def test_recovery_codes_count_too_and_are_refused_once_locked(user_a, clock):
    enrolment = enrol(user_a)
    guess_at_sign_in(user_a, enrolment.secret, clock, LIMIT - 2)
    client = APIClient()
    assert password_step(client, user_a).status_code == 202
    wrong = client.post(LOGIN_MFA, {"recovery_code": wrong_recovery_code()}, format="json")
    assert error_code(wrong) == "invalid_mfa_code"
    assert failures(user_a) == LIMIT - 1
    locking = client.post(LOGIN_MFA, {"recovery_code": wrong_recovery_code()}, format="json")
    assert error_code(locking) == "mfa_locked"

    window_passes()
    client = APIClient()
    assert password_step(client, user_a).status_code == 202
    valid = enrolment.recovery_codes[0]
    refused = client.post(LOGIN_MFA, {"recovery_code": valid}, format="json")
    assert error_code(refused) == "mfa_locked"
    assert MfaRecoveryCode.objects.filter(user=user_a, used_at__isnull=False).count() == 0


def test_step_up_codes_count_too(user_a, clock):
    enrolment = enrol(user_a)
    client = signed_in(user_a)
    for _ in range(LIMIT - 1):
        window_passes()
        response = client.post(VERIFY, {"code": wrong_code(enrolment.secret, clock)}, format="json")
        assert error_code(response) == "invalid_mfa_code"
    window_passes()
    locking = client.post(VERIFY, {"code": wrong_code(enrolment.secret, clock)}, format="json")
    assert locking.status_code == 403
    assert error_code(locking) == "mfa_locked"

    window_passes()
    clock.next_step()
    right = client.post(VERIFY, {"code": code_for(enrolment.secret, clock)}, format="json")
    assert error_code(right) == "mfa_locked"
    assert mfa_services.SESSION_VERIFIED_AT not in client.session
    # Replacing the recovery codes asks for a code as well: refused alike.
    replace = client.post(
        "/api/v1/auth/mfa/recovery-codes",
        {"code": code_for(enrolment.secret, clock)},
        format="json",
    )
    assert error_code(replace) == "mfa_locked"


def test_a_right_code_resets_the_count(user_a, clock):
    enrolment = enrol(user_a)
    guess_at_sign_in(user_a, enrolment.secret, clock, LIMIT - 1)
    assert sign_in_with_code(APIClient(), user_a, enrolment.secret, clock).status_code == 200
    assert failures(user_a) == 0
    guess_at_sign_in(user_a, enrolment.secret, clock, LIMIT - 1)
    assert not MfaDevice.objects.get(user=user_a).locked
    window_passes()
    client = APIClient()
    assert password_step(client, user_a).status_code == 202
    recovered = client.post(
        LOGIN_MFA, {"recovery_code": enrolment.recovery_codes[0]}, format="json"
    )
    assert recovered.status_code == 200  # a recovery code resets it too
    assert failures(user_a) == 0


def test_no_code_counts_without_the_password(api_client, user_a, clock):
    """Only someone past the password (a challenge) or with a session can add to the count:
    it is no lever for locking out an account whose password is safe."""
    enrolment = enrol(user_a)
    for _ in range(3):
        response = api_client.post(
            LOGIN_MFA, {"code": wrong_code(enrolment.secret, clock)}, format="json"
        )
        assert error_code(response) == "mfa_challenge_expired"
    assert failures(user_a) == 0


# --- telling the owner ----------------------------------------------------------------------------
WRONG_NOTICE = "Arkray CRM: A wrong verification code was entered"
LOCKED_NOTICE = "Arkray CRM: Two-step verification is locked"


def test_wrong_code_after_right_password_emails_owner(user_a, clock):
    enrolment = enrol(user_a)
    guess_at_sign_in(user_a, enrolment.secret, clock, 1)
    assert notices() == [WRONG_NOTICE]
    message = mail.outbox[0]
    assert message.to == [user_a.email]
    assert "reset it now" in message.body
    assert wrong_code(enrolment.secret, clock) not in message.body  # never a code


def test_the_wrong_code_email_is_rate_limited(user_a, clock):
    enrolment = enrol(user_a)
    guess_at_sign_in(user_a, enrolment.secret, clock, 3)
    assert notices() == [WRONG_NOTICE]  # once in the interval, not once per code

    # A right code in between doesn't restart the interval either.
    assert sign_in_with_code(APIClient(), user_a, enrolment.secret, clock).status_code == 200
    guess_at_sign_in(user_a, enrolment.secret, clock, 1)
    assert notices() == [WRONG_NOTICE]

    later = timezone.now() - timedelta(seconds=settings.MFA_FAILURE_NOTICE_INTERVAL_S + 1)
    MfaDevice.objects.filter(user=user_a).update(failure_notified_at=later)
    guess_at_sign_in(user_a, enrolment.secret, clock, 1)
    assert notices() == [WRONG_NOTICE, WRONG_NOTICE]


def test_the_owner_is_emailed_when_it_locks(user_a, clock):
    enrolment = enrol(user_a)
    lock(user_a, enrolment.secret, clock)
    assert notices() == [WRONG_NOTICE, LOCKED_NOTICE]
    assert "Forgot password" in mail.outbox[-1].body


# --- operations ---------------------------------------------------------------------------------
def test_the_lockout_gauge_counts_the_last_hour(user_a, clock, monkeypatch):
    def value() -> float:
        by_name = {gauge.name: gauge for gauge in metrics.gauges()}
        return by_name["arkray_mfa_lockouts_last_hour"].samples[0][1]

    enrolment = enrol(user_a)
    assert value() == 0
    lock(user_a, enrolment.secret, clock)
    assert value() == 1
    later = timezone.now() + timedelta(hours=1, minutes=1)
    monkeypatch.setattr(metrics.timezone, "now", lambda: later)
    assert value() == 0
