"""Super administrators and team administrators in user administration (ADR-0035).

The cast (root conftest): Anita is a super administrator (role `admin`); Tara an Admin (role
`team_admin`) whose team is Rahul and Mohan; Priya is in no team; Omar is another Admin, with
Kavya in his team.

- Tara administers herself and her team only: every /admin/users* route answers 404 for
  anyone else (Anita, Priya, Omar, Kavya), identical to an account that doesn't exist, and
  changes nothing. She creates sales users only, always into her own team, and never changes
  a role or a team (403).
- Anita changes roles (never another super administrator's, never her own) and teams; an
  Admin who stops being one leaves their team unassigned and their invitations revoked.
- Credentials of an administrator of either kind are theirs alone (ADR-0030); so, for the
  cooling-off, are an account's whose administrator role another took away
  ("demote-then-unshield").
- Support sessions, two-step verification resets and owner pickers follow the same reach,
  re-checked on every request.
- Everything is serialised by the user-administration lock and decided on locked rows: the
  races at the end run with real concurrency.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.core import mail
from django.db import connection, connections
from django.test import override_settings
from django.utils import timezone

from arkray.activities import services as activity_services
from arkray.activities.events import ActivityCreated
from arkray.audit import services as audit
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.core.errors import (
    BusinessRuleViolation,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from arkray.core.models import OutboxEvent
from arkray.identity import metrics, mfa_services, selectors, services, support, teams
from arkray.identity.models import (
    AccountToken,
    MfaDevice,
    Role,
    SupportEnd,
    SupportSession,
    TokenPurpose,
    TokenStatus,
    User,
    UserStatus,
)
from arkray.leads import services as lead_services
from arkray.leads.events import LeadCreated, LeadReassigned
from arkray.privacy import staff
from tests.factories import (
    DEFAULT_PASSWORD,
    AdminFactory,
    InvitedUserFactory,
    LeadFactory,
    TeamAdminFactory,
    UserFactory,
)
from tests.helpers import drain_outbox, run_concurrently, signed_in, without_request_id
from tests.mfa_helpers import enrol
from tests.security.test_admin_takeover import _hold_then

USERS = "/api/v1/admin/users"
SUPPORT = "/api/v1/admin/support-sessions"
TEMPORARY = "Temporary-Reset-Pass-77#"
MISSING = "5a1e4d2c-0000-4000-8000-00000000abcd"


def url(user: Any, action: str = "") -> str:
    ref = user if isinstance(user, str) else user.pk
    return f"{USERS}/{ref}{'/' + action if action else ''}"


def per_user_calls(target: Any, version: int = 1) -> list[tuple[str, str, dict[str, Any]]]:
    """Every /admin/users/{id}* operation, with a body each would accept."""
    return [
        ("get", url(target), {}),
        ("patch", url(target), {"last_name": "Changed", "version": version}),
        ("post", url(target, "change-email"), {"email": "moved@example.test", "version": version}),
        ("post", url(target, "deactivate"), {}),
        ("post", url(target, "activate"), {}),
        ("post", url(target, "resend-invitation"), {}),
        ("post", url(target, "set-password"), {"version": version, "new_password": TEMPORARY}),
        ("post", url(target, "mfa/reset"), {}),
        ("post", url(target, "clear-sign-in-lock"), {}),
    ]


CALL_NAMES = [
    "get",
    "patch",
    "change-email",
    "deactivate",
    "activate",
    "resend-invitation",
    "set-password",
    "mfa-reset",
    "clear-sign-in-lock",
]


def call(client, method: str, path: str, body: dict[str, Any]):
    return getattr(client, method)(path, body, format="json")


def everything() -> tuple[object, ...]:
    """The state of every account and everything user administration writes."""
    users = sorted(
        User.objects.values_list(
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "status",
            "manager_id",
            "version",
            "session_epoch",
            "password",
            "password_change_required",
        )
    )
    tokens = sorted(AccountToken.objects.values_list("id", "status"))
    devices = sorted(MfaDevice.objects.values_list("id", "user_id"))
    return (
        users,
        tokens,
        devices,
        AuditEvent.objects.count(),
        OutboxEvent.objects.count(),
        SupportSession.objects.count(),
    )


def error(response) -> tuple[int, str, str]:
    body = response.json()["error"]
    return response.status_code, body["code"], body["message"]


def manager_of(user: User) -> uuid.UUID | None:
    return User.objects.values_list("manager_id", flat=True).get(pk=user.pk)


def eligible_from(user: User) -> datetime:
    """When another administrator may change `user`'s credentials again: the cooling-off
    after the demotion recorded in the audit trail."""
    demoted = AuditEvent.objects.get(action=services.AUDIT_ROLE_CHANGED, target_id=str(user.pk))
    return demoted.occurred_at + timedelta(seconds=settings.ADMIN_PROMOTION_COOLING_OFF_S)


def assert_teams_consistent() -> None:
    """ADR-0035's invariant: a sales user's manager is a team administrator."""
    stale = User.objects.filter(manager__isnull=False).exclude(manager__role=Role.TEAM_ADMIN)
    assert not stale.exists(), list(stale.values_list("email", "manager__role"))


@pytest.fixture
def outsiders(admin, user_b, other_team_admin, other_team_member):
    return {"anita": admin, "priya": user_b, "omar": other_team_admin, "kavya": other_team_member}


@pytest.fixture
def tara(team_admin, team_member, team_member_2):
    """Tara, with her team in place (Rahul and Mohan)."""
    return team_admin


@pytest.fixture
def tara_client(tara):
    return signed_in(tara)


# --- reach: 404 outside the team, for every route ---------------------------------------------
@pytest.mark.django_db
class TestOutsideTheTeamIsNotFound:
    @pytest.mark.parametrize("who", ["anita", "priya", "omar", "kavya", "missing"])
    @pytest.mark.parametrize("index", range(len(CALL_NAMES)), ids=CALL_NAMES)
    def test_every_route_is_the_404_of_nobody_and_changes_nothing(
        self, tara_client, outsiders, who, index
    ):
        target = MISSING if who == "missing" else outsiders[who]
        version = 1 if who == "missing" else outsiders[who].version
        method, path, body = per_user_calls(target, version)[index]
        missing_method, missing_path, missing_body = per_user_calls(MISSING)[index]
        before = everything()
        response = call(tara_client, method, path, body)
        assert response.status_code == 404, response.content
        assert without_request_id(response) == without_request_id(
            call(tara_client, missing_method, missing_path, missing_body)
        )
        assert everything() == before

    @pytest.mark.parametrize("status", ["deactivated", "invited", "pseudonymised"])
    def test_whatever_the_outsiders_status(self, tara_client, admin, status):
        """The reach is decided first: no 422 about a status or a pseudonym leaks out."""
        outsider = {
            "deactivated": lambda: UserFactory(is_active=False),
            "invited": InvitedUserFactory,
            "pseudonymised": lambda: UserFactory(is_active=False),
        }[status]()
        if status == "pseudonymised":
            staff.pseudonymise(outsider.pk, operator_id=admin.pk)
            outsider.refresh_from_db()
        before = everything()
        for method, path, body in per_user_calls(outsider, outsider.version):
            assert call(tara_client, method, path, body).status_code == 404, path
        assert everything() == before

    def test_another_teams_administrator_is_out_of_reach_too(self, other_team_admin, tara):
        """Omar can't reach Tara's team or Tara, as she can't reach his."""
        client = signed_in(other_team_admin)
        for member in User.objects.filter(manager=tara):
            assert client.get(url(member)).status_code == 404
        assert client.get(url(tara)).status_code == 404

    def test_a_role_or_a_team_is_refused_whoever_the_target(self, tara_client, tara, outsiders):
        """Asking for a role or a team change is refused for what Tara is, before any target is
        read: the same 403 for her member, an outsider and nobody."""
        answers = set()
        for target in [User.objects.get(first_name="Rahul"), outsiders["priya"], MISSING]:
            for change in ({"role": "admin"}, {"manager": None}, {"manager": str(tara.pk)}):
                response = tara_client.patch(url(target), {**change, "version": 1}, format="json")
                assert response.status_code == 403, response.content
                body = without_request_id(response)
                answers.add((body["error"]["code"], body["error"]["message"]))
        assert answers == {("permission_denied", services.ROLES_AND_TEAMS_REFUSED)}


