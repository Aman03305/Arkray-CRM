"""The account-takeover settings of the review of 73ff1e3 (L1, L3) in each environment.

`MFA_REQUIRED_FOR_ALL_USERS` (two-step verification for every user, not only
administrators) is on by default in production, off in development and tests; an
organisational policy, so production accepts it off, but refuses a value that isn't
recognisably true or false (env.bool reads "ture" as false). The sign-in caps are fixed
values in config/settings/base.py, checked for consistency at start (arkray.E031).
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import override_settings

from arkray.identity.throttling import check_daily_window
from tests.architecture.test_production_settings import load_production_settings


def test_every_user_needs_a_second_factor_by_default_in_production():
    printed = load_production_settings("settings.MFA_REQUIRED_FOR_ALL_USERS")
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.strip() == "True"


def test_production_accepts_it_off_as_an_organisational_choice():
    printed = load_production_settings(
        "settings.MFA_REQUIRED_FOR_ALL_USERS", MFA_REQUIRED_FOR_ALL_USERS="false"
    )
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.strip() == "False"


@pytest.mark.parametrize("value", ["", "ture", "for sales only"])
def test_an_unrecognisable_value_is_refused(value):
    result = load_production_settings(MFA_REQUIRED_FOR_ALL_USERS=value)
    assert result.returncode != 0
    assert "MFA_REQUIRED_FOR_ALL_USERS" in result.stderr


def test_off_in_development_and_tests():
    assert settings.MFA_REQUIRED_FOR_ALL_USERS is False


def test_the_policy_values_are_the_reviewed_ones():
    """Owner decisions (docs/deployment.md): change them here and in the docs together."""
    assert settings.LOGIN_DAILY_FAILURE_CAP == 50
    assert settings.LOGIN_DAILY_WINDOW_S == 24 * 60 * 60
    assert settings.LOGIN_LOCK_MAX_AGE_S == 30 * 24 * 60 * 60
    assert settings.LOGIN_NETWORK_IPV4_PREFIX == 24
    assert settings.ADMIN_PROMOTION_COOLING_OFF_S == 7 * 24 * 60 * 60
    assert check_daily_window() == []


def test_a_window_longer_than_the_evidence_is_kept_is_refused():
    with override_settings(LOGIN_DAILY_WINDOW_S=settings.AUTH_THROTTLE_RETENTION_S + 1):
        assert [error.id for error in check_daily_window()] == ["arkray.E031"]
    with override_settings(LOGIN_DAILY_FAILURE_CAP=0):
        assert [error.id for error in check_daily_window()] == ["arkray.E031"]
