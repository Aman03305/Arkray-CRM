"""Wrong second-factor codes accumulate past new passwords, up to a cap only an MFA reset
lifts (R133, the residual of deep security audit M-4).

A new password (emailed reset, or set by an administrator) unlocks a factor locked by
MFA_MAX_CONSECUTIVE_FAILURES wrong codes in a row, so someone holding both the password and
the mailbox could buy that many more guesses with each emailed reset. The same wrong codes
also count in `cumulative_failures`, which only a right code (or an MFA reset, which deletes
the device) forgets: from MFA_MAX_FAILURES_BEFORE_RESET on, no code is accepted until an MFA
reset (`403 mfa_locked_until_reset`), whatever the password."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.identity import metrics, mfa, mfa_services
from arkray.identity import services as identity_services
from arkray.identity.models import AuthThrottleEvent, MfaDevice, MfaLock, MfaRecoveryCode
from config.settings import base
from tests.factories import DEFAULT_PASSWORD, AdminFactory, UserFactory
from tests.helpers import drain_outbox, last_secret, run_concurrently, signed_in
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

LIMIT = 7  # in a row: more than one challenge's five attempts
CAP = 15  # since the last right code: two whole locks, and one more code
PASSWORDS = ("freshly-chosen-passphrase-7", "another-fresh-passphrase-8", "third-new-passphrase-9")
UNTIL_RESET = "mfa_locked_until_reset"
LOCKED_UNTIL_RESET_NOTICE = "Arkray CRM: Two-step verification is locked until it is reset"


@pytest.fixture(autouse=True)
def _limits(settings):
    settings.MFA_MAX_CONSECUTIVE_FAILURES = LIMIT
    settings.MFA_MAX_FAILURES_BEFORE_RESET = CAP


def error_code(response) -> str:
    return str(response.json()["error"]["code"])


def wrong_code(secret: str, clock: Clock) -> str:
    """A well-formed code that matches none of the steps accepted now."""
    accepted = {code_for(secret, clock, steps) for steps in (-1, 0, 1)}
    return next(c for c in (f"{n:06d}" for n in range(1_000_000)) if c not in accepted)


def wrong_recovery_code() -> str:
    return mfa.generate_recovery_codes()[0]  # well-formed, never issued to anyone


def counts(user) -> tuple[int, int]:
    device = MfaDevice.objects.get(user=user)
    return device.consecutive_failures, device.cumulative_failures


def window_passes() -> None:
    """The throttles' windows slide on (the durable sign-in throttle's, and the request
    rate's per address): these tests are about what outlasts them."""
    AuthThrottleEvent.objects.all().delete()
    cache.clear()


def guess_at_sign_in(user, secret: str, clock: Clock, count: int, password: str) -> list[str]:
    """`count` wrong codes after the right password, in fresh browsers and challenges: the
    error code of each answer."""
    answers: list[str] = []
    while len(answers) < count:
        window_passes()
        client = APIClient()
        assert password_step(client, user, password).status_code == 202
        for _ in range(min(count - len(answers), settings.MFA_CHALLENGE_MAX_ATTEMPTS - 1)):
            response = client.post(LOGIN_MFA, {"code": wrong_code(secret, clock)}, format="json")
            assert response.status_code in (400, 403), response.content
            answers.append(error_code(response))
            if response.status_code == 403:
                break  # the challenge is over
    window_passes()
    return answers


def reset_password_by_email(user, new_password: str) -> None:
    client = APIClient()
    client.post("/api/v1/auth/password-reset", {"email": user.email}, format="json")
    drain_outbox()
    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        {"token": last_secret("reset-password"), "new_password": new_password},
        format="json",
    )
    assert confirmed.status_code == 204, confirmed.content


def set_password_as_administrator(admin, user, new_password: str) -> None:
    user.refresh_from_db()
    identity_services.set_user_password(
        actor_id=admin.pk, user_id=user.pk, version=user.version, new_password=new_password
    )


