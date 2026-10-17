"""An administrator has a password reset link emailed to an account (ADR-0036), end to end.

Three ways it is allowed, each run through every test of `TestTheReset`: a Super admin resets an
Admin, a Super admin resets a User, an Admin resets a User of their own team. The reset never
lets the administrator learn or choose a password: the account's current one is replaced by a
random one nobody knows, every session of the account ends, the credential links pending before
are revoked, and a single-use link (the usual short life) goes to the account's *own* address,
with a notice that an administrator did it. The owner then chooses a password through the
existing confirmation, under the password policy. Their second factor is not touched.

What the tests search for, in every place a secret could surface (API responses, the audit
trail, queued work, logs, every text column of the database): the link's one-time secret. It
exists in the email and nowhere else.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditDetail, AuditEvent
from arkray.core.logging import JsonFormatter
from arkray.core.models import OutboxEvent
from arkray.identity import mfa_services, services, tokens
from arkray.identity.models import (
    AccountToken,
    MfaDevice,
    MfaRecoveryCode,
    TokenPurpose,
    TokenStatus,
    User,
    UserStatus,
)
from tests.factories import DEFAULT_PASSWORD, AdminFactory, TeamAdminFactory, UserFactory
from tests.helpers import drain_outbox, emailed_secrets, last_secret, signed_in
from tests.mfa_helpers import enrol

pytestmark = pytest.mark.django_db

USERS = "/api/v1/admin/users"
LOGIN = "/api/v1/auth/login"
CONFIRM = "/api/v1/auth/password-reset/confirm"
CHOSEN = "Owner-Chosen-Phrase-Lantern-31"
ANOTHER = "Another-Chosen-Phrase-Willow-47"
LOGGERS = ("", "arkray", "django", "django.request", "django.security", "celery")


@dataclass
class Scenario:
    actor: User
    target: User
    client: APIClient  # the actor's


@pytest.fixture(
    params=["super admin -> admin", "super admin -> user", "admin -> user of her team"],
)
def scenario(request, admin, team_admin, team_member):
    if request.param == "super admin -> admin":
        target = TeamAdminFactory(first_name="Omar", last_name="Khan", email="omar.khan@x.test")
        return Scenario(admin, target, signed_in(admin))
    if request.param == "super admin -> user":
        target = UserFactory(first_name="Priya", last_name="Patel", email="priya.patel@x.test")
        return Scenario(admin, target, signed_in(admin))
    return Scenario(team_admin, team_member, signed_in(team_admin))


def reset(client: APIClient, target: User, **body: Any):
    return client.post(f"{USERS}/{target.pk}/reset-password", body, format="json")


def sign_in(email: str, password: str):
    return APIClient().post(LOGIN, {"email": email, "password": password}, format="json")


def confirm(secret: str, password: str = CHOSEN):
    return APIClient().post(CONFIRM, {"token": secret, "new_password": password}, format="json")


def error(response) -> tuple[int, str, str]:
    body = response.json()["error"]
    return response.status_code, body["code"], body["message"]


def reset_tokens(user: User, **filters: Any):
    return AccountToken.objects.filter(
        user=user, purpose=TokenPurpose.PASSWORD_RESET, **filters
    ).order_by("created_at", "pk")


def delivered() -> list[Any]:
    """Run the queued work: the messages the account's address would receive."""
    mail.outbox.clear()
    drain_outbox()
    return list(mail.outbox)


