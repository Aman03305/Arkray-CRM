"""Pseudonymising a former employee can't be undone (adversarial review of 73ff1e3, L4 /
PRIV-1).

`pseudonymised()` was an email-domain test and nothing in user administration asked it: an
administrator could PATCH the old name back, or reactivate the account (an unusable password
made that an invitation), change its email to their own mailbox and accept it: the former
employee's id, owning their historical deals, was a live login again.

Now `User.pseudonymised_at` marks it, and the database holds such an account to its pseudonym
(deactivated, an unusable password, the pseudonym's name and email: CHECK
`identity_user_pseudonym_final`; the mark itself can't be cleared: trigger
`identity_user_pseudonym_kept`). Every user-administration change is refused first with 422
`account_pseudonymised`, before anything is written.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from importlib import import_module

import pytest
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.db import IntegrityError, InternalError, connection, transaction
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core.models import OutboxEvent
from arkray.identity import services
from arkray.identity.models import AccountToken, TokenPurpose, User
from arkray.privacy import staff
from tests.factories import UserFactory
from tests.helpers import drain_outbox, signed_in
from tests.mfa_helpers import enrol

pytestmark = pytest.mark.django_db

USERS = "/api/v1/admin/users"


@pytest.fixture
def former(admin):
    user = UserFactory(first_name="Kiran", last_name="Desai", email="kiran.desai@arkray.example")
    services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
    staff.pseudonymise(user.pk, operator_id=admin.pk)
    drain_outbox()
    mail.outbox.clear()
    return User.objects.get(pk=user.pk)


def row(user: User) -> tuple[object, ...]:
    fresh = User.objects.get(pk=user.pk)
    return (
        fresh.first_name,
        fresh.last_name,
        fresh.email,
        fresh.role,
        fresh.status,
        fresh.password,
        fresh.session_epoch,
        fresh.version,
    )


def side_effects() -> tuple[int, int]:
    return AccountToken.objects.count(), OutboxEvent.objects.count()


def refused(response) -> None:
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "account_pseudonymised"


@pytest.mark.parametrize(
    ("method", "action", "payload"),
    [
        ("patch", "", {"first_name": "Kiran", "last_name": "Desai"}),
        ("patch", "", {"role": "admin"}),
        ("post", "activate", None),
        ("post", "change-email", {"email": "mallory@evil.test"}),
        ("post", "set-password", {"new_password": "Mallory-Chosen-Pass-61"}),
        ("post", "resend-invitation", None),
        ("post", "mfa/reset", None),
        ("post", "clear-sign-in-lock", None),
    ],
    ids=[
        "rename",
        "promote",
        "reactivate",
        "change-email",
        "set-password",
        "invite",
        "mfa",
        "lock",
    ],
)
def test_every_user_administration_change_is_refused(admin_client, former, method, action, payload):
    before, effects = row(former), side_effects()
    body = None if payload is None else {**payload, "version": former.version}
    target = f"{USERS}/{former.pk}{'/' + action if action else ''}"
    refused(getattr(admin_client, method)(target, body, format="json"))
    assert row(former) == before
    assert side_effects() == effects
    drain_outbox()
    assert mail.outbox == []


def test_the_review_revival_chain_fails_at_its_first_step(admin_client, former):
    refused(admin_client.post(f"{USERS}/{former.pk}/activate"))
    refused(
        admin_client.post(
            f"{USERS}/{former.pk}/change-email",
            {"email": "mallory@evil.test", "version": former.version},
            format="json",
        )
    )
    assert not AccountToken.objects.filter(user=former, status="pending").exists()


def test_a_support_session_for_one_is_refused(admin_client, former):
    response = admin_client.post(
        "/api/v1/admin/support-sessions", {"user": str(former.pk)}, format="json"
    )
    refused(response)


def test_deactivating_again_changes_nothing(admin_client, former):
    before = row(former)
    assert admin_client.post(f"{USERS}/{former.pk}/deactivate").status_code == 200
    assert row(former) == before


def test_the_services_refuse_it_too(admin, former):
    with pytest.raises(services.AccountPseudonymisedError):
        services.update_user(
            actor_id=admin.pk, user_id=former.pk, version=former.version, changes={"last_name": "D"}
        )
    with pytest.raises(services.AccountPseudonymisedError):
        services.reactivate_user(actor_id=admin.pk, user_id=former.pk)


def test_the_operator_mfa_reset_finds_nothing_to_restore(admin, former):
    from arkray.identity import mfa_services

    with pytest.raises(services.AccountPseudonymisedError):
        mfa_services.operator_reset(former.email, "ticket 1", operator_id=admin.pk)


def test_it_is_marked_and_reported(admin_client, former):
    assert former.pseudonymised_at is not None
    assert staff.pseudonymised(former)
    detail = admin_client.get(f"{USERS}/{former.pk}").json()
    assert detail["pseudonymised_at"] is not None


@pytest.mark.parametrize(
    "assignment",
    [
        # A usable password too, or the active-account rule alone would refuse it.
        "status = 'active', is_active = true, deactivated_at = NULL,"
        " password = 'md5$abc$0123456789abcdef0123456789abcdef'",
        "first_name = 'Kiran'",
        "last_name = 'Desai'",
        "email = 'mallory@evil.test'",
        "password = 'md5$abc$0123456789abcdef0123456789abcdef'",
    ],
)
def test_the_database_holds_it_to_its_pseudonym(former, assignment):
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE identity_user SET {assignment} WHERE id = %s",  # noqa: S608 — fixed text
            [former.pk],
        )


def test_the_mark_cant_be_cleared(former):
    with (
        pytest.raises((IntegrityError, InternalError)),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE identity_user SET pseudonymised_at = NULL WHERE id = %s", [former.pk]
        )


def test_an_ordinary_former_user_is_unaffected(admin_client, admin):
    """Not pseudonymised: reactivation works as before."""
    user = UserFactory()
    services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
    response = admin_client.post(f"{USERS}/{user.pk}/activate")
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["pseudonymised_at"] is None


def test_a_pseudonymised_account_with_a_second_factor_left_none(admin):
    user = UserFactory()
    enrol(user)
    services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
    staff.pseudonymise(user.pk, operator_id=admin.pk)
    assert not User.objects.get(pk=user.pk).has_usable_password()
    assert signed_in(admin).post(f"{USERS}/{user.pk}/mfa/reset").status_code == 422


# --- identity.0008's backfill (review round 1: IDENTITY-4 and IDENTITY-5) ---------------------
def backfill():
    """Run the migration's backfill, as `migrate` does (the schema is already the latest)."""
    migration = import_module("arkray.identity.migrations.0008_account_takeover_limits")
    with connection.cursor() as cursor:
        return migration.backfill_pseudonyms(cursor)


