"""R100: one administrator must never be able to take over another administrator's account
(docs/security.md#administrator-account-protection).

The attack: Admin A changed Admin B's sign-in email to a mailbox A reads, used Forgot
password there and signed in as B. A related chain: A demoted B to a sales user, set a
temporary password (allowed for sales users), signed in as B and chose a password at the
forced change. Policy: an administrator's credentials and standing (email, password, role)
are changed only by that administrator, whatever the account's status; another
administrator may still rename, deactivate and reactivate them (off-boarding)."""

from __future__ import annotations

import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from django.core import mail
from django.db import connections, transaction
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core.errors import BusinessRuleViolation, ConflictError
from arkray.core.models import OutboxEvent
from arkray.identity import services, support
from arkray.identity.models import AccountToken, Role, SupportSession, User, UserStatus
from arkray.identity.services import (
    ADMINISTRATOR_ACCOUNT_REFUSED,
    ADMINISTRATOR_EMAIL_REFUSED,
    ADMINISTRATOR_ROLE_REFUSED,
    OWN_ACCOUNT_REFUSED,
    PROMOTION_REFUSED_UNTIL_OWN_CHOICE,
)
from tests.factories import DEFAULT_PASSWORD, AdminFactory, InvitedUserFactory, UserFactory
from tests.helpers import drain_outbox, run_concurrently, signed_in

USERS = "/api/v1/admin/users"
ATTACKER_MAILBOX = "attacker@evil.test"
ATTACKER_PASSWORD = "Attacker-Chosen-Pass-61!"
CREDENTIAL_ACTIONS = {
    services.AUDIT_EMAIL_CHANGED,
    services.AUDIT_ROLE_CHANGED,
    services.AUDIT_PASSWORD_SET_BY_ADMIN,
}


def url(user, action=""):
    return f"{USERS}/{user.pk}{'/' + action if action else ''}"


def administrator(status: str, inviter: User | None = None) -> Any:
    """Another administrator account in the given lifecycle state."""
    if status == "active":
        return AdminFactory(first_name="Bina", last_name="Admin")
    if status == "deactivated":
        return AdminFactory(first_name="Bina", last_name="Admin", is_active=False)
    assert inviter is not None
    # Through the service, so a real pending invitation exists to be (not) replaced.
    invited = services.create_user(
        actor_id=inviter.pk,
        email="bina.invited@example.test",
        first_name="Bina",
        last_name="Admin",
        role="admin",
    )
    drain_outbox()
    mail.outbox.clear()
    return invited


def snapshot(user: User) -> tuple[object, ...]:
    """Everything that makes up an account's credentials and standing."""
    row = User.objects.get(pk=user.pk)
    tokens = sorted(
        AccountToken.objects.filter(user=row).values_list(
            "id", "purpose", "status", "token_hash", "expires_at"
        )
    )
    return (
        row.email,
        row.session_epoch,
        row.version,
        row.password,
        row.role,
        row.status,
        row.password_change_required,
        tokens,
    )


def error(response) -> tuple[int, str, str]:
    body = response.json()["error"]
    return response.status_code, body["code"], body["message"]


def sign_in(email: str, password: str):
    return APIClient().post(
        "/api/v1/auth/login", {"email": email, "password": password}, format="json"
    )


STATUSES = ["active", "deactivated", "invited"]