# --- within the team --------------------------------------------------------------------------
@pytest.mark.django_db
class TestWithinTheTeam:
    def test_every_route_reaches_a_member(self, tara_client, team_member):
        """Not refused for reach or permission (the operations' own rules may still answer)."""
        for method, path, body in per_user_calls(team_member, team_member.version):
            team_member.refresh_from_db()
            body = {**body, "version": team_member.version} if "version" in body else body
            response = call(tara_client, method, path, body)
            assert response.status_code not in (401, 403, 404), (path, response.content)

    def test_reading_a_member_names_their_admin(self, tara_client, tara, team_member):
        body = tara_client.get(url(team_member)).json()
        assert body["manager"] == {"id": str(tara.pk), "full_name": "Tara Lead"}
        assert tara_client.get(url(tara)).json()["manager"] is None

    def test_a_member_is_administered_like_any_user(self, tara_client, tara, team_member):
        version = team_member.version
        renamed = tara_client.patch(
            url(team_member), {"last_name": "Verma", "version": version}, format="json"
        )
        assert renamed.status_code == 200, renamed.content
        moved = tara_client.post(
            url(team_member, "change-email"),
            {"email": "rahul.new@example.test", "version": version + 1},
            format="json",
        )
        assert moved.status_code == 200, moved.content
        reset = tara_client.post(
            url(team_member, "set-password"),
            {"version": version + 2, "new_password": TEMPORARY},
            format="json",
        )
        assert reset.status_code == 200, reset.content
        assert tara_client.post(url(team_member, "deactivate")).json()["status"] == "deactivated"
        assert tara_client.get(url(team_member)).status_code == 200  # still hers, any status
        assert tara_client.post(url(team_member, "activate")).json()["status"] == "active"
        assert tara_client.post(url(team_member, "clear-sign-in-lock")).status_code == 200
        actors = set(
            AuditEvent.objects.filter(target_id=str(team_member.pk)).values_list(
                "actor_id", flat=True
            )
        )
        assert actors == {tara.pk}
        assert manager_of(team_member) == tara.pk  # still in her team

    def test_an_invited_member_gets_a_new_invitation(self, tara_client, tara):
        invited = InvitedUserFactory(manager=tara, email="ina@example.test")
        assert tara_client.post(url(invited, "resend-invitation")).status_code == 200
        drain_outbox()
        assert [m.to for m in mail.outbox] == [["ina@example.test"]]

    def test_a_members_second_factor_is_reset(self, tara_client, tara, team_member):
        enrol(team_member)
        assert tara_client.post(url(team_member, "mfa/reset")).status_code == 200
        assert not MfaDevice.objects.filter(user=team_member).exists()
        event = AuditEvent.objects.get(action=mfa_services.AUDIT_RESET)
        assert (event.actor_id, event.metadata) == (tara.pk, {"by": "administrator"})

    def test_her_own_account_keeps_the_self_service_rules(self, tara_client, tara):
        assert tara_client.get(url(tara)).status_code == 200
        renamed = tara_client.patch(url(tara), {"last_name": "Rao", "version": 1}, format="json")
        assert renamed.status_code == 200, renamed.content
        own = tara_client.post(
            url(tara, "set-password"), {"version": 2, "new_password": TEMPORARY}, format="json"
        )
        assert error(own)[2] == services.OWN_ACCOUNT_REFUSED
        assert error(tara_client.post(url(tara, "deactivate")))[2] == (
            "You can't deactivate your own account."
        )
        assert error(tara_client.post(url(tara, "mfa/reset")))[2] == (
            mfa_services.ADMINISTRATOR_MFA_REFUSED
        )
        without_password = tara_client.post(
            url(tara, "change-email"),
            {"email": "tara.new@example.test", "version": 2},
            format="json",
        )
        assert without_password.status_code == 400
        changed = tara_client.post(
            url(tara, "change-email"),
            {"email": "tara.new@example.test", "version": 2, "current_password": DEFAULT_PASSWORD},
            format="json",
        )
        assert changed.status_code == 200, changed.content
        assert tara_client.get("/api/v1/auth/me").json()["email"] == "tara.new@example.test"
        tara.refresh_from_db()
        assert (tara.role, tara.status) == (Role.TEAM_ADMIN, UserStatus.ACTIVE)

    def test_a_member_who_leaves_the_team_is_out_of_reach_at_once(
        self, admin, tara_client, team_member
    ):
        services.update_user(
            actor_id=admin.pk, user_id=team_member.pk, version=1, changes={"manager": None}
        )
        assert tara_client.get(url(team_member)).status_code == 404
        with pytest.raises(NotFoundError):
            services.deactivate_user(
                actor_id=User.objects.get(first_name="Tara").pk, user_id=team_member.pk
            )


# --- the users list ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestTheList:
    def names(self, client, **params) -> set[str]:
        response = client.get(USERS, params)
        assert response.status_code == 200, response.content
        return {row["first_name"] for row in response.json()["results"]}

    def test_a_team_administrator_lists_herself_and_her_team(self, tara_client, tara, outsiders):
        InvitedUserFactory(first_name="Ishan", manager=tara)
        UserFactory(first_name="Gita", is_active=False, manager=tara)
        assert self.names(tara_client) == {"Tara", "Rahul", "Mohan", "Ishan", "Gita"}
        rows = {row["first_name"]: row for row in tara_client.get(USERS).json()["results"]}
        assert rows["Rahul"]["manager"] == {"id": str(tara.pk), "full_name": "Tara Lead"}
        assert rows["Tara"]["manager"] is None

    def test_filters_are_intersected_with_the_reach(self, tara_client, tara, outsiders):
        omar = outsiders["omar"]
        assert self.names(tara_client, manager=str(tara.pk)) == {"Rahul", "Mohan"}
        assert self.names(tara_client, manager=str(omar.pk)) == set()
        assert self.names(tara_client, manager="none") == set()
        assert self.names(tara_client, role="admin") == set()
        assert self.names(tara_client, role="team_admin") == {"Tara"}
        assert self.names(tara_client, q="priya") == set()
        assert self.names(tara_client, q="kavya") == set()

    def test_a_super_administrator_lists_and_filters_everyone(self, admin_client, tara, outsiders):
        assert self.names(admin_client) == {
            "Anita",
            "Tara",
            "Rahul",
            "Mohan",
            "Priya",
            "Omar",
            "Kavya",
        }
        assert self.names(admin_client, manager=str(tara.pk)) == {"Rahul", "Mohan"}
        assert self.names(admin_client, manager=str(outsiders["omar"].pk)) == {"Kavya"}
        # Users in no team: sales users only (an administrator is in none by definition).
        assert self.names(admin_client, manager="none") == {"Priya"}
        assert self.names(admin_client, role="team_admin") == {"Tara", "Omar"}
        rows = {row["first_name"]: row for row in admin_client.get(USERS).json()["results"]}
        assert rows["Kavya"]["manager"]["full_name"] == "Omar Khan"
        assert rows["Tara"]["role_label"] == "Admin"
        assert rows["Anita"]["role_label"] == "Super admin"

    @pytest.mark.parametrize(
        "manager", ["NONE", "nobody", "5a1e4d2c00004000800000000000abcd", "{" + MISSING + "}"]
    )
    def test_the_manager_filter_is_an_id_or_none(self, admin_client, manager):
        response = admin_client.get(USERS, {"manager": manager})
        assert response.status_code == 400
        assert "manager" in response.json()["error"]["details"]

    @pytest.mark.parametrize("who", ["anita", "tara"])
    def test_the_managers_names_cost_no_extra_query(
        self, who, admin, tara, django_assert_num_queries
    ):
        for _ in range(5):
            UserFactory(manager=tara)
        client = signed_in(admin if who == "anita" else tara)
        # session + signed-in user + the list (manager names joined) + its sign-in locks
        with django_assert_num_queries(4):
            response = client.get(USERS, {"page_size": 100})
        assert response.status_code == 200

    def test_a_cursor_is_bound_to_its_manager_filter(self, admin_client, tara):
        first = admin_client.get(USERS, {"page_size": 1, "manager": str(tara.pk)}).json()
        (cursor,) = parse_qs(urlsplit(first["next"]).query)["cursor"]
        same = admin_client.get(USERS, {"page_size": 1, "manager": str(tara.pk), "cursor": cursor})
        assert same.status_code == 200, same.content
        replay = admin_client.get(USERS, {"page_size": 1, "manager": "none", "cursor": cursor})
        assert replay.status_code == 400

    def test_security_events_stay_with_the_super_administrators(self, tara_client, admin_client):
        assert tara_client.get("/api/v1/admin/security-events").status_code == 403
        assert admin_client.get("/api/v1/admin/security-events").status_code == 200