def old_release_pseudonymised(admin, user: User) -> None:
    """What the previous release's pseudonymisation left: the pseudonym, no mark, and its
    audit event (`user.pseudonymised`, always written with it)."""
    User.objects.filter(pk=user.pk).update(
        first_name="Former user",
        last_name=user.pk.hex[:8],
        email=f"former-{user.pk.hex}@pseudonymised.invalid",
        status="deactivated",
        is_active=False,
        deactivated_at=timezone.now(),
        password="!unusable",  # the unusable mark
    )
    audit.record(
        "user.pseudonymised",
        actor_id=admin.pk,
        target_type="user",
        target_id=user.pk,
        subject_user_id=user.pk,
        metadata={},
    )


def assert_pseudonymised(user: User) -> None:
    user.refresh_from_db()
    assert user.pseudonymised_at is not None
    assert (user.status, user.first_name, user.last_name, user.email) == (
        "deactivated",
        "Former user",
        user.pk.hex[:8],
        f"former-{user.pk.hex}@pseudonymised.invalid",
    )
    assert not user.has_usable_password()


def test_the_migration_marks_what_the_email_rule_called_pseudonymised():
    """An account pseudonymised before this release and revived halfway (reactivated: an
    invitation to its .invalid address, which can't be delivered) is marked and put back
    under its pseudonym; its reissued invitation is revoked."""
    revived = UserFactory(first_name="Kiran", last_name="Desai")
    User.objects.filter(pk=revived.pk).update(
        email=f"former-{revived.pk.hex}@pseudonymised.invalid",
        status="invited",
        is_active=False,
        activated_at=None,
        password="!unusable",  # the unusable mark
    )
    invitation = AccountToken.objects.create(
        user=revived,
        purpose=TokenPurpose.INVITATION,
        expires_at=timezone.now() + timedelta(days=1),
    )
    ordinary = UserFactory()
    report = backfill()
    assert_pseudonymised(revived)
    invitation.refresh_from_db()
    assert invitation.status == "revoked"
    assert User.objects.get(pk=ordinary.pk).pseudonymised_at is None
    assert report.marked == [str(revived.pk)]


