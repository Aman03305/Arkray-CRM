import pytest
from django.contrib.auth.models import AnonymousUser

from arkray.identity.policy import Capability, capabilities_for, has_capability
from tests.factories import AdminFactory, UserFactory

pytestmark = pytest.mark.django_db


def test_sales_user_capabilities_are_exactly_own_workspace_and_ai():
    assert capabilities_for(UserFactory()) == {Capability.CRM_ACCESS_OWN, Capability.AI_QUERY}


def test_admin_capabilities_are_explicit():
    assert capabilities_for(AdminFactory()) == set(Capability)


@pytest.mark.parametrize(
    "capability",
    [
        Capability.CRM_VIEW_ALL,
        Capability.WORKSPACE_VIEW_ANY,
        Capability.CRM_ASSIGN_ANY,
        Capability.CRM_MANAGE_ANY,
        Capability.USERS_MANAGE,
        Capability.CONFIG_MANAGE,
        Capability.AUDIT_VIEW,
    ],
)
def test_sales_user_lacks_privileged_capabilities(capability):
    assert not has_capability(UserFactory(), capability)


def test_deactivated_admin_has_nothing():
    assert capabilities_for(AdminFactory(is_active=False)) == frozenset()


def test_anonymous_has_nothing():
    assert capabilities_for(AnonymousUser()) == frozenset()
    assert capabilities_for(None) == frozenset()


def test_unknown_role_is_denied_by_default():
    user = UserFactory.build(role="future_role")
    assert capabilities_for(user) == frozenset()
