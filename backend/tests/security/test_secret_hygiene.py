"""Secrets never leak: not into logs, audit records, outbox payloads or API responses.

Runs every account flow end to end while capturing all log records, then searches every
place a secret could leak for the passwords, one-time link secrets and cookie values that
were used.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core.logging import JsonFormatter
from arkray.core.models import OutboxEvent
from arkray.identity.models import AccountToken, User
from tests.helpers import drain_outbox, last_secret, signed_in

pytestmark = pytest.mark.django_db

INITIAL_PASSWORD = "initial-secret-passphrase-1"
RESET_PASSWORD = "reset-secret-passphrase-2"
CHANGED_PASSWORD = "changed-secret-passphrase-3"
WRONG_PASSWORD = "wrong-secret-passphrase-4"


def run_every_flow(admin) -> tuple[set[str], list[Any]]:
    """Returns (secrets that must never appear anywhere, all API responses)."""
    responses = []
    admin_client = signed_in(admin)
    responses.append(
        admin_client.post(
            "/api/v1/admin/users",
            {"first_name": "Neha", "email": "neha@example.test", "role": "sales_user"},
            format="json",
        )
    )
    drain_outbox()
    invitation = last_secret("activate")

    public = APIClient()
    responses.append(
        public.post("/api/v1/auth/invitations/verify", {"token": invitation}, format="json")
    )
    responses.append(
        public.post(
            "/api/v1/auth/invitations/accept",
            {"token": invitation, "password": INITIAL_PASSWORD},
            format="json",
        )
    )
    responses.append(
        public.post(
            "/api/v1/auth/login",
            {"email": "neha@example.test", "password": WRONG_PASSWORD},
            format="json",
        )
    )
    responses.append(
        public.post("/api/v1/auth/password-reset", {"email": "neha@example.test"}, format="json")
    )
    drain_outbox()
    reset = last_secret("reset-password")
    responses.append(
        public.post(
            "/api/v1/auth/password-reset/confirm",
            {"token": reset, "new_password": RESET_PASSWORD},
            format="json",
        )
    )

    user_client = APIClient()
    login = user_client.post(
        "/api/v1/auth/login",
        {"email": "neha@example.test", "password": RESET_PASSWORD},
        format="json",
    )
    responses.append(login)
    session_cookie = login.cookies[settings.SESSION_COOKIE_NAME].value
    device_cookie = login.cookies[settings.LOGIN_DEVICE_COOKIE_NAME].value
    csrf_cookie = login.cookies[settings.CSRF_COOKIE_NAME].value
    responses.append(user_client.get("/api/v1/auth/me"))
    responses.append(
        user_client.post(
            "/api/v1/auth/password/change",
            {"current_password": RESET_PASSWORD, "new_password": CHANGED_PASSWORD},
            format="json",
        )
    )
    responses.append(user_client.post("/api/v1/auth/logout"))
    responses.append(admin_client.get("/api/v1/admin/users"))

    secrets = {
        INITIAL_PASSWORD,
        RESET_PASSWORD,
        CHANGED_PASSWORD,
        WRONG_PASSWORD,
        invitation,
        reset,
        session_cookie,
        device_cookie,
        csrf_cookie,
    }
    return secrets, responses


@pytest.fixture
def flows(admin, caplog):
    with caplog.at_level(logging.DEBUG):
        secrets, responses = run_every_flow(admin)
    return secrets, responses, caplog.records


def leaks(secrets: set[str], text: str) -> set[str]:
    return {secret for secret in secrets if secret in text}


def test_nothing_secret_is_logged(flows):
    secrets, _, records = flows
    formatter = JsonFormatter()
    logged = "\n".join(formatter.format(record) for record in records)
    assert records  # the flows did log (access lines, outbox, identity events)
    assert not leaks(secrets, logged)


def test_nothing_secret_is_audited(flows):
    secrets, _, _ = flows
    audited = json.dumps(list(AuditEvent.objects.values()), default=str)
    assert not leaks(secrets, audited)


def test_nothing_secret_is_queued(flows):
    secrets, _, _ = flows
    queued = json.dumps(list(OutboxEvent.objects.values("payload", "last_error")), default=str)
    assert not leaks(secrets, queued)


def test_only_digests_of_link_secrets_are_stored(flows):
    secrets, _, _ = flows
    stored = json.dumps(list(AccountToken.objects.values()), default=str)
    assert not leaks(secrets, stored)
    assert AccountToken.objects.exclude(token_hash=None).count() == 2


def test_passwords_are_stored_hashed(flows):
    secrets, _, _ = flows
    user = User.objects.get(email="neha@example.test")
    assert not leaks(secrets, user.password)
    assert user.check_password(CHANGED_PASSWORD)


def test_no_response_contains_a_secret_or_security_field(flows):
    secrets, responses, _ = flows
    for response in responses:
        body = response.content.decode()
        assert not leaks(secrets, body)
        for field in ('"password"', '"token_hash"', '"session_epoch"', '"is_superuser"'):
            assert field not in body, (response.request["PATH_INFO"], field)


@pytest.mark.usefixtures("flows")
def test_emails_carry_the_link_but_never_a_password():
    for message in mail.outbox:
        assert not leaks({INITIAL_PASSWORD, RESET_PASSWORD, CHANGED_PASSWORD}, message.body)
