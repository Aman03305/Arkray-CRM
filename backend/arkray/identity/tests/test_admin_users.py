"""Admin user management API (/api/v1/admin/users...), exercised as an administrator.

Authorization of these routes for other callers is covered by tests/security.
"""

from datetime import timedelta

import pytest
from django.core import mail
from django.db import connection
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.identity import services
from arkray.identity.models import AccountToken, Role, TokenPurpose, TokenStatus, User, UserStatus
from arkray.identity.services import (
    AUDIT_EMAIL_CHANGED,
    AUDIT_PROFILE_UPDATED,
    AUDIT_ROLE_CHANGED,
    AUDIT_USER_DEACTIVATED,
    AUDIT_USER_REACTIVATED,
)
from tests.factories import DEFAULT_PASSWORD, AdminFactory, InvitedUserFactory, UserFactory
from tests.helpers import drain_outbox, emailed_secrets, signed_in

pytestmark = pytest.mark.django_db

USERS = "/api/v1/admin/users"
SENSITIVE_KEYS = {"password", "token_hash", "session_epoch", "is_active", "is_superuser"}


def url(user, action=""):
    return f"{USERS}/{user.pk}{'/' + action if action else ''}"


def create_payload(**overrides):
    return {
        "first_name": "Neha",
        "last_name": "Verma",
        "email": "neha.verma@example.test",
        "role": "sales_user",
        **overrides,
    }


def signs_in(email, password=DEFAULT_PASSWORD):
    from rest_framework.test import APIClient

    response = APIClient().post(
        "/api/v1/auth/login", {"email": email, "password": password}, format="json"
    )
    return response.status_code == 200


class TestList:
    def test_lists_users_newest_first_without_security_fields(self, admin_client, admin, user_a):
        body = admin_client.get(USERS).json()
        assert [row["id"] for row in body["results"]] == [str(user_a.pk), str(admin.pk)]
        assert set(body) == {"results", "next", "previous"}
        for row in body["results"]:
            assert not SENSITIVE_KEYS & set(row)
        row = body["results"][0]
        assert (row["full_name"], row["role_label"], row["status"]) == (
            "Rahul Sharma",
            "User",
            "active",
        )

    def test_shows_the_state_of_a_pending_invitation(self, admin_client, admin):
        services.create_user(
            actor_id=admin.pk,
            email="inv@example.test",
            first_name="Inv",
            last_name="",
            role="sales_user",
        )
        row = admin_client.get(USERS, {"status": "invited"}).json()["results"][0]
        assert row["invitation"]["sent_at"] is None
        assert row["invitation"]["expired"] is False
        drain_outbox()
        row = admin_client.get(USERS, {"status": "invited"}).json()["results"][0]
        assert row["invitation"]["sent_at"] is not None

    def test_active_users_have_no_invitation_state(self, admin_client, user_a):
        rows = admin_client.get(USERS).json()["results"]
        assert all(row["invitation"] is None for row in rows)

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("rahul", {"Rahul Sharma"}),
            ("SHARMA", {"Rahul Sharma"}),
            ("rahul sharma", {"Rahul Sharma"}),
            ("sharma rahul", {"Rahul Sharma"}),
            ("pat", {"Priya Patel"}),
            ("example.test", {"Rahul Sharma", "Priya Patel", "Anita Admin"}),
            ("rahul patel", set()),
            ("zzz", set()),
        ],
    )
    def test_search_matches_every_term_against_name_or_email(
        self, admin_client, user_a, user_b, q, expected
    ):
        rows = admin_client.get(USERS, {"q": q}).json()["results"]
        assert {row["full_name"] for row in rows} == expected

    def test_filters_by_status_and_role(self, admin_client, admin, user_a):
        InvitedUserFactory(first_name="Ina")
        UserFactory(first_name="Gone", is_active=False)
        by = lambda **p: {r["first_name"] for r in admin_client.get(USERS, p).json()["results"]}  # noqa: E731
        assert by(status="invited") == {"Ina"}
        assert by(status="deactivated") == {"Gone"}
        assert by(role="admin") == {"Anita"}
        assert by(role="sales_user", status="active") == {"Rahul"}

    @pytest.mark.parametrize(
        "params",
        [
            {"q": "a"},
            {"q": "x" * 101},
            {"status": "banned"},
            {"role": "ADMIN"},
            {"page_size": 500},
            {"owner": "someone"},
            {"email__contains": "@"},
            {"ordering": "password"},
        ],
    )
    def test_invalid_or_unknown_parameters_are_rejected(self, admin_client, params):
        response = admin_client.get(USERS, params)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"

    def test_paginates_with_a_stable_cursor(self, admin_client, admin):
        UserFactory.create_batch(24)
        first = admin_client.get(USERS, {"page_size": 10}).json()
        second = admin_client.get(first["next"]).json()
        third = admin_client.get(second["next"]).json()
        ids = [r["id"] for page in (first, second, third) for r in page["results"]]
        assert len(ids) == len(set(ids)) == 25
        assert third["next"] is None

    @pytest.mark.parametrize("extra_users", [2, 40])
    def test_query_count_does_not_grow_with_the_page(
        self, admin_client, extra_users, django_assert_num_queries
    ):
        UserFactory.create_batch(extra_users)
        InvitedUserFactory.create_batch(3)
        # session + signed-in user + one list query (invitation state joined, not N+1)
        with django_assert_num_queries(3):
            response = admin_client.get(USERS, {"page_size": 100})
        assert response.status_code == 200

    def test_search_is_served_by_trigram_indexes(self, admin_client):
        UserFactory.create_batch(3)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            sql, params = User.objects.filter(first_name__icontains="rah").query.sql_with_params()
            cursor.execute(f"EXPLAIN {sql}", params)
            plan = "\n".join(row[0] for row in cursor.fetchall())
        assert "identity_user_first_trgm" in plan


