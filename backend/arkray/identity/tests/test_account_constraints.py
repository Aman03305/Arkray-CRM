"""Every lifecycle rule is a database constraint: proven by writes that bypass the ORM."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from arkray.identity.models import (
    AccountToken,
    AuthThrottleEvent,
    TokenPurpose,
    TokenStatus,
    User,
    UserStatus,
)
from tests.factories import InvitedUserFactory, UserFactory

pytestmark = pytest.mark.django_db


def violates(sql, params):
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


class TestUserLifecycle:
    @pytest.mark.parametrize(
        ("assignments", "label"),
        [
            ("status = 'active', is_active = false", "active but blocked"),
            ("status = 'deactivated', is_active = true", "deactivated but allowed in"),
            ("status = 'deactivated', deactivated_at = NULL, is_active = false", "no date"),
            ("status = 'active', activated_at = NULL", "active, never activated"),
            ("deactivated_at = now()", "active with a deactivation date"),
            ("status = 'suspended'", "unknown status"),
            ("password = '!unusable'", "active without a usable password"),
            ("first_name = ''", "blank first name"),
            ("email = 'Mixed@Example.test'", "non-canonical email"),
        ],
    )
    def test_impossible_states_are_rejected_for_an_active_user(self, assignments, label):
        user = UserFactory()
        violates(f"UPDATE identity_user SET {assignments} WHERE id = %s", [user.pk])  # noqa: S608

    def test_an_invited_user_cannot_carry_an_activation_date(self):
        user = InvitedUserFactory()
        violates("UPDATE identity_user SET activated_at = now() WHERE id = %s", [user.pk])

    def test_users_with_history_cannot_be_hard_deleted(self):
        user = InvitedUserFactory()
        AccountToken.objects.create(
            user=user,
            purpose=TokenPurpose.INVITATION,
            expires_at=timezone.now() + timedelta(days=1),
        )
        with pytest.raises(ProtectedError):
            user.delete()
        assert User.objects.filter(pk=user.pk).exists()


class TestAccountTokens:
    def token(self, user=None, **fields):
        now = timezone.now()
        defaults = {
            "user": user or InvitedUserFactory(),
            "purpose": TokenPurpose.INVITATION,
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
        }
        return AccountToken.objects.create(**{**defaults, **fields})

    def test_only_one_live_token_per_user_and_purpose(self):
        first = self.token()
        with pytest.raises(IntegrityError), transaction.atomic():
            self.token(user=first.user)
        # A different purpose, or a revoked predecessor, is fine.
        self.token(user=first.user, purpose=TokenPurpose.PASSWORD_RESET)
        AccountToken.objects.filter(pk=first.pk).update(
            status=TokenStatus.REVOKED, revoked_at=timezone.now()
        )
        self.token(user=first.user)

    @pytest.mark.parametrize(
        ("assignments", "label"),
        [
            ("status = 'used'", "used without a date or a secret"),
            ("used_at = now()", "a use date on a live token"),
            ("status = 'revoked'", "revoked without a date"),
            ("token_hash = 'not-a-sha256-digest'", "malformed digest"),
            ("token_hash = repeat('a', 64)", "digest without issue date"),
            ("sent_at = now()", "sent before it was issued"),
            ("expires_at = created_at", "expires on creation"),
            ("purpose = 'magic_link'", "unknown purpose"),
            ("status = 'unknown'", "unknown status"),
        ],
    )
    def test_impossible_token_states_are_rejected(self, assignments, label):
        token = self.token()
        violates(f"UPDATE identity_account_token SET {assignments} WHERE id = %s", [token.pk])  # noqa: S608

    def test_digests_are_unique(self):
        issued = {"token_hash": "a" * 64, "issued_at": timezone.now()}
        self.token(**issued)
        with pytest.raises(IntegrityError), transaction.atomic():
            self.token(purpose=TokenPurpose.PASSWORD_RESET, **issued)


class TestThrottleEvents:
    def test_kind_and_identifier_format_are_enforced(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            AuthThrottleEvent.objects.create(kind="other")
        with pytest.raises(IntegrityError), transaction.atomic():
            AuthThrottleEvent.objects.create(
                kind="login_failure", identifier_hash="user@example.test"
            )


def test_status_values_match_the_constraint():
    assert set(UserStatus.values) == {"invited", "active", "deactivated"}
