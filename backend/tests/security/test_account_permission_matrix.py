"""Who may reset a password or delete an account, and whose (ADR-0036).

The table is the contract of the two account operations and of the preview that explains the
deletion, for every kind of caller against every kind of target. The cast (root conftest style):

- Anita and Bina are Super admins; Tara and Omar are Admins, each with a team (Tara: Rahul,
  Gita who is deactivated, Ishan who is invited; Omar: Kavya, Kiran, Nina likewise); Priya is in
  no team; Dora is a deactivated and Ivy an invited Admin.
- A Super admin may act on every User and every Admin (the account must be active to be reset),
  never on a Super admin or on themselves. An Admin acts only on the Users of their own team and
  can't tell an account outside it from one that doesn't exist (404 first, whatever its status).
  A User or an anonymous caller is refused before any target is looked at (403 / 401).
- Whatever is refused changes nothing: no row of any account, no token, no audit event, no queued
  mail. Whatever is allowed changes the target (and, deleting an Admin, their team) and nobody
  else.

Around the table: bodies and query strings can't redirect or widen an operation, the methods are
the declared ones, the gates every administrator route has (support session, second factor,
forced password change, CSRF, throttle) apply, and a role, a team or a status that changed a
moment ago is read on the request that follows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache
from django.db import transaction
from rest_framework.settings import api_settings
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core.errors import PermissionDeniedError
from arkray.core.models import OutboxEvent
from arkray.identity import mfa_services, services
from arkray.identity.models import (
    AccountToken,
    MfaDevice,
    Role,
    SupportSession,
    TokenPurpose,
    TokenStatus,
    User,
    UserStatus,
)
from arkray.privacy import staff
from tests.factories import AdminFactory, InvitedUserFactory, TeamAdminFactory, UserFactory
from tests.helpers import signed_in, without_request_id
from tests.mfa_helpers import enrol

pytestmark = pytest.mark.django_db

USERS = "/api/v1/admin/users"
SUPPORT = "/api/v1/admin/support-sessions"
MISSING = uuid.UUID("5a1e4d2c-0000-4000-8000-00000000abcd")
ACTIONS = ["reset", "delete", "preview"]

Outcome = tuple[int, str | None]
OK: Outcome = (200, None)
NOT_FOUND: Outcome = (404, "not_found")
FORBIDDEN: Outcome = (403, "permission_denied")
RULE: Outcome = (422, "business_rule_violation")
PSEUDONYMISED: Outcome = (422, "account_pseudonymised")
UNAUTHENTICATED: Outcome = (401, "not_authenticated")

TARGETS = [
    "another_super_admin",
    "self",
    "admin",
    "deactivated_admin",
    "invited_admin",
    "team_user",
    "team_deactivated_user",
    "team_invited_user",
    "other_team_user",
    "other_team_deactivated_user",
    "other_team_invited_user",
    "unassigned_user",
    "pseudonymised_team_user",
    "pseudonymised_unassigned_user",
    "nobody",
]
CALLERS = ["anita", "tara", "rahul", "anonymous"]

# (reset, delete, preview) per target.
EXPECTED: dict[str, dict[str, tuple[Outcome, Outcome, Outcome]]] = {
    # Anita, a Super admin.
    "anita": {
        "another_super_admin": (FORBIDDEN, FORBIDDEN, OK),
        "self": (RULE, RULE, OK),
        "admin": (OK, OK, OK),
        "deactivated_admin": (RULE, OK, OK),
        "invited_admin": (RULE, OK, OK),
        "team_user": (OK, OK, OK),
        "team_deactivated_user": (RULE, OK, OK),
        "team_invited_user": (RULE, OK, OK),
        "other_team_user": (OK, OK, OK),
        "other_team_deactivated_user": (RULE, OK, OK),
        "other_team_invited_user": (RULE, OK, OK),
        "unassigned_user": (OK, OK, OK),
        "pseudonymised_team_user": (PSEUDONYMISED, PSEUDONYMISED, OK),
        "pseudonymised_unassigned_user": (PSEUDONYMISED, PSEUDONYMISED, OK),
        "nobody": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
    },
    # Tara, an Admin: her team, her own account (to be told why not), and nobody else.
    "tara": {
        "another_super_admin": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "self": (RULE, RULE, OK),
        "admin": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "deactivated_admin": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "invited_admin": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "team_user": (OK, OK, OK),
        "team_deactivated_user": (RULE, OK, OK),
        "team_invited_user": (RULE, OK, OK),
        "other_team_user": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "other_team_deactivated_user": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "other_team_invited_user": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "unassigned_user": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "pseudonymised_team_user": (PSEUDONYMISED, PSEUDONYMISED, OK),
        "pseudonymised_unassigned_user": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
        "nobody": (NOT_FOUND, NOT_FOUND, NOT_FOUND),
    },
    # Rahul, an ordinary user: refused before any target is read, one that exists or not.
    "rahul": dict.fromkeys(TARGETS, (FORBIDDEN, FORBIDDEN, FORBIDDEN)),
    "anonymous": dict.fromkeys(TARGETS, (UNAUTHENTICATED, UNAUTHENTICATED, UNAUTHENTICATED)),
}

# Which attribute of the cast each target name stands for ("self" and the rest: see target_named).
ATTRIBUTE = {
    "another_super_admin": "bina",
    "admin": "omar",
    "deactivated_admin": "dora",
    "invited_admin": "ivy",
    "team_user": "rahul",
    "team_deactivated_user": "gita",
    "team_invited_user": "ishan",
    "other_team_user": "kavya",
    "other_team_deactivated_user": "kiran",
    "other_team_invited_user": "nina",
    "unassigned_user": "priya",
}


@dataclass
class Cast:
    anita: User
    bina: User
    tara: User
    omar: User
    dora: User
    ivy: User
    rahul: User
    gita: User
    ishan: User
    kavya: User
    kiran: User
    nina: User
    priya: User


@pytest.fixture
def cast(db):
    tara = TeamAdminFactory(first_name="Tara", last_name="Lead")
    omar = TeamAdminFactory(first_name="Omar", last_name="Khan")
    return Cast(
        anita=AdminFactory(first_name="Anita", last_name="Admin"),
        bina=AdminFactory(first_name="Bina", last_name="Admin"),
        tara=tara,
        omar=omar,
        dora=TeamAdminFactory(first_name="Dora", last_name="Gone", is_active=False),
        ivy=InvitedUserFactory(role=Role.TEAM_ADMIN, first_name="Ivy", last_name="Invited"),
        rahul=UserFactory(first_name="Rahul", last_name="Sharma", manager=tara),
        gita=UserFactory(first_name="Gita", last_name="Gone", is_active=False, manager=tara),
        ishan=InvitedUserFactory(first_name="Ishan", last_name="New", manager=tara),
        kavya=UserFactory(first_name="Kavya", last_name="Iyer", manager=omar),
        kiran=UserFactory(first_name="Kiran", last_name="Gone", is_active=False, manager=omar),
        nina=InvitedUserFactory(first_name="Nina", last_name="New", manager=omar),
        priya=UserFactory(first_name="Priya", last_name="Patel"),
    )


def pseudonymised(cast, manager=None):
    """A former user, erased by a Super admin through the privacy command's own service."""
    former = UserFactory(is_active=False, manager=manager)
    staff.pseudonymise(former.pk, operator_id=cast.anita.pk)
    former.refresh_from_db()
    return former