def hard_lock(user, admin, secret: str, clock: Clock) -> str:
    """Two locks, each ended by a new password, then the code that reaches the cap. The
    password the account has then."""
    assert guess_at_sign_in(user, secret, clock, LIMIT, DEFAULT_PASSWORD)[-1] == "mfa_locked"
    reset_password_by_email(user, PASSWORDS[0])
    assert counts(user) == (0, LIMIT)
    assert guess_at_sign_in(user, secret, clock, LIMIT, PASSWORDS[0])[-1] == "mfa_locked"
    set_password_as_administrator(admin, user, PASSWORDS[1])
    assert counts(user) == (0, 2 * LIMIT)
    assert guess_at_sign_in(user, secret, clock, 1, PASSWORDS[1]) == [UNTIL_RESET]
    assert counts(user) == (1, CAP)
    return PASSWORDS[1]


def mfa_locked_events():
    return AuditEvent.objects.filter(action=mfa_services.AUDIT_LOCKED).order_by("occurred_at")


def notices() -> list[str]:
    drain_outbox()
    return [str(message.subject) for message in mail.outbox]


def test_the_shipped_cap_is_three_whole_locks():
    assert base.MFA_MAX_FAILURES_BEFORE_RESET == 3 * base.MFA_MAX_CONSECUTIVE_FAILURES == 60


# --- the cap ----------------------------------------------------------------------------------
def test_new_passwords_unlock_until_the_cap_then_only_an_mfa_reset_does(admin, user_a, clock):
    enrolment = enrol(user_a)
    password = hard_lock(user_a, admin, enrolment.secret, clock)

    # Audited once per lock, ids and counts only; the third says only an MFA reset helps.
    events = list(mfa_locked_events())
    assert [event.metadata for event in events] == [
        {"failures": LIMIT},
        {"failures": LIMIT},
        {"failures": CAP, "until": "mfa_reset"},
    ]
    assert (events[-1].actor_id, events[-1].target_id, events[-1].subject_user_id) == (
        None,
        str(user_a.pk),
        user_a.pk,
    )
    unlocked = AuditEvent.objects.filter(action=identity_services.AUDIT_MFA_UNLOCKED)
    assert unlocked.count() == 2

    # Locked: the right code is refused too, with what to do instead, and nothing is counted.
    window_passes()
    client = APIClient()
    assert password_step(client, user_a, password).status_code == 202
    clock.next_step()
    right = client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
    assert right.status_code == 403
    assert error_code(right) == UNTIL_RESET
    message = right.json()["error"]["message"]
    assert message == mfa_services.MfaLockedUntilResetError.default_message
    assert "won't unlock it" in message
    assert "_auth_user_id" not in client.session
    assert counts(user_a) == (1, CAP)
    # The challenge is over (as for any lock), and the failure is audited like one.
    again = client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
    assert error_code(again) == "mfa_challenge_expired"
    failed = AuditEvent.objects.filter(action="auth.login_failed", metadata__reason="mfa_locked")
    assert failed.count() == 4  # the three codes that locked it, and the one refused since

    # A password reset by email doesn't unlock it, nor does one set by an administrator.
    reset_password_by_email(user_a, PASSWORDS[2])
    assert MfaDevice.objects.get(user=user_a).locked_until_reset
    window_passes()
    refused = sign_in_with_code(APIClient(), user_a, enrolment.secret, clock, PASSWORDS[2])
    assert error_code(refused) == UNTIL_RESET
    set_password_as_administrator(admin, user_a, "a-fourth-new-passphrase-0")
    assert MfaDevice.objects.get(user=user_a).locked_until_reset
    assert unlocked.count() == 2  # neither recorded an unlock
    assert mfa_locked_events().count() == 3

    # An administrator's MFA reset does: the user signs in with the password, and a new
    # authenticator starts from nothing.
    reset = signed_in(admin).post(f"/api/v1/admin/users/{user_a.pk}/mfa/reset", format="json")
    assert reset.status_code == 200, reset.content
    assert reset.json()["mfa_lock"] is None
    assert not MfaDevice.objects.filter(user=user_a).exists()
    window_passes()
    assert password_step(APIClient(), user_a, "a-fourth-new-passphrase-0").status_code == 200
    again_enrolled = enrol(user_a)
    assert counts(user_a) == (0, 0)
    window_passes()
    response = sign_in_with_code(
        APIClient(), user_a, again_enrolled.secret, clock, "a-fourth-new-passphrase-0"
    )
    assert response.status_code == 200, response.content


