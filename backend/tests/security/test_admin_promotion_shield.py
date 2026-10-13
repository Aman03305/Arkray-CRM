"""Promote-then-shield (adversarial review of 73ff1e3, L3 / PRIV-2).

A compromised administrator took over an ordinary account and made it untouchable:
1. set its password (the owner wasn't told), or changed its email to a mailbox they read and
   reset the password there;
2. signed in as the user and chose a new password (which satisfied the "must choose their own
   password first" guard: the setter chose it);
3. promoted the account. Every other administrator then got 422 on its email, password and
   role (R100): only deactivating it was left, and everything done meanwhile was recorded as
   the real employee's own work.

Now (identity.services):
- the account's own address is emailed when an administrator sets its password, changes its
  email (the old address, as before) or changes its role;
- an account whose password or email an administrator changed (or whose second factor an
  administrator reset, or that an administrator created with a password) can't be made an
  administrator for ADMIN_PROMOTION_COOLING_OFF_S (7 days), whoever chose the password since;
- promotions are counted (`arkray_administrator_promotions_last_hour`) and alerted on
  (`ArkrayAdministratorPromoted`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from django.core import mail
from django.test import override_settings
from rest_framework.test import APIClient

from arkray.identity import metrics, services
from arkray.identity.models import Role, User
from tests.factories import DEFAULT_PASSWORD, UserFactory
from tests.helpers import drain_outbox, last_secret, signed_in

pytestmark = pytest.mark.django_db

USERS = "/api/v1/admin/users"
TEMPORARY = "Temporary-Pass-For-You-31"
ATTACKER_CHOSEN = "Attacker-Chosen-Pass-61!"
ATTACKER_MAILBOX = "mallory@evil.test"
RULES = Path(__file__).resolve().parents[3] / "infrastructure" / "alerts" / "arkray.rules.yml"


def url(user: User, action: str = "") -> str:
    return f"{USERS}/{user.pk}{'/' + action if action else ''}"


def version(user: User) -> int:
    return User.objects.get(pk=user.pk).version


def promote(client: APIClient, user: User):
    return client.patch(url(user), {"role": "admin", "version": version(user)}, format="json")


def sign_in(client: APIClient, email: str, password: str):
    return client.post("/api/v1/auth/login", {"email": email, "password": password}, format="json")


def mail_to(address: str) -> list[str]:
    return [str(message.subject) for message in mail.outbox if message.to == [address]]


class TestTheOwnerIsTold:
    def test_an_administrator_setting_the_password_emails_the_account(self, admin_client, user_a):
        response = admin_client.post(
            url(user_a, "set-password"),
            {"version": version(user_a), "new_password": TEMPORARY},
            format="json",
        )
        assert response.status_code == 200, response.content
        drain_outbox()
        (subject,) = mail_to(user_a.email)
        assert "password" in subject.lower()
        body = mail.outbox[-1].body
        assert TEMPORARY not in body  # never the password itself

    def test_a_role_change_emails_the_account(self, admin_client, user_a):
        assert promote(admin_client, user_a).status_code == 200
        drain_outbox()
        (subject,) = mail_to(user_a.email)
        assert "role" in subject.lower()
        # The role's label (ADR-0035: "admin" is the super administrator).
        assert "Super admin" in mail.outbox[-1].body

    def test_an_email_change_still_tells_the_old_address(self, admin_client, user_a):
        old = user_a.email
        admin_client.post(
            url(user_a, "change-email"),
            {"email": "rahul.new@example.test", "version": version(user_a)},
            format="json",
        )
        drain_outbox()
        assert len(mail_to(old)) == 1


class TestTheNoticeCantBeSuppressed:
    """Review round 1, IDENTITY-3: the notice job skipped accounts that weren't active when it
    ran, so set the password, deactivate before the worker ran, reactivate a minute later:
    the owner was never told (the second-factor notices had the same pattern). The address is
    now taken when the notice is queued and the notice goes whatever the account's status;
    only a pseudonymised account's notices are dropped (privacy.staff empties them)."""

    def test_deactivating_before_the_worker_runs_doesnt_stop_it(self, admin, user_a):
        services.set_user_password(
            actor_id=admin.pk, user_id=user_a.pk, version=version(user_a), new_password=TEMPORARY
        )
        services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        drain_outbox()
        services.reactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        drain_outbox()
        assert mail_to(user_a.email) == [
            "Arkray CRM: An administrator set a new password for your account"
        ]

    def test_it_goes_to_the_address_the_account_had_when_it_happened(self, admin, user_a):
        """An email change right after (to a mailbox the administrator reads) doesn't take the
        password notice with it."""
        owner = user_a.email
        services.set_user_password(
            actor_id=admin.pk, user_id=user_a.pk, version=version(user_a), new_password=TEMPORARY
        )
        services.change_user_email(
            actor_id=admin.pk, user_id=user_a.pk, version=version(user_a), email=ATTACKER_MAILBOX
        )
        drain_outbox()
        assert "Arkray CRM: An administrator set a new password for your account" in mail_to(owner)
        assert mail_to(ATTACKER_MAILBOX) == []

    def test_a_second_factor_reset_notice_survives_a_deactivation(self, admin, user_a):
        from tests.mfa_helpers import enrol

        enrol(user_a)
        signed_in(admin).post(url(user_a, "mfa/reset"))
        services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        drain_outbox()
        assert mail_to(user_a.email) == ["Arkray CRM: Two-step verification was reset"]

    def test_a_pseudonymised_accounts_notices_are_dropped(self, admin, user_a):
        from arkray.privacy import staff

        services.set_user_password(
            actor_id=admin.pk, user_id=user_a.pk, version=version(user_a), new_password=TEMPORARY
        )
        services.deactivate_user(actor_id=admin.pk, user_id=user_a.pk)
        staff.pseudonymise(user_a.pk, operator_id=admin.pk)
        drain_outbox()
        assert mail.outbox == []


