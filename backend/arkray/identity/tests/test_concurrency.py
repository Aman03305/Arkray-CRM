"""Races, run for real: each call in its own thread with its own database connection."""

import pytest
from django.conf import settings
from django.test import RequestFactory

from arkray.audit.models import AuditEvent
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    PermissionDeniedError,
    RateLimitedError,
)
from arkray.identity import services, throttling
from arkray.identity.authentication import InvalidCredentialsError, sign_in
from arkray.identity.models import (
    AccountToken,
    AuthThrottleEvent,
    Role,
    TokenPurpose,
    TokenStatus,
    User,
    UserStatus,
)
from arkray.identity.services import AUDIT_USER_ACTIVATED, AUDIT_USER_CREATED, InvalidTokenError
from tests.factories import AdminFactory, UserFactory
from tests.helpers import drain_outbox, last_secret, run_concurrently

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORDS = ("first-racer-passphrase-1", "second-racer-passphrase-2")


def outcomes(results, success_type):
    successes = [r for r in results if isinstance(r, success_type)]
    failures = [r for r in results if not isinstance(r, success_type)]
    return successes, failures


def invited_with_link(admin, email="racer@example.test"):
    user = services.create_user(
        actor_id=admin.pk, email=email, first_name="Racer", last_name="", role="sales_user"
    )
    drain_outbox()
    return user, last_secret("activate")


def test_two_admins_creating_the_same_email_at_once():
    first, second = AdminFactory(), AdminFactory()
    results = run_concurrently(
        *(
            lambda actor=actor, email=email: services.create_user(
                actor_id=actor.pk, email=email, first_name="Dup", last_name="", role="sales_user"
            )
            for actor, email in [(first, "dup@example.test"), (second, "DUP@example.test")]
        )
    )
    successes, failures = outcomes(results, User)
    assert len(successes) == 1
    assert [type(f) for f in failures] == [ConflictError]
    assert User.objects.filter(email="dup@example.test").count() == 1
    assert AuditEvent.objects.filter(action=AUDIT_USER_CREATED).count() == 1


def test_two_activations_of_the_same_invitation():
    user, secret = invited_with_link(AdminFactory())
    results = run_concurrently(
        *(lambda p=p: services.accept_invitation(secret, p) for p in PASSWORDS)
    )
    successes, failures = outcomes(results, User)
    assert len(successes) == 1
    assert [type(f) for f in failures] == [InvalidTokenError]
    user.refresh_from_db()
    assert user.status == UserStatus.ACTIVE
    assert sum(user.check_password(p) for p in PASSWORDS) == 1
    assert AuditEvent.objects.filter(action=AUDIT_USER_ACTIVATED).count() == 1


def test_resend_racing_activation_never_leaves_both_succeeding():
    admin = AdminFactory()
    user, secret = invited_with_link(admin)
    AccountToken.objects.filter(user=user).update(
        created_at=AccountToken.objects.get(user=user).created_at.replace(year=2020)
    )
    results = run_concurrently(
        lambda: services.accept_invitation(secret, PASSWORDS[0]),
        lambda: services.resend_invitation(actor_id=admin.pk, user_id=user.pk),
    )
    accepted, resent = (not isinstance(r, Exception) for r in results)
    assert accepted != resent  # exactly one wins
    user.refresh_from_db()
    pending = AccountToken.objects.filter(
        user=user, purpose=TokenPurpose.INVITATION, status=TokenStatus.PENDING
    ).count()
    if accepted:
        assert isinstance(results[1], BusinessRuleViolation)
        assert (user.status, pending) == (UserStatus.ACTIVE, 0)
    else:
        assert isinstance(results[0], InvalidTokenError)
        assert (user.status, pending) == (UserStatus.INVITED, 1)


def test_two_password_reset_confirmations_with_one_link():
    user = UserFactory()
    services.request_password_reset(user.email, ip=None)
    drain_outbox()
    secret = last_secret("reset-password")
    results = run_concurrently(
        *(lambda p=p: services.confirm_password_reset(secret, p) or "ok" for p in PASSWORDS)
    )
    assert sorted(type(r).__name__ for r in results) == ["InvalidTokenError", "str"]
    user.refresh_from_db()
    assert sum(user.check_password(p) for p in PASSWORDS) == 1


def test_two_admins_deactivating_each_other_leave_one_administrator():
    first, second = AdminFactory(), AdminFactory()
    results = run_concurrently(
        lambda: services.deactivate_user(actor_id=first.pk, user_id=second.pk),
        lambda: services.deactivate_user(actor_id=second.pk, user_id=first.pk),
    )
    successes, failures = outcomes(results, User)
    assert len(successes) == 1
    assert [type(f) for f in failures] == [PermissionDeniedError]
    assert User.objects.filter(role=Role.ADMIN, status=UserStatus.ACTIVE).count() == 1