def test_a_revival_completed_before_the_upgrade_is_undone(admin):
    """IDENTITY-4: the finding's whole chain had already happened (the email pointed at the
    administrator's mailbox, the invitation accepted): no longer in the pseudonym domain, but
    its `user.pseudonymised` audit event says what it is. Put back under its pseudonym,
    deactivated, its password unusable, its sessions ended."""
    revived = UserFactory(first_name="Kiran", last_name="Desai")
    old_release_pseudonymised(admin, revived)
    User.objects.filter(pk=revived.pk).update(
        first_name="Kiran",
        last_name="Desai",
        email="mallory.inbox@evil.test",
        status="active",
        is_active=True,
        deactivated_at=None,
        password=make_password(secrets.token_urlsafe(12)),
    )
    epoch = User.objects.get(pk=revived.pk).session_epoch
    report = backfill()
    assert_pseudonymised(revived)
    assert revived.session_epoch == epoch + 1
    assert report.marked == [str(revived.pk)]
    assert staff.pseudonymised(revived)


def test_an_untouched_pseudonym_is_marked_even_without_its_audit_event(admin):
    """An audit purge may have removed the event: a deactivated, password-less account under
    its own pseudonym is one all the same."""
    former = UserFactory()
    old_release_pseudonymised(admin, former)
    with connection.cursor() as cursor:  # as if purged: the operator's procedure, as the owner
        cursor.execute("SET session_replication_role = replica")
        cursor.execute("DELETE FROM audit_event WHERE action = 'user.pseudonymised'")
        cursor.execute("SET session_replication_role = origin")
    report = backfill()
    assert_pseudonymised(former)
    assert report.marked == [str(former.pk)]


def test_a_live_account_squatting_a_pseudonym_address_is_reported_never_pseudonymised(user_a):
    """IDENTITY-5: an administrator could give a live account a pseudonym-domain address; the
    email-domain rule alone would have pseudonymised it, irreversibly. Now it needs its own
    pseudonym and no usable password, or the audit event: otherwise it is reported."""
    other = UserFactory()
    for squatted in (
        f"former-{other.pk.hex}@pseudonymised.invalid",
        f"former-{user_a.pk.hex}@pseudonymised.invalid",
    ):
        User.objects.filter(pk=user_a.pk).update(email=squatted)
        report = backfill()
        user_a.refresh_from_db()
        assert user_a.pseudonymised_at is None
        assert (user_a.status, user_a.email) == ("active", squatted)
        assert user_a.has_usable_password()
        assert report.marked == []
        assert report.unmarked == [str(user_a.pk)]


def test_a_squatter_of_a_former_users_pseudonym_is_moved_aside(admin, user_a):
    """The former user's pseudonym is theirs: an account holding it (squatted before the
    upgrade) gets an address reserved for itself, stays unmarked, and is reported."""
    former = UserFactory()
    old_release_pseudonymised(admin, former)
    User.objects.filter(pk=former.pk).update(
        email="revived@evil.test",
        status="active",
        is_active=True,
        deactivated_at=None,
        password=make_password(secrets.token_urlsafe(12)),
    )
    User.objects.filter(pk=user_a.pk).update(email=f"former-{former.pk.hex}@pseudonymised.invalid")
    report = backfill()
    assert_pseudonymised(former)
    user_a.refresh_from_db()
    assert user_a.pseudonymised_at is None
    assert user_a.email == f"reserved-{user_a.pk.hex}@pseudonymised.invalid"
    assert report.moved_aside == [str(user_a.pk)]


def test_the_backfill_is_a_no_op_without_former_users():
    UserFactory.create_batch(3)
    report = backfill()
    assert (report.marked, report.moved_aside, report.unmarked) == ([], [], [])
    assert not User.objects.filter(pseudonymised_at__isnull=False).exists()


@pytest.mark.parametrize(
    "address",
    [
        "former-0123456789abcdef0123456789abcdef@pseudonymised.invalid",
        "anyone@pseudonymised.invalid",
        "Someone@Sub.PSEUDONYMISED.invalid",
    ],
)
def test_the_pseudonym_domain_is_reserved(admin_client, user_a, address):
    """IDENTITY-5: the API gave another user a former user's pseudonym, and pseudonymising the
    real one then failed (IntegrityError)."""
    changed = admin_client.post(
        f"{USERS}/{user_a.pk}/change-email",
        {"email": address, "version": User.objects.get(pk=user_a.pk).version},
        format="json",
    )
    assert changed.status_code == 400, changed.content
    assert "email" in changed.json()["error"]["details"]
    created = admin_client.post(
        USERS, {"first_name": "Neha", "email": address, "role": "sales_user"}, format="json"
    )
    assert created.status_code == 400, created.content
    assert User.objects.get(pk=user_a.pk).email != address.lower()