class TestNoPromotionRightAfterAnAdministratorChangedTheCredentials:
    def test_the_set_password_chain_ends_before_the_promotion(self, admin_client, user_a):
        """The review's chain A: set a password, sign in as the user and choose another one,
        promote."""
        admin_client.post(
            url(user_a, "set-password"),
            {"version": version(user_a), "new_password": TEMPORARY},
            format="json",
        )
        as_user = APIClient()
        assert sign_in(as_user, user_a.email, TEMPORARY).status_code == 200
        changed = as_user.post(
            "/api/v1/auth/password/change",
            {"current_password": TEMPORARY, "new_password": ATTACKER_CHOSEN},
            format="json",
        )
        assert changed.status_code == 204
        response = promote(admin_client, user_a)
        assert response.status_code == 422
        assert "7 days" in response.json()["error"]["message"]
        assert User.objects.get(pk=user_a.pk).role == Role.SALES_USER

    def test_the_email_change_chain_ends_before_the_promotion(self, admin_client, user_a):
        """The review's chain B: the email to a mailbox the administrator reads, Forgot
        password there, promote."""
        admin_client.post(
            url(user_a, "change-email"),
            {"email": ATTACKER_MAILBOX, "version": version(user_a)},
            format="json",
        )
        APIClient().post("/api/v1/auth/password-reset", {"email": ATTACKER_MAILBOX}, format="json")
        drain_outbox()
        confirmed = APIClient().post(
            "/api/v1/auth/password-reset/confirm",
            {"token": last_secret("reset-password"), "new_password": ATTACKER_CHOSEN},
            format="json",
        )
        assert confirmed.status_code == 204
        assert promote(admin_client, user_a).status_code == 422
        assert User.objects.get(pk=user_a.pk).role == Role.SALES_USER

    def test_a_second_factor_reset_by_an_administrator_counts_too(self, admin_client, user_a):
        from tests.mfa_helpers import enrol

        enrol(user_a)
        assert admin_client.post(url(user_a, "mfa/reset")).status_code == 200
        assert promote(admin_client, user_a).status_code == 422

    def test_a_user_created_with_a_password_counts_too(self, admin_client):
        created = admin_client.post(
            USERS,
            {
                "first_name": "Neha",
                "email": "neha@example.test",
                "role": "sales_user",
                "password": TEMPORARY,
            },
            format="json",
        )
        user = User.objects.get(pk=created.json()["id"])
        as_user = APIClient()
        sign_in(as_user, user.email, TEMPORARY)
        as_user.post(
            "/api/v1/auth/password/change",
            {"current_password": TEMPORARY, "new_password": ATTACKER_CHOSEN},
            format="json",
        )
        assert promote(admin_client, user).status_code == 422

    def test_the_service_refuses_it_too(self, admin, user_a):
        services.set_user_password(
            actor_id=admin.pk, user_id=user_a.pk, version=version(user_a), new_password=TEMPORARY
        )
        User.objects.filter(pk=user_a.pk).update(password_change_required=False)
        with pytest.raises(services.BusinessRuleViolation, match="7 days"):
            services.update_user(
                actor_id=admin.pk,
                user_id=user_a.pk,
                version=version(user_a),
                changes={"role": "admin"},
            )

    def test_the_owners_own_changes_dont_count(self, admin_client, user_a):
        own = signed_in(user_a)
        assert (
            own.post(
                "/api/v1/auth/password/change",
                {"current_password": DEFAULT_PASSWORD, "new_password": ATTACKER_CHOSEN},
                format="json",
            ).status_code
            == 204
        )
        assert promote(admin_client, user_a).status_code == 200

    def test_after_the_cooling_off_period_it_is_allowed(self, admin_client, user_a):
        admin_client.post(
            url(user_a, "set-password"),
            {"version": version(user_a), "new_password": TEMPORARY},
            format="json",
        )
        User.objects.filter(pk=user_a.pk).update(password_change_required=False)
        with override_settings(ADMIN_PROMOTION_COOLING_OFF_S=0):
            assert promote(admin_client, user_a).status_code == 200

    def test_another_users_changes_dont_hold_this_one_back(self, admin_client, user_a, user_b):
        admin_client.post(
            url(user_b, "set-password"),
            {"version": version(user_b), "new_password": TEMPORARY},
            format="json",
        )
        assert promote(admin_client, user_a).status_code == 200