# --- the reset ----------------------------------------------------------------------------------
class TestTheReset:
    def test_it_answers_with_the_account_and_no_secret(self, scenario):
        target = scenario.target
        response = reset(scenario.client, target)
        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["id"], body["email"], body["role"]) == (
            str(target.pk),
            target.email,
            target.role,
        )
        assert (body["status"], body["password_change_required"]) == ("active", False)
        assert body["version"] == target.version + 1
        text = response.content.decode()
        for never in ('"password"', '"token', "reset-password/", DEFAULT_PASSWORD):
            assert never not in text
        assert settings.SESSION_COOKIE_NAME not in response.cookies  # the actor's session is kept

    def test_the_old_password_stops_working(self, scenario):
        target = scenario.target
        assert sign_in(target.email, DEFAULT_PASSWORD).status_code == 200  # until now
        before = target.password
        assert reset(scenario.client, target).status_code == 200
        refused = sign_in(target.email, DEFAULT_PASSWORD)
        assert error(refused)[:2] == (400, "invalid_credentials")
        target.refresh_from_db()
        assert target.password != before
        assert target.has_usable_password()  # an active account always holds a hash...
        for guess in ("", target.email, target.first_name, settings.SECRET_KEY, CHOSEN):
            assert not target.check_password(guess)  # ...of a password nobody was told

    def test_each_reset_picks_a_new_password_nobody_knows(self, scenario):
        target = scenario.target
        hashes = {target.password}
        for _ in range(2):
            assert reset(scenario.client, target).status_code == 200
            target.refresh_from_db()
            hashes.add(target.password)
        assert len(hashes) == 3

    def test_every_session_of_the_account_ends_and_the_actors_stays(self, scenario):
        target = scenario.target
        here, there = signed_in(target), signed_in(target)
        epoch = target.session_epoch
        assert here.get("/api/v1/auth/me").status_code == 200
        assert reset(scenario.client, target).status_code == 200
        assert here.get("/api/v1/auth/me").status_code == 401
        assert there.get("/api/v1/auth/me").status_code == 401
        target.refresh_from_db()
        assert target.session_epoch == epoch + 1
        assert scenario.client.get("/api/v1/auth/me").status_code == 200

    def test_credential_links_pending_before_are_revoked_and_one_new_one_is_pending(self, scenario):
        target = scenario.target
        services.issue_password_reset(target.email)  # the owner asked for one earlier
        unlock = AccountToken.objects.create(
            user=target,
            purpose=TokenPurpose.SIGN_IN_UNLOCK,
            expires_at=timezone.now() + timedelta(hours=2),
        )
        earlier = reset_tokens(target).get()
        assert (earlier.status, earlier.created_by_id) == (TokenStatus.PENDING, None)

        assert reset(scenario.client, target).status_code == 200

        earlier.refresh_from_db()
        unlock.refresh_from_db()
        assert earlier.status == TokenStatus.REVOKED
        assert earlier.revoked_at is not None
        assert unlock.status == TokenStatus.REVOKED
        assert unlock.revoked_at is not None
        pending = reset_tokens(target, status=TokenStatus.PENDING).get()
        assert pending.pk != earlier.pk
        assert pending.created_by_id == scenario.actor.pk
        assert pending.expires_at - pending.created_at == timedelta(
            seconds=settings.PASSWORD_RESET_TTL_S
        )
        assert pending.token_hash is None  # the secret is minted when the mail goes out
        assert reset_tokens(target).count() == 2
        assert (
            AccountToken.objects.filter(user=target, status=TokenStatus.PENDING).count() == 1
        )  # nothing else is waiting to be used
        event = AuditEvent.objects.get(action=services.AUDIT_PASSWORD_RESET_BY_ADMIN)
        assert event.metadata["revoked_links"] == 2

    def test_the_link_is_queued_then_emailed_to_the_accounts_own_address_only(self, scenario):
        target, actor = scenario.target, scenario.actor
        assert reset(scenario.client, target).status_code == 200
        assert mail.outbox == []  # nothing is sent inside the request
        link, notice = (
            OutboxEvent.objects.get(topic=services.TOPIC_DELIVER_ACCOUNT_TOKEN),
            OutboxEvent.objects.get(topic=services.TOPIC_ACCOUNT_CHANGED),
        )
        assert link.payload == {"token_id": str(reset_tokens(target).get().pk)}
        assert notice.payload == {
            "user_id": str(target.pk),
            "change": "password_reset_by_admin",
            "email": target.email,
        }

        messages = delivered()

        assert len(messages) == 2
        for message in messages:
            assert message.to == [target.email]
            assert not message.cc
            assert not message.bcc
            assert actor.email not in message.body
        by_subject = {message.subject: str(message.body) for message in messages}
        assert set(by_subject) == {
            "Reset your Arkray CRM password",
            "Arkray CRM: An administrator reset your password",
        }
        link_mail = by_subject["Reset your Arkray CRM password"]
        secret = last_secret("reset-password")
        assert f"{settings.APP_BASE_URL}/reset-password/{secret}" in link_mail
        assert "can be used once and expires on" in link_mail
        notice_mail = by_subject["Arkray CRM: An administrator reset your password"]
        assert "the old password no longer works" in notice_mail
        assert "/reset-password/" not in notice_mail
        assert secret not in notice_mail
        sent = reset_tokens(target, status=TokenStatus.PENDING).get()
        assert sent.sent_at is not None
        assert sent.token_hash == tokens.digest(secret)

    def test_an_address_in_the_request_is_ignored(self, scenario):
        target = scenario.target
        response = reset(
            scenario.client,
            target,
            email="attacker@evil.test",
            to=["attacker@evil.test"],
            cc="attacker@evil.test",
        )
        assert response.status_code in (200, 400), response.content
        if response.status_code == 400:
            return
        recipients = {address for message in delivered() for address in message.to}
        assert recipients == {target.email}
        target.refresh_from_db()
        assert target.email != "attacker@evil.test"

    def test_the_owner_completes_the_reset_with_the_link(self, scenario):
        target = scenario.target
        reset(scenario.client, target)
        delivered()
        secret = last_secret("reset-password")
        epoch = User.objects.get(pk=target.pk).session_epoch

        done = confirm(secret)

        assert done.status_code == 204, done.content
        assert settings.SESSION_COOKIE_NAME not in done.cookies  # it doesn't sign anyone in
        opened = sign_in(target.email, CHOSEN)
        assert opened.status_code == 200, opened.content
        assert error(sign_in(target.email, DEFAULT_PASSWORD))[1] == "invalid_credentials"
        assert User.objects.get(pk=target.pk).session_epoch == epoch + 1  # sessions end again
        used = reset_tokens(target).get(token_hash=tokens.digest(secret))
        assert (used.status, used.used_at is not None) == (TokenStatus.USED, True)
        completed = AuditEvent.objects.get(action=services.AUDIT_PASSWORD_RESET_COMPLETED)
        assert (completed.actor_id, completed.target_id) == (target.pk, str(target.pk))

    @pytest.mark.parametrize(
        "weak",
        [
            "password1234",  # a known (breached) password
            "qwertyuiop12",  # another
            "1qaz2wsx3edc",  # and another
            "123456789012",  # only digits
            "short-Pw1!",  # too short
            "Arkray-Staff-Pass-2026",  # built around the product's name
            "x" * 129,  # too long
        ],
    )
    def test_a_weak_or_known_password_is_refused_and_does_not_burn_the_link(self, scenario, weak):
        target = scenario.target
        reset(scenario.client, target)
        delivered()
        secret = last_secret("reset-password")

        refused = confirm(secret, weak)

        assert refused.status_code == 400, refused.content
        assert refused.json()["error"]["details"]["new_password"]
        assert weak not in refused.content.decode()
        assert reset_tokens(target, status=TokenStatus.PENDING).count() == 1  # still usable
        assert error(sign_in(target.email, weak))[1] == "invalid_credentials"
        assert confirm(secret).status_code == 204

    def test_a_password_like_the_owners_email_is_refused_too(self, scenario):
        target = scenario.target
        reset(scenario.client, target)
        delivered()
        secret = last_secret("reset-password")
        refused = confirm(secret, f"{target.email}1")
        assert refused.status_code == 400, refused.content
        assert refused.json()["error"]["details"]["new_password"]
        assert confirm(secret).status_code == 204

    def test_the_link_works_only_once(self, scenario):
        target = scenario.target
        reset(scenario.client, target)
        delivered()
        secret = last_secret("reset-password")
        assert confirm(secret).status_code == 204
        replay = confirm(secret, ANOTHER)
        assert error(replay)[:2] == (400, "invalid_token")
        assert sign_in(target.email, CHOSEN).status_code == 200  # the first choice stands
        assert error(sign_in(target.email, ANOTHER))[1] == "invalid_credentials"

    def test_the_link_expires(self, scenario):
        target = scenario.target
        reset(scenario.client, target)
        delivered()
        secret = last_secret("reset-password")
        pending = reset_tokens(target, status=TokenStatus.PENDING).get()

        # Some of its life left: the link is read (a weak password is what it complains about).
        soon = timezone.now() + timedelta(seconds=90)
        AccountToken.objects.filter(pk=pending.pk).update(expires_at=soon)
        assert error(confirm(secret, "123456789012"))[1] == "validation_error"

        gone = timezone.now() - timedelta(seconds=1)
        AccountToken.objects.filter(pk=pending.pk).update(
            created_at=gone - timedelta(hours=1), expires_at=gone
        )
        refused = confirm(secret)
        assert error(refused)[:2] == (400, "invalid_token")
        assert error(confirm(secret, "123456789012"))[1] == "invalid_token"  # not even read
        assert error(sign_in(target.email, CHOSEN))[1] == "invalid_credentials"

    def test_a_second_reset_replaces_the_first_link(self, scenario):
        target = scenario.target
        reset(scenario.client, target)
        delivered()
        first = last_secret("reset-password")
        reset(scenario.client, target)
        delivered()
        second = last_secret("reset-password")
        assert first != second
        assert error(confirm(first))[:2] == (400, "invalid_token")  # the old one is dead
        assert confirm(second).status_code == 204
        assert reset_tokens(target).count() == 2

    def test_a_link_issued_before_the_account_was_deactivated_is_dead(self, scenario):
        target = scenario.target
        reset(scenario.client, target)
        assert any("/reset-password/" in str(m.body) for m in delivered())
        secret = last_secret("reset-password")
        services.deactivate_user(actor_id=AdminFactory().pk, user_id=target.pk)
        assert error(confirm(secret))[:2] == (400, "invalid_token")
        assert reset_tokens(target, status=TokenStatus.PENDING).count() == 0

    def test_the_second_factor_is_untouched(self, scenario):
        target = scenario.target
        enrol(target)
        MfaDevice.objects.filter(user=target).update(consecutive_failures=2)
        device = list(MfaDevice.objects.filter(user=target).values())
        codes = list(MfaRecoveryCode.objects.filter(user=target).order_by("pk").values())
        assert len(codes) == 10

        response = reset(scenario.client, target)

        assert response.status_code == 200, response.content
        assert response.json()["mfa_enabled"] is True
        assert list(MfaDevice.objects.filter(user=target).values()) == device
        assert list(MfaRecoveryCode.objects.filter(user=target).order_by("pk").values()) == codes
        # Only the owner's own completion forgets the wrong codes counted against the factor.
        delivered()
        assert confirm(last_secret("reset-password")).status_code == 204
        after = MfaDevice.objects.get(user=target)
        assert after.consecutive_failures == 0
        assert after.pk == device[0]["id"]
        assert after.secret == device[0]["secret"]
        assert list(MfaRecoveryCode.objects.filter(user=target).order_by("pk").values()) == codes

    def test_the_audit_trail_and_the_notice_are_written_with_the_change(self, scenario):
        target, actor = scenario.target, scenario.actor
        assert reset(scenario.client, target).status_code == 200
        done, issued = (
            AuditEvent.objects.get(action=services.AUDIT_PASSWORD_RESET_BY_ADMIN),
            AuditEvent.objects.get(action=services.AUDIT_PASSWORD_RESET_ISSUED),
        )
        for event in (done, issued):
            assert (event.actor_id, event.target_type, event.target_id) == (
                actor.pk,
                "user",
                str(target.pk),
            )
        assert done.metadata == {"role": target.role, "revoked_links": 0}
        assert issued.metadata == {"reason": "administrator"}
        assert OutboxEvent.objects.filter(topic=services.TOPIC_ACCOUNT_CHANGED).count() == 1