@pytest.mark.django_db
class TestAnotherAdministratorsEmail:
    @pytest.mark.parametrize("status", STATUSES)
    @pytest.mark.parametrize("with_own_password", [False, True])
    def test_refused_through_the_api(self, admin, admin_client, status, with_own_password):
        b = administrator(status, inviter=admin)
        before, outbox_before = snapshot(b), OutboxEvent.objects.count()
        body = {"email": ATTACKER_MAILBOX, "version": b.version}
        if with_own_password:  # A proving who A is changes nothing
            body["current_password"] = DEFAULT_PASSWORD
        response = admin_client.post(url(b, "change-email"), body, format="json")
        assert error(response) == (422, "business_rule_violation", ADMINISTRATOR_EMAIL_REFUSED)
        assert snapshot(b) == before  # email, epoch, version, hash, tokens
        assert OutboxEvent.objects.count() == outbox_before  # no notice, no new invitation
        assert not AuditEvent.objects.filter(action=services.AUDIT_EMAIL_CHANGED).exists()
        drain_outbox()
        assert not mail.outbox

    @pytest.mark.parametrize("status", STATUSES)
    def test_refused_by_the_service(self, admin, status):
        b = administrator(status, inviter=admin)
        before = snapshot(b)
        with pytest.raises(BusinessRuleViolation, match=ADMINISTRATOR_EMAIL_REFUSED):
            services.change_user_email(
                actor_id=admin.pk, user_id=b.pk, version=b.version, email=ATTACKER_MAILBOX
            )
        assert snapshot(b) == before
        assert not AuditEvent.objects.filter(action=services.AUDIT_EMAIL_CHANGED).exists()

    def test_the_reset_flow_at_the_old_address_still_reaches_only_b(self, admin, admin_client):
        b = administrator("active")
        admin_client.post(
            url(b, "change-email"), {"email": ATTACKER_MAILBOX, "version": 1}, format="json"
        )
        for address in (b.email, ATTACKER_MAILBOX):
            APIClient().post("/api/v1/auth/password-reset", {"email": address}, format="json")
        drain_outbox()
        assert [m.to for m in mail.outbox] == [[b.email]]

    def test_a_stale_page_gets_the_reason_not_a_conflict(self, admin, admin_client):
        """B was a sales user when A's page loaded; meanwhile B was made an administrator."""
        b = UserFactory()
        services.update_user(actor_id=admin.pk, user_id=b.pk, version=1, changes={"role": "admin"})
        response = admin_client.post(
            url(b, "change-email"), {"email": ATTACKER_MAILBOX, "version": 1}, format="json"
        )
        assert error(response)[2] == ADMINISTRATOR_EMAIL_REFUSED

    def test_your_own_email_still_changes_with_your_password(self, admin, admin_client):
        response = admin_client.post(
            url(admin, "change-email"),
            {"email": "anita.new@example.test", "version": 1, "current_password": DEFAULT_PASSWORD},
            format="json",
        )
        assert response.status_code == 200, response.content
        assert admin_client.get("/api/v1/auth/me").json()["email"] == "anita.new@example.test"


@pytest.mark.django_db
class TestAnotherAdministratorsPassword:
    @pytest.mark.parametrize("status", STATUSES)
    def test_refused_whatever_the_status(self, admin, admin_client, status):
        """Role-based: a deactivated or invited administrator used to get the "only active
        users" answer, so the protection hinged on the account's status."""
        b = administrator(status, inviter=admin)
        before = snapshot(b)
        response = admin_client.post(
            url(b, "set-password"),
            {"version": b.version, "new_password": ATTACKER_PASSWORD},
            format="json",
        )
        assert error(response) == (422, "business_rule_violation", ADMINISTRATOR_ACCOUNT_REFUSED)
        assert snapshot(b) == before
        with pytest.raises(BusinessRuleViolation, match="Administrators set their own"):
            services.set_user_password(
                actor_id=admin.pk, user_id=b.pk, version=b.version, new_password=ATTACKER_PASSWORD
            )

    def test_your_own_is_changed_in_settings(self, admin, admin_client):
        response = admin_client.post(
            url(admin, "set-password"),
            {"version": 1, "new_password": ATTACKER_PASSWORD},
            format="json",
        )
        assert error(response)[2] == OWN_ACCOUNT_REFUSED


@pytest.mark.django_db
class TestAnotherAdministratorsRole:
    @pytest.mark.parametrize("status", STATUSES)
    def test_demotion_is_refused(self, admin, admin_client, status):
        b = administrator(status, inviter=admin)
        before = snapshot(b)
        response = admin_client.patch(
            url(b), {"role": "sales_user", "version": b.version}, format="json"
        )
        assert error(response) == (422, "business_rule_violation", ADMINISTRATOR_ROLE_REFUSED)
        assert snapshot(b) == before
        assert not AuditEvent.objects.filter(action=services.AUDIT_ROLE_CHANGED).exists()
        with pytest.raises(BusinessRuleViolation, match="deactivate the account"):
            services.update_user(
                actor_id=admin.pk, user_id=b.pk, version=b.version, changes={"role": "sales_user"}
            )

    def test_a_stale_demotion_gets_the_reason(self, admin, admin_client):
        b = administrator("active")
        User.objects.filter(pk=b.pk).update(version=5)
        response = admin_client.patch(url(b), {"role": "sales_user", "version": 1}, format="json")
        assert error(response)[2] == ADMINISTRATOR_ROLE_REFUSED

    def test_names_can_still_be_corrected(self, admin, admin_client):
        b = administrator("active")
        response = admin_client.patch(
            url(b), {"last_name": "Kapoor", "role": "admin", "version": 1}, format="json"
        )
        assert response.status_code == 200, response.content
        assert response.json()["full_name"] == "Bina Kapoor"

    def test_your_own_role_stays_yours(self, admin, admin_client):
        response = admin_client.patch(
            url(admin), {"role": "sales_user", "version": 1}, format="json"
        )
        assert error(response)[2] == "You can't change your own role."

    def test_promotion_keeps_its_guard(self, admin, admin_client):
        given = UserFactory(password_change_required=True)
        refused = admin_client.patch(url(given), {"role": "admin", "version": 1}, format="json")
        assert error(refused)[2] == PROMOTION_REFUSED_UNTIL_OWN_CHOICE
        chosen = UserFactory()
        promoted = admin_client.patch(url(chosen), {"role": "admin", "version": 1}, format="json")
        assert promoted.status_code == 200, promoted.content
        assert promoted.json()["role"] == "admin"