# --- creating ---------------------------------------------------------------------------------
def new_user(**overrides: Any) -> dict[str, Any]:
    return {
        "first_name": "Neha",
        "last_name": "Verma",
        "email": "neha@example.test",
        "role": "sales_user",
        **overrides,
    }


@pytest.mark.django_db
class TestCreating:
    @pytest.mark.parametrize("manager", ["absent", "null", "herself"])
    def test_a_team_administrators_new_user_joins_her_team(self, tara_client, tara, manager):
        extra = {"absent": {}, "null": {"manager": None}, "herself": {"manager": str(tara.pk)}}
        response = tara_client.post(USERS, new_user(**extra[manager]), format="json")
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["manager"] == {"id": str(tara.pk), "full_name": "Tara Lead"}
        assert (body["role"], body["status"]) == ("sales_user", "invited")
        created = AuditEvent.objects.get(action=services.AUDIT_USER_CREATED)
        assert (created.actor_id, created.metadata["manager"]) == (tara.pk, str(tara.pk))
        token = AccountToken.objects.get(user_id=body["id"], purpose=TokenPurpose.INVITATION)
        assert token.created_by_id == tara.pk
        assert tara_client.get(url(body["id"])).status_code == 200

    def test_with_an_initial_password_too(self, tara_client, tara):
        response = tara_client.post(USERS, new_user(password=TEMPORARY), format="json")
        assert response.status_code == 201, response.content
        assert (response.json()["status"], response.json()["manager"]["id"]) == (
            "active",
            str(tara.pk),
        )

    @pytest.mark.parametrize("role", ["team_admin", "admin"])
    def test_a_team_administrator_creates_no_administrator(self, tara_client, role):
        before = everything()
        response = tara_client.post(USERS, new_user(role=role), format="json")
        assert error(response) == (
            403,
            "permission_denied",
            services.TEAM_ADMINISTRATORS_CREATE_USERS,
        )
        assert everything() == before

    @pytest.mark.parametrize("other", ["omar", "anita", "priya", "missing"])
    def test_nor_a_user_in_another_team(self, tara_client, outsiders, other):
        manager = MISSING if other == "missing" else str(outsiders[other].pk)
        before = everything()
        response = tara_client.post(USERS, new_user(manager=manager), format="json")
        assert error(response)[:2] == (403, "permission_denied")
        assert everything() == before

    def test_a_super_administrator_names_the_team(self, admin_client, tara, admin):
        response = admin_client.post(USERS, new_user(manager=str(tara.pk)), format="json")
        assert response.status_code == 201, response.content
        assert response.json()["manager"] == {"id": str(tara.pk), "full_name": "Tara Lead"}
        assert AuditEvent.objects.get(action=services.AUDIT_USER_CREATED).metadata == {
            "role": "sales_user",
            "activation": "invitation",
            "manager": str(tara.pk),
        }
        alone = admin_client.post(USERS, new_user(email="solo@example.test"), format="json")
        assert alone.json()["manager"] is None

    def test_an_invited_admin_may_have_a_team(self, admin_client, admin):
        invited = services.create_user(
            actor_id=admin.pk,
            email="ivy@example.test",
            first_name="Ivy",
            last_name="",
            role="team_admin",
        )
        response = admin_client.post(USERS, new_user(manager=str(invited.pk)), format="json")
        assert response.status_code == 201, response.content

    @pytest.mark.parametrize(
        "manager",
        ["deactivated admin", "super admin", "sales user", "missing", "pseudonymised admin"],
    )
    def test_the_team_must_be_an_active_or_invited_admin(self, admin_client, admin, manager):
        def pseudonymised():
            former = TeamAdminFactory(is_active=False)
            staff.pseudonymise(former.pk, operator_id=admin.pk, allow_administrator=True)
            return former

        target = {
            "deactivated admin": lambda: TeamAdminFactory(is_active=False),
            "super admin": AdminFactory,
            "sales user": UserFactory,
            "missing": lambda: None,
            "pseudonymised admin": pseudonymised,
        }[manager]()
        before = everything()
        response = admin_client.post(
            USERS, new_user(manager=MISSING if target is None else str(target.pk)), format="json"
        )
        assert response.status_code == 400, response.content
        assert response.json()["error"]["details"] == {"manager": [services.MANAGER_INVALID]}
        assert everything() == before

    @pytest.mark.parametrize("role", ["team_admin", "admin"])
    def test_only_a_user_has_a_team(self, admin_client, tara, role):
        response = admin_client.post(
            USERS, new_user(role=role, manager=str(tara.pk)), format="json"
        )
        assert response.status_code == 400
        assert response.json()["error"]["details"] == {"manager": [services.MANAGER_ONLY_FOR_USERS]}

    def test_admins_of_either_kind_are_invited(self, admin_client):
        for role in ("team_admin", "admin"):
            refused = admin_client.post(
                USERS, new_user(role=role, password=TEMPORARY), format="json"
            )
            assert refused.status_code == 400
            assert refused.json()["error"]["details"] == {
                "password": [services.ADMINISTRATOR_INVITED]
            }
        created = admin_client.post(USERS, new_user(role="team_admin"), format="json")
        assert created.status_code == 201, created.content
        assert (created.json()["status"], created.json()["role_label"]) == ("invited", "Admin")

    def test_unknown_fields_are_still_refused(self, admin_client, tara):
        response = admin_client.post(
            USERS, new_user(manager_id=str(tara.pk), team=str(tara.pk)), format="json"
        )
        assert response.status_code == 400
        assert not User.objects.filter(email="neha@example.test").exists()