def test_the_cap_reached_on_a_lock_is_one_lock_until_an_mfa_reset(user_a, clock, settings):
    """With the shipped proportions (the cap three whole locks) the third lock is the one
    only an MFA reset lifts: one event and one notice, not a lock a new password lifts."""
    settings.MFA_MAX_CONSECUTIVE_FAILURES, settings.MFA_MAX_FAILURES_BEFORE_RESET = 4, 12
    enrolment = enrol(user_a)
    MfaDevice.objects.filter(user=user_a).update(consecutive_failures=0, cumulative_failures=8)
    outcomes = []
    guess = wrong_code(enrolment.secret, clock)
    for _ in range(4):
        try:
            outcomes.append(mfa_services.verify_factor(user_a, code=guess))
        except mfa_services.MfaLockedError as exc:
            outcomes.append(exc.code)
    assert outcomes == [None, None, None, UNTIL_RESET]
    assert counts(user_a) == (4, 12)
    assert [event.metadata for event in mfa_locked_events()] == [
        {"failures": 12, "until": "mfa_reset"}
    ]
    assert notices()[-1] == LOCKED_UNTIL_RESET_NOTICE
    assert "Arkray CRM: Two-step verification is locked" not in notices()


def test_a_right_code_before_the_cap_forgets_both_counts(admin, user_a, clock):
    enrolment = enrol(user_a)
    guess_at_sign_in(user_a, enrolment.secret, clock, LIMIT, DEFAULT_PASSWORD)
    set_password_as_administrator(admin, user_a, PASSWORDS[0])
    guess_at_sign_in(user_a, enrolment.secret, clock, LIMIT - 1, PASSWORDS[0])
    assert counts(user_a) == (LIMIT - 1, 2 * LIMIT - 1)  # one code short of either lock

    signed_in_now = sign_in_with_code(APIClient(), user_a, enrolment.secret, clock, PASSWORDS[0])
    assert signed_in_now.status_code == 200, signed_in_now.content
    assert counts(user_a) == (0, 0)

    # A right recovery code forgets them too.
    guess_at_sign_in(user_a, enrolment.secret, clock, LIMIT - 1, PASSWORDS[0])
    MfaDevice.objects.filter(user=user_a).update(cumulative_failures=CAP - 1)
    window_passes()
    client = APIClient()
    assert password_step(client, user_a, PASSWORDS[0]).status_code == 202
    recovered = client.post(
        LOGIN_MFA, {"recovery_code": enrolment.recovery_codes[0]}, format="json"
    )
    assert recovered.status_code == 200, recovered.content
    assert counts(user_a) == (0, 0)


def test_recovery_codes_count_toward_the_cap_and_are_refused_past_it(user_a, clock):
    enrolment = enrol(user_a)
    MfaDevice.objects.filter(user=user_a).update(cumulative_failures=CAP - 2)
    client = APIClient()
    assert password_step(client, user_a).status_code == 202
    wrong = client.post(LOGIN_MFA, {"recovery_code": wrong_recovery_code()}, format="json")
    assert error_code(wrong) == "invalid_mfa_code"
    locking = client.post(LOGIN_MFA, {"recovery_code": wrong_recovery_code()}, format="json")
    assert error_code(locking) == UNTIL_RESET
    assert counts(user_a) == (2, CAP)

    window_passes()
    client = APIClient()
    assert password_step(client, user_a).status_code == 202
    refused = client.post(LOGIN_MFA, {"recovery_code": enrolment.recovery_codes[0]}, format="json")
    assert error_code(refused) == UNTIL_RESET
    assert not MfaRecoveryCode.objects.filter(user=user_a, used_at__isnull=False).exists()


