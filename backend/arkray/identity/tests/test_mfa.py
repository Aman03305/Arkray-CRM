"""Two-step verification over the API (ADR-0033, R125): enrolment, the two-step sign-in,
replay and guessing, recovery codes, step-up, the enrolment gate, support sessions,
session revocation, resets and key rotation."""

from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.management import CommandError, call_command
from django.db import connection
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core.errors import PermissionDeniedError
from arkray.identity import mfa, mfa_services
from arkray.identity.models import AuthThrottleEvent, MfaDevice, MfaRecoveryCode, ThrottleKind
from arkray.identity.permissions import needs_recent_mfa
from tests.architecture.test_authorization_baseline import api_views
from tests.authz_matrix import AUTHZ_MATRIX
from tests.factories import DEFAULT_PASSWORD, AdminFactory, UserFactory
from tests.helpers import drain_outbox, signed_in
from tests.mfa_helpers import (
    LOGIN_MFA,
    VERIFY,
    code_for,
    enrol,
    password_step,
    sign_in_with_code,
    verified_session,
)

pytestmark = pytest.mark.django_db

ME = "/api/v1/auth/me"
STATUS = "/api/v1/auth/mfa"
SETUP = "/api/v1/auth/mfa/setup"
CONFIRM = "/api/v1/auth/mfa/confirm"
CODES = "/api/v1/auth/mfa/recovery-codes"
DISABLE = "/api/v1/auth/mfa/disable"


def error_code(response):
    return response.json()["error"]["code"]


def failures() -> int:
    """Failures that still count toward a lockout. A right password (or code) forgives a
    browser's failures rather than deleting them: they still count toward the account's
    daily cap (review of 73ff1e3, L1)."""
    return AuthThrottleEvent.objects.filter(kind=ThrottleKind.LOGIN_FAILURE, forgiven=False).count()


def set_up(client, clock) -> tuple[str, list[str]]:
    """Enrol through the API; (secret, recovery codes)."""
    started = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json")
    assert started.status_code == 200, started.content
    secret = started.json()["secret"]
    confirmed = client.post(CONFIRM, {"code": code_for(secret, clock)}, format="json")
    assert confirmed.status_code == 200, confirmed.content
    return secret, confirmed.json()["recovery_codes"]


# --- enrolment ------------------------------------------------------------------------------------
class TestEnrolment:
    def test_setup_needs_the_current_password_and_returns_the_secret_once(self, user_a, clock):
        client = signed_in(user_a)
        wrong = client.post(SETUP, {"current_password": "not-it-at-all"}, format="json")
        assert wrong.status_code == 400
        assert error_code(wrong) == "validation_error"
        assert not MfaDevice.objects.exists()

        response = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"secret", "otpauth_uri", "qr"}
        assert set(body["qr"]) == {"size", "rows"}
        assert body["qr"]["size"] == len(body["qr"]["rows"])
        assert f"secret={body['secret']}" in body["otpauth_uri"]
        assert user_a.email in body["otpauth_uri"]
        # Not active until confirmed.
        device = MfaDevice.objects.get(user=user_a)
        assert device.confirmed_at is None
        assert client.get(STATUS).json()["enabled"] is False
        assert client.get(ME).json()["mfa_enabled"] is False

    def test_the_secret_is_encrypted_at_rest(self, user_a, clock):
        client = signed_in(user_a)
        secret = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json").json()[
            "secret"
        ]
        with connection.cursor() as cursor:
            cursor.execute("SELECT secret FROM identity_mfa_device WHERE user_id = %s", [user_a.pk])
            (stored,) = cursor.fetchone()
        assert stored != secret
        assert secret not in stored
        assert secret.lower() not in stored.lower()
        assert stored.startswith("gAAAAA")
        assert mfa.decrypt(stored) == secret

    def test_a_wrong_code_does_not_activate_it(self, user_a, clock):
        client = signed_in(user_a)
        secret = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json").json()[
            "secret"
        ]
        wrong = str((int(code_for(secret, clock)) + 1) % 1_000_000).zfill(6)
        response = client.post(CONFIRM, {"code": wrong}, format="json")
        assert response.status_code == 400
        assert error_code(response) == "invalid_mfa_code"
        assert response.json()["error"]["message"] == (
            "That code didn't work. Check your authenticator app and try again."
        )
        assert MfaDevice.objects.get(user=user_a).confirmed_at is None
        assert not MfaRecoveryCode.objects.exists()

    def test_confirming_activates_it_with_ten_recovery_codes(self, user_a, clock):
        client = signed_in(user_a)
        secret, codes = set_up(client, clock)
        assert len(codes) == len(set(codes)) == 10
        status = client.get(STATUS).json()
        assert status == {
            "enabled": True,
            "confirmed_at": status["confirmed_at"],
            "recovery_codes_remaining": 10,
            "required": False,
            "can_disable": True,
            "lock": None,  # R133: what unlocks it, once wrong codes locked it
        }
        assert status["confirmed_at"]
        me = client.get(ME).json()
        assert (me["mfa_enabled"], me["mfa_enrollment_required"]) == (True, False)
        # The secret is never returned again: not by the status, the viewer, or a new setup.
        for body in (json.dumps(status), json.dumps(me)):
            assert secret not in body
        again = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json")
        assert again.status_code == 409
        assert error_code(again) == "mfa_already_enabled"
        assert secret not in again.content.decode()
        assert AuditEvent.objects.filter(action="mfa.enabled", target_id=str(user_a.pk)).exists()

    def test_confirm_without_a_setup(self, user_a, clock):
        response = signed_in(user_a).post(CONFIRM, {"code": "123456"}, format="json")
        assert response.status_code == 400
        assert error_code(response) == "mfa_setup_required"

    def test_restarting_an_unconfirmed_setup_replaces_its_secret(self, user_a, clock):
        client = signed_in(user_a)
        first = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json")
        second = client.post(SETUP, {"current_password": DEFAULT_PASSWORD}, format="json")
        assert first.json()["secret"] != second.json()["secret"]
        stale = client.post(
            CONFIRM, {"code": code_for(first.json()["secret"], clock)}, format="json"
        )
        assert stale.status_code == 400
        assert MfaDevice.objects.filter(user=user_a).count() == 1

    def test_confirming_ends_every_other_session(self, user_a, clock):
        other = signed_in(user_a)
        client = signed_in(user_a)
        set_up(client, clock)
        assert other.get(ME).status_code == 401
        assert client.get(ME).status_code == 200