def caller_of(cast: Cast, name: str) -> tuple[User | None, APIClient]:
    if name == "anonymous":
        return None, APIClient()
    user = {"anita": cast.anita, "tara": cast.tara, "rahul": cast.rahul}[name]
    return user, signed_in(user)


def target_named(cast: Cast, name: str, caller: User | None) -> Any:
    if name == "self":
        return caller if caller is not None else cast.priya
    if name == "nobody":
        return SimpleNamespace(pk=MISSING, version=1, email="nobody@example.test")
    if name == "pseudonymised_team_user":
        return pseudonymised(cast, manager=cast.tara)
    if name == "pseudonymised_unassigned_user":
        return pseudonymised(cast)
    return getattr(cast, ATTRIBUTE[name])


def send(client: APIClient, action: str, target: Any, **extra: Any):
    base = f"{USERS}/{target.pk}"
    if action == "reset":
        return client.post(f"{base}/reset-password", extra, format="json")
    if action == "delete":
        body = {"version": target.version, "confirm_email": target.email, **extra}
        return client.post(f"{base}/delete", body, format="json")
    return client.get(f"{base}/deletion-preview")


def error_code(response) -> str | None:
    try:
        return str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None


def error_message(response) -> str:
    return str(response.json()["error"]["message"])


def snapshot() -> dict[str, Any]:
    """Everything these operations write: every account (credentials and standing), the
    account tokens, the second factors, and the counts of audit events, queued work and support
    sessions."""
    users = {
        row["id"]: row
        for row in User.objects.values(
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "status",
            "is_active",
            "manager_id",
            "version",
            "session_epoch",
            "password",
            "password_change_required",
            "pseudonymised_at",
            "activated_at",
            "deactivated_at",
        )
    }
    return {
        "users": users,
        "tokens": sorted(
            AccountToken.objects.values_list("id", "user_id", "purpose", "status", "token_hash"),
            key=repr,
        ),
        "devices": sorted(MfaDevice.objects.values_list("id", "user_id"), key=repr),
        "audit": AuditEvent.objects.count(),
        "audit_last": AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0,
        "outbox": OutboxEvent.objects.count(),
        "support": SupportSession.objects.count(),
    }