def test_step_up_codes_count_toward_the_cap_and_are_refused_past_it(user_a, clock):
    enrolment = enrol(user_a)
    MfaDevice.objects.filter(user=user_a).update(cumulative_failures=CAP - 1)
    client = signed_in(user_a)
    locking = client.post(VERIFY, {"code": wrong_code(enrolment.secret, clock)}, format="json")
    assert locking.status_code == 403
    assert error_code(locking) == UNTIL_RESET

    window_passes()
    clock.next_step()
    right = client.post(VERIFY, {"code": code_for(enrolment.secret, clock)}, format="json")
    assert error_code(right) == UNTIL_RESET
    assert mfa_services.SESSION_VERIFIED_AT not in client.session
    replace = client.post(
        "/api/v1/auth/mfa/recovery-codes",
        {"code": code_for(enrolment.secret, clock)},
        format="json",
    )
    assert error_code(replace) == UNTIL_RESET
    assert counts(user_a) == (1, CAP)


def test_an_operators_reset_mfa_lifts_it_for_an_administrator(admin, clock):
    target = AdminFactory(email="locked.admin@example.test")
    enrolment = enrol(target)
    MfaDevice.objects.filter(user=target).update(cumulative_failures=CAP)
    window_passes()
    refused = sign_in_with_code(APIClient(), target, enrolment.secret, clock)
    assert error_code(refused) == UNTIL_RESET

    call_command("reset_mfa", "--email", target.email, "--by", admin.email, "--reason", "ticket 9")
    assert not MfaDevice.objects.filter(user=target).exists()
    window_passes()
    assert password_step(APIClient(), target).status_code == 200


def test_a_password_change_in_a_session_forgets_nothing(user_a, clock):
    enrolment = enrol(user_a)
    MfaDevice.objects.filter(user=user_a).update(consecutive_failures=3, cumulative_failures=9)
    changed = signed_in(user_a).post(
        "/api/v1/auth/password/change",
        {"current_password": DEFAULT_PASSWORD, "new_password": PASSWORDS[0]},
        format="json",
    )
    assert changed.status_code == 204, changed.content
    assert counts(user_a) == (3, 9)
    assert enrolment.secret  # the device is kept


# --- races ------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_codes_racing_at_the_cap_lock_it_exactly_once(clock):
    """The counts are kept under the device's row lock: four wrong codes at once, two short
    of the cap, count two (the second locks it) and refuse two; never past the cap, one
    event, one notice."""
    user = UserFactory()
    enrolment = enrol(user)
    MfaDevice.objects.filter(user=user).update(cumulative_failures=CAP - 2)
    guess = wrong_code(enrolment.secret, clock)

    def attempt() -> object:
        return mfa_services.verify_factor(user, code=guess)

    results = run_concurrently(*([attempt] * 4))
    wrong = [result for result in results if result is None]
    locked = [r for r in results if isinstance(r, mfa_services.MfaLockedUntilResetError)]
    assert (len(wrong), len(locked)) == (1, 3), results
    assert counts(user) == (2, CAP)
    events = AuditEvent.objects.filter(action=mfa_services.AUDIT_LOCKED)
    assert [event.metadata for event in events] == [{"failures": CAP, "until": "mfa_reset"}]
    drain_outbox()
    subjects = [str(message.subject) for message in mail.outbox]
    assert subjects.count(LOCKED_UNTIL_RESET_NOTICE) == 1