# --- the two-step sign-in -------------------------------------------------------------------------
class TestTwoStepSignIn:
    def test_the_password_gives_a_challenge_and_the_code_a_session(self, user_a, clock):
        enrolment = enrol(user_a)
        client = APIClient()
        first = password_step(client, user_a)
        assert first.status_code == 202
        assert first.json() == {"mfa_required": True, "expires_in": 300}
        assert settings.LOGIN_DEVICE_COOKIE_NAME not in first.cookies
        assert client.get(ME).status_code == 401  # not signed in yet
        # The pending challenge is all the session holds.
        session = Session.objects.get(session_key=first.cookies[settings.SESSION_COOKIE_NAME].value)
        assert set(session.get_decoded()) <= {mfa_services.SESSION_CHALLENGE, "_session_expiry"}

        clock.next_step()
        second = client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
        assert second.status_code == 200, second.content
        assert second.json()["id"] == str(user_a.pk)
        assert second.json()["mfa_enabled"] is True
        assert settings.LOGIN_DEVICE_COOKIE_NAME in second.cookies
        assert client.get(ME).status_code == 200
        login = AuditEvent.objects.get(action="auth.login")
        assert login.metadata == {"trusted_device": False, "factor": "totp"}

    def test_without_a_second_factor_sign_in_is_unchanged(self, user_a):
        response = password_step(APIClient(), user_a)
        assert response.status_code == 200
        assert AuditEvent.objects.get(action="auth.login").metadata["factor"] == "password_only"

    def test_a_planted_session_never_carries_over(self, user_a, clock):
        from django.contrib.sessions.backends.db import SessionStore

        enrol(user_a)
        planted = SessionStore()
        planted["attacker"] = True
        planted.create()
        client = APIClient()
        client.cookies[settings.SESSION_COOKIE_NAME] = planted.session_key
        first = password_step(client, user_a)
        assert first.cookies[settings.SESSION_COOKIE_NAME].value != planted.session_key
        assert not Session.objects.filter(session_key=planted.session_key).exists()

    def test_a_wrong_code_is_refused_and_audited(self, user_a, clock):
        enrolment = enrol(user_a)
        client = APIClient()
        password_step(client, user_a)
        wrong = str((int(code_for(enrolment.secret, clock)) + 1) % 1_000_000).zfill(6)
        response = client.post(LOGIN_MFA, {"code": wrong}, format="json")
        assert response.status_code == 400
        assert error_code(response) == "invalid_mfa_code"
        assert client.get(ME).status_code == 401
        event = AuditEvent.objects.get(action="auth.login_failed")
        assert event.metadata == {"reason": "wrong_code", "trusted_device": False}
        assert wrong not in json.dumps(event.metadata)

    def test_five_wrong_codes_void_the_challenge(self, user_a, clock, settings):
        settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD = 50  # the challenge's own limit, alone
        enrolment = enrol(user_a)
        client = APIClient()
        password_step(client, user_a)
        for _ in range(5):
            assert client.post(LOGIN_MFA, {"code": "000000"}, format="json").status_code == 400
        clock.next_step()
        right = client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
        assert right.status_code == 400
        assert error_code(right) == "mfa_challenge_expired"
        assert right.json()["error"]["message"] == (
            "Your sign-in timed out. Enter your email and password again."
        )

    def test_wrong_codes_count_in_the_durable_throttle(self, user_a, clock):
        """Five wrong codes (across challenges too) lock this browser out of the account
        exactly like five wrong passwords."""
        enrol(user_a)
        client = APIClient()
        for _ in range(2):
            password_step(client, user_a)
            for _ in range(2):
                client.post(LOGIN_MFA, {"code": "000000"}, format="json")
        password_step(client, user_a)
        assert client.post(LOGIN_MFA, {"code": "000000"}, format="json").status_code == 400
        assert failures() == 5
        locked = password_step(client, user_a)
        assert locked.status_code == 429
        assert error_code(locked) == "rate_limited"
        assert locked["Retry-After"]

    def test_the_password_alone_forgives_no_failure(self, user_a, clock):
        enrolment = enrol(user_a)
        client = APIClient()
        for _ in range(3):
            assert password_step(client, user_a, "wrong-password-1234").status_code == 400
        assert failures() == 3
        assert password_step(client, user_a).status_code == 202
        assert failures() == 3  # its own reservation withdrawn, the others stand
        clock.next_step()
        done = client.post(LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json")
        assert done.status_code == 200
        assert failures() == 0

    def test_the_challenge_expires(self, user_a, clock):
        enrolment = enrol(user_a)
        client = APIClient()
        password_step(client, user_a)
        clock.advance(301)
        response = client.post(
            LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json"
        )
        assert error_code(response) == "mfa_challenge_expired"

    def test_no_challenge_at_all(self, api_client):
        response = api_client.post(LOGIN_MFA, {"code": "123456"}, format="json")
        assert response.status_code == 400
        assert error_code(response) == "mfa_challenge_expired"

    @pytest.mark.parametrize("change", ["password", "sign_out_everywhere", "deactivated"])
    def test_a_change_between_the_steps_voids_the_challenge(self, user_a, clock, change):
        enrolment = enrol(user_a)
        client = APIClient()
        password_step(client, user_a)
        if change == "password":
            user_a.set_password("Another-Long-Passphrase-9")
            user_a.save()
        elif change == "sign_out_everywhere":
            user_a.session_epoch += 1
            user_a.save()
        else:
            from arkray.identity import services

            services.deactivate_user(actor_id=AdminFactory().pk, user_id=user_a.pk)
        clock.next_step()
        response = client.post(
            LOGIN_MFA, {"code": code_for(enrolment.secret, clock)}, format="json"
        )
        assert error_code(response) == "mfa_challenge_expired"

    @pytest.mark.parametrize(
        "body", [{}, {"code": "123456", "recovery_code": "AAAA-AAAA-AAAA-AAAA"}]
    )
    def test_exactly_one_factor_is_sent(self, user_a, clock, body):
        enrol(user_a)
        client = APIClient()
        password_step(client, user_a)
        response = client.post(LOGIN_MFA, body, format="json")
        assert response.status_code == 400
        assert error_code(response) == "validation_error"


class TestReplay:
    def test_the_same_code_never_signs_in_twice(self, user_a, clock):
        enrolment = enrol(user_a)
        first = APIClient()
        assert sign_in_with_code(first, user_a, enrolment.secret, clock).status_code == 200
        code = code_for(enrolment.secret, clock)
        second = APIClient()
        assert password_step(second, user_a).status_code == 202
        replayed = second.post(LOGIN_MFA, {"code": code}, format="json")
        assert replayed.status_code == 400
        assert error_code(replayed) == "invalid_mfa_code"

    def test_two_servers_racing_with_one_code_let_one_in(self, user_a, clock):
        """Two browsers (any two servers: the step is stored on the device's row, checked
        under its lock) both at the second step, the same code: one succeeds."""
        enrolment = enrol(user_a)
        a, b = APIClient(), APIClient()
        password_step(a, user_a)
        password_step(b, user_a)
        clock.next_step()
        code = code_for(enrolment.secret, clock)
        results = [c.post(LOGIN_MFA, {"code": code}, format="json").status_code for c in (a, b)]
        assert sorted(results) == [200, 400]

    def test_an_earlier_code_after_a_later_one_is_refused(self, user_a, clock):
        enrolment = enrol(user_a)
        early = code_for(enrolment.secret, clock, steps=-1)
        client = APIClient()
        password_step(client, user_a)
        later = code_for(enrolment.secret, clock, steps=1)
        assert client.post(LOGIN_MFA, {"code": later}, format="json").status_code == 200
        other = APIClient()
        password_step(other, user_a)
        assert other.post(LOGIN_MFA, {"code": early}, format="json").status_code == 400


class TestRecoveryCodes:
    def test_a_recovery_code_signs_in_once_and_is_audited(self, user_a, clock):
        enrolment = enrol(user_a)
        code = enrolment.recovery_codes[0]
        client = APIClient()
        password_step(client, user_a)
        typed = code.lower().replace("-", " ")
        response = client.post(LOGIN_MFA, {"recovery_code": typed}, format="json")
        assert response.status_code == 200, response.content
        used = AuditEvent.objects.get(action="mfa.recovery_code_used")
        assert used.metadata == {"remaining": 9}
        assert AuditEvent.objects.get(action="auth.login").metadata["factor"] == "recovery_code"
        assert signed_in(user_a).get(STATUS).json()["recovery_codes_remaining"] == 9

        again = APIClient()
        password_step(again, user_a)
        refused = again.post(LOGIN_MFA, {"recovery_code": code}, format="json")
        assert refused.status_code == 400
        assert error_code(refused) == "invalid_mfa_code"
        failed = AuditEvent.objects.get(action="auth.login_failed")
        assert failed.metadata["reason"] == "wrong_recovery_code"

    def test_replacing_them_voids_the_old_ones_and_ends_other_sessions(self, user_a, clock):
        enrolment = enrol(user_a)
        other = signed_in(user_a)
        client = signed_in(user_a)
        clock.next_step()
        response = client.post(CODES, {"code": code_for(enrolment.secret, clock)}, format="json")
        assert response.status_code == 200, response.content
        new = response.json()["recovery_codes"]
        assert len(new) == 10
        assert not set(new) & set(enrolment.recovery_codes)
        assert other.get(ME).status_code == 401
        assert client.get(ME).status_code == 200
        assert AuditEvent.objects.filter(action="mfa.recovery_codes_replaced").count() == 1
        stale = APIClient()
        password_step(stale, user_a)
        old = stale.post(LOGIN_MFA, {"recovery_code": enrolment.recovery_codes[1]}, format="json")
        assert old.status_code == 400
        fresh = stale.post(LOGIN_MFA, {"recovery_code": new[1]}, format="json")
        assert fresh.status_code == 200

    def test_replacing_needs_a_right_code_and_is_throttled(self, user_a, clock):
        enrol(user_a)
        client = signed_in(user_a)
        response = client.post(CODES, {"code": "000000"}, format="json")
        assert response.status_code == 400
        assert error_code(response) == "invalid_mfa_code"
        assert failures() == 1
        assert MfaRecoveryCode.objects.filter(user=user_a).count() == 10


# --- step-up --------------------------------------------------------------------------------------
STEP_UP_ROUTES = sorted(
    (route, method)
    for route, rule in AUTHZ_MATRIX.items()
    for method in rule.methods
    if needs_recent_mfa(getattr(api_views()[route], "view_class", None), method)
)


def test_the_step_up_routes_are_exactly_these():
    """ADR-0033: user administration changes, support sessions, privacy exports and
    pseudonymisation (erasure and legal holds are operator commands, with no API)."""
    users = "api/v1/admin/users"
    member = "api/v1/admin/users/<uuid:user_id>"
    assert (
        sorted(
            [
                (users, "POST"),
                (member, "PATCH"),
                (f"{member}/change-email", "POST"),
                (f"{member}/deactivate", "POST"),
                (f"{member}/activate", "POST"),
                (f"{member}/resend-invitation", "POST"),
                (f"{member}/set-password", "POST"),
                (f"{member}/mfa/reset", "POST"),
                # Lifting a sign-in lock (review of 73ff1e3, L1).
                (f"{member}/clear-sign-in-lock", "POST"),
                # Correcting a closed deal's figures (review of 73ff1e3, M2; pipeline).
                (
                    "api/v1/workspaces/<str:workspace>/opportunities/<uuid:opportunity_id>/correct",
                    "POST",
                ),
                ("api/v1/admin/support-sessions", "POST"),
                ("api/v1/admin/privacy/exports", "POST"),
                ("api/v1/admin/privacy/users/<uuid:user_id>/pseudonymise", "POST"),
            ]
        )
        == STEP_UP_ROUTES
    )


def call(client, method, route, target):
    """The route for `target`; any other id is one nobody has (the step-up answers before
    anything is looked up) and the workspace the caller's own."""
    path = "/" + route.replace("<uuid:user_id>", str(target.pk))
    path = re.sub(r"<uuid:[a-z_]+>", lambda _: str(uuid4()), path).replace("<str:workspace>", "me")
    return getattr(client, method.lower())(path, {}, format="json")


class TestStepUp:
    @pytest.mark.parametrize(("route", "method"), STEP_UP_ROUTES)
    def test_needs_a_recent_code_from_a_user_with_a_second_factor(
        self, admin, user_b, clock, route, method
    ):
        enrolment = enrol(admin)
        client = signed_in(admin)
        stale = call(client, method, route, user_b)  # never verified in this session
        assert stale.status_code == 403
        assert error_code(stale) == "mfa_required"
        assert stale.json()["error"]["message"] == (
            "Enter a code from your authenticator app to continue."
        )

        verified_session(client, clock, age=settings.MFA_STEP_UP_TTL_S + 1)  # expired
        assert error_code(call(client, method, route, user_b)) == "mfa_required"

        clock.next_step()
        renewed = client.post(VERIFY, {"code": code_for(enrolment.secret, clock)}, format="json")
        assert renewed.status_code == 204
        response = call(client, method, route, user_b)
        assert response.status_code != 403 or error_code(response) != "mfa_required", (
            response.content
        )

        clock.advance(settings.MFA_STEP_UP_TTL_S)
        assert error_code(call(client, method, route, user_b)) == "mfa_required"

    @pytest.mark.parametrize(("route", "method"), STEP_UP_ROUTES)
    def test_no_second_factor_no_step_up(self, admin, user_b, route, method):
        response = call(signed_in(admin), method, route, user_b)
        assert not (response.status_code == 403 and error_code(response) == "mfa_required")

    def test_signing_in_with_a_code_counts_as_verified(self, admin, user_b, clock):
        enrolment = enrol(admin)
        client = APIClient()
        assert sign_in_with_code(client, admin, enrolment.secret, clock).status_code == 200
        response = client.post(f"/api/v1/admin/users/{user_b.pk}/deactivate", format="json")
        assert response.status_code == 200, response.content

    def test_a_wrong_step_up_code_is_refused_and_counted(self, admin, clock):
        enrol(admin)
        client = signed_in(admin)
        response = client.post(VERIFY, {"code": "000000"}, format="json")
        assert response.status_code == 400
        assert error_code(response) == "invalid_mfa_code"
        assert failures() == 1

    def test_a_recovery_code_also_steps_up(self, admin, user_b, clock):
        enrolment = enrol(admin)
        client = signed_in(admin)
        done = client.post(VERIFY, {"recovery_code": enrolment.recovery_codes[0]}, format="json")
        assert done.status_code == 204
        assert AuditEvent.objects.filter(action="mfa.recovery_code_used").count() == 1

    def test_step_up_without_a_device(self, user_a):
        response = signed_in(user_a).post(VERIFY, {"code": "123456"}, format="json")
        assert error_code(response) == "mfa_setup_required"

    def test_reading_needs_no_step_up(self, admin, clock):
        enrol(admin)
        client = signed_in(admin)
        assert client.get("/api/v1/admin/users").status_code == 200
        assert client.get("/api/v1/admin/security-events").status_code == 200


# --- the enrolment gate ---------------------------------------------------------------------------
class TestEnrolmentGate:
    @pytest.fixture(autouse=True)
    def _required(self, settings):
        settings.MFA_REQUIRED_FOR_ADMINS = True

    def test_an_administrator_without_one_reaches_only_the_setup(self, admin, clock):
        client = signed_in(admin)
        me = client.get(ME)
        assert me.status_code == 200
        assert (me.json()["mfa_enabled"], me.json()["mfa_enrollment_required"]) == (False, True)
        for path in (
            "/api/v1/admin/users",
            "/api/v1/workspaces/me",
            "/api/v1/admin/security-events",
        ):
            response = client.get(path)
            assert response.status_code == 403, path
            assert error_code(response) == "mfa_enrollment_required"
            assert (
                response.json()["error"]["message"] == "Set up two-step verification to continue."
            )
        assert client.get(STATUS).json()["required"] is True
        set_up(client, clock)
        assert client.get("/api/v1/admin/users").status_code == 200
        assert client.get(ME).json()["mfa_enrollment_required"] is False

    def test_the_password_change_stays_reachable(self, admin):
        response = signed_in(admin).post(
            "/api/v1/auth/password/change",
            {"current_password": DEFAULT_PASSWORD, "new_password": "A-Brand-New-Passphrase-42"},
            format="json",
        )
        assert response.status_code == 204

    def test_ordinary_users_are_not_asked(self, user_a):
        client = signed_in(user_a)
        assert client.get("/api/v1/workspaces/me").status_code == 200
        assert client.get(ME).json()["mfa_enrollment_required"] is False

    def test_an_administrator_cant_turn_it_off(self, admin, clock):
        enrolment = enrol(admin)
        client = signed_in(admin)
        clock.next_step()
        response = client.post(
            DISABLE,
            {"current_password": DEFAULT_PASSWORD, "code": code_for(enrolment.secret, clock)},
            format="json",
        )
        assert response.status_code == 422
        assert error_code(response) == "business_rule_violation"
        assert MfaDevice.objects.filter(user=admin).exists()
        assert failures() == 0  # refused before anything was checked

    def test_sign_in_of_an_unenrolled_administrator_says_so(self, admin):
        body = password_step(APIClient(), admin).json()
        assert body["mfa_enrollment_required"] is True


# --- disabling ------------------------------------------------------------------------------------
class TestDisable:
    def test_needs_the_password_and_a_code_and_ends_other_sessions(self, user_a, clock):
        enrolment = enrol(user_a)
        other = signed_in(user_a)
        client = signed_in(user_a)
        wrong = client.post(
            DISABLE, {"current_password": DEFAULT_PASSWORD, "code": "000000"}, format="json"
        )
        assert error_code(wrong) == "invalid_mfa_code"
        bad_password = client.post(
            DISABLE,
            {"current_password": "nope-nope-nope", "code": code_for(enrolment.secret, clock)},
            format="json",
        )
        assert bad_password.status_code == 400
        assert MfaDevice.objects.filter(user=user_a).exists()

        clock.next_step()
        done = client.post(
            DISABLE,
            {"current_password": DEFAULT_PASSWORD, "code": code_for(enrolment.secret, clock)},
            format="json",
        )
        assert done.status_code == 204
        assert not MfaDevice.objects.filter(user=user_a).exists()
        assert not MfaRecoveryCode.objects.filter(user=user_a).exists()
        assert other.get(ME).status_code == 401
        assert client.get(ME).json()["mfa_enabled"] is False
        assert AuditEvent.objects.filter(action="mfa.disabled").count() == 1
        assert password_step(APIClient(), user_a).status_code == 200

    def test_a_recovery_code_can_stand_in_for_a_lost_device(self, user_a, clock):
        enrolment = enrol(user_a)
        response = signed_in(user_a).post(
            DISABLE,
            {"current_password": DEFAULT_PASSWORD, "code": enrolment.recovery_codes[2]},
            format="json",
        )
        assert response.status_code == 204


# --- support sessions -----------------------------------------------------------------------------
class TestSupportSessions:
    @pytest.fixture
    def in_support(self, admin, user_a, clock):
        enrolment = enrol(admin)
        client = APIClient()
        assert sign_in_with_code(client, admin, enrolment.secret, clock).status_code == 200
        started = client.post(
            "/api/v1/admin/support-sessions", {"user": str(user_a.pk)}, format="json"
        )
        assert started.status_code == 201, started.content
        return client, enrolment

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", STATUS),
            ("post", SETUP),
            ("post", CONFIRM),
            ("post", CODES),
            ("post", DISABLE),
            ("post", VERIFY),
            ("post", LOGIN_MFA),
        ],
    )
    def test_every_two_step_endpoint_is_refused_inside_one(self, in_support, clock, method, path):
        client, enrolment = in_support
        clock.next_step()
        body = {"code": code_for(enrolment.secret, clock), "current_password": DEFAULT_PASSWORD}
        if path in (STATUS,):
            body = {}
        elif path in (VERIFY, LOGIN_MFA, CONFIRM, CODES):
            body = {"code": body["code"]}
        elif path == SETUP:
            body = {"current_password": DEFAULT_PASSWORD}
        response = getattr(client, method)(path, body, format="json")
        assert response.status_code == 403, response.content
        assert error_code(response) == "support_session_active"

    def test_a_support_session_needs_a_recent_code_to_start(self, admin, user_a, clock):
        enrol(admin)
        response = signed_in(admin).post(
            "/api/v1/admin/support-sessions", {"user": str(user_a.pk)}, format="json"
        )
        assert error_code(response) == "mfa_required"