class TestATemporaryPasswordWaitingToBeChanged:
    """An administrator's temporary password (ADR-0026) is a password somebody knows: a reset
    takes it away with the old one, and nothing of it is left for the setter's loss of reach to
    void later."""

    TEMPORARY = "Temporary-Reset-Pass-77#"

    @pytest.fixture(params=["super admin", "admin"])
    def setter(self, request, admin, team_admin, team_member):
        """Who set Rahul's password, and who resets it (always the Super admin)."""
        return admin if request.param == "super admin" else team_admin

    def test_it_goes_with_the_reset(self, setter, admin_client, team_member):
        services.set_user_password(
            actor_id=setter.pk, user_id=team_member.pk, version=1, new_password=self.TEMPORARY
        )
        assert User.objects.get(pk=team_member.pk).password_change_required
        assert sign_in(team_member.email, self.TEMPORARY).status_code == 200  # it works now

        assert reset(admin_client, team_member).status_code == 200

        team_member.refresh_from_db()
        assert team_member.password_change_required is False  # nothing typed by anyone is left
        assert error(sign_in(team_member.email, self.TEMPORARY))[1] == "invalid_credentials"
        delivered()
        assert confirm(last_secret("reset-password")).status_code == 204
        assert sign_in(team_member.email, CHOSEN).status_code == 200

    def test_the_setters_later_loss_of_reach_voids_nothing_more(
        self, admin, team_admin, team_member
    ):
        services.set_user_password(
            actor_id=team_admin.pk, user_id=team_member.pk, version=1, new_password=self.TEMPORARY
        )
        services.send_password_reset_link(actor_id=admin.pk, user_id=team_member.pk)
        services.update_user(
            actor_id=admin.pk,
            user_id=team_admin.pk,
            version=team_admin.version,
            changes={"role": "sales_user"},
        )
        assert not AuditEvent.objects.filter(
            action=services.AUDIT_TEMPORARY_PASSWORD_VOIDED
        ).exists()
        assert reset_tokens(team_member, status=TokenStatus.PENDING).count() == 1  # the reset's


