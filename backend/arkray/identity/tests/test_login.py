"""POST /api/v1/auth/login: sessions, enumeration resistance, CSRF, fixation, throttling."""

from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth.hashers import MD5PasswordHasher
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.identity.authentication import AUDIT_LOGIN
from arkray.identity.models import AuthThrottleEvent, ThrottleKind
from arkray.identity.throttling import AUDIT_ACTION_LOGIN_THROTTLED, login_identifier
from tests.factories import DEFAULT_PASSWORD, InvitedUserFactory, UserFactory
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login"
SESSION_COOKIE = settings.SESSION_COOKIE_NAME
DEVICE_COOKIE = settings.LOGIN_DEVICE_COOKIE_NAME


def login(client, email, password=DEFAULT_PASSWORD, **extra):
    return client.post(LOGIN, {"email": email, "password": password}, format="json", **extra)


def fail(client, email, times):
    for _ in range(times):
        assert login(client, email, "wrong-password-123").status_code == 400


class TestSuccessfulSignIn:
    def test_returns_the_viewer_and_an_http_only_session_cookie(self, api_client, user_a):
        response = login(api_client, user_a.email)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "role",
            "role_label",
            "capabilities",
            "features",
        }
        assert body["full_name"] == "Rahul Sharma"
        assert body["capabilities"] == ["ai.query", "crm.access_own"]
        cookie = response.cookies[SESSION_COOKIE]
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Lax"
        assert response["Cache-Control"].startswith("max-age=0")

    def test_admin_receives_admin_capabilities(self, api_client, admin):
        body = login(api_client, admin.email).json()
        assert body["role"] == "admin"
        assert "users.manage" in body["capabilities"]

    def test_email_is_matched_case_and_whitespace_insensitively(self, api_client, user_a):
        response = login(api_client, f"  {user_a.email.upper()} ")
        assert response.status_code == 200

    def test_the_session_is_usable_and_carries_timestamps(self, api_client, user_a):
        login(api_client, user_a.email)
        assert api_client.get("/api/v1/auth/me").json()["id"] == str(user_a.pk)

    def test_session_expiry_is_pinned_to_the_absolute_lifetime(self, api_client, user_a):
        before = timezone.now()
        response = login(api_client, user_a.email)
        session = Session.objects.get(session_key=response.cookies[SESSION_COOKIE].value)
        lifetime = timedelta(seconds=settings.SESSION_COOKIE_AGE)
        assert before + lifetime - timedelta(seconds=5) <= session.expire_date
        assert session.expire_date <= timezone.now() + lifetime

    def test_records_last_login_and_audits(self, api_client, user_a):
        login(api_client, user_a.email)
        user_a.refresh_from_db()
        assert user_a.last_login is not None
        event = AuditEvent.objects.get(action=AUDIT_LOGIN)
        assert (event.actor_id, event.target_id) == (user_a.pk, str(user_a.pk))
        assert event.request_id
        assert event.ip_address == "127.0.0.1"

    def test_marks_the_browser_as_trusted_for_this_account(self, api_client, user_a):
        cookie = login(api_client, user_a.email).cookies[DEVICE_COOKIE]
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Strict"
        # The API only (sign-in and the re-authentication of account changes), never pages.
        assert cookie["path"] == "/api/v1/"


class TestSessionFixation:
    def test_a_planted_session_key_is_replaced(self, api_client, user_a):
        planted = SessionStore()
        planted["attacker"] = True
        planted.create()
        api_client.cookies[SESSION_COOKIE] = planted.session_key

        response = login(api_client, user_a.email)

        new_key = response.cookies[SESSION_COOKIE].value
        assert new_key != planted.session_key
        assert not Session.objects.filter(session_key=planted.session_key).exists()

    def test_signing_in_again_as_the_same_user_still_rotates_the_key(self, user_a):
        client = APIClient()
        first = login(client, user_a.email).cookies[SESSION_COOKIE].value
        second = login(client, user_a.email).cookies[SESSION_COOKIE].value
        assert first != second
        assert not Session.objects.filter(session_key=first).exists()

    def test_signing_in_as_someone_else_discards_the_previous_session(self, user_a, user_b):
        client = signed_in(user_a)
        old_key = client.session.session_key
        login(client, user_b.email)
        assert client.get("/api/v1/auth/me").json()["id"] == str(user_b.pk)
        assert not Session.objects.filter(session_key=old_key).exists()

    def test_the_csrf_token_is_rotated(self, user_a):
        client = APIClient(enforce_csrf_checks=True)
        client.get("/api/v1/auth/csrf")
        before = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = login(client, user_a.email, HTTP_X_CSRFTOKEN=before)
        assert response.status_code == 200
        assert response.cookies[settings.CSRF_COOKIE_NAME].value != before


