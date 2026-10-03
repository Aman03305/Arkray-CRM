import pytest
from django.db import IntegrityError, connection, transaction

from arkray.identity.models import Role, User, UserStatus
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_email_is_normalised_to_lower_case():
    user = User.objects.create_user("  Rahul.Sharma@Example.COM ", "Rahul", "Sharma")
    assert user.email == "rahul.sharma@example.com"


def test_lookup_by_email_is_case_insensitive():
    user = User.objects.create_user("rahul@example.com", "Rahul")
    assert User.objects.get_by_natural_key("RAHUL@Example.com") == user


def test_email_uniqueness_is_case_insensitive():
    User.objects.create_user("rahul@example.com", "Rahul")
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user("RAHUL@EXAMPLE.COM", "Another Rahul")


def test_database_rejects_mixed_case_email_written_outside_the_orm():
    user = UserFactory()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE identity_user SET email = 'Mixed@Example.com' WHERE id = %s", [user.pk]
        )


def test_user_created_without_password_cannot_sign_in():
    user = User.objects.create_user("invitee@example.com", "Invitee")
    assert not user.has_usable_password()
    assert (user.status, user.is_active) == (UserStatus.INVITED, False)


def test_database_rejects_unknown_role():
    user = UserFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=user.pk).update(role="superuser")


def test_database_rejects_blank_name():
    with pytest.raises(IntegrityError), transaction.atomic():
        UserFactory(first_name="")


def test_create_superuser_bootstraps_an_admin():
    admin = User.objects.create_superuser(
        "root@example.com", "First", "Admin", password="a-strong-passphrase"
    )
    assert admin.role == Role.ADMIN
    assert (admin.status, admin.is_active) == (UserStatus.ACTIVE, True)
    assert admin.check_password("a-strong-passphrase")