# --- resets ---------------------------------------------------------------------------------------
class TestAdministratorReset:
    def reset(self, client, user):
        return client.post(f"/api/v1/admin/users/{user.pk}/mfa/reset", format="json")

    def test_an_ordinary_users_device_is_reset_and_their_sessions_end(self, admin, user_a, clock):
        enrol(user_a)
        theirs = signed_in(user_a)
        response = self.reset(signed_in(admin), user_a)
        assert response.status_code == 200, response.content
        assert response.json()["mfa_enabled"] is False
        assert not MfaDevice.objects.filter(user=user_a).exists()
        assert not MfaRecoveryCode.objects.filter(user=user_a).exists()
        assert theirs.get(ME).status_code == 401
        event = AuditEvent.objects.get(action="mfa.reset")
        assert (event.actor_id, event.metadata) == (admin.pk, {"by": "administrator"})
        assert password_step(APIClient(), user_a).status_code == 200

    def test_the_admin_list_shows_who_has_one(self, admin, user_a):
        enrol(user_a)
        rows = {r["id"]: r for r in signed_in(admin).get("/api/v1/admin/users").json()["results"]}
        assert rows[str(user_a.pk)]["mfa_enabled"] is True
        assert rows[str(admin.pk)]["mfa_enabled"] is False

    def test_never_another_administrators_nor_ones_own(self, admin, clock):
        other = AdminFactory()
        enrol(other)
        own = enrol(admin)
        client = signed_in(admin)
        verified_session(client, clock)
        for target in (other, admin):
            response = self.reset(client, target)
            assert response.status_code == 422, target
            assert response.json()["error"]["message"] == mfa_services.ADMINISTRATOR_MFA_REFUSED
            assert MfaDevice.objects.filter(user=target).exists()
        assert own.secret
        assert not AuditEvent.objects.filter(action="mfa.reset").exists()

    def test_no_device_is_a_no_op(self, admin, user_a):
        response = self.reset(signed_in(admin), user_a)
        assert response.status_code == 200
        assert not AuditEvent.objects.filter(action="mfa.reset").exists()


