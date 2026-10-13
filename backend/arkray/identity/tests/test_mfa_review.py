"""Regressions from the adversarial review of two-step verification (ADR-0033): one test
per finding (P2-1, P2-2, P2-3, P3 owner notices)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from rest_framework.test import APIClient

from arkray.identity import mfa_services
from arkray.identity.models import AuthThrottleEvent, MfaDevice, ThrottleKind
from arkray.identity.tasks import housekeeping
from tests.factories import DEFAULT_PASSWORD
from tests.helpers import drain_outbox, signed_in
from tests.mfa_helpers import (
    LOGIN_MFA,
    VERIFY,
    code_for,
    enrol,
    password_step,
)

pytestmark = pytest.mark.django_db

SETUP = "/api/v1/auth/mfa/setup"
CONFIRM = "/api/v1/auth/mfa/confirm"
CODES = "/api/v1/auth/mfa/recovery-codes"
DISABLE = "/api/v1/auth/mfa/disable"
PASSWORD = "/api/v1/auth/password/change"


def failures() -> int:
    """Failures that still count toward a lockout. A right password (or code) forgives a
    browser's failures rather than deleting them: they still count toward the account's
    daily cap (review of 73ff1e3, L1)."""
    return AuthThrottleEvent.objects.filter(kind=ThrottleKind.LOGIN_FAILURE, forgiven=False).count()


def error_code(response):
    return response.json()["error"]["code"]


def wrong(secret, clock) -> str:
    return str((int(code_for(secret, clock)) + 1) % 1_000_000).zfill(6)


# --- P2-1 ---------------------------------------------------------------------------------------
class TestAPasswordDoesNotForgiveWrongCodes:
    def test_wrong_step_up_codes_survive_a_password_re_check(self, user_a, clock):
        """4 wrong codes, then the right password (a setup: 409), then one more wrong code:
        the account is locked for this browser (the password used to wipe the bucket)."""
        enrolment = enrol(user_a)
        client = signed_in(user_a)
        for _ in range(4):
            assert error_code(client.post(VERIFY, {"code": "000000"}, format="json")) == (
                "invalid_mfa_code"
            )
        recheck = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json")
        assert recheck.status_code == 409  # the password was right...
        assert failures() == 4  # ...and forgave nothing
        assert client.post(VERIFY, {"code": "000000"}, format="json").status_code == 400
        clock.next_step()
        locked = client.post(VERIFY, {"code": code_for(enrolment.secret, clock)}, format="json")
        assert locked.status_code == 429
        assert error_code(locked) == "rate_limited"

    def test_a_right_code_still_forgives(self, user_a, clock):
        enrolment = enrol(user_a)
        client = signed_in(user_a)
        for _ in range(3):
            client.post(VERIFY, {"code": "000000"}, format="json")
        clock.next_step()
        done = client.post(VERIFY, {"code": code_for(enrolment.secret, clock)}, format="json")
        assert done.status_code == 204
        assert failures() == 0

    def test_without_a_second_factor_a_right_password_forgives_as_before(self, user_a):
        client = signed_in(user_a)
        for _ in range(3):
            client.post(SETUP, {"current_password": "not-the-password"}, format="json")
        assert failures() == 3
        assert (
            client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json").status_code
            == 200
        )
        assert failures() == 0

    def test_sign_in_lockout_is_unchanged(self, user_a):
        client = APIClient()
        for _ in range(5):
            assert password_step(client, user_a, "wrong-password-1234").status_code == 400
        assert password_step(client, user_a).status_code == 429


# --- P2-2 ---------------------------------------------------------------------------------------
class TestSetupsExpire:
    def start(self, client):
        response = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json")
        assert response.status_code == 200, response.content
        return response.json()["secret"]

    def test_wrong_confirmation_codes_are_throttled_and_void_the_setup(self, user_a, clock):
        client = signed_in(user_a)
        secret = self.start(client)
        for attempt in range(1, settings.MFA_SETUP_MAX_ATTEMPTS + 1):
            response = client.post(CONFIRM, {"code": wrong(secret, clock)}, format="json")
            assert error_code(response) == "invalid_mfa_code", attempt
            assert failures() == attempt  # a failed sign-in each, in the durable throttle
        assert not MfaDevice.objects.filter(user=user_a).exists()  # the 5th voided it
        clock.next_step()
        late = client.post(CONFIRM, {"code": code_for(secret, clock)}, format="json")
        # The setup is gone, and the browser is locked out for a while anyway.
        assert late.status_code in (400, 429)
        assert error_code(late) in ("mfa_setup_required", "rate_limited")

    def test_after_the_fifth_wrong_code_the_setup_must_start_again(self, user_a, clock, settings):
        settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD = 50  # the setup's own limit, alone
        client = signed_in(user_a)
        secret = self.start(client)
        for _ in range(settings.MFA_SETUP_MAX_ATTEMPTS):
            client.post(CONFIRM, {"code": wrong(secret, clock)}, format="json")
        clock.next_step()
        right = client.post(CONFIRM, {"code": code_for(secret, clock)}, format="json")
        assert error_code(right) == "mfa_setup_required"
        assert self.start(client)  # a new setup works

    def test_a_setup_expires_after_ten_minutes(self, user_a, clock):
        client = signed_in(user_a)
        secret = self.start(client)
        clock.advance(settings.MFA_SETUP_TTL_S + 1)
        late = client.post(CONFIRM, {"code": code_for(secret, clock)}, format="json")
        assert late.status_code == 400
        assert error_code(late) == "mfa_setup_required"
        assert not MfaDevice.objects.filter(user=user_a).exists()

    def test_in_time_it_confirms_and_the_right_code_forgives(self, user_a, clock):
        client = signed_in(user_a)
        secret = self.start(client)
        client.post(CONFIRM, {"code": wrong(secret, clock)}, format="json")
        clock.advance(settings.MFA_SETUP_TTL_S - 60)
        done = client.post(CONFIRM, {"code": code_for(secret, clock)}, format="json")
        assert done.status_code == 200, done.content
        assert failures() == 0
        device = MfaDevice.objects.get(user=user_a)
        assert (device.pending_until, device.confirmed_at is not None) == (None, True)

    def test_housekeeping_deletes_setups_nobody_finished(self, user_a, user_b, clock):
        self.start(signed_in(user_a))
        enrol(user_b)  # confirmed: never touched
        pending = MfaDevice.objects.get(user=user_a).pending_until
        assert mfa_services.purge_expired_setups(pending - timedelta(seconds=1)) == 0
        assert mfa_services.purge_expired_setups(pending + timedelta(seconds=1)) == 1
        assert list(MfaDevice.objects.values_list("user_id", flat=True)) == [user_b.pk]

    def test_housekeeping_runs_the_purge(self, user_a):
        MfaDevice.objects.create(
            user=user_a,
            secret=mfa_services.mfa.encrypt("JBSWY3DPEHPK3PXP"),
            pending_until=mfa_services.timezone.now() - timedelta(minutes=1),
        )
        assert housekeeping()["mfa_setups_expired"] == 1


# --- P2-3 ---------------------------------------------------------------------------------------
def attacker_fails(user, times: int = 5) -> None:
    attacker = APIClient()
    for _ in range(times):
        password_step(attacker, user, "attacker-guess-1234")
    assert password_step(attacker, user).status_code == 429  # the shared budget is spent


class TestTheOwnersBrowserStaysTrusted:
    def owner(self, user):
        client = APIClient()
        assert password_step(client, user).status_code == 200  # trusted-device cookie
        return client

    def test_after_enrolling(self, user_a, clock):
        client = self.owner(user_a)
        secret = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json").json()[
            "secret"
        ]
        confirmed = client.post(CONFIRM, {"code": code_for(secret, clock)}, format="json")
        assert settings.LOGIN_DEVICE_COOKIE_NAME in confirmed.cookies
        attacker_fails(user_a)
        assert password_step(client, user_a).status_code == 202  # not 429

    def test_after_replacing_recovery_codes_and_disabling(self, user_a, clock):
        enrolment = enrol(user_a)
        client = APIClient()
        assert password_step(client, user_a).status_code == 202
        clock.next_step()
        client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
        clock.next_step()
        replaced = client.post(CODES, {"code": code_for(enrolment.secret, clock)}, format="json")
        assert replaced.status_code == 200
        attacker_fails(user_a)
        assert password_step(client, user_a).status_code == 202
        clock.next_step()
        client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
        clock.next_step()
        disabled = client.post(
            DISABLE,
            {"current_password": DEFAULT_PASSWORD, "code": code_for(enrolment.secret, clock)},
            format="json",
        )
        assert disabled.status_code == 204
        AuthThrottleEvent.objects.all().delete()
        attacker_fails(user_a)
        assert password_step(client, user_a).status_code == 200

    def test_after_a_password_change(self, user_a):
        client = self.owner(user_a)
        changed = client.post(
            PASSWORD,
            {"current_password": DEFAULT_PASSWORD, "new_password": "A-Brand-New-Passphrase-42"},
            format="json",
        )
        assert changed.status_code == 204
        attacker_fails(user_a)
        assert password_step(client, user_a, "A-Brand-New-Passphrase-42").status_code == 200


# --- P3: the owner is told -----------------------------------------------------------------------
class TestOwnerNotices:
    def test_every_change_is_emailed_to_the_owner_without_a_secret(self, admin, user_a, clock):
        client = signed_in(user_a)
        started = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json")
        secret = started.json()["secret"]
        codes = client.post(CONFIRM, {"code": code_for(secret, clock)}, format="json").json()[
            "recovery_codes"
        ]
        other = APIClient()
        password_step(other, user_a)
        other.post(LOGIN_MFA, {"recovery_code": codes[0]}, format="json")
        clock.next_step()
        new = client.post(CODES, {"code": code_for(secret, clock)}, format="json").json()[
            "recovery_codes"
        ]
        signed_in(admin).post(f"/api/v1/admin/users/{user_a.pk}/mfa/reset", format="json")
        drain_outbox()
        assert [m.to for m in mail.outbox] == [[user_a.email]] * 4
        # In any order: since the review of 73ff1e3 (M6) the outbox takes turns between the
        # people whose work it is, so the administrator's reset notice may go out first.
        subjects = sorted(m.subject for m in mail.outbox)
        assert subjects == [
            "Arkray CRM: A recovery code was used",
            "Arkray CRM: New recovery codes",
            "Arkray CRM: Two-step verification was reset",
            "Arkray CRM: Two-step verification was turned on",
        ]
        bodies = "\n".join(str(m.body) for m in mail.outbox)
        for value in [secret, *codes, *new]:
            assert value not in bodies
            assert value.replace("-", "") not in bodies

    def test_turning_it_off_is_emailed(self, user_a, clock):
        enrolment = enrol(user_a)
        signed_in(user_a).post(
            DISABLE,
            {"current_password": DEFAULT_PASSWORD, "code": code_for(enrolment.secret, clock)},
            format="json",
        )
        drain_outbox()
        assert [m.subject for m in mail.outbox] == [
            "Arkray CRM: Two-step verification was turned off"
        ]
