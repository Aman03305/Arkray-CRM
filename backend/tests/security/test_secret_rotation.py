"""Rotating DJANGO_SECRET_KEY (docs/runbooks.md#rotate-a-secret): with the previous key in
DJANGO_SECRET_KEY_FALLBACKS nobody is signed out; without it everyone is."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from tests.factories import DEFAULT_PASSWORD

pytestmark = pytest.mark.django_db

OLD_KEY = "old-production-key-" + "o" * 48
NEW_KEY = "new-production-key-" + "n" * 48


def signed_in_with_key(settings, user) -> APIClient:
    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = OLD_KEY, []
    client = APIClient()
    client.get("/api/v1/auth/csrf")
    response = client.post(
        "/api/v1/auth/login", {"email": user.email, "password": DEFAULT_PASSWORD}, format="json"
    )
    assert response.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    return client


def test_with_the_old_key_as_a_fallback_sessions_survive_the_rotation(settings, user_a):
    client = signed_in_with_key(settings, user_a)
    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = NEW_KEY, [OLD_KEY]
    assert client.get("/api/v1/auth/me").status_code == 200


def test_without_it_every_session_ends(settings, user_a):
    client = signed_in_with_key(settings, user_a)
    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = NEW_KEY, []
    assert client.get("/api/v1/auth/me").status_code in (401, 403)
