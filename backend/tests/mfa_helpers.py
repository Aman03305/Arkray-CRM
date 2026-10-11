"""Two-step verification helpers for tests (ADR-0033): a controllable clock for the codes,
enrolling a user directly, and signing in with both steps."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.identity import mfa, mfa_services
from arkray.identity.models import MfaDevice, MfaRecoveryCode, User
from tests.factories import DEFAULT_PASSWORD

LOGIN = "/api/v1/auth/login"
LOGIN_MFA = "/api/v1/auth/login/mfa"
VERIFY = "/api/v1/auth/mfa/verify"
START = 1_800_000_000.0  # a fixed moment, well inside the 30-second grid


@dataclass
class Clock:
    """The time mfa_services sees (codes, challenge and step-up ages)."""

    now: float = START

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def next_step(self) -> None:
        """Codes are accepted once per 30-second step: move to the next one."""
        self.advance(mfa.STEP_S)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    current = Clock()
    monkeypatch.setattr(mfa_services, "_now", lambda: current.now)
    return current


def code_for(secret: str, clock: Clock, steps: int = 0) -> str:
    return mfa.totp(mfa.secret_bytes(secret), clock.now + mfa.STEP_S * steps)


@dataclass
class Enrolment:
    secret: str
    recovery_codes: list[str] = field(default_factory=list)


def enrol(user: User) -> Enrolment:
    """A confirmed device and ten recovery codes, as if the user had set them up."""
    secret = mfa.generate_secret()
    MfaDevice.objects.create(user=user, secret=mfa.encrypt(secret), confirmed_at=timezone.now())
    codes = mfa.generate_recovery_codes()
    MfaRecoveryCode.objects.bulk_create(
        [MfaRecoveryCode(user=user, code_hash=h) for h in mfa.hash_recovery_codes(codes)]
    )
    user.__dict__.pop("_arkray_mfa_confirmed", None)
    return Enrolment(secret=secret, recovery_codes=codes)


def password_step(client: APIClient, user: User, password: str = DEFAULT_PASSWORD):
    return client.post(LOGIN, {"email": user.email, "password": password}, format="json")


def sign_in_with_code(
    client: APIClient, user: User, secret: str, clock: Clock, password: str = DEFAULT_PASSWORD
):
    first = password_step(client, user, password)
    assert first.status_code == 202, first.content
    clock.next_step()
    return client.post(LOGIN_MFA, {"code": code_for(secret, clock)}, format="json")


def verified_session(client: APIClient, clock: Clock, *, age: float = 0.0) -> None:
    """Record a step-up verification `age` seconds ago in the client's session."""
    session = client.session
    session[mfa_services.SESSION_VERIFIED_AT] = clock.now - age
    session.save()
