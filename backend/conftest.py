from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from tests.ai_fixtures import ai_on, golden, scripted  # noqa: F401 — shared fixtures
from tests.factories import (
    AdminFactory,
    TeamAdminFactory,
    UserFactory,
    ensure_lead_configuration,
    ensure_pipeline_configuration,
)
from tests.helpers import signed_in


@pytest.fixture(autouse=True)
def _isolated_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user_a(db):
    return UserFactory(first_name="Rahul", last_name="Sharma")


@pytest.fixture
def user_b(db):
    return UserFactory(first_name="Priya", last_name="Patel")


@pytest.fixture
def admin(db):
    """A super administrator (ADR-0035: the role value "admin")."""
    return AdminFactory(first_name="Anita", last_name="Admin")


# --- teams (ADR-0035): Tara manages Rahul (user_a) and Mohan; Priya (user_b) is in no team ---
@pytest.fixture
def team_admin(db):
    return TeamAdminFactory(first_name="Tara", last_name="Lead")


@pytest.fixture
def team_member(user_a, team_admin):
    """user_a (Rahul), in team_admin's team."""
    user_a.manager = team_admin
    user_a.save(update_fields=["manager"])
    return user_a


@pytest.fixture
def team_member_2(team_admin):
    return UserFactory(first_name="Mohan", last_name="Das", manager=team_admin)


@pytest.fixture
def other_team_admin(db):
    """Another team's administrator, with a member of their own (`other_team_member`)."""
    return TeamAdminFactory(first_name="Omar", last_name="Khan")


@pytest.fixture
def other_team_member(other_team_admin):
    return UserFactory(first_name="Kavya", last_name="Iyer", manager=other_team_admin)


@pytest.fixture
def api_client():
    """Anonymous client. CSRF checks are off (as for Django's test client); security
    tests that exercise CSRF use `csrf_client`."""
    return APIClient()


@pytest.fixture
def csrf_client():
    return APIClient(enforce_csrf_checks=True)


@pytest.fixture
def admin_client(admin):
    return signed_in(admin)


@pytest.fixture
def user_a_client(user_a):
    return signed_in(user_a)


@pytest.fixture
def lead_configuration(transactional_db):
    """Lead statuses and sources for transactional tests (see ensure_lead_configuration)."""
    ensure_lead_configuration()


@pytest.fixture
def crm_configuration(lead_configuration):
    """Lead statuses and sources plus the default pipeline, for transactional tests."""
    ensure_pipeline_configuration()
