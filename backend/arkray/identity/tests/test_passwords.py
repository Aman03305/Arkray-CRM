"""Password policy: length and known-bad passwords, not composition rules."""

import pytest
from django.contrib.auth.password_validation import CommonPasswordValidator, validate_password
from django.core.exceptions import ValidationError

from arkray.core.errors import InvalidInputError
from arkray.identity.passwords import check_new_password
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return UserFactory.build(
        email="rahul.sharma@example.test", first_name="Rahul", last_name="Sharma"
    )


def a_common_password_of_at_least(length):
    return next(p for p in sorted(CommonPasswordValidator().passwords) if len(p) >= length)


@pytest.mark.parametrize(
    "password",
    [
        "correct horse battery staple",  # a long passphrase with spaces
        "lowercaseonlybutlong",  # no "complexity theatre"
        "Zq8#pLm2!vRt",  # exactly 12
        "x" * 128,
    ],
)
def test_long_uncommon_passwords_are_accepted(user, password):
    validate_password(password, user)


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("Short1!pass", "too short"),
        ("x" * 129, "too long"),
        ("123456789012345", "entirely numeric"),
        ("rahul.sharma@example.test", "too similar"),
        ("my-arkray-password-1", "name of this application"),
    ],
)
def test_weak_passwords_are_rejected_with_a_reason(user, password, reason):
    with pytest.raises(ValidationError) as caught:
        validate_password(password, user)
    assert reason in " ".join(caught.value.messages)


def test_common_passwords_are_rejected(user):
    with pytest.raises(ValidationError):
        validate_password(a_common_password_of_at_least(12), user)


def test_policy_failures_become_field_errors(user):
    with pytest.raises(InvalidInputError) as caught:
        check_new_password("short", user, field="password")
    assert caught.value.details["password"]