def changed_users(before: dict[str, Any], after: dict[str, Any]) -> set[uuid.UUID]:
    return {pk for pk, row in after["users"].items() if before["users"].get(pk) != row}


def audit_actions_since(before: dict[str, Any]) -> list[str]:
    return sorted(
        AuditEvent.objects.filter(id__gt=before["audit_last"]).values_list("action", flat=True)
    )


# --- the table ---------------------------------------------------------------------------------
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("target_name", TARGETS)
@pytest.mark.parametrize("caller_name", CALLERS)
def test_the_table(cast, caller_name, target_name, action):
    caller, client = caller_of(cast, caller_name)
    target = target_named(cast, target_name, caller)
    expected = EXPECTED[caller_name][target_name]
    status, code = expected[ACTIONS.index(action)]
    before = snapshot()

    response = send(client, action, target)

    assert (response.status_code, error_code(response)) == (status, code), response.content
    after = snapshot()
    if status != 200 or action == "preview":
        assert after == before  # refused, or only read: nothing was written anywhere
    elif action == "reset":
        assert caller is not None
        assert changed_users(before, after) == {target.pk}
        row, previous = after["users"][target.pk], before["users"][target.pk]
        assert row["version"] == previous["version"] + 1
        assert row["session_epoch"] == previous["session_epoch"] + 1
        assert row["password"] != previous["password"]
        assert (row["role"], row["manager_id"], row["email"]) == (
            previous["role"],
            previous["manager_id"],
            previous["email"],
        )
        pending = AccountToken.objects.filter(
            user_id=target.pk, purpose=TokenPurpose.PASSWORD_RESET, status=TokenStatus.PENDING
        )
        assert [token.created_by_id for token in pending] == [caller.pk]
        assert audit_actions_since(before) == [
            "auth.password_reset_by_admin",
            "auth.password_reset_issued",
        ]
        assert after["outbox"] == before["outbox"] + 2  # the link's email, the owner's notice
    else:
        assert caller is not None
        members = {  # an Admin's team is released with them
            pk for pk, row in before["users"].items() if row["manager_id"] == target.pk
        }
        assert changed_users(before, after) <= {target.pk} | members
        erased = User.objects.get(pk=target.pk)
        assert (erased.pseudonymised_at is not None, erased.status) == (
            True,
            UserStatus.DEACTIVATED,
        )
        assert not erased.has_usable_password()
        deleted = AuditEvent.objects.get(action="user.deleted")
        assert (deleted.actor_id, deleted.target_id) == (caller.pk, str(target.pk))
        assert all(User.objects.get(pk=pk).manager_id is None for pk in members)

    if action == "preview" and status == 200:
        body = response.json()
        assert set(body) == {
            "leads",
            "open_deals",
            "open_activities",
            "personal_pipelines",
            "team_members",
            "legal_hold",
            "needs_new_owner",
            "blocked_reason",
        }
        deletable = expected[ACTIONS.index("delete")] == OK
        assert (body["blocked_reason"] is None) == deletable, body  # it says why when it won't


