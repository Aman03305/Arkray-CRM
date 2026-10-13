"""The password policy holds on every way a password is set (adversarial review of 73ff1e3,
L1 (e) and L11 / AUTH-5).

The policy (settings.AUTH_PASSWORD_VALIDATORS: at least 12 characters, at most 128, not one
of the 20,000 common passwords Django ships, not all digits, not like the name or email, not
the product's name) is the offline breached-password check: no request leaves the
deployment. It ran on every API path, but `manage.py createsuperuser --noinput` (the
documented bootstrap) never called it: the first administrator could be created with the
password "a", and whoever signed in first owned the only administrator. The interactive
command let the operator answer "y" to "Bypass password validation?". The manager now
validates in `create_superuser` (both paths go through it).
"""

from __future__ import annotations

import builtins
import getpass
import secrets

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from arkray.identity import services
from arkray.identity.models import Role, User
from tests.factories import DEFAULT_PASSWORD, InvitedUserFactory
from tests.helpers import drain_outbox, last_secret, signed_in

pytestmark = pytest.mark.django_db

COMMON = "qwertyuiop123"  # 13 characters, in Django's common-password list
# A strong password made up for each run (no fixed value a secret scanner would flag).
STRONG = secrets.token_urlsafe(18)
EMAIL = "first.admin@example.test"


class _Terminal:
    """What createsuperuser checks before prompting (Django's own tests do the same)."""

    def isatty(self) -> bool:
        return True


class TestBootstrapAdministrator:
    @pytest.mark.parametrize("password", ["a", COMMON, "first.admin@example.test1", "123456789012"])
    def test_createsuperuser_noinput_refuses_a_weak_password(self, monkeypatch, password):
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", password)
        with pytest.raises(CommandError):
            call_command(
                "createsuperuser",
                interactive=False,
                email=EMAIL,
                first_name="First",
                last_name="Admin",
                verbosity=0,
            )
        assert not User.objects.filter(email=EMAIL).exists()

    def test_createsuperuser_noinput_accepts_a_strong_one(self, monkeypatch):
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", STRONG)
        call_command(
            "createsuperuser",
            interactive=False,
            email=EMAIL,
            first_name="First",
            last_name="Admin",
            verbosity=0,
        )
        user = User.objects.get(email=EMAIL)
        assert (user.role, user.status) == (Role.ADMIN, "active")

    def test_the_interactive_bypass_is_gone(self, monkeypatch):
        """Django asks "Bypass password validation and create user anyway? [y/N]"; "y" no
        longer creates the account."""
        answers = iter([EMAIL, "First", "Admin", "y"])
        monkeypatch.setattr(builtins, "input", lambda *_: next(answers))
        monkeypatch.setattr(getpass, "getpass", lambda *_: "a")
        with pytest.raises(CommandError):
            call_command("createsuperuser", interactive=True, stdin=_Terminal(), verbosity=0)
        assert not User.objects.filter(email=EMAIL).exists()

    def test_the_manager_refuses_it_directly(self):
        with pytest.raises(ValidationError):
            User.objects.create_superuser(EMAIL, "First", "Admin", password="a")
        assert not User.objects.filter(email=EMAIL).exists()

    def test_a_non_ascii_email_is_refused(self):
        kelvin = chr(0x212A)
        with pytest.raises((ValidationError, ValueError)):
            User.objects.create_superuser(f"{kelvin}elvin@example.test", "K", "", password=STRONG)


class TestEveryApiPath:
    """A common password is refused wherever someone chooses one."""

    def test_an_administrator_creating_a_user(self, admin_client):
        response = admin_client.post(
            "/api/v1/admin/users",
            {
                "first_name": "Neha",
                "email": "neha@example.test",
                "role": "sales_user",
                "password": COMMON,
            },
            format="json",
        )
        assert response.status_code == 400
        assert "password" in response.json()["error"]["details"]

    def test_an_administrator_setting_one(self, admin_client, user_a):
        response = admin_client.post(
            f"/api/v1/admin/users/{user_a.pk}/set-password",
            {"version": user_a.version, "new_password": COMMON},
            format="json",
        )
        assert response.status_code == 400

    def test_the_users_own_change(self, user_a):
        response = signed_in(user_a).post(
            "/api/v1/auth/password/change",
            {"current_password": DEFAULT_PASSWORD, "new_password": COMMON},
            format="json",
        )
        assert response.status_code == 400

    def test_accepting_an_invitation(self, admin):
        invited = InvitedUserFactory(email="invitee@example.test")
        services.resend_invitation(actor_id=admin.pk, user_id=invited.pk)
        drain_outbox()
        response = APIClient().post(
            "/api/v1/auth/invitations/accept",
            {"token": last_secret("activate"), "password": COMMON},
            format="json",
        )
        assert response.status_code == 400

    def test_a_reset_by_email(self, user_a):
        APIClient().post("/api/v1/auth/password-reset", {"email": user_a.email}, format="json")
        drain_outbox()
        response = APIClient().post(
            "/api/v1/auth/password-reset/confirm",
            {"token": last_secret("reset-password"), "new_password": COMMON},
            format="json",
        )
        assert response.status_code == 400
