"""Resetting a password and deleting an account, run for real against each other and against the
other things that change who may do what (threads, separate connections; ADR-0036).

Both operations (and every other change of an account) are serialised by the user-administration
lock and the account's row lock, and decide on rows read under them: the acting administrator is
re-read, the target locked, the reach and the role checked on the locked row. So whatever the
interleaving, the outcome is as if the calls had run one after the other, and never one that no
order could produce. Each race is run both ways in a fixed order (`_hold_then`: the first call's
transaction stays open while the second starts and must wait for it), then free-running through
the API, where the assertions allow every order and forbid every impossible result:

- two resets of one account: both succeed (two links are under the cap), one link is pending;
- a reset and a deactivation: reset then deactivated, or the reset refused (422); never a
  deactivated account with a link that works;
- a reset and the account's move to another team: before the move, or 404 after it;
- a reset and a deletion: the same, nothing left to use after the erasure;
- two deletions: one completes, the others are clean 422s;
- an Admin's deletion and the demotion of that Admin: before it, or refused (403, or 404 once
  the account left her reach) with the account intact;
- a CRM write for a user and that user's deletion: the write is refused, or the deletion hands
  the new record over; never a record left to an erased account;
- an Admin's CRM write for a member and the deletion of that Admin: no deadlock (ADR-0035's
  row-lock order holds for the deletion too);
- bursts of resets: never more than three links per account per hour;
- a password change racing a reset: the reset's password is never replaced by a choice made
  with a session the reset ended;
- the owner finishing the previous link while the administrator resets again: the account ends
  with the reset's password and one pending link.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from django.core import mail
from django.db import connection, connections
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core.domain_events import subscribed
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from arkray.core.models import OutboxEvent
from arkray.identity import services, tokens
from arkray.identity.models import (
    AccountToken,
    Role,
    TokenPurpose,
    TokenStatus,
    User,
    UserStatus,
)
from arkray.leads.events import LeadCreated
from arkray.leads.models import Lead
from arkray.privacy import account_deletion
from tests.factories import DEFAULT_PASSWORD, AdminFactory, TeamAdminFactory, UserFactory
from tests.helpers import drain_outbox, emailed_secrets, key_header, run_concurrently, signed_in
from tests.security.test_admin_takeover import _hold_then
from tests.security.test_teams_identity import (
    _create_for_a_member,
    _create_in_a_members_workspace,
    _note_on_a_members_customer,
    _waits_on_a_lock,
)

pytestmark = pytest.mark.django_db(transaction=True)

USERS = "/api/v1/admin/users"
CHOSEN = "Owner-Chosen-Phrase-Lantern-31"
RESET_BY_ADMIN = services.AUDIT_PASSWORD_RESET_BY_ADMIN


@dataclass
class World:
    anita: User  # Super admin
    bina: User  # another Super admin
    tara: User  # Admin; her team is Rahul
    omar: User  # another Admin, with no team yet
    rahul: User
    priya: User  # in no team


def make_world():
    tara = TeamAdminFactory(first_name="Tara", last_name="Lead")
    return World(
        anita=AdminFactory(first_name="Anita", last_name="Admin"),
        bina=AdminFactory(first_name="Bina", last_name="Admin"),
        tara=tara,
        omar=TeamAdminFactory(first_name="Omar", last_name="Khan"),
        rahul=UserFactory(first_name="Rahul", last_name="Sharma", manager=tara),
        priya=UserFactory(first_name="Priya", last_name="Patel"),
    )


# --- calls through the API (each with its own signed-in client, made before the threads start) ----
def reset_by(actor: User, target: User) -> Callable[[], Any]:
    client = signed_in(actor)
    return lambda: client.post(f"{USERS}/{target.pk}/reset-password", {}, format="json")


def deactivation_by(actor: User, target: User) -> Callable[[], Any]:
    client = signed_in(actor)
    return lambda: client.post(f"{USERS}/{target.pk}/deactivate", {}, format="json")


def move_by(actor: User, target: User, to: User) -> Callable[[], Any]:
    client = signed_in(actor)
    body = {"manager": str(to.pk), "version": User.objects.get(pk=target.pk).version}
    return lambda: client.patch(f"{USERS}/{target.pk}", body, format="json")


def demotion_by(actor: User, target: User) -> Callable[[], Any]:
    client = signed_in(actor)
    body = {"role": "sales_user", "version": User.objects.get(pk=target.pk).version}
    return lambda: client.patch(f"{USERS}/{target.pk}", body, format="json")


def deletion_by(actor: User, target: User) -> Callable[[], Any]:
    client = signed_in(actor)
    fresh = User.objects.get(pk=target.pk)
    body = {"version": fresh.version, "confirm_email": fresh.email}
    return lambda: client.post(f"{USERS}/{target.pk}/delete", body, format="json")


def delayed(call: Callable[[], Any], seconds: float) -> Callable[[], Any]:
    """The same call, a little later: another interleaving each round."""

    def later() -> Any:
        time.sleep(seconds)
        return call()

    return later


# Who starts how much later, per round: together, either one late, one very late.
OFFSETS = [(0.0, 0.0), (0.0, 0.03), (0.03, 0.0), (0.0, 0.08)]


def codes(results: list[Any]) -> list[int]:
    """The status of every response; anything that isn't one (a deadlock, a 500 raised through
    the test client) fails the test with what it was."""
    for result in results:
        assert not isinstance(result, BaseException), repr(result)
    return [result.status_code for result in results]


# --- the state a race is judged by ----------------------------------------------------------------
def account(user: User) -> User:
    return User.objects.get(pk=user.pk)


def reset_links(user: User, **filters: Any):
    return AccountToken.objects.filter(
        user=user, purpose=TokenPurpose.PASSWORD_RESET, **filters
    ).order_by("created_at", "pk")


def audit_id(action: str, target: User | None = None) -> int | None:
    found = AuditEvent.objects.filter(action=action)
    if target is not None:
        found = found.filter(target_id=str(target.pk))
    return found.order_by("id").values_list("id", flat=True).first()


def reset_audits(target: User) -> int:
    return AuditEvent.objects.filter(action=RESET_BY_ADMIN, target_id=str(target.pk)).count()


def links_mailed() -> list[str]:
    """Run the queued work; the secrets of the reset links emailed by it."""
    mail.outbox.clear()
    drain_outbox()
    return emailed_secrets("reset-password")


def delete_now(actor: User, target: User):
    """The deletion of the account as it stands (its version and address as listed now)."""
    fresh = account(target)
    return account_deletion.delete_account(
        actor_id=actor.pk,
        user_id=target.pk,
        version=fresh.version,
        confirm_email=fresh.email,
        reassign_to=None,
    )


def consume(secret: str, password: str = CHOSEN):
    return APIClient().post(
        "/api/v1/auth/password-reset/confirm",
        {"token": secret, "new_password": password},
        format="json",
    )


def assert_one_live_link_at_most(user: User) -> None:
    assert reset_links(user, status=TokenStatus.PENDING).count() <= 1
    assert (
        AccountToken.objects.filter(user=user, status=TokenStatus.PENDING).count() <= 1
    )  # the database's own rule, seen from outside


# --- (a) two resets of one account at once --------------------------------------------------------
@pytest.mark.parametrize("n", [2, 3])
@pytest.mark.parametrize(
    "who", ["one administrator", "two Super admins", "a Super admin and the Admin"]
)
def test_simultaneous_resets_of_one_account_each_succeed_and_leave_one_live_link(who, n):
    w = make_world()
    actors = {
        "one administrator": [w.anita] * n,
        "two Super admins": [w.anita, w.bina, w.anita][:n],
        "a Super admin and the Admin": [w.anita, w.tara, w.anita][:n],
    }[who]
    before = account(w.rahul)

    results = run_concurrently(*(reset_by(actor, w.rahul) for actor in actors))

    assert codes(results) == [200] * n  # under the cap of three: all of them
    links = list(reset_links(w.rahul))
    assert len(links) == n
    assert [link.status for link in links].count(TokenStatus.PENDING) == 1
    assert [link.status for link in links].count(TokenStatus.REVOKED) == n - 1
    assert {link.created_by_id for link in links} <= {actor.pk for actor in actors}
    after = account(w.rahul)
    assert after.version == before.version + n
    assert after.session_epoch == before.session_epoch + n
    revoked = sorted(
        event.metadata["revoked_links"]
        for event in AuditEvent.objects.filter(action=RESET_BY_ADMIN)
    )
    assert revoked == [0] + [1] * (n - 1)  # each one voided the link the one before it left
    assert AuditEvent.objects.filter(action=services.AUDIT_PASSWORD_RESET_ISSUED).count() == n
    assert_one_live_link_at_most(w.rahul)

    mailed = links_mailed()
    assert len(mailed) == 1  # only the newest link goes out, the replaced ones are skipped
    pending = reset_links(w.rahul, status=TokenStatus.PENDING).get()
    assert pending.token_hash == tokens.digest(mailed[0])
    notices = [m for m in mail.outbox if "reset your password" in m.subject]
    assert len(notices) == n
    assert {tuple(m.to) for m in mail.outbox} == {(w.rahul.email,)}
    assert consume(mailed[0]).status_code == 204


# --- (b) a reset against a deactivation of the same account ---------------------------------------
def test_a_reset_that_commits_first_is_ended_by_the_deactivation_that_waits():
    w = make_world()
    before = account(w.rahul)

    reset, deactivation = _hold_then(
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
        lambda: services.deactivate_user(actor_id=w.anita.pk, user_id=w.rahul.pk),
    )

    assert isinstance(reset, User), reset
    assert isinstance(deactivation, User), deactivation
    after = account(w.rahul)
    assert after.status == UserStatus.DEACTIVATED
    assert not after.is_active
    assert after.session_epoch == before.session_epoch + 2
    assert reset_links(w.rahul).count() == 1
    assert reset_links(w.rahul, status=TokenStatus.REVOKED).count() == 1  # the deactivation's
    assert AccountToken.objects.filter(user=w.rahul, status=TokenStatus.PENDING).count() == 0
    assert audit_id(RESET_BY_ADMIN, w.rahul) < audit_id("user.deactivated", w.rahul)
    assert links_mailed() == []  # the link was never mailed: its account is not active
    assert not AccountToken.objects.filter(user=w.rahul, token_hash__isnull=False).exists()


def test_a_deactivation_that_commits_first_refuses_the_reset_that_waits():
    w = make_world()
    before = account(w.rahul)

    deactivation, reset = _hold_then(
        lambda: services.deactivate_user(actor_id=w.anita.pk, user_id=w.rahul.pk),
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
    )

    assert isinstance(deactivation, User), deactivation
    assert isinstance(reset, BusinessRuleViolation), reset
    assert str(reset) == services.RESET_INACTIVE_REFUSED
    after = account(w.rahul)
    assert after.status == UserStatus.DEACTIVATED
    assert after.session_epoch == before.session_epoch + 1  # the deactivation's alone
    assert after.check_password(DEFAULT_PASSWORD)  # the reset didn't touch the password
    assert reset_links(w.rahul).count() == 0
    assert reset_audits(w.rahul) == 0
    assert links_mailed() == []


@pytest.mark.parametrize("round_", range(4))
def test_racing_a_reset_and_a_deactivation_never_leaves_a_live_link(round_):
    w = make_world()
    late_reset, late_deactivation = OFFSETS[round_]
    reset_result, deactivation_result = run_concurrently(
        delayed(reset_by(w.tara, w.rahul), late_reset),
        delayed(deactivation_by(w.anita, w.rahul), late_deactivation),
    )

    reset_status, deactivation_status = codes([reset_result, deactivation_result])
    assert deactivation_status == 200
    assert reset_status in (200, 422)
    assert account(w.rahul).status == UserStatus.DEACTIVATED
    assert AccountToken.objects.filter(user=w.rahul, status=TokenStatus.PENDING).count() == 0
    if reset_status == 200:
        assert audit_id(RESET_BY_ADMIN, w.rahul) < audit_id("user.deactivated", w.rahul)
    else:
        assert reset_audits(w.rahul) == 0
        assert reset_links(w.rahul).count() == 0
    assert links_mailed() == []  # whichever won, nobody holds a link to a deactivated account


# --- (c) a reset against the account's move to another team ---------------------------------------
def test_a_move_that_commits_first_makes_the_admins_reset_not_found():
    w = make_world()
    before = account(w.rahul)

    move, reset = _hold_then(
        lambda: services.update_user(
            actor_id=w.anita.pk,
            user_id=w.rahul.pk,
            version=before.version,
            changes={"manager": w.omar.pk},
        ),
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
    )

    assert isinstance(move, User), move
    assert isinstance(reset, NotFoundError), reset
    after = account(w.rahul)
    assert after.manager_id == w.omar.pk
    assert after.check_password(DEFAULT_PASSWORD)
    assert after.session_epoch == before.session_epoch
    assert reset_links(w.rahul).count() == 0
    assert reset_audits(w.rahul) == 0
    # His new Admin does reach him, and the Super admin always did.
    assert isinstance(
        services.send_password_reset_link(actor_id=w.omar.pk, user_id=w.rahul.pk), User
    )


def test_a_reset_that_commits_first_stands_and_the_move_follows():
    w = make_world()
    before = account(w.rahul)

    reset, move = _hold_then(
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
        lambda: services.update_user(
            actor_id=w.anita.pk,
            user_id=w.rahul.pk,
            version=before.version + 1,
            changes={"manager": w.omar.pk},
        ),
    )

    assert isinstance(reset, User), reset
    assert isinstance(move, User), move
    assert reset_links(w.rahul, status=TokenStatus.PENDING).get().created_by_id == w.tara.pk
    assert audit_id(RESET_BY_ADMIN, w.rahul) < audit_id("user.manager_changed", w.rahul)
    assert account(w.rahul).manager_id == w.omar.pk
    # The link goes to Rahul's own address; moving him changed nobody's access to it.
    assert len(links_mailed()) == 1
    assert {tuple(m.to) for m in mail.outbox} == {(w.rahul.email,)}


@pytest.mark.parametrize("round_", range(4))
def test_racing_a_reset_by_the_admin_and_the_super_admins_move_never_resets_after_the_move(round_):
    """The move was asked for on the account as listed (version 1): after the reset it is stale
    (409), after the move the reset is 404. Exactly one of the two calls does its work."""
    w = make_world()
    late_reset, late_move = OFFSETS[round_]
    reset_result, move_result = run_concurrently(
        delayed(reset_by(w.tara, w.rahul), late_reset),
        delayed(move_by(w.anita, w.rahul, w.omar), late_move),
    )

    outcome = tuple(codes([reset_result, move_result]))
    assert outcome in {(200, 409), (404, 200)}, outcome
    rahul = account(w.rahul)
    if outcome == (200, 409):  # the reset ran before the move could commit
        assert rahul.manager_id == w.tara.pk
        assert reset_links(w.rahul, status=TokenStatus.PENDING).get().created_by_id == w.tara.pk
        assert audit_id("user.manager_changed", w.rahul) is None
    else:  # the move committed first: the reset found nobody in her team
        assert rahul.manager_id == w.omar.pk
        assert reset_audits(w.rahul) == 0
        assert reset_links(w.rahul).count() == 0
        assert rahul.check_password(DEFAULT_PASSWORD)


# --- a reset against the deletion of the same account ---------------------------------------------
def test_a_reset_that_commits_first_makes_the_deletion_that_waits_stale():
    """The page the deletion was confirmed on predates the reset: its checks pass, then it
    waits for the reset in its final transaction, finds the account changed (409) and erases
    nothing; the page is reloaded and the second attempt ends the reset's link with the
    account."""
    w = make_world()
    seen = account(w.rahul)

    reset, deleted = _hold_then(
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
        lambda: account_deletion.delete_account(
            actor_id=w.anita.pk,
            user_id=w.rahul.pk,
            version=seen.version,
            confirm_email=w.rahul.email,
            reassign_to=None,
        ),
    )

    assert isinstance(reset, User), reset
    assert isinstance(deleted, ConflictError), deleted
    assert account(w.rahul).pseudonymised_at is None
    assert reset_links(w.rahul, status=TokenStatus.PENDING).count() == 1  # the reset's, intact

    again = delete_now(w.anita, w.rahul)
    assert again.completed
    erased = account(w.rahul)
    assert erased.pseudonymised_at is not None
    assert not erased.has_usable_password()
    assert AccountToken.objects.filter(user=w.rahul, status=TokenStatus.PENDING).count() == 0
    assert links_mailed() == []


def test_a_deletion_that_commits_first_refuses_the_reset_that_waits():
    w = make_world()

    deleted, reset = _hold_then(
        lambda: delete_now(w.anita, w.rahul),
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
    )

    assert isinstance(deleted, account_deletion.DeletionResult)
    assert deleted.completed
    assert isinstance(reset, services.AccountPseudonymisedError), reset
    assert reset_links(w.rahul).count() == 0
    assert reset_audits(w.rahul) == 0
    assert links_mailed() == []


@pytest.mark.parametrize("round_", range(4))
def test_racing_a_reset_and_a_deletion_leaves_no_link_to_an_erased_account(round_):
    """The deletion was confirmed on the account as listed (version 1): a reset that ran first
    made that page stale (409, the account stays, the new link works), one that ran after was
    refused (422, nothing left to reset)."""
    w = make_world()
    late_reset, late_deletion = OFFSETS[round_]
    reset_result, deletion_result = run_concurrently(
        delayed(reset_by(w.tara, w.rahul), late_reset),
        delayed(deletion_by(w.anita, w.rahul), late_deletion),
    )

    outcome = tuple(codes([reset_result, deletion_result]))
    assert outcome in {(200, 409), (422, 200)}, outcome
    rahul = account(w.rahul)
    if outcome == (422, 200):
        assert rahul.pseudonymised_at is not None
        assert reset_audits(w.rahul) == 0
        assert reset_links(w.rahul).count() == 0
        assert AccountToken.objects.filter(user=w.rahul, status=TokenStatus.PENDING).count() == 0
        assert links_mailed() == []
    else:
        assert rahul.pseudonymised_at is None
        assert rahul.status == UserStatus.ACTIVE
        assert not AuditEvent.objects.filter(action="user.deleted").exists()
        mailed = links_mailed()
        assert len(mailed) == 1
        assert consume(mailed[0]).status_code == 204


# --- (d) two deletions of the same account --------------------------------------------------------
def test_a_second_deletion_waiting_on_the_first_is_refused_cleanly():
    w = make_world()

    first, second = _hold_then(
        lambda: delete_now(w.anita, w.rahul),
        lambda: account_deletion.delete_account(
            actor_id=w.tara.pk,
            user_id=w.rahul.pk,
            version=1,
            confirm_email=w.rahul.email,
            reassign_to=None,
        ),
    )

    assert isinstance(first, account_deletion.DeletionResult), first
    assert first.completed
    assert isinstance(second, services.AccountPseudonymisedError), second  # a clean 422
    assert AuditEvent.objects.filter(action="user.deleted").count() == 1


@pytest.mark.parametrize(
    "who",
    [
        ["super admin", "super admin"],
        ["super admin", "admin"],
        ["admin", "super admin", "super admin", "admin"],
    ],
    ids=["two Super admins", "a Super admin and the Admin", "four at once"],
)
def test_simultaneous_deletions_of_one_account_complete_once(who):
    w = make_world()
    actors = {"super admin": w.anita, "admin": w.tara}
    results = run_concurrently(*(deletion_by(actors[name], w.rahul) for name in who))

    answers = codes(results)
    assert sorted(answers) == [200] + [422] * (len(who) - 1), answers
    for result in results:
        if result.status_code == 422:
            assert result.json()["error"]["code"] == "account_pseudonymised"
    erased = account(w.rahul)
    assert erased.pseudonymised_at is not None
    assert erased.status == UserStatus.DEACTIVATED
    for action in ("user.deleted", "user.pseudonymised", "user.deactivated"):
        assert AuditEvent.objects.filter(action=action, target_id=str(w.rahul.pk)).count() == 1
    assert User.objects.filter(pk=w.rahul.pk).count() == 1  # never a row delete


# --- (e) an Admin's deletion against the demotion of that Admin -----------------------------------
def test_a_demotion_that_commits_first_refuses_the_admins_deletion_and_spares_the_account():
    w = make_world()
    rahul = account(w.rahul)
    tara = account(w.tara)

    demotion, deletion_result = _hold_then(
        lambda: services.update_user(
            actor_id=w.anita.pk,
            user_id=w.tara.pk,
            version=tara.version,
            changes={"role": "sales_user"},
        ),
        lambda: account_deletion.delete_account(
            actor_id=w.tara.pk,
            user_id=w.rahul.pk,
            version=rahul.version,
            confirm_email=rahul.email,
            reassign_to=None,
        ),
    )

    assert isinstance(demotion, User), demotion
    # Refused, whichever way: she no longer holds the capability (403), nor reaches the account.
    assert isinstance(deletion_result, PermissionDeniedError | NotFoundError), deletion_result
    intact = account(w.rahul)
    assert intact.pseudonymised_at is None
    assert intact.status == UserStatus.ACTIVE
    assert intact.email == rahul.email
    assert intact.check_password(DEFAULT_PASSWORD)
    assert intact.manager_id is None  # released by the demotion, nothing else changed
    assert not AuditEvent.objects.filter(action="user.deleted").exists()
    assert account(w.tara).role == Role.SALES_USER


def test_a_deletion_that_commits_first_stands_and_the_demotion_follows():
    w = make_world()
    tara = account(w.tara)

    deleted, demotion = _hold_then(
        lambda: delete_now(w.tara, w.rahul),
        lambda: services.update_user(
            actor_id=w.anita.pk,
            user_id=w.tara.pk,
            version=tara.version,
            changes={"role": "sales_user"},
        ),
    )

    assert isinstance(deleted, account_deletion.DeletionResult), deleted
    assert deleted.completed
    assert isinstance(demotion, User), demotion
    assert account(w.rahul).pseudonymised_at is not None
    assert audit_id("user.deleted") < audit_id(services.AUDIT_ROLE_CHANGED)
    assert not User.objects.filter(manager__isnull=False).exclude(manager__role=Role.TEAM_ADMIN)


@pytest.mark.parametrize("round_", range(4))
def test_racing_an_admins_deletion_and_her_demotion_never_deletes_after_the_demotion(round_):
    w = make_world()
    rahul = account(w.rahul)
    late_deletion, late_demotion = OFFSETS[round_]
    delete_result, demotion_result = run_concurrently(
        delayed(deletion_by(w.tara, w.rahul), late_deletion),
        delayed(demotion_by(w.anita, w.tara), late_demotion),
    )

    delete_status, demotion_status = codes([delete_result, demotion_result])
    assert demotion_status == 200
    # 401: her session ended with the demotion before the request read it; 403: the demotion
    # committed between the request's sign-in and the lock the deletion takes; 404: the account
    # was no longer in her reach when the lock was taken.
    assert delete_status in (200, 401, 403, 404)
    if delete_status == 200:
        assert audit_id("user.deleted", w.rahul) < audit_id(services.AUDIT_ROLE_CHANGED, w.tara)
        assert account(w.rahul).pseudonymised_at is not None
    else:
        intact = account(w.rahul)
        assert intact.pseudonymised_at is None
        assert intact.status == UserStatus.ACTIVE
        assert intact.email == rahul.email
        assert not AuditEvent.objects.filter(action="user.deleted").exists()
    assert account(w.tara).role == Role.SALES_USER


# --- (f) bursts of resets -------------------------------------------------------------------------
def test_a_burst_of_resets_by_an_admin_never_exceeds_the_cap():
    w = make_world()
    before = account(w.rahul)
    burst = 8

    results = run_concurrently(*(reset_by(w.tara, w.rahul) for _ in range(burst)))

    answers = codes(results)
    assert sorted(answers) == [200] * services.RESET_LINK_LIMIT_PER_HOUR + [429] * (
        burst - services.RESET_LINK_LIMIT_PER_HOUR
    )
    for result in results:
        if result.status_code == 429:
            error = result.json()["error"]
            assert (error["code"], error["message"]) == (
                "rate_limited",
                services.RESET_LINK_LIMIT_REFUSED,
            )
            assert result["Retry-After"] == str(60 * 60)
    assert reset_links(w.rahul).count() == services.RESET_LINK_LIMIT_PER_HOUR
    assert reset_links(w.rahul, status=TokenStatus.PENDING).count() == 1
    assert reset_audits(w.rahul) == services.RESET_LINK_LIMIT_PER_HOUR
    after = account(w.rahul)
    assert after.version == before.version + services.RESET_LINK_LIMIT_PER_HOUR
    assert after.session_epoch == before.session_epoch + services.RESET_LINK_LIMIT_PER_HOUR
    queued = OutboxEvent.objects.filter(topic=services.TOPIC_ACCOUNT_CHANGED).count()
    assert queued == services.RESET_LINK_LIMIT_PER_HOUR  # a refused request queued no notice
    # The newest link survived every refused attempt: it is the one that works.
    mailed = links_mailed()
    assert len(mailed) == 1
    assert consume(mailed[0]).status_code == 204


def test_a_burst_from_two_administrators_shares_one_cap():
    w = make_world()
    calls = [reset_by(w.tara, w.rahul) for _ in range(5)] + [
        reset_by(w.anita, w.rahul) for _ in range(5)
    ]

    answers = codes(run_concurrently(*calls))

    assert sorted(answers) == [200] * 3 + [429] * 7, answers
    assert reset_links(w.rahul).count() == 3
    assert reset_audits(w.rahul) == 3
    assert_one_live_link_at_most(w.rahul)


def test_a_burst_over_several_accounts_is_capped_per_account():
    w = make_world()
    accounts = [w.rahul, w.priya, UserFactory()]
    calls = [reset_by(w.anita, target) for target in accounts for _ in range(3)]

    answers = codes(run_concurrently(*calls))

    assert answers == [200] * 9  # three each: the cap is per account
    for target in accounts:
        assert reset_links(target).count() == 3
        assert reset_audits(target) == 3
        assert_one_live_link_at_most(target)


# --- a link being mailed while the next reset replaces it -----------------------------------------
@pytest.mark.parametrize("round_", range(3))
def test_a_link_mailed_while_the_next_reset_replaces_it_leaves_one_that_works(round_):
    w = make_world()
    services.send_password_reset_link(actor_id=w.anita.pk, user_id=w.rahul.pk)
    first = reset_links(w.rahul).get()

    mailing, second = run_concurrently(
        delayed(lambda: services.deliver_account_token(first.pk), 0.01 * round_),
        reset_by(w.anita, w.rahul),
    )

    assert not isinstance(mailing, BaseException), repr(mailing)
    assert codes([second]) == [200]
    drain_outbox()  # the rest of the queue; what already went out stays in the mailbox
    mailed = emailed_secrets("reset-password")
    pending = reset_links(w.rahul, status=TokenStatus.PENDING).get()
    works = [secret for secret in mailed if tokens.digest(secret) == pending.token_hash]
    assert len(works) == 1, mailed  # exactly one emailed link is the pending one
    for stale in set(mailed) - set(works):
        assert consume(stale).status_code == 400  # the replaced link, if it got out, is dead
    assert consume(works[0]).status_code == 204


# --- a password change against a reset ------------------------------------------------------------
def test_a_password_change_in_flight_cannot_undo_a_reset_that_ended_its_session():
    """The owner's session (a stolen one, say) asked to change the password just before the
    administrator's reset committed: it waits for the account's row, finds its session ended and
    changes nothing."""
    w = make_world()
    owner = signed_in(w.rahul)
    body = {"current_password": DEFAULT_PASSWORD, "new_password": CHOSEN}

    reset, change = _hold_then(
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
        lambda: owner.post("/api/v1/auth/password/change", body, format="json"),
    )

    assert isinstance(reset, User), reset
    assert not isinstance(change, BaseException), repr(change)
    assert change.status_code in (401, 403), change.content  # never 204
    final = account(w.rahul)
    assert not final.check_password(CHOSEN)
    assert not final.check_password(DEFAULT_PASSWORD)
    assert reset_links(w.rahul, status=TokenStatus.PENDING).count() == 1  # the link still stands
    assert not AuditEvent.objects.filter(action="auth.password_changed").exists()


@pytest.mark.parametrize("round_", range(4))
def test_racing_the_owners_password_change_and_a_reset_never_keeps_the_choice(round_):
    w = make_world()
    owner = signed_in(w.rahul)
    body = {"current_password": DEFAULT_PASSWORD, "new_password": CHOSEN}

    def change() -> Any:
        return owner.post("/api/v1/auth/password/change", body, format="json")

    late_reset, late_change = OFFSETS[round_]
    reset_result, change_result = run_concurrently(
        delayed(reset_by(w.tara, w.rahul), late_reset), delayed(change, late_change)
    )

    reset_status, change_status = codes([reset_result, change_result])
    assert reset_status == 200
    # 204: made before the reset; 400: the current password it re-read was already the reset's;
    # 401: its session was already dead; 403: it got the account's row after the reset and
    # found its session ended.
    assert change_status in (204, 400, 401, 403)
    final = account(w.rahul)
    # Whichever came first, the account ends with the reset's password: a choice made before
    # it was replaced, one made after it was refused.
    assert not final.check_password(CHOSEN)
    assert not final.check_password(DEFAULT_PASSWORD)
    if change_status == 204:
        assert audit_id("auth.password_changed", w.rahul) < audit_id(RESET_BY_ADMIN, w.rahul)
    else:
        assert not AuditEvent.objects.filter(action="auth.password_changed").exists()
    assert reset_links(w.rahul, status=TokenStatus.PENDING).count() == 1


# --- the owner finishing the previous link against the next reset ---------------------------------
def test_a_reset_that_commits_first_kills_the_link_the_owner_is_using():
    w = make_world()
    services.send_password_reset_link(actor_id=w.anita.pk, user_id=w.rahul.pk)
    (old,) = links_mailed()

    reset, confirmed = _hold_then(
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
        lambda: consume(old),
    )

    assert isinstance(reset, User), reset
    assert not isinstance(confirmed, BaseException), repr(confirmed)
    assert confirmed.status_code == 400  # it waited for the account, then found the link revoked
    assert confirmed.json()["error"]["code"] == "invalid_token"
    assert audit_id(services.AUDIT_PASSWORD_RESET_COMPLETED, w.rahul) is None
    assert not account(w.rahul).check_password(CHOSEN)


def test_a_confirmation_that_commits_first_is_replaced_by_the_reset_that_waits():
    w = make_world()
    services.send_password_reset_link(actor_id=w.anita.pk, user_id=w.rahul.pk)
    (old,) = links_mailed()

    confirmed, reset = _hold_then(
        lambda: services.confirm_password_reset(old, CHOSEN),
        lambda: services.send_password_reset_link(actor_id=w.tara.pk, user_id=w.rahul.pk),
    )

    assert confirmed is None, confirmed  # the owner chose a password...
    assert isinstance(reset, User), reset  # ...and the reset, right after, replaced it
    assert not account(w.rahul).check_password(CHOSEN)
    completed = audit_id(services.AUDIT_PASSWORD_RESET_COMPLETED, w.rahul)
    first_reset, second_reset = (
        AuditEvent.objects.filter(action=RESET_BY_ADMIN, target_id=str(w.rahul.pk))
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert first_reset < completed < second_reset
    assert reset_links(w.rahul, status=TokenStatus.PENDING).get().created_by_id == w.tara.pk


@pytest.mark.parametrize("round_", range(4))
def test_racing_the_owner_finishing_the_old_link_and_a_second_reset(round_):
    w = make_world()
    services.send_password_reset_link(actor_id=w.anita.pk, user_id=w.rahul.pk)
    (old,) = links_mailed()
    late_confirm, late_reset = OFFSETS[round_]

    confirmed, again = run_concurrently(
        delayed(lambda: consume(old), late_confirm),
        delayed(reset_by(w.tara, w.rahul), late_reset),
    )

    confirm_status, reset_status = codes([confirmed, again])
    assert reset_status == 200
    assert confirm_status in (204, 400)
    # Whichever came first, the account ends with a password nobody chose and one fresh link.
    assert not account(w.rahul).check_password(CHOSEN)
    assert reset_links(w.rahul, status=TokenStatus.PENDING).get().created_by_id == w.tara.pk
    completed = audit_id(services.AUDIT_PASSWORD_RESET_COMPLETED, w.rahul)
    resets = list(
        AuditEvent.objects.filter(action=RESET_BY_ADMIN, target_id=str(w.rahul.pk))
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert len(resets) == 2
    if confirm_status == 204:
        assert completed is not None
        assert resets[0] < completed < resets[1]
    else:
        assert completed is None
    mailed = links_mailed()
    assert len(mailed) == 1
    assert consume(mailed[0], "Another-Chosen-Phrase-Willow-47").status_code == 204


# --- a CRM write for a user against that user's deletion ----------------------------------------
WORKSPACES = "/api/v1/workspaces"


@pytest.mark.usefixtures("crm_configuration")
class TestACrmWriteAgainstTheDeletionOfItsOwner:
    """The pattern of test_admin_workspace_races, for deletion: a record is never given to an
    account that is being erased. Either the write commits first and the deletion, which waits
    for the account's row, finds the new record and hands it over, or the deletion commits first
    and the write is refused with nothing written."""

    def new_lead(self, writer: User, owner: User):
        client = signed_in(writer)
        return lambda: client.post(
            f"{WORKSPACES}/{owner.pk}/leads",
            {"first_name": "Race", "organization_name": "Apollo"},
            format="json",
            headers=key_header(),
        )

    def deletion(self, actor: User, target: User, new_owner: User | None):
        client = signed_in(actor)
        fresh = account(target)
        body = {"version": fresh.version, "confirm_email": fresh.email}
        if new_owner is not None:
            body["reassign_to"] = str(new_owner.pk)
        return lambda: client.post(f"{USERS}/{target.pk}/delete", body, format="json")

    @pytest.mark.parametrize("writer", ["super admin", "admin"])
    def test_a_deletion_that_commits_first_refuses_the_write_that_waits(self, monkeypatch, writer):
        w = make_world()
        who = w.bina if writer == "super admin" else w.tara
        locked = threading.Event()
        finished = {}
        record = services._audit_user

        def slow_audit(*args: Any, **kwargs: Any) -> None:
            record(*args, **kwargs)
            finished["locked"] = time.monotonic()
            locked.set()
            time.sleep(1.2)  # the deletion holds the account's row meanwhile

        monkeypatch.setattr(services, "_audit_user", slow_audit)
        delete, write = self.deletion(w.anita, w.rahul, None), self.new_lead(who, w.rahul)

        def late_write() -> Any:
            assert locked.wait(10)
            response = write()
            finished["write"] = time.monotonic()
            return response

        deleted, written = run_concurrently(delete, late_write)

        assert codes([deleted]) == [200]
        assert codes([written])[0] in (400, 422), written.content
        assert finished["write"] - finished["locked"] >= 0.8  # it waited for the deletion
        assert not Lead.objects.exists()  # nothing was written
        assert account(w.rahul).pseudonymised_at is not None

    @pytest.mark.parametrize("writer", ["super admin", "admin"])
    def test_a_write_that_commits_first_is_handed_over_by_the_deletion_that_waits(self, writer):
        w = make_world()
        who = w.bina if writer == "super admin" else w.tara
        in_write = threading.Event()

        def slow(_event: object) -> None:
            in_write.set()
            time.sleep(0.8)  # inside the write's transaction, holding the account's row

        write = self.new_lead(who, w.rahul)
        delete = self.deletion(w.anita, w.rahul, w.priya)

        def slow_write() -> Any:
            with subscribed(LeadCreated, slow):
                return write()

        def late_delete() -> Any:
            assert in_write.wait(10)
            return delete()

        written, deleted = run_concurrently(slow_write, late_delete)

        assert codes([written]) == [201], written.content
        # The deletion decided before the lead existed; its final transaction, which waited
        # for the write, finds it, changes nothing and asks to be repeated.
        assert codes([deleted]) == [202], deleted.content
        assert deleted.json() == {"completed": False, "reassigned_leads": 0, "remaining_leads": 1}
        assert account(w.rahul).pseudonymised_at is None
        repeated = delete()
        assert codes([repeated]) == [200], repeated.content
        assert repeated.json() == {"completed": True, "reassigned_leads": 1, "remaining_leads": 0}
        (lead,) = Lead.objects.all()
        assert lead.owner_id == w.priya.pk  # handed over, not left to an erased account
        assert lead.created_by_id == who.pk  # and the attribution stays
        assert account(w.rahul).pseudonymised_at is not None

    def test_a_write_that_commits_first_makes_a_deletion_without_a_new_owner_ask_for_one(self):
        w = make_world()
        in_write = threading.Event()

        def slow(_event: object) -> None:
            in_write.set()
            time.sleep(0.8)

        write = self.new_lead(w.bina, w.rahul)
        delete = self.deletion(w.anita, w.rahul, None)

        def slow_write() -> Any:
            with subscribed(LeadCreated, slow):
                return write()

        def late_delete() -> Any:
            assert in_write.wait(10)
            return delete()

        written, deleted = run_concurrently(slow_write, late_delete)

        assert codes([written]) == [201], written.content
        assert codes([deleted]) == [400], deleted.content  # the work that appeared needs an owner
        assert "reassign_to" in deleted.json()["error"]["details"]
        rahul = account(w.rahul)
        assert rahul.pseudonymised_at is None
        assert rahul.status == UserStatus.ACTIVE
        assert Lead.objects.get().owner_id == w.rahul.pk
        assert not AuditEvent.objects.filter(action="user.deleted").exists()


@pytest.mark.usefixtures("crm_configuration")
class TestNoDeadlockBetweenAnAdminsWriteAndHerDeletion:
    """Tara's write for Rahul holds FOR SHARE on Rahul's row and, at its commit, KEY SHARE on her
    own row (the foreign key of `created_by`); the Super admin's deletion of Tara locks her row
    FOR NO KEY UPDATE, then updates her members' rows to release the team. That order doesn't
    conflict (the same one ADR-0035's role change has): the deletion waits for the write, then
    both finish. Tara's row is only changed in a key column (her email) at the very end, when
    the write is long committed."""

    def race(self, crm_write):
        anita, tara = AdminFactory(), TeamAdminFactory()
        rahul = UserFactory(manager=tara)
        UserFactory(manager=tara)  # Mohan
        tara = account(tara)
        results = {}
        deletion_pid = []
        deletion_started = threading.Event()
        deletion_waited = threading.Event()
        write_holds_its_locks = threading.Event()

        def pause(_event: object) -> None:
            # Inside the write's transaction, its share lock on Rahul's row taken: let the
            # deletion run into it, then commit.
            write_holds_its_locks.set()
            assert deletion_started.wait(10)

            def watch() -> None:  # its own thread: its own connection
                if _waits_on_a_lock(deletion_pid[0]):
                    deletion_waited.set()

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

        def delete() -> None:
            try:
                assert write_holds_its_locks.wait(10)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    deletion_pid.append(cursor.fetchone()[0])
                deletion_started.set()
                results["delete"] = account_deletion.delete_account(
                    actor_id=anita.pk,
                    user_id=tara.pk,
                    version=tara.version,
                    confirm_email=tara.email,
                    reassign_to=None,
                )
            except BaseException as exc:
                results["delete"] = exc
            finally:
                deletion_started.set()
                connections.close_all()

        threads = [threading.Thread(target=write), threading.Thread(target=delete)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(40)
        return tara, rahul, results.get("write"), results.get("delete"), deletion_waited.is_set()

    @pytest.mark.parametrize(
        "crm_write",
        [_create_for_a_member, _create_in_a_members_workspace, _note_on_a_members_customer],
        ids=["create-for-a-member", "create-in-a-members-workspace", "note-on-a-members-customer"],
    )
    def test_the_deletion_waits_for_the_write_and_both_succeed(self, crm_write):
        tara, rahul, written, deleted, waited = self.race(crm_write)

        assert not isinstance(written, BaseException), repr(written)
        assert not isinstance(deleted, BaseException), repr(deleted)
        assert deleted.completed
        assert waited, "the deletion never waited for the write: nothing was raced"
        erased = account(tara)
        assert erased.pseudonymised_at is not None
        assert erased.status == UserStatus.DEACTIVATED
        assert account(rahul).manager_id is None  # the team was released
        assert not User.objects.filter(manager__isnull=False).exclude(manager__role=Role.TEAM_ADMIN)