# --- each refusal says why, in the words the screens show ----------------------------------------
@pytest.mark.parametrize(
    ("caller_name", "action", "target_name", "status", "message"),
    [
        ("anita", "reset", "another_super_admin", 403, services.SUPER_ADMIN_ACCOUNT_REFUSED),
        ("anita", "delete", "another_super_admin", 403, services.SUPER_ADMIN_ACCOUNT_REFUSED),
        ("anita", "reset", "self", 422, services.OWN_ACCOUNT_REFUSED),
        ("anita", "delete", "self", 422, services.DELETE_OWN_ACCOUNT_REFUSED),
        ("tara", "reset", "self", 422, services.OWN_ACCOUNT_REFUSED),
        ("tara", "delete", "self", 422, services.DELETE_OWN_ACCOUNT_REFUSED),
        ("anita", "reset", "deactivated_admin", 422, services.RESET_INACTIVE_REFUSED),
        ("anita", "reset", "invited_admin", 422, services.RESET_INACTIVE_REFUSED),
        ("anita", "reset", "team_deactivated_user", 422, services.RESET_INACTIVE_REFUSED),
        ("tara", "reset", "team_invited_user", 422, services.RESET_INACTIVE_REFUSED),
        ("anita", "reset", "pseudonymised_unassigned_user", 422, None),
        ("tara", "delete", "pseudonymised_team_user", 422, None),
    ],
)
def test_each_refusal_says_why(cast, caller_name, action, target_name, status, message):
    caller, client = caller_of(cast, caller_name)
    target = target_named(cast, target_name, caller)
    response = send(client, action, target)
    assert response.status_code == status
    expected = message or services.AccountPseudonymisedError().message
    assert error_message(response) == expected
    # The preview of the same account gives the same reason when it is about deleting.
    if action == "delete":
        reason = send(client, "preview", target).json()["blocked_reason"]
        assert reason == expected


# --- outside the reach: indistinguishable from nobody ---------------------------------------------
OUTSIDE_TARA = [
    "another_super_admin",
    "admin",
    "deactivated_admin",
    "invited_admin",
    "other_team_user",
    "other_team_deactivated_user",
    "other_team_invited_user",
    "unassigned_user",
    "pseudonymised_unassigned_user",
]
# What a response may differ in without telling anything about the account: the request's id and
# the clock (`Date`, and `Expires` which never-cache responses set to now).
ENVELOPE_HEADERS = {"x-request-id", "date", "expires"}


def envelope(response) -> dict[str, str]:
    return {k.lower(): v for k, v in response.items() if k.lower() not in ENVELOPE_HEADERS}


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("target_name", OUTSIDE_TARA)
def test_what_an_admin_cannot_reach_answers_exactly_like_what_does_not_exist(
    cast, target_name, action
):
    client = signed_in(cast.tara)
    target = target_named(cast, target_name, cast.tara)
    missing = target_named(cast, "nobody", cast.tara)
    before = snapshot()
    answer = send(client, action, target)
    nobody = send(client, action, missing)
    assert answer.status_code == nobody.status_code == 404
    assert without_request_id(answer) == without_request_id(nobody)
    assert envelope(answer) == envelope(nobody)
    assert snapshot() == before


def test_guessing_every_id_of_other_teams_finds_nothing(cast):
    """An IDOR sweep: Tara asks for every account that is not hers, three ways, and learns
    only that none exists. Her own team answers as before."""
    client = signed_in(cast.tara)
    team = {cast.tara.pk, cast.rahul.pk, cast.gita.pk, cast.ishan.pk}
    before = snapshot()
    for user in User.objects.exclude(pk__in=team):
        for action in ACTIONS:
            response = send(client, action, user)
            assert response.status_code == 404, (user.email, action, response.content)
    assert snapshot() == before
    assert send(client, "preview", cast.rahul).status_code == 200


# --- bodies, queries and paths can't redirect or widen -------------------------------------------
SMUGGLED_FIELDS = [
    "role",
    "is_staff",
    "is_superuser",
    "manager",
    "user",
    "user_id",
    "id",
    "actor",
    "actor_id",
    "email",
    "to",
    "status",
    "capabilities",
    "pseudonymised_at",
]


def smuggled(cast: Cast) -> dict[str, Any]:
    """Fields a hostile client might add: a role, a team, another account, another actor,
    another address for the link."""
    values: dict[str, Any] = {
        "role": "admin",
        "is_staff": True,
        "is_superuser": True,
        "manager": str(cast.anita.pk),
        "user": str(cast.priya.pk),
        "user_id": str(cast.priya.pk),
        "id": str(cast.priya.pk),
        "actor": str(cast.anita.pk),
        "actor_id": str(cast.anita.pk),
        "email": "attacker@evil.test",
        "to": "attacker@evil.test",
        "status": "active",
        "capabilities": ["users.manage"],
        "pseudonymised_at": None,
    }
    assert list(values) == SMUGGLED_FIELDS
    return values