@pytest.mark.django_db
class TestOffBoardingStillWorks:
    def test_another_administrator_is_deactivated_and_reactivated_with_an_audit_trail(
        self, admin, admin_client
    ):
        b = administrator("active")
        b_client = signed_in(b)
        assert admin_client.post(url(b, "deactivate")).json()["status"] == "deactivated"
        assert b_client.get("/api/v1/auth/me").status_code == 401  # their sessions end
        assert admin_client.post(url(b, "activate")).json()["status"] == "active"
        assert sign_in(b.email, DEFAULT_PASSWORD).status_code == 200  # their own password
        actions = list(
            AuditEvent.objects.filter(actor_id=admin.pk, target_id=str(b.pk))
            .order_by("id")
            .values_list("action", flat=True)
        )
        assert actions == [services.AUDIT_USER_DEACTIVATED, services.AUDIT_USER_REACTIVATED]

    def test_an_invited_administrators_invitation_is_resent_to_their_own_address(
        self, admin, admin_client
    ):
        b = InvitedUserFactory(role=Role.ADMIN, email="bina.invited@example.test")
        assert admin_client.post(url(b, "resend-invitation")).status_code == 200
        drain_outbox()
        assert [m.to for m in mail.outbox] == [["bina.invited@example.test"]]

    def test_own_account_rules_are_unchanged(self, admin, admin_client):
        response = admin_client.post(url(admin, "deactivate"))
        assert error(response)[2] == "You can't deactivate your own account."
        # (The last-active-administrator guard: test_admin_users.TestServiceLevelGuards.)


@pytest.mark.django_db
def test_sales_users_are_managed_exactly_as_before(admin, admin_client, user_a):
    """Regression: R100 changes nothing for non-administrators."""
    started = admin_client.post(
        "/api/v1/admin/support-sessions", {"user": str(user_a.pk)}, format="json"
    )
    assert started.status_code == 201, started.content
    assert admin_client.delete("/api/v1/admin/support-sessions/current").status_code == 204

    renamed = admin_client.patch(url(user_a), {"last_name": "Verma", "version": 1}, format="json")
    assert renamed.status_code == 200, renamed.content
    moved = admin_client.post(
        url(user_a, "change-email"),
        {"email": "rahul.new@example.test", "version": 2},
        format="json",
    )
    assert moved.status_code == 200, moved.content
    reset = admin_client.post(
        url(user_a, "set-password"),
        {"version": 3, "new_password": "Temporary-Reset-Pass-77#"},
        format="json",
    )
    assert reset.status_code == 200, reset.content
    assert reset.json()["password_change_required"] is True
    assert admin_client.post(url(user_a, "deactivate")).json()["status"] == "deactivated"
    assert admin_client.post(url(user_a, "activate")).json()["status"] == "active"
    invited = InvitedUserFactory()
    assert admin_client.post(url(invited, "resend-invitation")).status_code == 200
    created = admin_client.post(
        USERS,
        {"first_name": "Neha", "email": "neha@example.test", "role": "sales_user"},
        format="json",
    )
    assert created.status_code == 201, created.content
    promoted = admin_client.patch(
        url(UserFactory()), {"role": "admin", "version": 1}, format="json"
    )
    assert promoted.status_code == 200, promoted.content
    assert (
        set(
            AuditEvent.objects.filter(action__in=CREDENTIAL_ACTIONS).values_list(
                "action", flat=True
            )
        )
        == CREDENTIAL_ACTIONS
    )


