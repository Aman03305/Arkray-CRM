"""The audit trail of resetting a password and of deleting an account (ADR-0036).

Every reset and every deletion leaves events that say who did what to whom and when, in the
append-only trail, and nothing that could be used: no password, no hash, no link secret, no
digest, and (for a deletion) none of the personal data the deletion erases. The administrators
who watch the organisation read them in the security events: a Super admin only.

The events (named in ADR-0036):
- a reset: `auth.password_reset_by_admin` (role, count of revoked links) and
  `auth.password_reset_issued` (reason administrator);
- a deletion: `user.deactivated` (reason deleted), `user.pseudonymised` (deleted) and
  `user.deleted` (role, who took the work over, counts), all in the deleting request.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditDetail, AuditEvent
from arkray.identity import services, tokens
from arkray.identity.models import PSEUDONYM_FIRST_NAME, AccountToken, TokenStatus, User
from tests.factories import DEFAULT_PASSWORD, AdminFactory, UserFactory
from tests.helpers import drain_outbox, emailed_secrets, last_secret, signed_in

pytestmark = pytest.mark.django_db

USERS = "/api/v1/admin/users"
EVENTS = "/api/v1/admin/security-events"
CHOSEN = "Owner-Chosen-Phrase-Lantern-31"
RESET_ACTIONS = {"auth.password_reset_by_admin", "auth.password_reset_issued"}
DELETE_ACTIONS = {"user.deactivated", "user.pseudonymised", "user.deleted"}
# Words that would mean a secret is being written down. The exceptions are two counters of the
# erasure (privacy.staff) whose names carry "token" and "session": the audit sanitiser takes them
# for secrets and blanks their values.
FORBIDDEN = ("password", "passwd", "secret", "token", "pbkdf2", "argon2", "md5$", "bearer")
COUNTERS = {"tokens_revoked", "support_sessions_ended"}


# The address the requests come from: what the trail keeps of the administrator.
ADMINISTRATORS_ADDRESS = "203.0.113.7"


def reset(client: APIClient, target: User):
    path = f"{USERS}/{target.pk}/reset-password"
    return client.post(path, {}, format="json", REMOTE_ADDR=ADMINISTRATORS_ADDRESS)


def delete(client: APIClient, target: User, **body: Any):
    """The deletion of the account as it is now (its version and address as listed)."""
    now = User.objects.get(pk=target.pk)
    payload = {"version": now.version, "confirm_email": now.email, **body}
    return client.post(
        f"{USERS}/{target.pk}/delete", payload, format="json", REMOTE_ADDR=ADMINISTRATORS_ADDRESS
    )


def delivered() -> list[Any]:
    mail.outbox.clear()
    drain_outbox()
    return list(mail.outbox)


def events_of(actions: set[str], target: User | None = None):
    found = AuditEvent.objects.filter(action__in=actions).order_by("id")
    return found.filter(target_id=str(target.pk)) if target is not None else found


def leaves(value: Any, where: str = "") -> Iterator[tuple[str, str, Any]]:
    """(path, "key" or "value", text or scalar) for everything inside a JSON document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{where}.{key}", "key", str(key)
            yield from leaves(item, f"{where}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from leaves(item, f"{where}[{index}]")
    else:
        yield where, "value", value


def stored_audit() -> list[tuple[str, str, Any]]:
    """Every key and value of every event's metadata and every detail's values."""
    found: list[tuple[str, str, Any]] = []
    for event in AuditEvent.objects.order_by("id"):
        found += list(leaves(event.metadata, f"event {event.pk} {event.action} metadata"))
    for detail in AuditDetail.objects.order_by("event_id"):
        found += list(leaves(detail.values, f"detail {detail.event_id} values"))
    return found


def assert_nothing_secret_is_written(*also: str) -> None:
    """No key or value that names or holds a secret, and none of `also` anywhere in the trail
    (the whole rows, not only the JSON: actor, target and request columns too)."""
    items = stored_audit()
    assert items  # there was something to look through
    for where, kind, content in items:
        if where.endswith(tuple(f".{name}" for name in COUNTERS)):
            continue  # a count whose name carries "token"; the sanitiser blanks its value
        text = str(content).lower()
        for word in FORBIDDEN:
            assert word not in text, (where, kind, content)
        assert "[redacted]" not in text, (where, kind, content)  # nothing was even offered
    everything = json.dumps(list(AuditEvent.objects.values()), default=str)
    everything += json.dumps(list(AuditDetail.objects.values()), default=str)
    for needle in also:
        assert needle not in everything, needle


# --- resetting a password ---------------------------------------------------------------------
class TestAResetIsAudited:
    @pytest.mark.parametrize("who", ["super admin -> user", "admin -> user"])
    def test_two_events_say_who_did_what_to_whom_and_when(
        self, who, admin, team_admin, team_member, user_b
    ):
        actor, target = (
            (admin, user_b) if who == "super admin -> user" else (team_admin, team_member)
        )
        client = signed_in(actor)
        started = timezone.now()

        response = reset(client, target)

        finished = timezone.now()
        assert response.status_code == 200, response.content
        events = list(events_of(RESET_ACTIONS))
        assert [event.action for event in events] == [
            "auth.password_reset_by_admin",
            "auth.password_reset_issued",
        ]
        for event in events:
            assert (event.actor_type, event.actor_id) == ("user", actor.pk)
            assert (event.target_type, event.target_id) == ("user", str(target.pk))
            assert started <= event.occurred_at <= finished
            assert event.request_id == response["X-Request-ID"] != ""
            assert event.support_session_id is None
            assert event.ip_address is None  # the legacy column; the address is in the detail
            detail = AuditDetail.objects.get(event=event)
            assert detail.ip_address == ADMINISTRATORS_ADDRESS  # the actor's, not the owner's
            assert detail.values == {}
        done, issued = events
        assert done.subject_user_id == target.pk  # whose account the change concerned
        assert done.metadata == {"role": target.role, "revoked_links": 0}
        assert issued.metadata == {"reason": "administrator"}

    def test_each_reset_is_its_own_pair_of_events(self, admin_client, admin, user_b):
        for _ in range(3):
            assert reset(admin_client, user_b).status_code == 200
        counts = {
            action: AuditEvent.objects.filter(action=action).count() for action in RESET_ACTIONS
        }
        assert counts == dict.fromkeys(RESET_ACTIONS, 3)
        revoked = [
            event.metadata["revoked_links"] for event in events_of({"auth.password_reset_by_admin"})
        ]
        assert revoked == [0, 1, 1]  # each reset voided the one pending before it

    def test_a_refused_reset_writes_no_event(self, admin, admin_client, team_admin, user_b):
        gone = UserFactory(is_active=False)
        refusals = [
            (admin_client, admin, 422),  # oneself
            (admin_client, AdminFactory(), 403),  # a Super admin
            (admin_client, gone, 422),  # not active
            (signed_in(team_admin), user_b, 404),  # outside her team
            (signed_in(user_b), gone, 403),  # not an administrator
            (APIClient(), gone, 401),  # not signed in
        ]
        for client, target, status in refusals:
            assert reset(client, target).status_code == status
        assert not events_of(RESET_ACTIONS).exists()
        # The fourth for one account inside an hour is refused too, and writes none.
        target = UserFactory()
        for _ in range(3):
            assert reset(admin_client, target).status_code == 200
        before = AuditEvent.objects.count()
        assert reset(admin_client, target).status_code == 429
        assert AuditEvent.objects.count() == before

    def test_the_trail_holds_no_secret_hash_or_password(self, admin_client, team_member):
        old_hash = team_member.password
        assert reset(admin_client, team_member).status_code == 200
        delivered()
        secret = last_secret("reset-password")
        digest = tokens.digest(secret)
        new_hash = User.objects.get(pk=team_member.pk).password
        done = APIClient().post(
            "/api/v1/auth/password-reset/confirm",
            {"token": secret, "new_password": CHOSEN},
            format="json",
        )
        assert done.status_code == 204, done.content
        chosen = User.objects.get(pk=team_member.pk)
        final_hash = chosen.password
        assert chosen.check_password(CHOSEN)

        assert_nothing_secret_is_written(
            secret, digest, old_hash, new_hash, final_hash, DEFAULT_PASSWORD, CHOSEN
        )
        # The trail does hold the completion, by the owner, as one more fact.
        completed = AuditEvent.objects.get(action=services.AUDIT_PASSWORD_RESET_COMPLETED)
        assert completed.actor_id == team_member.pk

    def test_the_security_events_show_it_to_a_super_admin(self, admin, admin_client, team_member):
        reset(admin_client, team_member)
        listed = admin_client.get(EVENTS)
        assert listed.status_code == 200, listed.content
        shown = [
            e for e in listed.json()["results"] if e["action"] == "auth.password_reset_by_admin"
        ]
        assert len(shown) == 1
        event = shown[0]
        assert event["actor"] == {"id": str(admin.pk), "full_name": admin.full_name}
        assert event["user"] == {"id": str(team_member.pk), "full_name": team_member.full_name}
        assert event["details"] == {}  # who and when; nothing about the link
        assert event["in_support_session"] is False
        occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        stored = AuditEvent.objects.get(action="auth.password_reset_by_admin")
        assert occurred == stored.occurred_at
        text = listed.content.decode()
        for never in (team_member.password, DEFAULT_PASSWORD, "reset-password/", '"token'):
            assert never not in text

    def test_only_a_super_admin_reads_the_security_events(
        self, admin, admin_client, team_admin, team_member, user_b
    ):
        tara, ordinary = signed_in(team_admin), signed_in(user_b)
        assert reset(tara, team_member).status_code == 200  # her own reset...
        reset(admin_client, user_b)
        assert tara.get(EVENTS).status_code == 403  # ...is not for her to read back
        assert User.objects.get(pk=user_b.pk).session_epoch == 2  # (the reset ended this one)
        assert ordinary.get(EVENTS).status_code == 401
        assert signed_in(User.objects.get(pk=user_b.pk)).get(EVENTS).status_code == 403
        assert APIClient().get(EVENTS).status_code == 401
        listed = admin_client.get(EVENTS).json()["results"]
        actors = {e["actor"]["id"] for e in listed if e["action"] == "auth.password_reset_by_admin"}
        assert actors == {str(team_admin.pk), str(admin.pk)}


# --- deleting an account --------------------------------------------------------------------------
class TestADeletionIsAudited:
    def test_a_trivial_delete_leaves_three_events_in_one_request(
        self, admin, admin_client, team_member
    ):
        started = timezone.now()

        response = delete(admin_client, team_member)

        finished = timezone.now()
        assert response.status_code == 200, response.content
        events = list(events_of(DELETE_ACTIONS))
        assert [event.action for event in events] == [
            "user.deactivated",
            "user.pseudonymised",
            "user.deleted",
        ]
        for event in events:
            assert (event.actor_type, event.actor_id) == ("user", admin.pk)
            assert (event.target_type, event.target_id) == ("user", str(team_member.pk))
            assert started <= event.occurred_at <= finished
            assert event.request_id == response["X-Request-ID"] != ""
        deactivated, pseudonymised, deleted = events
        assert pseudonymised.subject_user_id == deleted.subject_user_id == team_member.pk
        assert deactivated.metadata == {
            "role": "sales_user",
            "previous_status": "active",
            "revoked_links": 0,
            "revoked_invitations": 0,
            "unassigned_members": 0,
            "reason": "deleted",
        }
        assert pseudonymised.metadata["deleted"] is True
        assert deleted.metadata == {
            "role": "sales_user",
            "erasure": "full",  # a Super admin handles privacy requests
            "reassigned_to": None,
            "reassigned_leads": 0,
            "archived_pipelines": 0,
            "unassigned_members": 0,
        }

    def test_an_admins_deletion_counts_the_team_it_released(
        self, admin_client, team_admin, team_member, team_member_2
    ):
        assert delete(admin_client, team_admin).status_code == 200
        deleted = AuditEvent.objects.get(action="user.deleted")
        assert deleted.metadata["role"] == "team_admin"
        assert deleted.metadata["unassigned_members"] == 2
        deactivated = AuditEvent.objects.get(action="user.deactivated")
        assert (deactivated.metadata["role"], deactivated.metadata["unassigned_members"]) == (
            "team_admin",
            2,
        )

    def test_an_admins_own_deletion_names_her_as_the_actor(self, team_admin, team_member):
        assert delete(signed_in(team_admin), team_member).status_code == 200
        deleted = AuditEvent.objects.get(action="user.deleted")
        assert (deleted.actor_id, deleted.target_id) == (team_admin.pk, str(team_member.pk))

    def test_what_happened_before_stays_and_names_only_ids(self, admin_client, team_member, user_b):
        reset(admin_client, team_member)
        earlier = list(AuditEvent.objects.order_by("id").values())
        original = (team_member.email, team_member.first_name, team_member.last_name)

        assert delete(admin_client, team_member).status_code == 200

        # Nothing that was recorded was altered or removed; the deletion only added.
        now = list(AuditEvent.objects.order_by("id").values())
        assert now[: len(earlier)] == earlier
        assert len(now) > len(earlier)
        assert_nothing_secret_is_written(*original)
        # The ids are the link to the person: the target, still named, is now a pseudonym.
        erased = User.objects.get(pk=team_member.pk)
        assert erased.first_name == PSEUDONYM_FIRST_NAME
        for event in AuditEvent.objects.filter(target_id=str(team_member.pk)):
            assert event.target_type == "user"

    def test_a_link_issued_before_the_deletion_is_dead_and_the_trail_holds_no_secret(
        self, admin_client, team_member
    ):
        reset(admin_client, team_member)
        delivered()
        secret = last_secret("reset-password")
        assert emailed_secrets("reset-password") == [secret]
        hashes = [team_member.password, User.objects.get(pk=team_member.pk).password]

        assert delete(admin_client, team_member).status_code == 200

        refused = APIClient().post(
            "/api/v1/auth/password-reset/confirm",
            {"token": secret, "new_password": CHOSEN},
            format="json",
        )
        assert (refused.status_code, refused.json()["error"]["code"]) == (400, "invalid_token")
        assert not AccountToken.objects.filter(
            user=team_member, status=TokenStatus.PENDING
        ).exists()
        assert_nothing_secret_is_written(secret, tokens.digest(secret), *hashes, DEFAULT_PASSWORD)

    def test_a_refused_delete_writes_no_event(self, admin, admin_client, team_admin, user_b):
        other = UserFactory()
        refusals = [
            (admin_client, admin, {}),  # oneself
            (admin_client, AdminFactory(), {}),  # a Super admin
            (admin_client, other, {"confirm_email": "someone.else@example.test"}),  # not confirmed
            (admin_client, other, {"version": other.version + 3}),  # a stale page
            (signed_in(team_admin), user_b, {}),  # outside her team
            (signed_in(user_b), other, {}),  # not an administrator
            (APIClient(), other, {}),  # not signed in
        ]
        before = AuditEvent.objects.count()
        for client, target, body in refusals:
            assert delete(client, target, **body).status_code in (401, 403, 404, 409, 422)
        assert AuditEvent.objects.count() == before
        assert not events_of(DELETE_ACTIONS).exists()

    def test_the_security_events_show_the_deletion_without_naming_the_person(
        self, admin, admin_client, team_member
    ):
        name, email = team_member.full_name, team_member.email
        reset(admin_client, team_member)  # an earlier event about the same person
        assert delete(admin_client, team_member).status_code == 200

        listed = admin_client.get(EVENTS)

        assert listed.status_code == 200, listed.content
        results = listed.json()["results"]
        deleted = next(e for e in results if e["action"] == "user.deleted")
        assert deleted["actor"] == {"id": str(admin.pk), "full_name": admin.full_name}
        assert deleted["user"]["id"] == str(team_member.pk)
        assert deleted["user"]["full_name"].startswith(PSEUDONYM_FIRST_NAME)
        assert deleted["details"] == {"role": "sales_user"}
        assert deleted["in_support_session"] is False
        stored = AuditEvent.objects.get(action="user.deleted")
        assert datetime.fromisoformat(deleted["occurred_at"].replace("Z", "+00:00")) == (
            stored.occurred_at
        )
        deactivated = next(e for e in results if e["action"] == "user.deactivated")
        assert deactivated["details"] == {"role": "sales_user"}
        reset_event = next(e for e in results if e["action"] == "auth.password_reset_by_admin")
        assert reset_event["user"]["full_name"].startswith(PSEUDONYM_FIRST_NAME)  # retroactively
        text = listed.content.decode()
        for personal in (name, email, team_member.first_name, team_member.last_name):
            assert personal not in text

    def test_only_a_super_admin_reads_it(self, admin_client, team_admin, team_member, user_b):
        tara = signed_in(team_admin)
        assert delete(tara, team_member).status_code == 200
        assert tara.get(EVENTS).status_code == 403
        assert signed_in(user_b).get(EVENTS).status_code == 403
        assert APIClient().get(EVENTS).status_code == 401
        deleted = [
            e for e in admin_client.get(EVENTS).json()["results"] if e["action"] == "user.deleted"
        ]
        assert [e["actor"]["id"] for e in deleted] == [str(team_admin.pk)]

    def test_the_listing_pages_through_the_new_events_newest_first(self, admin_client, team_admin):
        members = [UserFactory(manager=team_admin) for _ in range(3)]
        for member in members:
            assert delete(admin_client, member).status_code == 200
        first = admin_client.get(EVENTS, {"page_size": 2}).json()
        assert first["results"][0]["action"] == "user.deleted"
        stamps = [e["occurred_at"] for e in first["results"]]
        assert stamps == sorted(stamps, reverse=True)
        assert first["next"] is not None
