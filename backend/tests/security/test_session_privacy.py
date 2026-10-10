"""Sessions and credentials in the browser (privacy remediation P2-12;
docs/authorization.md#sessions).

- The session cookie ends with the browser (no Expires/Max-Age), while the server keeps the
  idle and absolute limits and the row's pinned expiry.
- The login-device cookie, kept at sign-out on purpose (anti-lockout), carries nothing
  readable: keyed hashes and a random id, never the email.
- No API ever returns a password or its hash; administrators can't take over another
  administrator (unchanged; tests/security/test_admin_takeover.py has the full matrix).
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.utils import timezone

from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

PASSWORD = "Correct-Horse-Battery-77"


@pytest.fixture
def account(user_a):
    user_a.set_password(PASSWORD)
    user_a.save()
    return user_a


def login(client, email):
    return client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")


def test_the_session_cookie_ends_with_the_browser(api_client, account):
    response = login(api_client, account.email)
    assert response.status_code == 200
    cookie = response.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie["max-age"] == ""
    assert cookie["expires"] == ""
    assert cookie["httponly"]
    # The server still pins the row's expiry to sign-in + SESSION_COOKIE_AGE.
    row = Session.objects.get(session_key=cookie.value)
    limit = timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE)
    assert row.expire_date <= limit


def test_the_login_device_cookie_holds_nothing_readable(api_client, account):
    response = login(api_client, account.email)
    raw = response.cookies[settings.LOGIN_DEVICE_COOKIE_NAME].value
    payload = raw.split(":")[0]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    data = json.loads(decoded)
    assert set(data) == {"i", "d", "t"}
    text = json.dumps(data).lower()
    assert account.email.lower() not in text
    assert account.first_name.lower() not in text
    assert all(len(str(v)) in (32, 64) for v in data.values())  # hashes and a random id


def test_sign_out_ends_the_session(api_client, account):
    login(api_client, account.email)
    assert api_client.get("/api/v1/auth/me").status_code == 200
    api_client.post("/api/v1/auth/logout")
    assert api_client.get("/api/v1/auth/me").status_code == 401


def test_no_user_api_returns_a_password_or_hash(admin, account):
    body = json.dumps(signed_in(admin).get(f"/api/v1/admin/users/{account.pk}").json())
    assert 'password"' not in body
    assert account.password not in body
