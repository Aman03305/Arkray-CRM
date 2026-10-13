import pytest
from django.contrib.auth.models import AnonymousUser

from arkray.identity.models import Role
from arkray.identity.permissions import requires, requires_any
from arkray.identity.policy import (
    Capability,
    administrator_roles,
    capabilities_for,
    has_any_capability,
    has_capability,
    roles_with,
)
from tests.factories import AdminFactory, TeamAdminFactory, UserFactory

pytestmark = pytest.mark.django_db

# A team administrator's counterparts of the organisation-wide capabilities (ADR-0035).
TEAM_CAPABILITIES = frozenset(
    {
        Capability.CRM_VIEW_TEAM,
        Capability.WORKSPACE_VIEW_TEAM,
        Capability.CRM_ASSIGN_TEAM,
        Capability.CRM_MANAGE_TEAM,
        Capability.USERS_MANAGE_TEAM,
        Capability.SUPPORT_ACCESS_TEAM,
    }
)
ORGANISATION_WIDE = frozenset(
    {
        Capability.CRM_VIEW_ALL,
        Capability.WORKSPACE_VIEW_ANY,
        Capability.CRM_ASSIGN_ANY,
        Capability.CRM_MANAGE_ANY,
        Capability.USERS_MANAGE,
        Capability.CONFIG_MANAGE,
        Capability.AUDIT_VIEW,
        Capability.SUPPORT_ACCESS,
        Capability.PRIVACY_MANAGE,
    }
)


def test_sales_user_capabilities_are_exactly_own_workspace_and_ai():
    assert capabilities_for(UserFactory()) == {Capability.CRM_ACCESS_OWN, Capability.AI_QUERY}


def test_admin_capabilities_are_explicit():
    """The super administrator keeps every organisation-wide capability and holds none of
    the team ones (ADR-0035: a check written for a team's reach never sees them)."""
    assert capabilities_for(AdminFactory()) == set(Capability) - TEAM_CAPABILITIES


def test_team_admin_capabilities_are_the_team_counterparts_only():
    """No organisation-wide capability, no configuration, audit or privacy (ADR-0035)."""
    assert capabilities_for(TeamAdminFactory()) == TEAM_CAPABILITIES | {
        Capability.CRM_ACCESS_OWN,
        Capability.AI_QUERY,
    }
    assert not capabilities_for(TeamAdminFactory()) & ORGANISATION_WIDE


def test_every_capability_is_organisation_wide_team_or_everyones():
    """A new capability has to be placed deliberately in one of these."""
    assert set(Capability) == ORGANISATION_WIDE | TEAM_CAPABILITIES | {
        Capability.CRM_ACCESS_OWN,
        Capability.AI_QUERY,
    }


@pytest.mark.parametrize("capability", sorted(ORGANISATION_WIDE | TEAM_CAPABILITIES))
def test_sales_user_lacks_privileged_capabilities(capability):
    assert not has_capability(UserFactory(), capability)


def test_administrator_roles_are_both_kinds_and_the_last_administrator_counts_super_admins():
    assert administrator_roles() == {Role.ADMIN, Role.TEAM_ADMIN}
    assert roles_with(Capability.USERS_MANAGE) == {Role.ADMIN}
    assert roles_with(Capability.USERS_MANAGE_TEAM) == {Role.TEAM_ADMIN}


def test_has_any_capability():
    team_admin = TeamAdminFactory()
    assert has_any_capability(team_admin, Capability.USERS_MANAGE, Capability.USERS_MANAGE_TEAM)
    assert not has_any_capability(team_admin, Capability.USERS_MANAGE, Capability.AUDIT_VIEW)
    assert not has_any_capability(
        UserFactory(), Capability.USERS_MANAGE, Capability.USERS_MANAGE_TEAM
    )
    gone = TeamAdminFactory(is_active=False)
    assert not has_any_capability(gone, Capability.USERS_MANAGE, Capability.USERS_MANAGE_TEAM)
    assert not has_any_capability(None, Capability.USERS_MANAGE_TEAM)


def test_requires_any_is_one_class_per_pair_and_never_a_single_capability():
    pair = (Capability.USERS_MANAGE, Capability.USERS_MANAGE_TEAM)
    assert requires_any(*pair) is requires_any(*pair)
    assert requires_any(*pair) is not requires(Capability.USERS_MANAGE)
    with pytest.raises(ValueError, match="at least two"):
        requires_any(Capability.USERS_MANAGE)


def test_deactivated_admin_has_nothing():
    assert capabilities_for(AdminFactory(is_active=False)) == frozenset()
    assert capabilities_for(TeamAdminFactory(is_active=False)) == frozenset()


def test_anonymous_has_nothing():
    assert capabilities_for(AnonymousUser()) == frozenset()
    assert capabilities_for(None) == frozenset()


def test_unknown_role_is_denied_by_default():
    user = UserFactory.build(role="future_role")
    assert capabilities_for(user) == frozenset()