# --- roles and teams (super administrators only) ----------------------------------------------
@pytest.mark.django_db
class TestRolesAndTeams:
    def patch(self, client, user: User, **changes: Any):
        user.refresh_from_db()
        return client.patch(url(user), {**changes, "version": user.version}, format="json")

    def test_a_team_administrator_changes_neither(self, tara_client, tara, team_member):
        before = everything()
        for change in ({"role": "team_admin"}, {"manager": None}, {"manager": str(tara.pk)}):
            assert error(self.patch(tara_client, team_member, **change))[:2] == (
                403,
                "permission_denied",
            )
        assert error(self.patch(tara_client, tara, role="admin"))[0] == 403
        assert everything() == before

    def test_moving_a_user_between_teams(self, admin_client, admin, tara, team_member, outsiders):
        omar = outsiders["omar"]
        epoch = team_member.session_epoch
        response = self.patch(admin_client, team_member, manager=str(omar.pk))
        assert response.status_code == 200, response.content
        assert response.json()["manager"] == {"id": str(omar.pk), "full_name": "Omar Khan"}
        assert response.json()["version"] == 2  # an administrator's edit, like any other
        team_member.refresh_from_db()
        assert team_member.session_epoch == epoch  # their own access didn't change
        event = AuditEvent.objects.get(action=services.AUDIT_MANAGER_CHANGED)
        assert (event.actor_id, event.metadata) == (
            admin.pk,
            {"from": str(tara.pk), "to": str(omar.pk)},
        )
        assert signed_in(tara).get(url(team_member)).status_code == 404
        assert signed_in(omar).get(url(team_member)).status_code == 200
        out = self.patch(admin_client, team_member, manager=None)
        assert out.json()["manager"] is None
        assert AuditEvent.objects.filter(action=services.AUDIT_MANAGER_CHANGED).count() == 2

    def test_a_move_is_shown_in_the_security_events(self, admin_client, team_member, tara):
        self.patch(admin_client, team_member, manager=None)
        events = admin_client.get("/api/v1/admin/security-events").json()["results"]
        assert events[0]["action"] == "user.manager_changed"
        assert events[0]["actor"]["full_name"] == "Anita Admin"
        assert events[0]["user"] == {"id": str(team_member.pk), "full_name": "Rahul Sharma"}
        # The teams are ids in the audit log, not details here (an allowlist of none).
        assert events[0]["details"] == {}
        assert services.AUDIT_MANAGER_CHANGED in selectors.SECURITY_ACTIONS

    def test_the_same_team_is_a_no_op(self, admin_client, tara, team_member):
        response = self.patch(admin_client, team_member, manager=str(tara.pk))
        assert response.json()["version"] == 1
        assert not AuditEvent.objects.exists()

    def test_only_a_user_joins_a_team(self, admin_client, tara, outsiders):
        omar = outsiders["omar"]
        for target in (tara, outsiders["anita"]):
            response = self.patch(admin_client, target, manager=str(omar.pk))
            assert response.status_code == 400
            assert response.json()["error"]["details"] == {
                "manager": [services.MANAGER_ONLY_FOR_USERS]
            }
        assert manager_of(tara) is None

    @pytest.mark.parametrize("manager", ["deactivated", "super admin", "sales user", "missing"])
    def test_the_team_must_be_an_active_or_invited_admin(
        self, admin_client, team_member, tara, manager
    ):
        target = {
            "deactivated": lambda: str(TeamAdminFactory(is_active=False).pk),
            "super admin": lambda: str(AdminFactory().pk),
            "sales user": lambda: str(UserFactory().pk),
            "missing": lambda: MISSING,
        }[manager]()
        before = everything()
        response = self.patch(admin_client, team_member, manager=target)
        assert response.status_code == 400
        assert response.json()["error"]["details"] == {"manager": [services.MANAGER_INVALID]}
        assert everything() == before

    def test_promoting_a_member_takes_them_out_of_the_team(
        self, admin_client, admin, tara, team_member
    ):
        epoch = team_member.session_epoch
        response = self.patch(admin_client, team_member, role="team_admin")
        assert response.status_code == 200, response.content
        assert (response.json()["role"], response.json()["manager"]) == ("team_admin", None)
        team_member.refresh_from_db()
        assert team_member.session_epoch == epoch + 1  # signed out everywhere (L-2)
        actions = list(AuditEvent.objects.order_by("id").values_list("action", "metadata"))
        assert actions == [
            (
                services.AUDIT_ROLE_CHANGED,
                {"from": "sales_user", "to": "team_admin", "revoked_invitations": 0},
            ),
            (services.AUDIT_MANAGER_CHANGED, {"from": str(tara.pk), "to": None}),
        ]

    def test_a_promotion_naming_a_team_is_refused_whole(self, admin_client, team_member, outsiders):
        before = everything()
        response = self.patch(
            admin_client, team_member, role="team_admin", manager=str(outsiders["omar"].pk)
        )
        assert response.status_code == 400
        assert everything() == before

    @pytest.mark.parametrize("new_role", ["sales_user", "admin"])
    def test_an_admin_who_stops_being_one_leaves_the_team_unassigned(
        self, admin_client, admin, tara, team_member, team_member_2, new_role
    ):
        invited = services.create_user(
            actor_id=tara.pk,
            email="ina@example.test",
            first_name="Ina",
            last_name="",
            role="sales_user",
        )
        tara_client = signed_in(tara)
        versions = {u.pk: u.version for u in (team_member, team_member_2)}
        response = self.patch(admin_client, tara, role=new_role)
        assert response.status_code == 200, response.content
        for member in (team_member, team_member_2, invited):
            member.refresh_from_db()
            assert member.manager_id is None
        assert team_member.version == versions[team_member.pk] + 1
        assert AccountToken.objects.get(user=invited).status == TokenStatus.REVOKED
        event = AuditEvent.objects.get(action=services.AUDIT_ROLE_CHANGED)
        assert event.metadata == {
            "from": "team_admin",
            "to": new_role,
            "revoked_invitations": 1,
            "unassigned_members": 3,
        }
        assert tara_client.get("/api/v1/auth/me").status_code == 401  # signed out (L-2)
        assert_teams_consistent()

    def test_demoted_and_moved_into_another_team_in_one_request(
        self, admin_client, tara, team_member, outsiders
    ):
        omar = outsiders["omar"]
        response = self.patch(admin_client, tara, role="sales_user", manager=str(omar.pk))
        assert response.status_code == 200, response.content
        assert response.json()["manager"]["id"] == str(omar.pk)
        assert manager_of(team_member) is None
        assert_teams_consistent()

    def test_never_into_their_own_team(self, admin_client, tara):
        response = self.patch(admin_client, tara, role="sales_user", manager=str(tara.pk))
        assert response.status_code == 400
        assert User.objects.get(pk=tara.pk).role == Role.TEAM_ADMIN

    def test_another_super_administrators_role_is_never_changed(self, admin_client):
        other = AdminFactory()
        for role in ("team_admin", "sales_user"):
            response = self.patch(admin_client, other, role=role)
            assert error(response) == (
                422,
                "business_rule_violation",
                services.ADMINISTRATOR_ROLE_REFUSED,
            )
        assert User.objects.get(pk=other.pk).role == Role.ADMIN

    def test_nobody_changes_their_own_role(self, admin_client, admin):
        response = self.patch(admin_client, admin, role="team_admin")
        assert error(response)[2] == "You can't change your own role."

    def test_an_admin_becomes_a_super_admin(self, admin_client, tara):
        response = self.patch(admin_client, tara, role="admin")
        assert (response.json()["role"], response.json()["role_label"]) == ("admin", "Super admin")

    @pytest.mark.parametrize("role", ["team_admin", "admin"])
    def test_promotion_guards_apply_to_either_administrator_role(self, admin_client, admin, role):
        given = UserFactory(password_change_required=True)
        assert error(self.patch(admin_client, given, role=role))[2] == (
            services.PROMOTION_REFUSED_UNTIL_OWN_CHOICE
        )
        changed = UserFactory()
        services.set_user_password(
            actor_id=admin.pk, user_id=changed.pk, version=1, new_password=TEMPORARY
        )
        User.objects.filter(pk=changed.pk).update(password_change_required=False)
        refused = self.patch(admin_client, changed, role=role)
        assert error(refused)[0] == 422
        assert "can't be made an administrator until" in error(refused)[2]

    def test_a_team_administrators_credential_change_shields_too(self, tara, team_member, admin):
        """Tara set Rahul's password: Rahul can't be promoted for the cooling-off either."""
        services.set_user_password(
            actor_id=tara.pk, user_id=team_member.pk, version=1, new_password=TEMPORARY
        )
        User.objects.filter(pk=team_member.pk).update(password_change_required=False)
        with pytest.raises(BusinessRuleViolation, match="can't be made an administrator"):
            services.update_user(
                actor_id=admin.pk, user_id=team_member.pk, version=2, changes={"role": "team_admin"}
            )

    def test_promotions_and_deactivations_of_admins_are_counted(self, admin, tara, user_b):
        def value(name: str) -> float:
            return {g.name: g for g in metrics.gauges()}[name].samples[0][1]

        services.update_user(
            actor_id=admin.pk, user_id=user_b.pk, version=1, changes={"role": "team_admin"}
        )
        services.deactivate_user(actor_id=admin.pk, user_id=tara.pk)
        assert value("arkray_administrator_promotions_last_hour") == 1
        assert value("arkray_administrator_deactivations_last_hour") == 1

    def test_the_service_refuses_a_team_administrator_too(self, tara, team_member):
        """Below the API (a future caller): the same rules."""
        with pytest.raises(PermissionDeniedError):
            services.update_user(
                actor_id=tara.pk, user_id=team_member.pk, version=1, changes={"manager": None}
            )
        with pytest.raises(PermissionDeniedError):
            services.create_user(
                actor_id=tara.pk,
                email="x@example.test",
                first_name="X",
                last_name="",
                role="sales_user",
                manager=uuid.uuid4(),
            )
        with pytest.raises(InvalidInputError):
            services.update_user(
                actor_id=tara.pk, user_id=team_member.pk, version=1, changes={"team": "x"}
            )