def test_a_body_cannot_redirect_or_widen_a_reset(cast):
    """The reset takes no body: whatever is sent is ignored (or refused), and the one account
    in the path is the one reset, with its role, team and address untouched."""
    client = signed_in(cast.anita)
    before = snapshot()
    response = client.post(
        f"{USERS}/{cast.rahul.pk}/reset-password",
        {
            **smuggled(cast),
            "password": "Attacker-Chosen-Pass-61!",
            "new_password": "Attacker-Pass-61!",
        },
        format="json",
    )
    assert response.status_code in (200, 400), response.content
    after = snapshot()
    if response.status_code == 400:
        assert after == before
        return
    assert changed_users(before, after) == {cast.rahul.pk}
    rahul = User.objects.get(pk=cast.rahul.pk)
    assert (rahul.role, rahul.manager_id, rahul.email) == (
        Role.SALES_USER,
        cast.tara.pk,
        cast.rahul.email,
    )
    for password in ("Attacker-Chosen-Pass-61!", "Attacker-Pass-61!"):
        assert not rahul.check_password(password)
    assert response.json()["id"] == str(cast.rahul.pk)
    tokens = AccountToken.objects.filter(status=TokenStatus.PENDING)
    assert {token.user_id for token in tokens} == {cast.rahul.pk}


def test_a_body_cannot_take_an_admin_outside_her_team(cast):
    """Tara names her own member in the body and asks for Kavya's id in the path: Kavya is
    still outside her reach, and Rahul is not touched."""
    client = signed_in(cast.tara)
    before = snapshot()
    body = {"user_id": str(cast.rahul.pk), "id": str(cast.rahul.pk), "manager": str(cast.tara.pk)}
    # The reset takes no body at all: Kavya's id in the path is the only account it looks at.
    assert send(client, "reset", cast.kavya, **body).status_code == 404
    # The deletion checks the shape of its body first (the same answer for every target, so it
    # tells nothing about the account), and refuses fields it doesn't declare.
    refused = send(client, "delete", cast.kavya, **body)
    assert (refused.status_code, error_code(refused)) == (400, "validation_error")
    # With a valid body Kavya is still nobody to Tara.
    assert send(client, "delete", cast.kavya).status_code == 404
    assert snapshot() == before


@pytest.mark.parametrize("field", SMUGGLED_FIELDS)
def test_a_delete_refuses_a_field_it_does_not_declare(cast, field):
    client = signed_in(cast.anita)
    before = snapshot()
    response = send(client, "delete", cast.rahul, **{field: smuggled(cast)[field]})
    assert (response.status_code, error_code(response)) == (400, "validation_error")
    assert field in str(response.json()["error"]["details"])
    assert snapshot() == before


def test_a_delete_with_a_missing_or_malformed_confirmation_changes_nothing(cast):
    client = signed_in(cast.anita)
    before = snapshot()
    base = f"{USERS}/{cast.rahul.pk}/delete"
    for body in (
        {},
        {"version": cast.rahul.version},
        {"confirm_email": cast.rahul.email},
        {"version": 0, "confirm_email": cast.rahul.email},
        {"version": "x", "confirm_email": cast.rahul.email},
        {"version": cast.rahul.version, "confirm_email": ["a"]},
        {"version": cast.rahul.version, "confirm_email": cast.rahul.email, "reassign_to": "x"},
    ):
        response = client.post(base, body, format="json")
        assert response.status_code == 400, (body, response.content)
    assert snapshot() == before


@pytest.mark.parametrize("action", ACTIONS)
def test_a_query_string_is_refused_for_an_allowed_caller(cast, action):
    client = signed_in(cast.anita)
    before = snapshot()
    base = f"{USERS}/{cast.rahul.pk}"
    route = {"reset": "reset-password", "delete": "delete", "preview": "deletion-preview"}[action]
    path = f"{base}/{route}?user_id={cast.priya.pk}&role=admin"
    if action == "preview":
        response = client.get(path)
    else:
        body = {"version": cast.rahul.version, "confirm_email": cast.rahul.email}
        response = client.post(path, body if action == "delete" else {}, format="json")
    assert (response.status_code, error_code(response)) == (400, "validation_error")
    assert snapshot() == before


@pytest.mark.parametrize(
    "path",
    [
        "{id}/reset-password/",
        "{id}/reset-password/extra",
        "{id}/RESET-PASSWORD",
        "{upper}/reset-password",
        "{upper}/delete",
        "{upper}/deletion-preview",
        "not-a-uuid/reset-password",
        "not-a-uuid/delete",
        "'; DROP TABLE identity_user; --/reset-password",
        "{id}%00/reset-password",
        "{id}/../{other}/reset-password",
        "{other}/../{id}/delete",
        "{id}/delete/",
    ],
)
def test_malformed_ids_and_paths_are_unknown_urls(cast, path):
    client = signed_in(cast.anita)
    before = snapshot()
    url = f"{USERS}/" + path.format(
        id=cast.rahul.pk, upper=str(cast.rahul.pk).upper(), other=cast.priya.pk
    )
    for method in ("get", "post"):
        response = getattr(client, method)(url, {}, format="json")
        assert response.status_code == 404, (method, url, response.status_code)
    assert snapshot() == before