class TestFailuresRevealNothing:
    @pytest.fixture
    def accounts(self, db):
        return {
            "active": UserFactory(email="active@example.test"),
            "invited": InvitedUserFactory(email="invited@example.test"),
            "deactivated": UserFactory(email="gone@example.test", is_active=False),
        }

    def attempts(self, accounts):
        return {
            "unknown email": ("nobody@example.test", DEFAULT_PASSWORD),
            "wrong password": ("active@example.test", "not-the-password-1"),
            "invited, no password yet": ("invited@example.test", DEFAULT_PASSWORD),
            "deactivated, right password": ("gone@example.test", DEFAULT_PASSWORD),
            "malformed email": ("not-an-email", DEFAULT_PASSWORD),
        }

    def test_every_failure_gets_the_identical_response(self, accounts):
        responses = {}
        for label, (email, password) in self.attempts(accounts).items():
            response = login(APIClient(), email, password)
            responses[label] = (
                response.status_code,
                without_request_id(response),
                sorted(response.cookies.keys()),
            )
        expected = (
            400,
            {
                "error": {
                    "code": "invalid_credentials",
                    "message": "Invalid email or password.",
                    "details": None,
                }
            },
            [],
        )
        assert responses == dict.fromkeys(responses, expected)

    def test_the_password_hasher_runs_exactly_once_in_every_case(self, accounts, monkeypatch):
        """No timing oracle: unknown and password-less accounts verify a decoy hash."""
        calls = []
        original = MD5PasswordHasher.verify

        def counting_verify(self, password, encoded):
            calls.append(password)
            return original(self, password, encoded)

        monkeypatch.setattr(MD5PasswordHasher, "verify", counting_verify)
        for email, password in self.attempts(accounts).values():
            calls.clear()
            login(APIClient(), email, password)
            assert len(calls) == 1, email

    def test_failures_are_not_audited_one_by_one(self, api_client, user_a):
        fail(api_client, user_a.email, 2)
        assert not AuditEvent.objects.exists()

    def test_blank_fields_are_validation_errors(self, api_client):
        response = api_client.post(LOGIN, {"email": "", "password": ""}, format="json")
        assert response.status_code == 400
        assert set(response.json()["error"]["details"]) == {"email", "password"}


class TestCsrf:
    def test_sign_in_without_a_csrf_token_is_refused(self, csrf_client, user_a):
        response = login(csrf_client, user_a.email)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_failed"
        assert SESSION_COOKIE not in response.cookies

    def test_sign_in_with_the_token_from_the_csrf_endpoint_works(self, csrf_client, user_a):
        assert csrf_client.get("/api/v1/auth/csrf").status_code == 204
        token = csrf_client.cookies[settings.CSRF_COOKIE_NAME].value
        assert login(csrf_client, user_a.email, HTTP_X_CSRFTOKEN=token).status_code == 200

    def test_a_foreign_origin_is_refused_even_with_a_token(self, csrf_client, user_a):
        csrf_client.get("/api/v1/auth/csrf")
        token = csrf_client.cookies[settings.CSRF_COOKIE_NAME].value
        response = login(
            csrf_client, user_a.email, HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="https://evil.example"
        )
        assert response.status_code == 403


class TestThrottling:
    def test_the_account_locks_and_even_the_right_password_is_refused(self, api_client, user_a):
        fail(api_client, user_a.email, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD)
        response = login(api_client, user_a.email)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"
        assert 1 <= int(response["Retry-After"]) <= settings.LOGIN_LOCKOUT_BASE_S
        assert SESSION_COOKIE not in response.cookies

    def test_unknown_emails_are_throttled_exactly_like_real_ones(self, user_a):
        responses = []
        for email in (user_a.email, "nobody@example.test"):
            client = APIClient()
            fail(client, email, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD)
            response = login(client, email)
            responses.append((response.status_code, without_request_id(response)))
        assert responses[0] == responses[1]

    def test_a_trusted_browser_is_not_locked_out_by_an_attacker(self, user_a):
        owner = APIClient()
        assert login(owner, user_a.email).status_code == 200
        owner.post("/api/v1/auth/logout")

        attacker = APIClient()
        fail(attacker, user_a.email, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD)
        assert login(attacker, user_a.email).status_code == 429

        assert login(owner, user_a.email).status_code == 200

    def test_a_trusted_browser_has_its_own_budget(self, user_a):
        owner = APIClient()
        login(owner, user_a.email)
        fail(owner, user_a.email, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD)
        assert login(owner, user_a.email).status_code == 429

    def test_success_resets_the_failure_budget(self, api_client, user_a):
        fail(api_client, user_a.email, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD - 1)
        assert login(api_client, user_a.email).status_code == 200
        api_client.post("/api/v1/auth/logout")
        fail(api_client, user_a.email, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD - 1)
        assert login(api_client, user_a.email).status_code == 200

    def test_lockouts_expire(self, api_client, user_a):
        fail(api_client, user_a.email, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD)
        AuthThrottleEvent.objects.update(
            occurred_at=timezone.now() - timedelta(seconds=settings.LOGIN_LOCKOUT_BASE_S + 1)
        )
        assert login(api_client, user_a.email).status_code == 200

    def test_one_source_spraying_many_accounts_is_blocked(self, api_client, user_a):
        AuthThrottleEvent.objects.bulk_create(
            AuthThrottleEvent(
                kind=ThrottleKind.LOGIN_FAILURE,
                identifier_hash=login_identifier(f"victim{i}@example.test"),
                ip_address="127.0.0.1",
            )
            for i in range(settings.LOGIN_SOURCE_FAILURE_THRESHOLD)
        )
        response = login(api_client, user_a.email)
        assert response.status_code == 429
        # Another source is unaffected.
        assert login(APIClient(REMOTE_ADDR="198.51.100.7"), user_a.email).status_code == 200

    def test_crossing_the_threshold_is_audited_once_without_the_email(self, api_client, user_a):
        fail(api_client, user_a.email, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD)
        login(api_client, user_a.email)  # refused while locked: not recorded
        (event,) = AuditEvent.objects.filter(action=AUDIT_ACTION_LOGIN_THROTTLED)
        assert event.actor_id is None
        assert event.target_type == "login_identifier"
        assert user_a.email not in str(event.metadata) + event.target_id

    def test_refused_attempts_are_not_recorded(self, api_client, user_a):
        threshold = settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD
        fail(api_client, user_a.email, threshold)
        for _ in range(3):
            login(api_client, user_a.email, "still-wrong-123")
        assert AuthThrottleEvent.objects.count() == threshold