# --- administrators' credentials (ADR-0030, both kinds) and demote-then-unshield --------------
@pytest.mark.django_db
class TestAdministratorsCredentials:
    @pytest.mark.parametrize("status", ["active", "invited", "deactivated"])
    def test_a_team_administrators_credentials_are_hers_alone(self, admin_client, admin, status):
        tara = {
            "active": lambda: TeamAdminFactory(),
            "invited": lambda: InvitedUserFactory(role=Role.TEAM_ADMIN),
            "deactivated": lambda: TeamAdminFactory(is_active=False),
        }[status]()
        before = everything()
        email = admin_client.post(
            url(tara, "change-email"), {"email": "evil@example.test", "version": 1}, format="json"
        )
        assert error(email) == (
            422,
            "business_rule_violation",
            services.ADMINISTRATOR_EMAIL_REFUSED,
        )
        password = admin_client.post(
            url(tara, "set-password"), {"version": 1, "new_password": TEMPORARY}, format="json"
        )
        assert error(password)[2] == services.ADMINISTRATOR_ACCOUNT_REFUSED
        reset = admin_client.post(url(tara, "mfa/reset"))
        assert error(reset)[2] == mfa_services.ADMINISTRATOR_MFA_REFUSED
        assert everything() == before

    def test_but_she_is_renamed_deactivated_and_reactivated_without_her_password(
        self, admin_client, tara
    ):
        renamed = admin_client.patch(url(tara), {"last_name": "Rao", "version": 1}, format="json")
        assert renamed.status_code == 200
        assert admin_client.post(url(tara, "deactivate")).json()["status"] == "deactivated"
        assert admin_client.post(url(tara, "activate")).json()["status"] == "active"
        # R126 (ADR-0033): an administrator reactivated by another has no known password.
        from rest_framework.test import APIClient

        login = APIClient().post(
            "/api/v1/auth/login", {"email": tara.email, "password": DEFAULT_PASSWORD}, format="json"
        )
        assert login.status_code == 400
        assert AuditEvent.objects.get(action=services.AUDIT_USER_REACTIVATED).metadata == {
            "status": "active",
            "reset_link": True,
        }

    def test_deactivating_her_revokes_her_invitations_and_keeps_her_team(
        self, admin_client, tara, team_member
    ):
        invited = services.create_user(
            actor_id=tara.pk,
            email="ina@example.test",
            first_name="Ina",
            last_name="",
            role="sales_user",
        )
        assert admin_client.post(url(tara, "deactivate")).status_code == 200
        assert AccountToken.objects.get(user=invited).status == TokenStatus.REVOKED
        event = AuditEvent.objects.get(action=services.AUDIT_USER_DEACTIVATED)
        assert (event.metadata["role"], event.metadata["revoked_invitations"]) == ("team_admin", 1)
        assert manager_of(team_member) == tara.pk
        assert manager_of(invited) == tara.pk
        listed = admin_client.get(USERS, {"manager": str(tara.pk)}).json()["results"]
        assert {row["first_name"] for row in listed} == {"Rahul", "Mohan", "Ina"}

    def test_the_last_administrator_guard_counts_super_administrators_only(
        self, admin, tara, monkeypatch
    ):
        """With Anita the only super administrator, an active Admin doesn't count."""
        monkeypatch.setattr(services, "_acting_manager", lambda actor_id: tara)
        monkeypatch.setattr(services, "_is_super_administrator", lambda actor: True)
        with pytest.raises(BusinessRuleViolation, match="at least one active super admin"):
            services.deactivate_user(actor_id=tara.pk, user_id=admin.pk)

    def test_no_support_session_with_an_admin(self, admin_client, tara):
        response = admin_client.post(SUPPORT, {"user": str(tara.pk)}, format="json")
        assert error(response)[2] == support.NOT_FOR_ADMINS

    def test_no_pseudonymising_an_admin_through_the_api(self, admin_client, admin, tara):
        services.deactivate_user(actor_id=admin.pk, user_id=tara.pk)
        tara.refresh_from_db()
        response = admin_client.post(
            f"/api/v1/admin/privacy/users/{tara.pk}/pseudonymise",
            {"confirm_email": tara.email},
            format="json",
        )
        assert response.status_code == 403
        with pytest.raises(PermissionDeniedError):
            staff.pseudonymise(tara.pk, operator_id=admin.pk)
        assert not User.objects.get(pk=tara.pk).is_pseudonymised


@pytest.mark.django_db
class TestDemoteThenUnshield:
    """A super administrator could make Tara a user, then set her password (allowed for
    users) and sign in as her. For the cooling-off after another took away an administrator's
    role, the account's credentials stay its own."""

    def demote(self, admin, tara):
        return services.update_user(
            actor_id=admin.pk, user_id=tara.pk, version=1, changes={"role": "sales_user"}
        )

    def test_her_credentials_stay_hers_after_a_demotion(self, admin, tara):
        demoted = self.demote(admin, tara)
        client = signed_in(admin)
        before = everything()
        for response in (
            client.post(
                url(tara, "change-email"),
                {"email": "evil@example.test", "version": demoted.version},
                format="json",
            ),
            client.post(
                url(tara, "set-password"),
                {"version": demoted.version, "new_password": TEMPORARY},
                format="json",
            ),
            client.post(url(tara, "mfa/reset")),
        ):
            status, code, message = error(response)
            assert (status, code) == (422, "business_rule_violation")
            # Self-explanatory (the UI shows it as it is): why, and from when it is allowed.
            assert message == services._unshield_refused(eligible_from(tara))
            assert message.startswith("This account stopped being an administrator in the last 7")
            assert " until " in message
        assert everything() == before

    def test_whoever_asks_after_the_move(self, admin, tara, other_team_admin):
        """Moved into Omar's team, Tara is in his reach: still refused for him."""
        services.update_user(
            actor_id=admin.pk,
            user_id=tara.pk,
            version=1,
            changes={"role": "sales_user", "manager": other_team_admin.pk},
        )
        with pytest.raises(BusinessRuleViolation, match="stopped being an administrator"):
            services.set_user_password(
                actor_id=other_team_admin.pk, user_id=tara.pk, version=2, new_password=TEMPORARY
            )
        enrol(tara)
        with pytest.raises(BusinessRuleViolation, match="stopped being an administrator"):
            mfa_services.reset_for_user(actor_id=other_team_admin.pk, user_id=tara.pk)
        assert MfaDevice.objects.filter(user=tara).exists()

    def test_an_operators_demotion_counts(self, admin, tara):
        """The documented shell demotion records no actor: still another's action."""
        User.objects.filter(pk=tara.pk).update(role=Role.SALES_USER, version=2)
        audit.record(
            services.AUDIT_ROLE_CHANGED,
            actor_id=None,
            target_type="user",
            target_id=tara.pk,
            metadata={"from": "team_admin", "to": "sales_user", "revoked_invitations": 0},
        )
        with pytest.raises(BusinessRuleViolation, match="stopped being an administrator"):
            services.change_user_email(
                actor_id=admin.pk, user_id=tara.pk, version=2, email="evil@example.test"
            )

    def test_a_promotion_between_administrator_roles_is_no_demotion(self, admin, tara):
        services.update_user(
            actor_id=admin.pk, user_id=tara.pk, version=1, changes={"role": "admin"}
        )
        assert services._demotion_cooling_off_until(tara, timezone.now()) is None

    @override_settings(ADMIN_PROMOTION_COOLING_OFF_S=0)
    def test_after_the_cooling_off_she_is_a_user_like_any_other(self, admin, tara):
        demoted = self.demote(admin, tara)
        changed = services.set_user_password(
            actor_id=admin.pk, user_id=tara.pk, version=demoted.version, new_password=TEMPORARY
        )
        assert changed.password_change_required

    def test_an_ordinary_user_is_unaffected(self, admin, team_member):
        changed = services.set_user_password(
            actor_id=admin.pk, user_id=team_member.pk, version=1, new_password=TEMPORARY
        )
        assert changed.password_change_required


# --- temporary passwords go with the reach (security review of ADR-0035, P3) ------------------
def sign_in(email: str, password: str) -> int:
    from rest_framework.test import APIClient

    response = APIClient().post(
        "/api/v1/auth/login", {"email": email, "password": password}, format="json"
    )
    return int(response.status_code)


def voided(user: User) -> list[dict[str, Any]]:
    return list(
        AuditEvent.objects.filter(
            action=services.AUDIT_TEMPORARY_PASSWORD_VOIDED, target_id=str(user.pk)
        ).values("actor_id", "metadata")
    )


