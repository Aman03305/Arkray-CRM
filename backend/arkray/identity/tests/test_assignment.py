"""Write authorization for workspaces and the rules for who can own CRM records."""

import pytest
from django.contrib.auth.models import AnonymousUser

from arkray.core.access import AccessScope
from arkray.core.errors import PermissionDeniedError
from arkray.identity.models import Role
from arkray.identity.policy import ROLE_CAPABILITIES, Capability
from arkray.identity.selectors import assignable_users, lock_assignable_user
from arkray.identity.workspaces import authorize_write
from tests.factories import AdminFactory, InvitedUserFactory, TeamAdminFactory, UserFactory

pytestmark = pytest.mark.django_db


class TestAuthorizeWrite:
    def test_everyone_may_write_in_their_own_workspace(self, user_a, admin):
        authorize_write(user_a, AccessScope.own(user_a.pk))
        authorize_write(admin, AccessScope.own(admin.pk))

    def test_sales_users_may_not_write_anywhere_else(self, user_a, user_b):
        for scope in (
            AccessScope.for_user(user_a.pk, user_b.pk),
            AccessScope.organization(user_a.pk),
        ):
            with pytest.raises(PermissionDeniedError):
                authorize_write(user_a, scope)

    def test_admins_write_in_delegated_workspaces_through_manage_any(
        self, admin, user_a, monkeypatch
    ):
        authorize_write(admin, AccessScope.for_user(admin.pk, user_a.pk))
        authorize_write(admin, AccessScope.organization(admin.pk))
        monkeypatch.setitem(
            ROLE_CAPABILITIES,
            Role.ADMIN,
            ROLE_CAPABILITIES[Role.ADMIN] - {Capability.CRM_MANAGE_ANY},
        )
        with pytest.raises(PermissionDeniedError):
            authorize_write(admin, AccessScope.for_user(admin.pk, user_a.pk))

    def test_a_scope_resolved_for_someone_else_is_refused(self, admin, user_a):
        with pytest.raises(PermissionDeniedError):
            authorize_write(user_a, AccessScope.organization(admin.pk))
        with pytest.raises(PermissionDeniedError):
            authorize_write(admin, AccessScope.own(user_a.pk))

    def test_anonymous_and_deactivated_actors_are_refused(self, user_a):
        with pytest.raises(PermissionDeniedError):
            authorize_write(AnonymousUser(), AccessScope.own(user_a.pk))
        gone = UserFactory(is_active=False)
        with pytest.raises(PermissionDeniedError):
            authorize_write(gone, AccessScope.own(gone.pk))


class TestAssignableUsers:
    def test_only_active_users_whose_role_works_in_the_crm(self, monkeypatch):
        rahul, anita = UserFactory(first_name="Rahul"), AdminFactory(first_name="Anita")
        gone, invited = UserFactory(is_active=False), InvitedUserFactory()
        assert set(assignable_users(actor=anita)) == {rahul, anita}
        assert lock_assignable_user(rahul.pk)
        assert lock_assignable_user(anita.pk)
        assert not lock_assignable_user(gone.pk)
        assert not lock_assignable_user(invited.pk)
        assert not lock_assignable_user(UserFactory.build().pk)
        # A role without a CRM workspace (e.g. a future auditor) can't own records.
        monkeypatch.setitem(ROLE_CAPABILITIES, Role.SALES_USER, frozenset())
        assert not lock_assignable_user(rahul.pk)
        assert set(assignable_users(actor=anita)) == {anita}

    def test_search_matches_name_or_email(self, admin):
        rahul = UserFactory(first_name="Rahul", last_name="Sharma", email="rahul@arkray.example")
        UserFactory(first_name="Priya", last_name="Patel", email="priya@arkray.example")
        assert list(assignable_users(actor=admin, q="sharma")) == [rahul]
        assert list(assignable_users(actor=admin, q="rahul@arkray")) == [rahul]
        assert list(assignable_users(actor=admin, q="rahul patel")) == []

    def test_a_team_administrator_assigns_within_their_team(
        self, admin, team_admin, team_member, team_member_2, user_b, other_team_member
    ):
        """ADR-0035: themselves and their team's active members; never a deactivated or
        invited member, another team's, a user in no team, or an administrator."""
        gone = UserFactory(is_active=False, manager=team_admin)
        invited = InvitedUserFactory(manager=team_admin)
        found = set(assignable_users(actor=team_admin))
        assert found == {team_admin, team_member, team_member_2}
        assert not found & {gone, invited, user_b, other_team_member, admin}
        assert list(assignable_users(actor=team_admin, q="mohan")) == [team_member_2]
        assert list(assignable_users(actor=team_admin, q="priya")) == []
        # The super administrator's list includes team administrators (they own records).
        assert team_admin in set(assignable_users(actor=admin))

    def test_nobody_else_gets_anyone(self, user_a, user_b):
        assert list(assignable_users(actor=user_a)) == []
        assert list(assignable_users(actor=TeamAdminFactory(is_active=False))) == []