class TestOperatorCommands:
    def test_reset_mfa_removes_an_administrators_factor_with_an_audit_trail(self, admin, clock):
        target = AdminFactory(email="locked.out@example.test")
        enrol(target)
        theirs = signed_in(target)
        call_command(
            "reset_mfa",
            "--email",
            target.email.upper(),
            "--by",
            admin.email,
            "--reason",
            "Lost phone, ticket 42",
        )
        assert not MfaDevice.objects.filter(user=target).exists()
        assert theirs.get(ME).status_code == 401
        event = AuditEvent.objects.get(action="mfa.reset")
        assert event.actor_id == admin.pk  # the operator named by --by (review P3)
        assert event.metadata == {"by": "operator"}
        assert event.detail.values == {"reason": "Lost phone, ticket 42"}
        drain_outbox()
        assert [m.to for m in mail.outbox] == [["locked.out@example.test"]]

    def test_the_sole_administrator_can_be_reset_on_their_own_behalf(self, admin):
        enrol(admin)
        call_command(
            "reset_mfa", "--email", admin.email, "--by", admin.email, "--reason", "ticket 7"
        )
        assert not MfaDevice.objects.filter(user=admin).exists()

    def test_reset_mfa_refuses_unknown_accounts_blank_reasons_and_bad_operators(
        self, admin, user_a
    ):
        enrol(user_a)
        by = ["--by", admin.email]
        with pytest.raises(CommandError):
            call_command("reset_mfa", "--email", "nobody@example.test", "--reason", "x", *by)
        with pytest.raises(CommandError):
            call_command("reset_mfa", "--email", admin.email, "--reason", "   ", *by)
        with pytest.raises(CommandError):  # --by is required
            call_command("reset_mfa", "--email", user_a.email, "--reason", "x")
        for operator in (user_a.email, "nobody@example.test"):  # not an administrator
            with pytest.raises(CommandError, match="--by"):
                call_command(
                    "reset_mfa", "--email", user_a.email, "--reason", "x", "--by", operator
                )
        assert MfaDevice.objects.filter(user=user_a).exists()

    def test_only_a_super_administrator_is_the_operators_principal(self, team_admin, team_member):
        """ADR-0035: a team administrator administers only their team's users, and never an
        administrator's second factor; the command (and the service under it) name a super
        administrator."""
        enrol(team_member)
        with pytest.raises(CommandError, match="--by must name an active super admin"):
            call_command(
                "reset_mfa", "--email", team_member.email, "--reason", "x", "--by", team_admin.email
            )
        with pytest.raises(PermissionDeniedError):
            mfa_services.operator_reset(team_member.email, "x", operator_id=team_admin.pk)
        assert MfaDevice.objects.filter(user=team_member).exists()

    def test_mfa_reencrypt_moves_every_secret_to_the_current_key(self, user_a, clock, settings):
        old_key = "old-key-" + "q1w2e3r4" * 8
        settings.SECRET_KEY = old_key
        settings.SECRET_KEY_FALLBACKS = []
        enrolment = enrol(user_a)
        settings.SECRET_KEY = "new-key-" + "t5y6u7i8" * 8
        settings.SECRET_KEY_FALLBACKS = [old_key]
        stored = MfaDevice.objects.get(user=user_a).secret
        assert not mfa.under_current_key(stored)
        call_command("mfa_reencrypt")
        assert mfa.under_current_key(MfaDevice.objects.get(user=user_a).secret)

        settings.SECRET_KEY_FALLBACKS = []  # the old key retired: sign-in still works
        assert sign_in_with_code(APIClient(), user_a, enrolment.secret, clock).status_code == 200

    def test_mfa_reencrypt_reports_secrets_under_no_key(self, user_a, settings):
        enrol(user_a)
        settings.SECRET_KEY = "unrelated-" + "z9x8c7v6" * 8
        settings.SECRET_KEY_FALLBACKS = []
        with pytest.raises(CommandError, match="1 secret"):
            call_command("mfa_reencrypt")

    def test_a_verification_re_encrypts_under_the_current_key(self, user_a, clock, settings):
        old_key = "old-key-" + "q1w2e3r4" * 8
        settings.SECRET_KEY = old_key
        enrolment = enrol(user_a)
        settings.SECRET_KEY = "new-key-" + "t5y6u7i8" * 8
        settings.SECRET_KEY_FALLBACKS = [old_key]
        assert sign_in_with_code(APIClient(), user_a, enrolment.secret, clock).status_code == 200
        assert mfa.under_current_key(MfaDevice.objects.get(user=user_a).secret)