def test_headers_do_not_change_who_is_asking(cast):
    """A User can't pass for an administrator with headers a proxy might trust elsewhere."""
    client = signed_in(cast.rahul)
    before = snapshot()
    headers = {
        "HTTP_X_ROLE": "admin",
        "HTTP_X_USER_ID": str(cast.anita.pk),
        "HTTP_X_FORWARDED_USER": str(cast.anita.pk),
        "HTTP_X_HTTP_METHOD_OVERRIDE": "POST",
        "HTTP_AUTHORIZATION": "Bearer " + "a" * 40,
    }
    for action in ACTIONS:
        response = send_with_headers(client, action, cast.priya, headers)
        assert response.status_code in (401, 403), (action, response.content)
    assert snapshot() == before


def send_with_headers(client, action, target, headers):
    base = f"{USERS}/{target.pk}"
    if action == "reset":
        return client.post(f"{base}/reset-password", {}, format="json", **headers)
    if action == "delete":
        body = {"version": target.version, "confirm_email": target.email}
        return client.post(f"{base}/delete", body, format="json", **headers)
    return client.get(f"{base}/deletion-preview", **headers)


# --- the methods are the declared ones ------------------------------------------------------------
NOT_POST = ["get", "put", "patch", "delete"]


@pytest.mark.parametrize("method", NOT_POST)
@pytest.mark.parametrize("route", ["reset-password", "delete"])
def test_the_write_routes_are_posts_only(cast, route, method):
    client = signed_in(cast.anita)
    before = snapshot()
    response = getattr(client, method)(f"{USERS}/{cast.rahul.pk}/{route}", format="json")
    assert (response.status_code, error_code(response)) == (405, "method_not_allowed")
    assert snapshot() == before


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_preview_is_a_get_only(cast, method):
    client = signed_in(cast.anita)
    before = snapshot()
    response = getattr(client, method)(f"{USERS}/{cast.rahul.pk}/deletion-preview", format="json")
    assert (response.status_code, error_code(response)) == (405, "method_not_allowed")
    assert snapshot() == before


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
@pytest.mark.parametrize("route", ["reset-password", "delete", "deletion-preview"])
def test_the_method_is_not_a_way_round_the_caller_checks(cast, route, method):
    """401 and 403 come before 405: a caller who may not ask isn't told which methods exist."""
    path = f"{USERS}/{cast.rahul.pk}/{route}"
    anonymous = getattr(APIClient(), method)(path, format="json")
    assert (anonymous.status_code, error_code(anonymous)) == (401, "not_authenticated")
    ordinary = getattr(signed_in(cast.priya), method)(path, format="json")
    assert (ordinary.status_code, error_code(ordinary)) == (403, "permission_denied")


# --- the gates every administrator route has ------------------------------------------------------
def start_support_session(client: APIClient, user: User) -> None:
    started = client.post(SUPPORT, {"user": str(user.pk), "reason": "ticket 4"}, format="json")
    assert started.status_code == 201, started.content


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("caller_name", ["anita", "tara"])
def test_not_inside_a_support_session(cast, caller_name, action):
    """Identity work needs the administrator's own context: the session's user is a member of
    the team (or anyone, for a Super admin) and every route says to exit it first."""
    _, client = caller_of(cast, caller_name)
    start_support_session(client, cast.rahul)
    before = snapshot()
    target = cast.gita if caller_name == "tara" else cast.priya
    response = send(client, action, target)
    assert (response.status_code, error_code(response)) == (403, "support_session_active")
    assert snapshot() == before
    assert client.delete(f"{SUPPORT}/current").status_code == 204
    assert send(client, "preview", target).status_code == 200


@pytest.fixture
def two_step(settings):
    settings.MFA_REQUIRED_FOR_ADMINS = True


@pytest.mark.parametrize("action", ACTIONS)
def test_a_super_admin_without_a_second_factor_reaches_nothing(two_step, cast, action):
    before = snapshot()
    response = send(signed_in(cast.anita), action, cast.rahul)
    assert (response.status_code, error_code(response)) == (403, "mfa_enrollment_required")
    assert snapshot() == before