class TestPromotionsAreWatched:
    def test_the_gauge_counts_promotions_only(self, admin, user_a, user_b):
        def value() -> float:
            by_name = {gauge.name: gauge for gauge in metrics.gauges()}
            return by_name["arkray_administrator_promotions_last_hour"].samples[0][1]

        assert value() == 0
        services.update_user(
            actor_id=admin.pk, user_id=user_b.pk, version=1, changes={"last_name": "Iyer"}
        )
        assert value() == 0
        services.update_user(
            actor_id=admin.pk, user_id=user_a.pk, version=1, changes={"role": "admin"}
        )
        assert value() == 1

    def test_an_alert_rule_raises_it(self):
        loaded = yaml.safe_load(RULES.read_text(encoding="utf-8"))
        rules = {rule["alert"]: rule for group in loaded["groups"] for rule in group["rules"]}
        rule = rules["ArkrayAdministratorPromoted"]
        assert "arkray_administrator_promotions_last_hour" in rule["expr"]

    def test_a_promotion_is_a_security_event(self, admin, admin_client, user_a):
        promote(admin_client, user_a)
        events = admin_client.get("/api/v1/admin/security-events").json()["results"]
        (event,) = [e for e in events if e["action"] == "user.role_changed"]
        assert event["details"] == {"from": "sales_user", "to": "admin"}


def test_new_administrators_still_come_by_invitation(admin_client):
    """Unchanged: inviting an administrator needs no cooling-off (nobody chose a password)."""
    invited = admin_client.post(
        USERS, {"first_name": "Bina", "email": "bina@example.test", "role": "admin"}, format="json"
    )
    assert invited.status_code == 201
    assert invited.json()["role"] == "admin"
    assert UserFactory().role == Role.SALES_USER