class TestCreate:
    def test_creates_an_invited_user(self, admin_client):
        response = admin_client.post(USERS, create_payload(), format="json")
        assert response.status_code == 201
        body = response.json()
        assert response["Location"] == f"{USERS}/{body['id']}"
        assert (body["status"], body["role"], body["email"]) == (
            "invited",
            "sales_user",
            "neha.verma@example.test",
        )
        assert body["invitation"]["expired"] is False

    def test_can_create_an_admin(self, admin_client):
        response = admin_client.post(USERS, create_payload(role="admin"), format="json")
        assert response.json()["role"] == "admin"

    def test_email_is_stored_canonically(self, admin_client):
        response = admin_client.post(
            USERS, create_payload(email="  Neha.VERMA@Example.TEST "), format="json"
        )
        assert response.json()["email"] == "neha.verma@example.test"

    def test_duplicate_email_is_a_conflict_regardless_of_case(self, admin_client, user_a):
        response = admin_client.post(
            USERS, create_payload(email=user_a.email.upper()), format="json"
        )
        assert response.status_code == 409
        assert response.json()["error"]["details"] == {
            "email": ["A user with this email address already exists."]
        }

    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"email": "not-an-email"}, "email"),
            ({"email": "\u0430dmin@example.test"}, "email"),  # Cyrillic a: a look-alike
            ({"role": "ADMIN"}, "role"),
            ({"role": "superuser"}, "role"),
            ({"first_name": ""}, "first_name"),
            ({"first_name": "   "}, "first_name"),
            ({"first_name": "x" * 101}, "first_name"),
        ],
    )
    def test_invalid_input_is_rejected(self, admin_client, overrides, field):
        response = admin_client.post(USERS, create_payload(**overrides), format="json")
        assert response.status_code == 400
        assert field in response.json()["error"]["details"]
        assert not User.objects.filter(first_name="Neha").exists()

    def test_mass_assignment_is_refused(self, admin_client):
        payload = create_payload(
            is_superuser=True,
            capabilities=["*"],
            status="active",
            is_active=True,
            password="i-choose-your-password",
        )
        response = admin_client.post(USERS, payload, format="json")
        assert response.status_code == 400
        message = response.json()["error"]["details"]["non_field_errors"][0]
        assert message.startswith("Unknown field(s): capabilities, is_active, is_superuser")
        assert not User.objects.filter(email="neha.verma@example.test").exists()

    def test_an_admin_never_chooses_the_password(self, admin_client):
        admin_client.post(USERS, create_payload(), format="json")
        assert not User.objects.get(email="neha.verma@example.test").has_usable_password()