@pytest.mark.django_db
def test_the_whole_takeover_chain_is_closed(admin, admin_client):
    """Every step an administrator could try against another administrator, in order."""
    b = administrator("active")
    b_client = signed_in(b)
    before = snapshot(b)

    # 1. Email change to a mailbox A reads.
    response = admin_client.post(
        url(b, "change-email"), {"email": ATTACKER_MAILBOX, "version": 1}, format="json"
    )
    assert error(response)[2] == ADMINISTRATOR_EMAIL_REFUSED
    # 2. Demotion (sales users can be given a temporary password).
    response = admin_client.patch(url(b), {"role": "sales_user", "version": 1}, format="json")
    assert error(response)[2] == ADMINISTRATOR_ROLE_REFUSED
    # 3. A temporary password.
    response = admin_client.post(
        url(b, "set-password"), {"version": 1, "new_password": ATTACKER_PASSWORD}, format="json"
    )
    assert error(response)[2] == ADMINISTRATOR_ACCOUNT_REFUSED
    # 4. Forgot password for B (anyone can ask): the link goes to B's own mailbox only.
    public = APIClient()
    assert (
        public.post("/api/v1/auth/password-reset", {"email": b.email}, format="json").status_code
        == 202
    )
    drain_outbox()
    assert [m.to for m in mail.outbox] == [[b.email]]
    # 5. A support session (B's CRM without B's password): not for administrators.
    response = admin_client.post(
        "/api/v1/admin/support-sessions", {"user": str(b.pk)}, format="json"
    )
    assert error(response)[2] == support.NOT_FOR_ADMINS
    assert not SupportSession.objects.exists()
    # 6. Signing in as B, by any credential A could know.
    for email in (b.email, ATTACKER_MAILBOX):
        assert sign_in(email, ATTACKER_PASSWORD).status_code == 400
    me = admin_client.get("/api/v1/auth/me").json()
    assert (me["id"], me["support_session"]) == (str(admin.pk), None)

    # B is untouched: same identity and password, still signed in, signs in as before.
    assert snapshot(b)[:7] == before[:7]
    assert b_client.get("/api/v1/auth/me").json()["id"] == str(b.pk)
    assert sign_in(b.email, DEFAULT_PASSWORD).status_code == 200
    assert not AuditEvent.objects.filter(action__in=CREDENTIAL_ACTIONS).exists()


@pytest.mark.django_db
def test_the_documented_operator_demotion_works(admin):
    """docs/security.md#administrator-account-protection: the shell snippet an operator runs
    to demote an administrator who stays, executed as written."""
    docs = Path(__file__).resolve().parents[3] / "docs" / "security.md"
    section = docs.read_text(encoding="utf-8").split("## Administrator account protection")[1]
    snippet = textwrap.dedent(section.split("```python")[1].split("```")[0])
    b = AdminFactory(email="bina@example.com")
    b_client = signed_in(b)
    services.create_user(
        actor_id=b.pk, email="new@example.test", first_name="N", last_name="", role="admin"
    )
    exec(snippet, {})  # noqa: S102 — the documented procedure itself
    b.refresh_from_db()
    assert (b.role, b.version) == (Role.SALES_USER, 2)
    assert b_client.get("/api/v1/auth/me").status_code == 401  # signed out
    assert not AccountToken.objects.filter(created_by=b, status="pending").exists()
    event = AuditEvent.objects.get(action=services.AUDIT_ROLE_CHANGED)
    assert (event.actor_id, event.metadata["revoked_invitations"]) == (None, 1)
    events = signed_in(admin).get("/api/v1/admin/security-events").json()["results"]
    assert (events[0]["action"], events[0]["actor"]) == ("user.role_changed", None)


# --- races ----------------------------------------------------------------------------------
def _hold_then(first, then, hold_s: float = 0.6) -> list[object]:
    """Run `first` in a transaction kept open for `hold_s` after it returns, and start `then`
    in another connection while it is open: `then` must wait for the admin lock and must see
    what `first` committed."""
    results: list[object] = [None, None]
    holding = threading.Event()

    def run_first() -> None:
        try:
            with transaction.atomic():
                results[0] = first()
                holding.set()
                time.sleep(hold_s)
        except BaseException as exc:
            results[0] = exc
        finally:
            holding.set()
            connections.close_all()

    def run_then() -> None:
        try:
            holding.wait(10)
            results[1] = then()
        except BaseException as exc:
            results[1] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run_first), threading.Thread(target=run_then)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