# --- the link's secret exists in the email and nowhere else -------------------------------------
def text_columns() -> list[tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.table_name, c.column_name
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'
              AND c.data_type IN ('text', 'character varying', 'character', 'json', 'jsonb')
            ORDER BY 1, 2
            """
        )
        return list(cursor.fetchall())


def where_found(needle: str, columns: list[tuple[str, str]] | None = None) -> list[str]:
    found = []
    with connection.cursor() as cursor:
        for table, column in columns or text_columns():
            cursor.execute(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE strpos("{column}"::text, %s) > 0)',  # noqa: S608 - names from the catalogue
                [needle],
            )
            if cursor.fetchone()[0]:
                found.append(f"{table}.{column}")
    return found


@pytest.fixture
def records(caplog):
    for name in LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


class TestTheSecretStaysInTheEmail:
    def test_it_is_in_no_response_no_log_no_audit_no_queue_no_column(
        self, scenario, records, admin
    ):
        target = scenario.target
        responses = [reset(scenario.client, target)]
        delivered()
        secret = last_secret("reset-password")
        digest = tokens.digest(secret)
        link = f"{settings.APP_BASE_URL}/reset-password/"
        assert emailed_secrets("reset-password") == [secret]

        done = confirm(secret)
        assert done.status_code == 204
        responses.append(done)
        for path in (
            f"{USERS}/{target.pk}",
            f"{USERS}/{target.pk}/deletion-preview",
            f"{USERS}?page_size=100",
            "/api/v1/admin/security-events",
        ):
            responses.append(signed_in(admin).get(path))
        for response in responses:
            assert response.status_code in (200, 204), (response.request, response.content)

        # What the API said, headers included.
        said = "\n".join(
            response.content.decode() + "\n".join(f"{k}: {v}" for k, v in response.items())
            for response in responses
        )
        # What was logged, as the production formatter writes it and as the raw records hold it.
        formatter = JsonFormatter()
        logged = "\n".join(formatter.format(record) for record in records.records)
        logged += "\n".join(f"{r.getMessage()} {r.__dict__}" for r in records.records)
        audited = json.dumps(list(AuditEvent.objects.values()), default=str)
        audited += json.dumps(list(AuditDetail.objects.values()), default=str)
        queued = json.dumps(list(OutboxEvent.objects.values("payload", "last_error")), default=str)
        assert logged  # the flows did log
        for place, text in {
            "responses": said,
            "logs": logged,
            "audit": audited,
            "queue": queued,
        }.items():
            for secret_thing in (secret, digest, link):
                assert secret_thing not in text, (place, secret_thing)
        columns = text_columns()
        assert len(columns) > 50  # really every table, not a filtered few
        assert where_found(secret, columns) == []
        assert where_found(link, columns) == []
        # Its digest is stored in one column, as designed; the new password nowhere at all.
        assert where_found(digest, columns) == ["identity_account_token.token_hash"]
        assert where_found(CHOSEN, columns) == []
        assert where_found(DEFAULT_PASSWORD, columns) == []

    def test_the_audit_and_the_requests_log_lines_name_ids_only(self, scenario, records):
        target = scenario.target
        reset(scenario.client, target)
        formatter = JsonFormatter()
        logged = "\n".join(formatter.format(record) for record in records.records)
        for personal in (target.email, target.first_name, target.last_name):
            assert personal not in logged
        audited = json.dumps(list(AuditEvent.objects.values("metadata", "target_id")), default=str)
        for personal in (target.email, target.first_name, target.last_name):
            assert personal not in audited


# --- the hourly cap ------------------------------------------------------------------------------
def state(target: User) -> tuple[Any, ...]:
    return (
        list(User.objects.filter(pk=target.pk).values()),
        list(AccountToken.objects.filter(user=target).order_by("pk").values()),
        AuditEvent.objects.count(),
        OutboxEvent.objects.count(),
    )


class TestTheHourlyCap:
    def test_the_cap_is_three_links_per_account(self):
        assert services.RESET_LINK_LIMIT_PER_HOUR == 3

    def test_the_fourth_link_inside_an_hour_is_refused_and_changes_nothing(
        self, admin_client, team_member
    ):
        for _ in range(3):
            assert reset(admin_client, team_member).status_code == 200
        before = state(team_member)

        refused = reset(admin_client, team_member)

        assert error(refused) == (429, "rate_limited", services.RESET_LINK_LIMIT_REFUSED)
        assert int(refused["Retry-After"]) == 60 * 60
        assert state(team_member) == before
        assert reset_tokens(team_member).count() == 3
        assert reset_tokens(team_member, status=TokenStatus.PENDING).count() == 1
        # The link the owner holds still works: a refused request revoked nothing.
        delivered()
        assert confirm(last_secret("reset-password")).status_code == 204

    def test_the_cap_is_shared_by_every_administrator(self, admin_client, team_admin, team_member):
        tara = signed_in(team_admin)
        assert reset(tara, team_member).status_code == 200
        assert reset(admin_client, team_member).status_code == 200
        assert reset(tara, team_member).status_code == 200
        for client in (admin_client, tara):
            assert error(reset(client, team_member))[:2] == (429, "rate_limited")

    def test_another_account_is_not_affected(self, admin_client, team_member, user_b):
        for _ in range(3):
            reset(admin_client, team_member)
        assert error(reset(admin_client, team_member))[1] == "rate_limited"
        assert reset(admin_client, user_b).status_code == 200

    def test_the_hour_is_a_sliding_window(self, admin_client, team_member):
        for _ in range(3):
            reset(admin_client, team_member)
        AccountToken.objects.filter(user=team_member).update(
            created_at=timezone.now() - timedelta(minutes=59)
        )
        assert error(reset(admin_client, team_member))[1] == "rate_limited"  # still inside it
        AccountToken.objects.filter(user=team_member).update(
            created_at=timezone.now() - timedelta(minutes=61)
        )
        assert reset(admin_client, team_member).status_code == 200  # outside it

    def test_links_the_owner_asked_for_do_not_count_against_the_administrators(
        self, admin_client, team_member
    ):
        now = timezone.now()
        for age in (5, 10, 15):
            AccountToken.objects.create(
                user=team_member,
                purpose=TokenPurpose.PASSWORD_RESET,
                status=TokenStatus.REVOKED,
                revoked_at=now,
                created_at=now - timedelta(minutes=age),
                expires_at=now + timedelta(minutes=45),
                created_by=None,
            )
        for _ in range(3):
            assert reset(admin_client, team_member).status_code == 200
        assert error(reset(admin_client, team_member))[1] == "rate_limited"

    def test_a_refused_attempt_by_an_administrator_who_may_not_ask_counts_nothing(
        self, admin_client, team_member, other_team_admin
    ):
        omar = signed_in(other_team_admin)
        for _ in range(5):
            assert reset(omar, team_member).status_code == 404  # outside his team
        assert reset_tokens(team_member).count() == 0
        for _ in range(3):
            assert reset(admin_client, team_member).status_code == 200


# --- the address is the account's own, as it is now ----------------------------------------------
class TestTheAccountsOwnAddress:
    def test_a_users_new_address_gets_the_link_not_the_old_one(self, admin, admin_client, user_b):
        services.change_user_email(
            actor_id=admin.pk, user_id=user_b.pk, version=1, email="priya.new@x.test"
        )
        old = user_b.email
        mail.outbox.clear()
        drain_outbox()  # the change's notice to the old address
        assert [m.to for m in mail.outbox] == [[old]]

        assert reset(admin_client, user_b).status_code == 200

        messages = delivered()
        assert {tuple(m.to) for m in messages} == {("priya.new@x.test",)}
        assert len(messages) == 2

    def test_nothing_goes_to_the_administrator(self, scenario):
        reset(scenario.client, scenario.target)
        addresses = {address for message in delivered() for address in message.to}
        assert scenario.actor.email not in addresses
        assert addresses == {scenario.target.email}


# --- a reset in the middle of the account's own life -------------------------------------------
class TestAroundTheOwnersChoices:
    def test_the_owners_forgot_password_still_works_after_a_reset(self, admin_client, user_b):
        reset(admin_client, user_b)
        delivered()
        assert confirm(last_secret("reset-password")).status_code == 204
        asked = APIClient().post(
            "/api/v1/auth/password-reset", {"email": user_b.email}, format="json"
        )
        assert asked.status_code == 202
        delivered()
        assert confirm(last_secret("reset-password"), ANOTHER).status_code == 204
        assert sign_in(user_b.email, ANOTHER).status_code == 200

    def test_an_account_deactivated_after_a_reset_comes_back_needing_forgot_password(
        self, admin, user_b
    ):
        services.send_password_reset_link(actor_id=admin.pk, user_id=user_b.pk)
        services.deactivate_user(actor_id=admin.pk, user_id=user_b.pk)
        services.reactivate_user(actor_id=admin.pk, user_id=user_b.pk)
        user_b.refresh_from_db()
        assert user_b.status == UserStatus.ACTIVE
        assert error(sign_in(user_b.email, DEFAULT_PASSWORD))[1] == "invalid_credentials"
        # No link is pending: the owner uses Forgot password.
        assert reset_tokens(user_b, status=TokenStatus.PENDING).count() == 0
        asked = APIClient().post(
            "/api/v1/auth/password-reset", {"email": user_b.email}, format="json"
        )
        assert asked.status_code == 202
        delivered()
        assert confirm(last_secret("reset-password")).status_code == 204


# --- a Super admin is out of reach, whatever protects them -----------------------------------
class TestASuperAdminsAccountIsOutOfReach:
    def test_their_password_and_second_factor_stay_theirs(self, settings, admin):
        """No reset for a Super admin, whoever asks: nothing of theirs changes, and their
        second factor is still asked for at the next sign-in."""
        settings.MFA_REQUIRED_FOR_ADMINS = True
        bina = AdminFactory(first_name="Bina", last_name="Admin")
        enrol(admin)
        enrol(bina)
        client = signed_in(admin)
        session = client.session
        mfa_services.mark_verified(session)
        session.save()
        sessions = signed_in(bina)
        before = state(bina)
        device = list(MfaDevice.objects.filter(user=bina).values())

        refused = reset(client, bina)

        assert error(refused) == (403, "permission_denied", services.SUPER_ADMIN_ACCOUNT_REFUSED)
        assert state(bina) == before
        assert list(MfaDevice.objects.filter(user=bina).values()) == device
        assert not AccountToken.objects.filter(user=bina).exists()
        assert sessions.get("/api/v1/auth/me").status_code == 200  # still signed in
        # Her password is her own and her second factor is still the second step.
        opened = sign_in(bina.email, DEFAULT_PASSWORD)
        assert opened.status_code == 202, opened.content
        assert opened.json()["mfa_required"] is True

    def test_nor_by_an_admin(self, team_admin, admin):
        before = state(admin)
        refused = reset(signed_in(team_admin), admin)
        assert refused.status_code == 404  # not even known to exist
        assert state(admin) == before