class TestUpdate:
    def test_edits_names_and_bumps_the_version(self, admin_client, admin, user_a):
        response = admin_client.patch(
            url(user_a), {"first_name": " Rahul K ", "version": 1}, format="json"
        )
        assert response.status_code == 200
        assert (response.json()["first_name"], response.json()["version"]) == ("Rahul K", 2)
        event = AuditEvent.objects.get(action=AUDIT_PROFILE_UPDATED)
        assert (event.actor_id, event.metadata) == (admin.pk, {"fields": ["first_name"]})

    def test_role_change_is_audited_with_before_and_after(self, admin_client, user_a):
        response = admin_client.patch(url(user_a), {"role": "admin", "version": 1}, format="json")
        assert response.json()["role"] == "admin"
        event = AuditEvent.objects.get(action=AUDIT_ROLE_CHANGED)
        assert event.metadata == {"from": "sales_user", "to": "admin", "revoked_invitations": 0}

    def test_a_stale_version_is_a_conflict(self, admin_client, user_a):
        admin_client.patch(url(user_a), {"last_name": "S", "version": 1}, format="json")
        response = admin_client.patch(url(user_a), {"last_name": "T", "version": 1}, format="json")
        assert response.status_code == 409
        assert User.objects.get(pk=user_a.pk).last_name == "S"

    def test_the_version_is_required(self, admin_client, user_a):
        response = admin_client.patch(url(user_a), {"last_name": "S"}, format="json")
        assert response.status_code == 400
        assert "version" in response.json()["error"]["details"]

    @pytest.mark.parametrize(
        "field", ["email", "status", "is_active", "password", "is_superuser", "capabilities"]
    )
    def test_identity_and_security_fields_cannot_be_patched(self, admin_client, user_a, field):
        response = admin_client.patch(url(user_a), {field: "x", "version": 1}, format="json")
        assert response.status_code == 400
        assert User.objects.get(pk=user_a.pk).version == 1

    def test_an_admin_cannot_change_their_own_role(self, admin_client, admin):
        response = admin_client.patch(
            url(admin), {"role": "sales_user", "version": 1}, format="json"
        )
        assert response.status_code == 422
        assert User.objects.get(pk=admin.pk).role == Role.ADMIN

    def test_a_no_op_keeps_the_version(self, admin_client, user_a):
        response = admin_client.patch(
            url(user_a), {"first_name": "Rahul", "version": 1}, format="json"
        )
        assert response.json()["version"] == 1
        assert not AuditEvent.objects.exists()

    def test_unknown_user_is_404(self, admin_client):
        response = admin_client.patch(
            f"{USERS}/00000000-0000-4000-8000-000000000000", {"version": 1}, format="json"
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("ref", ["not-a-uuid", "00000000-0000-4000-8000-00000000000G"])
    def test_malformed_ids_are_404(self, admin_client, ref):
        assert admin_client.get(f"{USERS}/{ref}").status_code == 404


class TestChangeEmail:
    def change(self, client, user, email, version=1):
        return client.post(
            url(user, "change-email"), {"email": email, "version": version}, format="json"
        )

    def test_changes_the_sign_in_identity(self, admin_client, admin, user_a):
        old = user_a.email
        response = self.change(admin_client, user_a, "Rahul.New@Example.test")
        assert response.status_code == 200
        assert response.json()["email"] == "rahul.new@example.test"
        assert not signs_in(old)
        assert signs_in("rahul.new@example.test")
        event = AuditEvent.objects.get(action=AUDIT_EMAIL_CHANGED)
        assert (event.actor_id, event.metadata) == (
            admin.pk,
            {"from": old, "to": "rahul.new@example.test", "revoked_links": 0},
        )

    def test_notifies_the_previous_address(self, admin_client, user_a):
        old = user_a.email
        self.change(admin_client, user_a, "rahul.new@example.test")
        drain_outbox()
        (message,) = mail.outbox
        assert message.to == [old]
        assert "r***@example.test" in message.body

    def test_an_invited_user_gets_a_fresh_invitation_at_the_new_address(self, admin_client, admin):
        invited = services.create_user(
            actor_id=admin.pk,
            email="typo@exmaple.test",
            first_name="Ina",
            last_name="",
            role="sales_user",
        )
        drain_outbox()
        stale_link = emailed_secrets("activate")[-1]
        self.change(admin_client, invited, "ina@example.test")
        drain_outbox()
        assert mail.outbox[-1].to == ["ina@example.test"]
        accept = admin_client.post(
            "/api/v1/auth/invitations/accept",
            {"token": stale_link, "password": "a-long-and-unusual-passphrase"},
            format="json",
        )
        assert accept.status_code == 400  # the link sent to the wrong mailbox is dead

    def test_pending_password_reset_links_are_revoked(self, admin_client, user_a):
        services.request_password_reset(user_a.email, ip=None)
        drain_outbox()
        self.change(admin_client, user_a, "rahul.new@example.test")
        token = AccountToken.objects.get(user=user_a, purpose=TokenPurpose.PASSWORD_RESET)
        assert token.status == TokenStatus.REVOKED

    def test_uniqueness_is_enforced(self, admin_client, user_a, user_b):
        response = self.change(admin_client, user_a, user_b.email.upper())
        assert response.status_code == 409

    def test_changing_your_own_email_keeps_your_session(self, admin_client, admin):
        response = admin_client.post(
            url(admin, "change-email"),
            {"email": "anita.new@example.test", "version": 1, "current_password": DEFAULT_PASSWORD},
            format="json",
        )
        assert response.status_code == 200
        assert admin_client.get("/api/v1/auth/me").json()["email"] == "anita.new@example.test"

    @pytest.mark.parametrize("current_password", [None, "", "not-my-password"])
    def test_changing_your_own_email_requires_your_current_password(
        self, admin_client, admin, current_password
    ):
        """A hijacked session must not become a permanent takeover of the account."""
        body = {"email": "attacker@evil.test", "version": 1}
        if current_password is not None:
            body["current_password"] = current_password
        response = admin_client.post(url(admin, "change-email"), body, format="json")
        assert response.status_code == 400
        assert "current_password" in response.json()["error"]["details"]
        admin.refresh_from_db()
        assert admin.email != "attacker@evil.test"

    def test_non_ascii_and_malformed_addresses_are_rejected(self, admin_client, user_a):
        assert self.change(admin_client, user_a, "r\u0430hul@example.test").status_code == 400
        assert self.change(admin_client, user_a, "nope").status_code == 400


class TestDeactivateAndReactivate:
    def test_deactivation_keeps_the_user_and_history(self, admin_client, admin, user_a):
        response = admin_client.post(url(user_a, "deactivate"))
        assert response.status_code == 200
        assert response.json()["status"] == "deactivated"
        assert User.objects.filter(pk=user_a.pk).exists()
        assert not signs_in(user_a.email)
        event = AuditEvent.objects.get(action=AUDIT_USER_DEACTIVATED)
        assert (event.actor_id, event.metadata) == (
            admin.pk,
            {"previous_status": "active", "revoked_links": 0, "revoked_invitations": 0},
        )

    def test_deactivation_is_idempotent(self, admin_client, user_a):
        admin_client.post(url(user_a, "deactivate"))
        assert admin_client.post(url(user_a, "deactivate")).status_code == 200
        assert AuditEvent.objects.filter(action=AUDIT_USER_DEACTIVATED).count() == 1

    def test_deactivation_revokes_pending_links(self, admin_client, admin):
        invited = services.create_user(
            actor_id=admin.pk,
            email="i@example.test",
            first_name="I",
            last_name="",
            role="sales_user",
        )
        admin_client.post(url(invited, "deactivate"))
        assert set(AccountToken.objects.values_list("status", flat=True)) == {TokenStatus.REVOKED}

    def test_an_admin_cannot_deactivate_themselves(self, admin_client, admin):
        response = admin_client.post(url(admin, "deactivate"))
        assert response.status_code == 422
        assert User.objects.get(pk=admin.pk).is_active

    def test_reactivation_restores_access_with_the_existing_password(
        self, admin_client, admin, user_a
    ):
        admin_client.post(url(user_a, "deactivate"))
        response = admin_client.post(url(user_a, "activate"))
        assert response.json()["status"] == "active"
        assert signs_in(user_a.email)
        event = AuditEvent.objects.get(action=AUDIT_USER_REACTIVATED)
        assert event.metadata == {"status": "active"}

    def test_reactivating_a_never_activated_user_reinvites_them(self, admin_client, admin):
        invited = services.create_user(
            actor_id=admin.pk,
            email="i@example.test",
            first_name="I",
            last_name="",
            role="sales_user",
        )
        admin_client.post(url(invited, "deactivate"))
        response = admin_client.post(url(invited, "activate"))
        assert response.json()["status"] == "invited"
        drain_outbox()
        assert [m.to for m in mail.outbox] == [["i@example.test"]]

    def test_an_invited_user_cannot_be_activated_by_an_admin(self, admin_client, admin):
        invited = InvitedUserFactory()
        assert admin_client.post(url(invited, "activate")).status_code == 422

    def test_activating_an_active_user_is_a_no_op(self, admin_client, user_a):
        assert admin_client.post(url(user_a, "activate")).json()["status"] == "active"
        assert not AuditEvent.objects.exists()

    def test_another_admin_can_be_deactivated_while_one_remains(self, admin_client):
        other = AdminFactory()
        assert admin_client.post(url(other, "deactivate")).status_code == 200

    def test_a_deactivated_admin_can_no_longer_administer(self, admin):
        other = AdminFactory()
        other_client = signed_in(other)
        signed_in(admin).post(url(other, "deactivate"))
        assert other_client.get(USERS).status_code == 401


class TestServiceLevelGuards:
    """Rules enforced below the HTTP layer, for any future caller (CLI, jobs)."""

    def test_a_non_manager_actor_is_refused(self, user_a, user_b):
        from arkray.core.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            services.deactivate_user(actor_id=user_a.pk, user_id=user_b.pk)

    def test_the_last_active_administrator_cannot_be_removed(self, admin, user_a, monkeypatch):
        """Unreachable through the API today (the actor is always another active admin),
        but enforced independently, e.g. for a future system actor."""
        from arkray.core.errors import BusinessRuleViolation

        monkeypatch.setattr(services, "_acting_manager", lambda actor_id: user_a)
        with pytest.raises(BusinessRuleViolation):
            services.deactivate_user(actor_id=user_a.pk, user_id=admin.pk)
        with pytest.raises(BusinessRuleViolation):
            services.update_user(
                actor_id=user_a.pk, user_id=admin.pk, version=1, changes={"role": "sales_user"}
            )
        admin.refresh_from_db()
        assert (admin.status, admin.role) == (UserStatus.ACTIVE, Role.ADMIN)

    def test_update_rejects_fields_outside_the_allowlist(self, admin, user_a):
        from arkray.core.errors import InvalidInputError

        with pytest.raises(InvalidInputError):
            services.update_user(
                actor_id=admin.pk, user_id=user_a.pk, version=1, changes={"is_active": "0"}
            )

    def test_expired_invitations_are_reported_as_expired(self, admin_client, admin):
        invited = services.create_user(
            actor_id=admin.pk,
            email="i@example.test",
            first_name="I",
            last_name="",
            role="sales_user",
        )
        past = timezone.now() - timedelta(minutes=1)
        AccountToken.objects.filter(user=invited).update(
            created_at=past - timedelta(days=3), expires_at=past
        )
        assert admin_client.get(url(invited)).json()["invitation"]["expired"] is True