@pytest.mark.django_db(transaction=True)
class TestRaces:
    def test_an_email_change_waiting_on_a_promotion_sees_the_new_administrator(self):
        """The role is read from the locked row, after the lock: not before it."""
        first, second = AdminFactory(), AdminFactory()
        c = UserFactory(email="c@example.test")
        results = _hold_then(
            lambda: services.update_user(
                actor_id=first.pk, user_id=c.pk, version=1, changes={"role": "admin"}
            ),
            lambda: services.change_user_email(
                actor_id=second.pk, user_id=c.pk, version=2, email=ATTACKER_MAILBOX
            ),
        )
        assert isinstance(results[0], User), results[0]
        assert isinstance(results[1], BusinessRuleViolation), results[1]
        c.refresh_from_db()
        assert (c.role, c.email) == (Role.ADMIN, "c@example.test")

    def test_a_promotion_waiting_on_an_email_change_happens_after_it(self):
        first, second = AdminFactory(), AdminFactory()
        c = UserFactory()
        results = _hold_then(
            lambda: services.change_user_email(
                actor_id=second.pk, user_id=c.pk, version=1, email="c.new@example.test"
            ),
            lambda: services.update_user(
                actor_id=first.pk, user_id=c.pk, version=2, changes={"role": "admin"}
            ),
        )
        assert all(isinstance(r, User) for r in results), results
        assert email_changes_precede_promotions()

    def test_racing_email_changes_and_promotions_never_change_an_administrators_email(self):
        """Both from version 1: whichever runs second is refused (by the policy, checked
        before the version, or by the version)."""
        for _ in range(4):
            first, second = AdminFactory(), AdminFactory()
            c = UserFactory()
            results = run_concurrently(
                lambda c=c, a=first: services.update_user(
                    actor_id=a.pk, user_id=c.pk, version=1, changes={"role": "admin"}
                ),
                lambda c=c, a=second: services.change_user_email(
                    actor_id=a.pk, user_id=c.pk, version=1, email=f"moved-{c.pk}@example.test"
                ),
            )
            assert_settled(results)
            assert email_changes_precede_promotions()

    def test_racing_a_demotion_and_an_email_change_both_fail(self):
        first, second = AdminFactory(), AdminFactory()
        b = AdminFactory()
        before = snapshot(b)
        results = run_concurrently(
            lambda: services.update_user(
                actor_id=first.pk, user_id=b.pk, version=1, changes={"role": "sales_user"}
            ),
            lambda: services.change_user_email(
                actor_id=second.pk, user_id=b.pk, version=1, email=ATTACKER_MAILBOX
            ),
            lambda: services.set_user_password(
                actor_id=second.pk, user_id=b.pk, version=1, new_password=ATTACKER_PASSWORD
            ),
        )
        assert all(isinstance(r, BusinessRuleViolation) for r in results), results
        assert snapshot(b) == before

    def test_racing_a_temporary_password_and_a_promotion_never_makes_a_known_administrator(self):
        for _ in range(4):
            first, second = AdminFactory(), AdminFactory()
            c = UserFactory()
            results = run_concurrently(
                lambda c=c, a=first: services.update_user(
                    actor_id=a.pk, user_id=c.pk, version=1, changes={"role": "admin"}
                ),
                lambda c=c, a=second: services.set_user_password(
                    actor_id=a.pk, user_id=c.pk, version=1, new_password=ATTACKER_PASSWORD
                ),
            )
            assert_settled(results)
            c.refresh_from_db()
            assert not (c.role == Role.ADMIN and c.password_change_required)
            assert c.status == UserStatus.ACTIVE


def assert_settled(results: list[object]) -> None:
    """One call won, the other was refused cleanly (no deadlock, no unexpected error)."""
    assert sum(isinstance(r, User) for r in results) == 1, results
    for result in results:
        assert isinstance(result, User | BusinessRuleViolation | ConflictError), repr(result)


def email_changes_precede_promotions() -> bool:
    """No user's email was changed by someone else after they became an administrator."""
    promoted = {
        e.target_id: e.pk
        for e in AuditEvent.objects.filter(action=services.AUDIT_ROLE_CHANGED, metadata__to="admin")
    }
    return all(
        e.pk < promoted[e.target_id]
        for e in AuditEvent.objects.filter(action=services.AUDIT_EMAIL_CHANGED)
        if e.target_id in promoted and e.actor_id != e.target_id
    )