@pytest.mark.django_db
class TestTemporaryPasswordsGoWithTheReach:
    """An administrator who loses their reach over an account takes the temporary password
    they set with them, as their invitations: a random password nobody knows replaces it, the
    account's sessions end, and its owner is mailed a reset link at their own address."""

    def neha(self, tara_client) -> User:
        """Created by Tara with an initial (temporary) password."""
        created = tara_client.post(
            USERS,
            {
                "first_name": "Neha",
                "last_name": "Verma",
                "email": "neha@example.test",
                "role": "sales_user",
                "password": TEMPORARY,
            },
            format="json",
        )
        assert created.status_code == 201, created.content
        return User.objects.get(email="neha@example.test")

    @pytest.mark.parametrize("to", ["omar", "no team"])
    def test_moved_out_of_her_team(self, tara_client, tara, admin, admin_client, outsiders, to):
        neha = self.neha(tara_client)
        assert sign_in(neha.email, TEMPORARY) == 200  # it worked until now
        neha_client = signed_in(neha)
        neha.refresh_from_db()
        manager = str(outsiders["omar"].pk) if to == "omar" else None
        moved = admin_client.patch(
            url(neha), {"manager": manager, "version": neha.version}, format="json"
        )
        assert moved.status_code == 200, moved.content
        assert tara_client.get(url(neha)).status_code == 404
        assert sign_in(neha.email, TEMPORARY) == 400  # Tara's password no longer opens it
        assert neha_client.get("/api/v1/auth/me").status_code == 401  # sessions ended
        neha.refresh_from_db()
        assert (neha.password_change_required, neha.has_usable_password()) == (False, True)
        assert moved.json()["password_change_required"] is False
        assert voided(neha) == [
            {
                "actor_id": admin.pk,
                "metadata": {"reason": "team_moved", "set_by": str(tara.pk), "revoked_links": 0},
            }
        ]
        reset = AccountToken.objects.get(user=neha, purpose=TokenPurpose.PASSWORD_RESET)
        assert (reset.status, reset.created_by_id) == (TokenStatus.PENDING, admin.pk)
        mail.outbox.clear()
        drain_outbox()
        assert [m.to for m in mail.outbox] == [["neha@example.test"]]  # her own address only

    def test_a_password_someone_else_set_since_stays(self, tara, admin, team_member):
        services.set_user_password(
            actor_id=tara.pk, user_id=team_member.pk, version=1, new_password=TEMPORARY
        )
        again = "Another-Temporary-Pass-91!"
        services.set_user_password(
            actor_id=admin.pk, user_id=team_member.pk, version=2, new_password=again
        )
        services.update_user(
            actor_id=admin.pk, user_id=team_member.pk, version=3, changes={"manager": None}
        )
        assert sign_in(team_member.email, again) == 200  # Anita's: she still reaches him
        assert voided(team_member) == []

    def test_a_password_the_user_chose_since_stays(self, tara_client, admin):
        neha = self.neha(tara_client)
        client = signed_in(neha)
        changed = client.post(
            "/api/v1/auth/password/change",
            {"current_password": TEMPORARY, "new_password": "Nehas-Own-Choice-2026!"},
            format="json",
        )
        assert changed.status_code == 204, changed.content
        neha.refresh_from_db()
        services.update_user(
            actor_id=admin.pk, user_id=neha.pk, version=neha.version, changes={"manager": None}
        )
        assert sign_in(neha.email, "Nehas-Own-Choice-2026!") == 200
        assert voided(neha) == []

    def test_a_rename_or_the_same_team_voids_nothing(self, tara_client, tara, admin):
        """Renaming the user, or naming the team they are in already, changes no reach."""
        neha = self.neha(tara_client)
        services.update_user(
            actor_id=admin.pk,
            user_id=neha.pk,
            version=neha.version,
            changes={"last_name": "Rao", "manager": tara.pk},
        )
        assert sign_in(neha.email, TEMPORARY) == 200
        assert voided(neha) == []

    @pytest.mark.parametrize("how", ["demoted", "deactivated"])
    def test_her_demotion_or_deactivation(self, tara_client, tara, admin, team_member, how):
        neha = self.neha(tara_client)
        services.set_user_password(
            actor_id=tara.pk, user_id=team_member.pk, version=1, new_password=TEMPORARY
        )
        mohan = User.objects.get(first_name="Mohan")
        mohan_password = mohan.password
        if how == "demoted":
            services.update_user(
                actor_id=admin.pk, user_id=tara.pk, version=1, changes={"role": "sales_user"}
            )
        else:
            services.deactivate_user(actor_id=admin.pk, user_id=tara.pk)
        for account in (neha, team_member):
            assert sign_in(account.email, TEMPORARY) == 400
            (event,) = voided(account)
            assert event["metadata"]["reason"] == f"setter_{how}"
        assert User.objects.get(pk=mohan.pk).password == mohan_password  # nothing of hers

    def test_her_promotion_to_super_admin_keeps_her_reach(self, tara_client, tara, admin):
        neha = self.neha(tara_client)
        services.update_user(
            actor_id=admin.pk, user_id=tara.pk, version=1, changes={"role": "admin"}
        )
        assert sign_in(neha.email, TEMPORARY) == 200
        assert voided(neha) == []

    def test_a_super_administrator_deactivated_takes_hers_too(self, admin, user_b):
        bina = AdminFactory(first_name="Bina")
        services.set_user_password(
            actor_id=bina.pk, user_id=user_b.pk, version=1, new_password=TEMPORARY
        )
        services.deactivate_user(actor_id=admin.pk, user_id=bina.pk)
        assert sign_in(user_b.email, TEMPORARY) == 400
        assert voided(user_b)[0]["metadata"]["reason"] == "setter_deactivated"

    def test_a_deactivated_account_comes_back_without_it(self, tara_client, tara, admin):
        """Deactivated with Tara's password, then Tara is deactivated: reactivating the account
        must not bring her password back (no reset link while it is deactivated)."""
        neha = self.neha(tara_client)
        assert tara_client.post(url(neha, "deactivate")).status_code == 200
        services.deactivate_user(actor_id=admin.pk, user_id=tara.pk)
        assert voided(neha)[0]["metadata"]["reason"] == "setter_deactivated"
        assert not AccountToken.objects.filter(
            user=neha, purpose=TokenPurpose.PASSWORD_RESET
        ).exists()
        services.reactivate_user(actor_id=admin.pk, user_id=neha.pk)
        assert sign_in(neha.email, TEMPORARY) == 400

    def test_it_is_a_security_event(self, tara_client, admin_client):
        neha = self.neha(tara_client)
        neha.refresh_from_db()
        admin_client.patch(url(neha), {"manager": None, "version": neha.version}, format="json")
        events = admin_client.get("/api/v1/admin/security-events").json()["results"]
        (event,) = [e for e in events if e["action"] == "auth.temporary_password_voided"]
        assert (event["user"]["id"], event["details"]) == (str(neha.pk), {"reason": "team_moved"})


# --- promote-then-shield with teams (security review of ADR-0035, P3) -------------------------
@pytest.mark.django_db
class TestPromotingAccountsATeamAdministratorCreated:
    """Tara chooses the address of every account she creates: like an email change by an
    administrator, that keeps the account from being made an administrator for the
    cooling-off. And no invited account is promoted: its pending invitation goes to the
    address whoever invited it chose."""

    def zed(self, tara_client) -> User:
        created = tara_client.post(
            USERS,
            {"first_name": "Zed", "email": "tara.alt@example.test", "role": "sales_user"},
            format="json",
        )
        assert created.status_code == 201, created.content
        return User.objects.get(email="tara.alt@example.test")

    @pytest.mark.parametrize("role", ["team_admin", "admin"])
    @pytest.mark.parametrize("inviter", ["tara", "anita"])
    def test_an_invited_account_is_never_promoted(
        self, tara_client, admin, admin_client, role, inviter
    ):
        if inviter == "tara":
            zed = self.zed(tara_client)
        else:
            zed = services.create_user(
                actor_id=admin.pk,
                email="zed@example.test",
                first_name="Zed",
                last_name="",
                role="sales_user",
            )
        before = everything()
        response = admin_client.patch(url(zed), {"role": role, "version": 1}, format="json")
        assert error(response) == (
            422,
            "business_rule_violation",
            services.INVITED_PROMOTION_REFUSED,
        )
        assert everything() == before

    def test_the_creation_is_recorded_as_a_team_administrators(
        self, tara_client, tara, admin, admin_client
    ):
        self.zed(tara_client)
        admin_client.post(
            USERS,
            {"first_name": "Ann", "email": "ann@example.test", "role": "sales_user"},
            format="json",
        )
        by_actor = {
            event.actor_id: event.metadata.get("creator_scope")
            for event in AuditEvent.objects.filter(action=services.AUDIT_USER_CREATED)
        }
        assert by_actor == {tara.pk: "team", admin.pk: None}

    @pytest.mark.parametrize("activation", ["invitation", "password"])
    def test_an_account_she_created_waits_out_the_cooling_off(
        self, tara, admin, admin_client, activation
    ):
        created = services.create_user(
            actor_id=tara.pk,
            email="zed@example.test",
            first_name="Zed",
            last_name="",
            role="sales_user",
            password=TEMPORARY if activation == "password" else None,
        )
        # Active now, with a password of their own (the invitation accepted, or the temporary
        # password replaced).
        user = User.objects.get(pk=created.pk)
        user.set_password("Zeds-Own-Choice-2026!")
        user.status, user.is_active, user.activated_at = UserStatus.ACTIVE, True, timezone.now()
        user.password_change_required = False
        user.save()
        response = admin_client.patch(url(user), {"role": "admin", "version": 1}, format="json")
        assert error(response)[0] == 422
        assert "can't be made an administrator until" in error(response)[2]
        assert User.objects.get(pk=user.pk).role == Role.SALES_USER

    @override_settings(ADMIN_PROMOTION_COOLING_OFF_S=0)
    def test_after_the_cooling_off_she_is_promoted(self, tara, admin):
        created = services.create_user(
            actor_id=tara.pk,
            email="zed@example.test",
            first_name="Zed",
            last_name="",
            role="sales_user",
            password=TEMPORARY,
        )
        User.objects.filter(pk=created.pk).update(password_change_required=False)
        promoted = services.update_user(
            actor_id=admin.pk, user_id=created.pk, version=1, changes={"role": "team_admin"}
        )
        assert promoted.role == Role.TEAM_ADMIN

    def test_an_account_a_super_administrator_invited_is_promoted_once_active(
        self, admin, admin_client
    ):
        created = services.create_user(
            actor_id=admin.pk,
            email="ann@example.test",
            first_name="Ann",
            last_name="",
            role="sales_user",
        )
        user = User.objects.get(pk=created.pk)
        user.set_password("Anns-Own-Choice-2026!")
        user.status, user.is_active, user.activated_at = UserStatus.ACTIVE, True, timezone.now()
        user.save()
        response = admin_client.patch(
            url(user), {"role": "team_admin", "version": 1}, format="json"
        )
        assert response.status_code == 200, response.content