@pytest.mark.parametrize("action", ["reset", "delete"])
def test_a_super_admin_needs_a_recent_second_factor_for_the_writes(two_step, cast, action):
    enrol(cast.anita)
    client = signed_in(cast.anita)
    before = snapshot()
    refused = send(client, action, cast.rahul)
    assert (refused.status_code, error_code(refused)) == (403, "mfa_required")
    assert snapshot() == before
    session = client.session
    mfa_services.mark_verified(session)
    session.save()
    cache.clear()
    assert send(client, action, cast.rahul).status_code == 200
    assert snapshot() != before


def test_reading_the_preview_needs_no_code_but_the_enrolment(two_step, cast):
    enrol(cast.anita)
    response = send(signed_in(cast.anita), "preview", cast.rahul)
    assert response.status_code == 200, response.content


def test_an_admin_is_never_asked_for_a_second_factor(two_step, cast):
    """Two-step verification is for Super admins (ADR-0033): an Admin works with or without a
    device left over from before."""
    client = signed_in(cast.tara)
    assert send(client, "preview", cast.rahul).status_code == 200
    assert send(client, "reset", cast.rahul).status_code == 200
    enrol(cast.tara)
    other = signed_in(cast.tara)
    assert send(other, "preview", cast.gita).status_code == 200
    assert send(other, "delete", cast.gita).status_code == 200


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("caller_name", ["anita", "tara"])
def test_an_administrator_who_must_change_their_password_reaches_nothing(cast, caller_name, action):
    caller, client = caller_of(cast, caller_name)
    assert caller is not None
    User.objects.filter(pk=caller.pk).update(password_change_required=True)
    before = snapshot()
    response = send(client, action, cast.rahul)
    assert (response.status_code, error_code(response)) == (403, "password_change_required")
    assert snapshot() == before


@pytest.mark.parametrize("action", ["reset", "delete"])
def test_a_write_needs_the_csrf_token(cast, action):
    client = signed_in(cast.anita, APIClient(enforce_csrf_checks=True))
    before = snapshot()
    response = send(client, action, cast.rahul)
    assert (response.status_code, error_code(response)) == (403, "csrf_failed")
    assert snapshot() == before
    assert send(client, "preview", cast.rahul).status_code == 200  # a read needs none


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("caller_name", ["anita", "tara"])
def test_a_deactivated_administrators_open_session_reaches_nothing(cast, caller_name, action):
    caller, client = caller_of(cast, caller_name)
    assert caller is not None
    services.deactivate_user(actor_id=cast.bina.pk, user_id=caller.pk)
    before = snapshot()
    response = send(client, action, cast.rahul)
    assert (response.status_code, error_code(response)) == (401, "not_authenticated")
    assert snapshot() == before


@pytest.mark.parametrize("route", ["reset-password", "delete"])
def test_the_writes_are_rate_limited_per_administrator(cast, route):
    """The `auth` budget (per signed-in user, 20 a minute by default) counts every request,
    refused ones too; the one after it is a 429 that says when to come back."""
    limit = int(api_settings.DEFAULT_THROTTLE_RATES["auth"].split("/")[0])
    client = signed_in(cast.anita)
    body = {"version": 1, "confirm_email": "nobody@example.test"}
    path = f"{USERS}/{MISSING}/{route}"
    for _ in range(limit):
        assert client.post(path, body, format="json").status_code == 404
    response = client.post(path, body, format="json")
    assert (response.status_code, error_code(response)) == (429, "rate_limited")
    assert int(response["Retry-After"]) > 0
    assert send(client, "preview", cast.rahul).status_code == 200  # reading has its own budget


# --- what changed a moment ago is read on the next request ----------------------------------------
@pytest.mark.parametrize("action", ACTIONS)
def test_an_admin_demoted_through_the_services_loses_the_session_too(cast, action):
    client = signed_in(cast.tara)
    services.update_user(
        actor_id=cast.anita.pk,
        user_id=cast.tara.pk,
        version=cast.tara.version,
        changes={"role": "sales_user"},
    )
    before = snapshot()
    response = send(client, action, cast.rahul)
    assert (response.status_code, error_code(response)) == (401, "not_authenticated")
    assert snapshot() == before


@pytest.mark.parametrize("action", ACTIONS)
def test_an_admin_demoted_behind_the_services_back_is_refused_on_the_next_request(cast, action):
    """A role changed in the database without the services' sign-out: the capability is read
    from the account on every request, so the session that remains grants nothing."""
    client = signed_in(cast.tara)
    assert send(client, "preview", cast.rahul).status_code == 200
    User.objects.filter(pk=cast.tara.pk).update(role=Role.SALES_USER)
    before = snapshot()
    response = send(client, action, cast.rahul)
    assert (response.status_code, error_code(response)) == (403, "permission_denied")
    assert snapshot() == before