def test_an_admin_deactivated_mid_request_cannot_complete_an_action_afterwards():
    """The acting admin is re-checked under the admin lock: their action either completes
    before the deactivation commits, or is refused. Never after."""
    first, second = AdminFactory(), AdminFactory()
    results = run_concurrently(
        lambda: services.deactivate_user(actor_id=first.pk, user_id=second.pk),
        lambda: services.create_user(
            actor_id=second.pk,
            email="late@example.test",
            first_name="L",
            last_name="",
            role="admin",
        ),
    )
    assert isinstance(results[0], User)
    deactivation = AuditEvent.objects.get(action="user.deactivated")
    created = AuditEvent.objects.filter(action=AUDIT_USER_CREATED, actor_id=second.pk).first()
    if isinstance(results[1], User):
        assert created is not None
        assert created.pk < deactivation.pk
    else:
        assert isinstance(results[1], PermissionDeniedError)
        assert created is None


def test_a_parallel_burst_of_guesses_cannot_exceed_the_failure_budget():
    user = UserFactory()
    factory = RequestFactory()

    def guess():
        request = factory.post("/api/v1/auth/login", REMOTE_ADDR="203.0.113.5")
        try:
            sign_in(request, user.email, "wrong-password-guess")
        except (InvalidCredentialsError, RateLimitedError) as exc:
            return type(exc)

    results = run_concurrently(*(guess for _ in range(12)))
    assert AuthThrottleEvent.objects.count() <= settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD
    assert RateLimitedError in results


def test_a_parallel_spray_from_one_source_cannot_exceed_its_budget():
    """Review F3: the per-source check and insert used to race across accounts."""
    limit = settings.LOGIN_SOURCE_FAILURE_THRESHOLD
    AuthThrottleEvent.objects.bulk_create(
        AuthThrottleEvent(kind="login_failure", ip_address="203.0.113.5") for _ in range(limit - 4)
    )
    factory = RequestFactory()

    def spray(n):
        request = factory.post("/api/v1/auth/login", REMOTE_ADDR="203.0.113.5")
        try:
            sign_in(request, f"victim{n}@example.test", "Summer2026!!")
        except (InvalidCredentialsError, RateLimitedError) as exc:
            return type(exc)

    run_concurrently(*(lambda n=n: spray(n) for n in range(16)))
    assert AuthThrottleEvent.objects.filter(ip_address="203.0.113.5").count() == limit


def test_parallel_reset_requests_cannot_exceed_the_source_limit():
    """Review F3 / admin #4: the reset limit was check-then-insert without a lock."""

    def request_reset(n):
        try:
            services.request_password_reset(f"x{n}@example.test", ip="203.0.113.5")
            return "accepted"
        except RateLimitedError:
            return "limited"

    results = run_concurrently(*(lambda n=n: request_reset(n) for n in range(30)))
    assert results.count("accepted") == settings.PASSWORD_RESET_SOURCE_LIMIT_PER_HOUR
    assert results.count("limited") == 30 - settings.PASSWORD_RESET_SOURCE_LIMIT_PER_HOUR


def test_the_owner_is_not_refused_while_an_attacker_is_guessing():
    """Review F4: the old non-blocking lock answered "busy" to the owner mid-attack."""
    from django.http import HttpResponse

    from arkray.identity.throttling import remember_device

    user = UserFactory()
    identifier = throttling.login_identifier(user.email)
    cookie = HttpResponse()
    remember_device(cookie, identifier, "d" * 32)
    device_cookie = cookie.cookies[settings.LOGIN_DEVICE_COOKIE_NAME].value
    factory = RequestFactory()

    def attacker():
        try:
            sign_in(factory.post("/", REMOTE_ADDR="198.51.100.66"), user.email, "guess-guess-1")
        except (InvalidCredentialsError, RateLimitedError) as exc:
            return type(exc)

    def owner():
        from django.contrib.sessions.backends.db import SessionStore

        request = factory.post("/", REMOTE_ADDR="192.0.2.10")
        request.COOKIES[settings.LOGIN_DEVICE_COOKIE_NAME] = device_cookie
        request.session = SessionStore()
        return sign_in(request, user.email, "correct-horse-battery-staple").user.pk

    for _ in range(3):
        results = run_concurrently(owner, *(attacker for _ in range(6)))
        assert results[0] == user.pk, results[0]