# --- support sessions -------------------------------------------------------------------------
@pytest.mark.django_db
class TestSupportSessions:
    def start(self, client, user: Any):
        ref = user if isinstance(user, str) else str(user.pk)
        return client.post(SUPPORT, {"user": ref, "reason": "ticket 9"}, format="json")

    def test_with_a_member(self, tara_client, tara, team_member):
        response = self.start(tara_client, team_member)
        assert response.status_code == 201, response.content
        assert tara_client.get(f"/api/v1/workspaces/{team_member.pk}/leads").status_code == 200
        for workspace in ("me", "all"):
            refused = tara_client.get(f"/api/v1/workspaces/{workspace}/leads")
            assert error(refused)[1] == "support_session_active"
        assert tara_client.delete(f"{SUPPORT}/current").status_code == 204

    @pytest.mark.parametrize("who", ["anita", "priya", "omar", "kavya", "missing", "herself"])
    def test_with_anyone_else_it_is_the_404_of_nobody(self, tara_client, tara, outsiders, who):
        target = {"missing": MISSING, "herself": tara}.get(who) or outsiders[who]
        response = self.start(tara_client, target)
        assert response.status_code == 404
        assert without_request_id(response) == without_request_id(self.start(tara_client, MISSING))
        assert not SupportSession.objects.exists()
        assert not AuditEvent.objects.filter(action=support.AUDIT_STARTED).exists()

    def test_a_deactivated_member_is_in_reach_but_cant_sign_in(self, tara_client, tara):
        gone = UserFactory(is_active=False, manager=tara)
        assert error(self.start(tara_client, gone))[2] == support.NOT_ACTIVE_USER

    def test_it_ends_when_the_member_leaves_the_team(self, admin, tara_client, tara, team_member):
        assert self.start(tara_client, team_member).status_code == 201
        services.update_user(
            actor_id=admin.pk, user_id=team_member.pk, version=1, changes={"manager": None}
        )
        assert tara_client.get("/api/v1/auth/me").json()["support_session"] is None
        session = SupportSession.objects.get()
        assert session.end_reason == SupportEnd.NOT_ALLOWED
        ended = AuditEvent.objects.get(action=support.AUDIT_ENDED)
        assert (ended.actor_id, ended.metadata["end"]) == (None, "not_allowed")
        assert tara_client.get(f"/api/v1/workspaces/{team_member.pk}/leads").status_code == 404

    def test_it_ends_when_she_stops_being_an_admin(self, tara_client, tara, team_member):
        """A role change behind the services' back (no sign-out): the capability re-check
        ends it on the next request."""
        assert self.start(tara_client, team_member).status_code == 201
        User.objects.filter(pk=tara.pk).update(role=Role.SALES_USER)
        tara_client.get("/api/v1/auth/me")
        assert SupportSession.objects.get().end_reason == SupportEnd.NOT_ALLOWED

    def test_a_super_administrator_still_supports_anyone_in_a_team(
        self, admin_client, team_member, other_team_member
    ):
        for target in (team_member, other_team_member):
            assert self.start(admin_client, target).status_code == 201
            assert admin_client.delete(f"{SUPPORT}/current").status_code == 204

    def test_a_member_cant(self, team_member, team_member_2):
        assert self.start(signed_in(team_member), team_member_2).status_code == 403


# --- owner pickers ----------------------------------------------------------------------------
@pytest.mark.django_db
class TestAssignees:
    def names(self, client) -> set[str]:
        response = client.get("/api/v1/assignees")
        assert response.status_code == 200, response.content
        return {row["full_name"] for row in response.json()["results"]}

    def test_a_team_administrator_sees_herself_and_her_active_members(
        self, tara_client, tara, outsiders
    ):
        UserFactory(first_name="Gita", last_name="Gone", is_active=False, manager=tara)
        InvitedUserFactory(first_name="Ishan", last_name="New", manager=tara)
        assert self.names(tara_client) == {"Tara Lead", "Rahul Sharma", "Mohan Das"}

    def test_a_super_administrator_sees_every_active_user(self, admin_client, tara, outsiders):
        assert self.names(admin_client) == {
            "Anita Admin",
            "Tara Lead",
            "Rahul Sharma",
            "Mohan Das",
            "Priya Patel",
            "Omar Khan",
            "Kavya Iyer",
        }

    def test_a_member_cant_ask(self, team_member):
        assert signed_in(team_member).get("/api/v1/assignees").status_code == 403