@pytest.mark.parametrize("action", ACTIONS)
def test_a_user_moved_to_another_team_just_before_is_not_found(cast, action):
    client = signed_in(cast.tara)
    assert send(client, "preview", cast.rahul).status_code == 200
    services.update_user(
        actor_id=cast.anita.pk,
        user_id=cast.rahul.pk,
        version=cast.rahul.version,
        changes={"manager": cast.omar.pk},
    )
    cast.rahul.refresh_from_db()
    before = snapshot()
    response = send(client, action, cast.rahul)
    assert (response.status_code, error_code(response)) == (404, "not_found")
    assert snapshot() == before
    # The new team's Admin reaches them now.
    assert send(signed_in(cast.omar), "preview", cast.rahul).status_code == 200


@pytest.mark.parametrize("action", ACTIONS)
def test_a_user_released_from_every_team_is_not_found(cast, action):
    client = signed_in(cast.tara)
    services.update_user(
        actor_id=cast.anita.pk,
        user_id=cast.rahul.pk,
        version=cast.rahul.version,
        changes={"manager": None},
    )
    cast.rahul.refresh_from_db()
    before = snapshot()
    assert send(client, action, cast.rahul).status_code == 404
    assert snapshot() == before


@pytest.mark.parametrize("action", ACTIONS)
def test_a_user_promoted_to_admin_just_before_leaves_the_admins_reach(cast, action):
    client = signed_in(cast.tara)
    services.update_user(
        actor_id=cast.anita.pk,
        user_id=cast.rahul.pk,
        version=cast.rahul.version,
        changes={"role": "team_admin"},
    )
    cast.rahul.refresh_from_db()
    assert cast.rahul.role == Role.TEAM_ADMIN
    before = snapshot()
    assert send(client, action, cast.rahul).status_code == 404
    assert snapshot() == before
    # And a Super admin may now reset them as an Admin, which is what they are.
    assert send(signed_in(cast.anita), "reset", cast.rahul).status_code == 200


# --- below the API: the same rules in the services ------------------------------------------------
def test_the_services_refuse_a_caller_who_cannot_administer_users(cast):
    """Below the API (a future caller, a job): the actor is re-read and must hold the
    capability, whoever the target."""
    before = snapshot()
    for actor_id in (cast.rahul.pk, cast.priya.pk, uuid.uuid4()):
        with pytest.raises(PermissionDeniedError):
            services.send_password_reset_link(actor_id=actor_id, user_id=cast.gita.pk)
        with transaction.atomic(), pytest.raises(PermissionDeniedError):
            services.lock_account_for_removal(actor_id, cast.gita.pk)
    assert snapshot() == before


def test_a_role_nobody_listed_is_refused_until_it_is_added(cast):
    """Deny by default: the rule names the roles it operates on, so a role added later is
    refused for everyone until someone decides otherwise."""
    future = SimpleNamespace(pk=uuid.uuid4(), role="auditor")
    for actor in (cast.anita, cast.tara):
        with pytest.raises(PermissionDeniedError):
            services.refuse_account_operation_unless_permitted(actor, future)
    users = SimpleNamespace(pk=uuid.uuid4(), role=Role.SALES_USER)
    services.refuse_account_operation_unless_permitted(cast.anita, users)
    admins = SimpleNamespace(pk=uuid.uuid4(), role=Role.TEAM_ADMIN)
    services.refuse_account_operation_unless_permitted(cast.anita, admins)
    with pytest.raises(PermissionDeniedError):  # an Admin never acts on an Admin
        services.refuse_account_operation_unless_permitted(cast.tara, admins)
    supers = SimpleNamespace(pk=uuid.uuid4(), role=Role.ADMIN)
    for actor in (cast.anita, cast.tara):
        with pytest.raises(PermissionDeniedError):
            services.refuse_account_operation_unless_permitted(actor, supers)


def test_a_read_another_site_made_the_browser_send_is_not_found(cast):
    """The preview is a GET: a link on another site can make a browser send it with the
    administrator's cookies, so it is refused like an unknown URL (core.api.refuse_foreign_site)."""
    client = signed_in(cast.anita)
    path = f"{USERS}/{cast.rahul.pk}/deletion-preview"
    before = snapshot()
    for site in ("cross-site", "same-site"):
        response = client.get(path, HTTP_SEC_FETCH_SITE=site)
        assert (response.status_code, error_code(response)) == (404, "not_found")
    assert snapshot() == before
    assert client.get(path, HTTP_SEC_FETCH_SITE="same-origin").status_code == 200