# --- what the audit trail and the security events show --------------------------------------------
def test_no_secret_code_or_hash_reaches_the_audit_trail(admin, user_a, clock):
    enrolment = enrol(user_a)
    client = APIClient()
    sign_in_with_code(client, user_a, enrolment.secret, clock)
    other = APIClient()
    password_step(other, user_a)
    other.post(LOGIN_MFA, {"recovery_code": enrolment.recovery_codes[0]}, format="json")
    clock.next_step()
    replaced = client.post(CODES, {"code": code_for(enrolment.secret, clock)}, format="json")
    new_codes = replaced.json()["recovery_codes"]
    audited = json.dumps(
        [
            [e.action, e.metadata, getattr(getattr(e, "detail", None), "values", None)]
            for e in AuditEvent.objects.select_related("detail")
        ],
        default=str,
    )
    hashes = list(MfaRecoveryCode.objects.values_list("code_hash", flat=True))
    for secret in [enrolment.secret, *enrolment.recovery_codes, *new_codes, *hashes]:
        assert secret not in audited
        assert secret.replace("-", "") not in audited


def test_security_events_show_failed_sign_ins_and_two_step_changes(admin, user_a, clock):
    enrol(user_a)
    password_step(APIClient(), user_a, "wrong-password-1234")
    signed_in(admin).post(f"/api/v1/admin/users/{user_a.pk}/mfa/reset", format="json")
    events = signed_in(admin).get("/api/v1/admin/security-events").json()["results"]
    by_action = {e["action"]: e for e in events}
    assert by_action["auth.login_failed"]["details"] == {"reason": "wrong_password"}
    assert by_action["auth.login_failed"]["user"]["id"] == str(user_a.pk)
    assert by_action["auth.login_failed"]["actor"] is None
    assert by_action["mfa.reset"]["details"] == {"by": "administrator"}


def test_unknown_users_nothing_audited_and_same_answer(api_client):
    UserFactory(email="known@example.test")
    unknown = password_step(api_client, UserFactory.build(email="ghost@example.test"))
    assert unknown.status_code == 400
    assert not AuditEvent.objects.exists()