# --- telling the owner, administrators and operators --------------------------------------------
def test_the_owner_is_emailed_that_a_new_password_wont_help(admin, user_a, clock):
    enrolment = enrol(user_a)
    hard_lock(user_a, admin, enrolment.secret, clock)
    sent = notices()
    assert sent[-1] == LOCKED_UNTIL_RESET_NOTICE
    message = mail.outbox[-1]
    assert message.to == [user_a.email]
    assert "resetting your password no longer unlocks it" in message.body
    assert "reset your two-step verification" in message.body
    assert "secure your email account first" in message.body
    assert wrong_code(enrolment.secret, clock) not in message.body  # never a code


def test_the_lock_shows_on_the_mfa_status_and_to_administrators(admin, user_a, clock):
    enrol(user_a)
    own = signed_in(user_a)
    administrator = signed_in(admin)

    def shown() -> tuple[object, object, object]:
        detail = administrator.get(f"/api/v1/admin/users/{user_a.pk}").json()
        listed = administrator.get("/api/v1/admin/users").json()["results"]
        row = next(item for item in listed if item["id"] == str(user_a.pk))
        assert row["mfa_lock"] == detail["mfa_lock"]
        return own.get("/api/v1/auth/mfa").json()["lock"], detail["mfa_lock"], row["mfa_lock"]

    assert shown() == (None, None, None)
    MfaDevice.objects.filter(user=user_a).update(
        consecutive_failures=LIMIT, cumulative_failures=LIMIT
    )
    assert shown() == ("password_reset",) * 3
    MfaDevice.objects.filter(user=user_a).update(consecutive_failures=1, cumulative_failures=CAP)
    assert shown() == ("mfa_reset",) * 3
    # Someone without a second factor: no lock.
    assert administrator.get(f"/api/v1/admin/users/{admin.pk}").json()["mfa_lock"] is None
    assert {choice.value for choice in MfaLock} == {"password_reset", "mfa_reset"}


def test_administrators_see_which_lock_it_was(admin, user_a, clock):
    enrolment = enrol(user_a)
    hard_lock(user_a, admin, enrolment.secret, clock)
    events = signed_in(admin).get("/api/v1/admin/security-events").json()["results"]
    locks = [event["details"] for event in events if event["action"] == "mfa.locked"]
    assert locks[0] == {"failures": str(CAP), "until": "mfa_reset"}  # newest first
    assert locks[1:] == [{"failures": str(LIMIT)}] * 2


def test_the_until_reset_gauge_counts_the_last_hour(admin, user_a, clock, monkeypatch):
    def value(name: str) -> float:
        by_name = {gauge.name: gauge for gauge in metrics.gauges()}
        return by_name[name].samples[0][1]

    enrolment = enrol(user_a)
    hard_lock(user_a, admin, enrolment.secret, clock)
    assert value("arkray_mfa_locked_until_reset_last_hour") == 1
    assert value("arkray_mfa_lockouts_last_hour") == 3  # every lock, this one included
    later = timezone.now() + timedelta(hours=1, minutes=1)
    monkeypatch.setattr(metrics.timezone, "now", lambda: later)
    assert value("arkray_mfa_locked_until_reset_last_hour") == 0


# --- configuration ----------------------------------------------------------------------------
@pytest.mark.parametrize(("consecutive", "cap"), [(20, 20), (20, 19), (0, 60), (-1, 60)])
def test_the_cap_must_be_more_than_the_consecutive_limit(settings, consecutive, cap):
    settings.MFA_MAX_CONSECUTIVE_FAILURES, settings.MFA_MAX_FAILURES_BEFORE_RESET = consecutive, cap
    assert [error.id for error in mfa_services.check_failure_limits()] == ["arkray.E030"]


def test_the_shipped_limits_pass_the_check(settings):
    settings.MFA_MAX_CONSECUTIVE_FAILURES, settings.MFA_MAX_FAILURES_BEFORE_RESET = 20, 60
    assert mfa_services.check_failure_limits() == []