# --- races ------------------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
class TestRaces:
    """Real concurrency (own connections): the user-administration lock serialises every
    change of a role, a team or an account, and each re-reads what it decides on."""

    def cast(self):
        first, second = AdminFactory(), AdminFactory()
        tara = TeamAdminFactory(first_name="Tara")
        rahul = UserFactory(first_name="Rahul")
        return first, second, tara, rahul

    def test_a_move_into_a_team_waiting_on_its_admins_demotion_is_refused(self):
        first, second, tara, rahul = self.cast()
        results = _hold_then(
            lambda: services.update_user(
                actor_id=first.pk, user_id=tara.pk, version=1, changes={"role": "sales_user"}
            ),
            lambda: services.update_user(
                actor_id=second.pk, user_id=rahul.pk, version=1, changes={"manager": tara.pk}
            ),
        )
        assert isinstance(results[0], User), results[0]
        assert isinstance(results[1], InvalidInputError), results[1]
        assert manager_of(rahul) is None
        assert_teams_consistent()

    def test_a_demotion_waiting_on_a_move_into_the_team_unassigns_it(self):
        first, second, tara, rahul = self.cast()
        results = _hold_then(
            lambda: services.update_user(
                actor_id=second.pk, user_id=rahul.pk, version=1, changes={"manager": tara.pk}
            ),
            lambda: services.update_user(
                actor_id=first.pk, user_id=tara.pk, version=1, changes={"role": "sales_user"}
            ),
        )
        assert all(isinstance(r, User) for r in results), results
        assert manager_of(rahul) is None
        event = AuditEvent.objects.get(action=services.AUDIT_ROLE_CHANGED)
        assert event.metadata["unassigned_members"] == 1
        assert_teams_consistent()

    def test_racing_demotions_and_moves_never_leave_a_user_with_a_non_admin_manager(self):
        for _ in range(4):
            first, second, tara, rahul = self.cast()
            results = run_concurrently(
                lambda f=first, t=tara: services.update_user(
                    actor_id=f.pk, user_id=t.pk, version=1, changes={"role": "sales_user"}
                ),
                lambda s=second, r=rahul, t=tara: services.update_user(
                    actor_id=s.pk, user_id=r.pk, version=1, changes={"manager": t.pk}
                ),
            )
            for result in results:
                assert isinstance(result, User | InvalidInputError), repr(result)
            assert_teams_consistent()

    def test_a_team_administrator_creating_a_user_while_being_demoted(self):
        first, _, tara, _ = self.cast()
        demoted_first = _hold_then(
            lambda: services.update_user(
                actor_id=first.pk, user_id=tara.pk, version=1, changes={"role": "sales_user"}
            ),
            lambda: services.create_user(
                actor_id=tara.pk,
                email="a@example.test",
                first_name="A",
                last_name="",
                role="sales_user",
            ),
        )
        assert isinstance(demoted_first[1], PermissionDeniedError), demoted_first[1]
        assert not User.objects.filter(email="a@example.test").exists()

        tara_2 = TeamAdminFactory()
        created_first = _hold_then(
            lambda: services.create_user(
                actor_id=tara_2.pk,
                email="b@example.test",
                first_name="B",
                last_name="",
                role="sales_user",
            ),
            lambda: services.update_user(
                actor_id=first.pk, user_id=tara_2.pk, version=1, changes={"role": "sales_user"}
            ),
        )
        assert all(isinstance(r, User) for r in created_first), created_first
        created = User.objects.get(email="b@example.test")
        assert created.manager_id is None  # unassigned with the rest of the team
        assert AccountToken.objects.get(user=created).status == TokenStatus.REVOKED
        assert_teams_consistent()

    def test_a_team_administrator_acting_on_a_member_being_moved_out(self):
        first, _, tara, rahul = self.cast()
        User.objects.filter(pk=rahul.pk).update(manager=tara)
        moved_first = _hold_then(
            lambda: services.update_user(
                actor_id=first.pk, user_id=rahul.pk, version=1, changes={"manager": None}
            ),
            lambda: services.set_user_password(
                actor_id=tara.pk, user_id=rahul.pk, version=2, new_password=TEMPORARY
            ),
        )
        assert isinstance(moved_first[0], User), moved_first[0]
        assert isinstance(moved_first[1], NotFoundError), moved_first[1]
        assert not User.objects.get(pk=rahul.pk).password_change_required

    def test_a_demotion_and_a_password_set_on_the_demoted_admin_both_orders_refused(self):
        first, second, tara, _ = self.cast()
        demotion_first = _hold_then(
            lambda: services.update_user(
                actor_id=first.pk, user_id=tara.pk, version=1, changes={"role": "sales_user"}
            ),
            lambda: services.set_user_password(
                actor_id=second.pk, user_id=tara.pk, version=2, new_password=TEMPORARY
            ),
        )
        assert isinstance(demotion_first[1], BusinessRuleViolation), demotion_first[1]
        tara_2 = TeamAdminFactory()
        password_first = _hold_then(
            lambda: services.set_user_password(
                actor_id=second.pk, user_id=tara_2.pk, version=1, new_password=TEMPORARY
            ),
            lambda: services.update_user(
                actor_id=first.pk, user_id=tara_2.pk, version=1, changes={"role": "sales_user"}
            ),
        )
        assert isinstance(password_first[0], BusinessRuleViolation), password_first[0]
        for account in (tara, tara_2):
            assert not User.objects.get(pk=account.pk).password_change_required

    def test_a_temporary_password_racing_a_move_out_never_outlives_it(self):
        """Tara sets Rahul's password while Anita moves him out of her team: the move waits
        for the lock and voids the password Tara set (the other order: tara gets a 404,
        test_a_team_administrator_acting_on_a_member_being_moved_out)."""
        first, _, tara, rahul = self.cast()
        User.objects.filter(pk=rahul.pk).update(manager=tara)
        results = _hold_then(
            lambda: services.set_user_password(
                actor_id=tara.pk, user_id=rahul.pk, version=1, new_password=TEMPORARY
            ),
            lambda: services.update_user(
                actor_id=first.pk, user_id=rahul.pk, version=2, changes={"manager": None}
            ),
        )
        assert all(isinstance(r, User) for r in results), results
        assert sign_in(rahul.email, TEMPORARY) == 400
        assert not User.objects.get(pk=rahul.pk).password_change_required


# --- no deadlock with the team's CRM writes (domain review of ADR-0035, P1) -------------------
def _waits_on_a_lock(pid: int, timeout_s: float = 10) -> bool:
    """Whether backend `pid` is seen waiting for a lock within `timeout_s` (polled from a
    connection of its own, outside any transaction: pg_stat_activity is a per-transaction
    snapshot)."""
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [pid])
                row = cursor.fetchone()
            if row is not None and row[0] == "Lock":
                return True
            time.sleep(0.02)
        return False
    finally:
        connections.close_all()


def _create_for_a_member(tara, rahul, pause):
    with subscribed(LeadCreated, pause):
        return lead_services.create_lead(
            actor=tara,
            scope=teams.team_scope(tara),
            fields={"first_name": "Asha", "organization_name": "Apollo"},
            owner_id=rahul.pk,
        )


def _create_in_a_members_workspace(tara, rahul, pause):
    with subscribed(LeadCreated, pause):
        return lead_services.create_lead(
            actor=tara,
            scope=AccessScope.for_user(tara.pk, rahul.pk),
            fields={"first_name": "Asha", "organization_name": "Apollo"},
        )


def _assign_to_a_member(tara, rahul, pause):
    lead = LeadFactory(owner=tara)
    with subscribed(LeadReassigned, pause):
        return lead_services.reassign_lead(
            actor=tara,
            scope=teams.team_scope(tara),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=rahul.pk,
        )


def _note_on_a_members_customer(tara, rahul, pause):
    lead = LeadFactory(owner=rahul)
    with subscribed(ActivityCreated, pause):
        return activity_services.create_activity(
            actor=tara,
            scope=teams.team_scope(tara),
            activity_type="note",
            fields={"description": "Called the lab head."},
            lead_id=lead.pk,
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
class TestNoDeadlockWithTheTeamsCrmWrites:
    """Tara's CRM write for Rahul holds FOR SHARE on Rahul's row (the team and assignability
    checks) and, at its commit, takes KEY SHARE on her own row (the deferred foreign key of
    `created_by`); Anita's change of Tara's role locks Tara's row, then updates Rahul's to
    leave the team unassigned. With Tara's row locked FOR UPDATE the two waited for each
    other (`deadlock detected`, a 500 for one of them); user administration locks user rows
    FOR NO KEY UPDATE, which the key check doesn't conflict with: the role change waits for
    the write, then clears the team."""

    def race(self, crm_write, role):
        anita, tara = AdminFactory(), TeamAdminFactory()
        rahul = UserFactory(manager=tara)
        UserFactory(manager=tara)  # Mohan
        tara = User.objects.get(pk=tara.pk)
        results = {}
        role_change_pid = []
        role_change_started = threading.Event()
        role_change_waited = threading.Event()
        write_holds_its_locks = threading.Event()

        def pause(_event: object) -> None:
            # Inside the write's transaction, its share lock on Rahul's row taken: let the role
            # change run into it, then commit.
            write_holds_its_locks.set()
            assert role_change_started.wait(10)

            def watch() -> None:  # its own thread: its own connection
                if _waits_on_a_lock(role_change_pid[0]):
                    role_change_waited.set()

            watcher = threading.Thread(target=watch)
            watcher.start()
            watcher.join(15)

        def write() -> None:
            try:
                results["write"] = crm_write(tara, rahul, pause)
            except BaseException as exc:
                results["write"] = exc
            finally:
                write_holds_its_locks.set()
                connections.close_all()

        def change_role() -> None:
            try:
                assert write_holds_its_locks.wait(10)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    role_change_pid.append(cursor.fetchone()[0])
                role_change_started.set()
                results["role"] = services.update_user(
                    actor_id=anita.pk, user_id=tara.pk, version=tara.version, changes={"role": role}
                )
            except BaseException as exc:
                results["role"] = exc
            finally:
                role_change_started.set()
                connections.close_all()

        threads = [threading.Thread(target=write), threading.Thread(target=change_role)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(40)
        return results.get("write"), results.get("role"), role_change_waited.is_set()

    @pytest.mark.parametrize("role", ["sales_user", "admin"])
    @pytest.mark.parametrize(
        "crm_write",
        [_create_for_a_member, _create_in_a_members_workspace, _assign_to_a_member],
        ids=["create-for-a-member", "create-in-a-members-workspace", "assign-to-a-member"],
    )
    def test_a_role_change_waits_for_the_write_and_both_succeed(self, crm_write, role):
        written, changed, waited = self.race(crm_write, role)
        assert not isinstance(written, BaseException), repr(written)
        assert not isinstance(changed, BaseException), repr(changed)
        assert waited, "the role change never waited for the write: nothing was raced"
        assert_teams_consistent()
        assert not User.objects.filter(manager__isnull=False).exists()  # the team was cleared

    def test_a_note_on_a_members_customer(self):
        written, changed, _ = self.race(_note_on_a_members_customer, "sales_user")
        assert not isinstance(written, BaseException), repr(written)
        assert not isinstance(changed, BaseException), repr(changed)
        assert_teams_consistent()
