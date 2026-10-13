"""Teams in workspace resolution and write authorization (ADR-0035).

A team administrator (Tara, role team_admin) reaches herself and her team's sales users
(Rahul and Mohan, any status) and nobody else: workspace `all` is her team (scope TEAM), a
member's id opens that member's workspace (USER), and every other id answers the same 404 as
a user who doesn't exist. Writes need crm.manage_team and every owner of the scope still in
her team, re-read at the write: a scope built elsewhere can't widen it, and a member who left
the team is out of reach at once. Priya is in no team; Kavya is in Omar's.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import AnonymousUser

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope, ScopeKind
from arkray.core.errors import NotFoundError, PermissionDeniedError
from arkray.identity import selectors, teams
from arkray.identity.models import Role, User
from arkray.identity.policy import ROLE_CAPABILITIES, Capability
from arkray.identity.workspaces import (
    AUDIT_ACTION_WORKSPACE_ACCESSED,
    WORKSPACE_ORGANIZATION,
    WORKSPACE_SELF,
    authorize_write,
    resolve_workspace,
    workspace_segment,
)
from tests.factories import InvitedUserFactory, TeamAdminFactory, UserFactory
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db


def access_events():
    return AuditEvent.objects.filter(action=AUDIT_ACTION_WORKSPACE_ACCESSED)


@pytest.fixture
def team(team_admin, team_member, team_member_2):
    """Tara's whole team: Rahul, Mohan, a deactivated member and an invited one."""
    gone = UserFactory(first_name="Gita", is_active=False, manager=team_admin)
    invited = InvitedUserFactory(first_name="Ishan", manager=team_admin)
    return {"rahul": team_member, "mohan": team_member_2, "gone": gone, "invited": invited}


@pytest.fixture
def outsiders(admin, user_b, other_team_admin, other_team_member):
    """Everyone Tara must not reach: Anita (super admin), Priya (no team), Omar and Kavya."""
    return {"anita": admin, "priya": user_b, "omar": other_team_admin, "kavya": other_team_member}


# --- the scope itself -------------------------------------------------------------------------
class TestTheTeamScope:
    def test_it_holds_the_administrator_and_the_members(self):
        tara, rahul, mohan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        scope = AccessScope.team(tara, [rahul, mohan])
        assert (scope.kind, scope.owner_ids) == (ScopeKind.TEAM, {tara, rahul, mohan})
        assert scope.is_team
        assert scope.is_delegated
        assert not scope.is_organization_wide

    def test_an_empty_team_is_the_administrator_alone_never_everyone(self):
        tara = uuid.uuid4()
        scope = AccessScope.team(tara, [])
        assert scope.owner_ids == {tara}
        assert scope.permits_owner(tara)
        assert not scope.permits_owner(uuid.uuid4())
        assert scope.condition("owner_id") != AccessScope.organization(tara).condition("owner_id")

    def test_it_always_contains_its_administrator(self):
        with pytest.raises(ValueError, match="always contains"):
            AccessScope(actor_id=uuid.uuid4(), kind=ScopeKind.TEAM, owner_ids=frozenset())
        with pytest.raises(ValueError, match="always contains"):
            AccessScope(
                actor_id=uuid.uuid4(), kind=ScopeKind.TEAM, owner_ids=frozenset({uuid.uuid4()})
            )

    def test_it_has_no_single_subject(self):
        """Code written for one workspace owner must ask is_team first (ADR-0035)."""
        scope = AccessScope.team(uuid.uuid4(), [uuid.uuid4()])
        with pytest.raises(ValueError, match="no single subject"):
            _ = scope.subject_user_id

    def test_it_filters_by_its_owners(self, team_admin, team_member, user_b):
        scope = AccessScope.team(team_admin.pk, [team_member.pk])
        found = set(scope.apply(User.objects.all(), owner_field="pk"))
        assert found == {team_admin, team_member}
        assert set(User.objects.filter(scope.condition("pk"))) == {team_admin, team_member}
        assert scope.permits_owner(team_member.pk)
        assert not scope.permits_owner(user_b.pk)

    def test_it_is_addressed_as_all(self, team_admin):
        assert workspace_segment(AccessScope.team(team_admin.pk, [])) == WORKSPACE_ORGANIZATION
        assert selectors.workspace_subject(AccessScope.team(team_admin.pk, [])) is None


# --- the team helpers -------------------------------------------------------------------------
class TestTeamHelpers:
    def test_members_are_sales_users_naming_the_administrator_any_status(
        self, team_admin, team, outsiders
    ):
        assert teams.team_member_ids(team_admin.pk) == {u.pk for u in team.values()}
        for member in team.values():
            assert teams.is_team_member(team_admin.pk, member.pk)
            assert teams.in_reach(team_admin.pk, member)
            assert teams.lock_team_member(team_admin.pk, member.pk)
        for outsider in outsiders.values():
            assert not teams.is_team_member(team_admin.pk, outsider.pk)
            assert not teams.in_reach(team_admin.pk, outsider)
            assert not teams.lock_team_member(team_admin.pk, outsider.pk)
        assert teams.in_reach(team_admin.pk, team_admin)  # themselves
        assert not teams.is_team_member(team_admin.pk, team_admin.pk)  # but no member
        assert set(User.objects.filter(teams.reach_condition(team_admin.pk))) == {
            team_admin,
            *team.values(),
        }

    def test_all_in_team(self, team_admin, team, user_b, other_team_member):
        rahul, mohan = team["rahul"], team["mohan"]
        assert teams.all_in_team(team_admin.pk, [])
        assert teams.all_in_team(team_admin.pk, [team_admin.pk, rahul.pk, mohan.pk])
        assert not teams.all_in_team(team_admin.pk, [rahul.pk, user_b.pk])
        assert not teams.all_in_team(team_admin.pk, [other_team_member.pk])
        assert not teams.all_in_team(team_admin.pk, [uuid.uuid4()])

    def test_the_database_keeps_teams_to_sales_users(self, team_admin, team_member):
        """Only a sales user has a manager, never themselves (CHECKs), so a member's role
        can't change under a reference: every helper also asks for it regardless."""
        from django.db import IntegrityError, transaction

        for change in (
            {"role": Role.TEAM_ADMIN},
            {"role": Role.ADMIN},
            {"manager": team_member},
        ):
            with pytest.raises(IntegrityError), transaction.atomic():
                User.objects.filter(pk=team_member.pk).update(**change)
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=team_admin.pk).update(manager=team_member)


# --- resolution -------------------------------------------------------------------------------
class TestResolvingForATeamAdministrator:
    def test_all_is_exactly_the_team(self, team_admin, team, outsiders):
        scope = resolve_workspace(team_admin, WORKSPACE_ORGANIZATION)
        assert scope.kind is ScopeKind.TEAM
        assert scope.actor_id == team_admin.pk
        assert scope.owner_ids == {team_admin.pk, *(u.pk for u in team.values())}
        assert not scope.owner_ids & {u.pk for u in outsiders.values()}

    def test_a_team_without_members_is_the_administrator_alone(self, other_team_admin):
        scope = resolve_workspace(other_team_admin, WORKSPACE_ORGANIZATION)
        assert (scope.kind, scope.owner_ids) == (ScopeKind.TEAM, {other_team_admin.pk})

    @pytest.mark.parametrize("member", ["rahul", "mohan", "gone", "invited"])
    def test_a_members_workspace_is_theirs_alone(self, team_admin, team, member):
        """Any status: a deactivated member's history stays viewable, like everyone's."""
        subject = team[member]
        scope = resolve_workspace(team_admin, str(subject.pk))
        assert (scope.kind, scope.owner_ids, scope.actor_id) == (
            ScopeKind.USER,
            {subject.pk},
            team_admin.pk,
        )

    def test_me_and_their_own_id_are_their_own_records(self, team_admin):
        assert resolve_workspace(team_admin, WORKSPACE_SELF).kind is ScopeKind.SELF
        assert resolve_workspace(team_admin, str(team_admin.pk)).kind is ScopeKind.SELF

    def test_everyone_else_is_the_same_404_as_nobody(self, team_admin, team, outsiders):
        errors = set()
        for ref in [*(str(u.pk) for u in outsiders.values()), str(uuid.uuid4())]:
            with pytest.raises(NotFoundError) as caught:
                resolve_workspace(team_admin, ref)
            errors.add((type(caught.value), caught.value.message, caught.value.code))
        assert len(errors) == 1
        assert not access_events().exists()  # nothing was opened, so nothing is audited

    def test_a_member_who_left_the_team_is_out_of_reach_at_once(
        self, admin, team_admin, team_member
    ):
        from arkray.identity import services

        assert resolve_workspace(team_admin, str(team_member.pk)).owner_ids == {team_member.pk}
        services.update_user(
            actor_id=admin.pk, user_id=team_member.pk, version=1, changes={"manager": None}
        )
        with pytest.raises(NotFoundError):
            resolve_workspace(team_admin, str(team_member.pk))
        assert resolve_workspace(team_admin, WORKSPACE_ORGANIZATION).owner_ids == {team_admin.pk}

    def test_a_demoted_administrator_reaches_nobody_even_with_references_left(
        self, team_admin, team_member
    ):
        """The members' references outlive a role change made behind the services' back (a
        raw update): the capability is asked first, so they grant nothing."""
        User.objects.filter(pk=team_admin.pk).update(role=Role.SALES_USER)
        team_admin.refresh_from_db()
        for ref in (WORKSPACE_ORGANIZATION, str(team_member.pk)):
            with pytest.raises(NotFoundError):
                resolve_workspace(team_admin, ref)

    def test_a_member_never_reaches_their_administrator(self, team_admin, team_member):
        for ref in (str(team_admin.pk), WORKSPACE_ORGANIZATION):
            with pytest.raises(NotFoundError):
                resolve_workspace(team_member, ref)

    def test_super_administrators_are_unchanged(self, admin, team_admin, team_member, user_b):
        assert resolve_workspace(admin, WORKSPACE_ORGANIZATION).kind is ScopeKind.ORGANIZATION
        for subject in (team_admin, team_member, user_b):
            assert resolve_workspace(admin, str(subject.pk)).owner_ids == {subject.pk}

    def test_without_the_team_capabilities_the_team_is_closed(
        self, monkeypatch, team_admin, team_member
    ):
        monkeypatch.setitem(
            ROLE_CAPABILITIES,
            Role.TEAM_ADMIN,
            ROLE_CAPABILITIES[Role.TEAM_ADMIN]
            - {Capability.CRM_VIEW_TEAM, Capability.WORKSPACE_VIEW_TEAM},
        )
        for ref in (WORKSPACE_ORGANIZATION, str(team_member.pk)):
            with pytest.raises(NotFoundError):
                resolve_workspace(team_admin, ref)


class TestTheWorkspaceEndpoint:
    def test_all_describes_the_team(self, team_admin, team):
        assert signed_in(team_admin).get("/api/v1/workspaces/all").json() == {
            "kind": "team",
            "subject": None,
        }

    def test_a_member(self, team_admin, team_member):
        body = signed_in(team_admin).get(f"/api/v1/workspaces/{team_member.pk}").json()
        assert body == {
            "kind": "user",
            "subject": {"id": str(team_member.pk), "full_name": "Rahul Sharma", "status": "active"},
        }

    def test_an_outsider_is_the_404_of_nobody(self, team_admin, outsiders):
        client = signed_in(team_admin)
        bodies = []
        for ref in [*(u.pk for u in outsiders.values()), uuid.uuid4()]:
            response = client.get(f"/api/v1/workspaces/{ref}")
            assert response.status_code == 404
            bodies.append(without_request_id(response))
        assert all(body == bodies[0] for body in bodies)


# --- auditing ---------------------------------------------------------------------------------
class TestAuditing:
    def test_the_team_workspace_is_audited_as_the_team(self, team_admin, team):
        resolve_workspace(team_admin, WORKSPACE_ORGANIZATION)
        resolve_workspace(team_admin, WORKSPACE_ORGANIZATION)  # same window: once
        event = access_events().get()
        assert (event.actor_id, event.target_id, event.subject_user_id, event.metadata) == (
            team_admin.pk,
            WORKSPACE_ORGANIZATION,
            None,
            {"scope": "team"},
        )

    def test_a_members_workspace_is_audited_with_them_as_the_subject(self, team_admin, team_member):
        resolve_workspace(team_admin, str(team_member.pk))
        event = access_events().get()
        assert (event.actor_id, event.subject_user_id, event.metadata) == (
            team_admin.pk,
            team_member.pk,
            {"scope": "user"},
        )

    def test_the_team_and_the_organisation_are_audited_apart(self, admin, team_admin, team):
        """Promoted from her team to the organisation within one window, Tara's first look at
        the whole organisation is audited too: the team's window is its own."""
        from arkray.identity import services

        resolve_workspace(team_admin, WORKSPACE_ORGANIZATION)
        services.update_user(
            actor_id=admin.pk, user_id=team_admin.pk, version=1, changes={"role": "admin"}
        )
        team_admin.refresh_from_db()
        assert resolve_workspace(team_admin, WORKSPACE_ORGANIZATION).is_organization_wide
        scopes = [e.metadata["scope"] for e in access_events().order_by("id")]
        assert scopes == ["team", "organization"]

    def test_their_own_workspace_is_not_delegated_access(self, team_admin):
        resolve_workspace(team_admin, WORKSPACE_SELF)
        assert not access_events().exists()


# --- writes -----------------------------------------------------------------------------------
class TestAuthorizeWrite:
    def test_within_the_team(self, team_admin, team):
        authorize_write(team_admin, AccessScope.own(team_admin.pk))
        authorize_write(team_admin, resolve_workspace(team_admin, WORKSPACE_ORGANIZATION))
        for member in team.values():
            authorize_write(team_admin, AccessScope.for_user(team_admin.pk, member.pk))
        authorize_write(team_admin, AccessScope.team(team_admin.pk, []))

    @pytest.mark.parametrize("outsider", ["anita", "priya", "omar", "kavya"])
    def test_a_hand_built_scope_reaching_outside_is_refused(
        self, team_admin, team, outsiders, outsider
    ):
        """A scope is re-checked at the write: one built anywhere (or by mistake) with an
        owner outside the team is refused, whole team scope or one user's."""
        stranger = outsiders[outsider].pk
        for scope in (
            AccessScope.for_user(team_admin.pk, stranger),
            AccessScope.team(team_admin.pk, [team["rahul"].pk, stranger]),
        ):
            with pytest.raises(PermissionDeniedError):
                authorize_write(team_admin, scope)

    def test_never_organisation_wide(self, team_admin):
        with pytest.raises(PermissionDeniedError):
            authorize_write(team_admin, AccessScope.organization(team_admin.pk))

    def test_an_unknown_owner_is_refused(self, team_admin):
        with pytest.raises(PermissionDeniedError):
            authorize_write(team_admin, AccessScope.for_user(team_admin.pk, uuid.uuid4()))

    def test_a_scope_resolved_before_the_member_left_is_refused_after(
        self, admin, team_admin, team_member
    ):
        from arkray.identity import services

        member_scope = resolve_workspace(team_admin, str(team_member.pk))
        team_scope = resolve_workspace(team_admin, WORKSPACE_ORGANIZATION)
        services.update_user(
            actor_id=admin.pk, user_id=team_member.pk, version=1, changes={"manager": None}
        )
        for scope in (member_scope, team_scope):
            with pytest.raises(PermissionDeniedError):
                authorize_write(team_admin, scope)

    def test_a_scope_for_someone_else_is_refused(self, team_admin, other_team_admin, team_member):
        with pytest.raises(PermissionDeniedError):
            authorize_write(other_team_admin, AccessScope.team(team_admin.pk, [team_member.pk]))
        with pytest.raises(PermissionDeniedError):
            authorize_write(other_team_admin, AccessScope.for_user(team_admin.pk, team_member.pk))

    def test_viewing_the_team_is_not_managing_it(self, monkeypatch, team_admin, team_member):
        monkeypatch.setitem(
            ROLE_CAPABILITIES,
            Role.TEAM_ADMIN,
            ROLE_CAPABILITIES[Role.TEAM_ADMIN] - {Capability.CRM_MANAGE_TEAM},
        )
        for scope in (
            AccessScope.for_user(team_admin.pk, team_member.pk),
            AccessScope.team(team_admin.pk, [team_member.pk]),
        ):
            with pytest.raises(PermissionDeniedError):
                authorize_write(team_admin, scope)
        authorize_write(team_admin, AccessScope.own(team_admin.pk))  # still their own

    def test_a_sales_user_never_writes_in_a_team_scope(self, team_member, team_member_2):
        with pytest.raises(PermissionDeniedError):
            authorize_write(team_member, AccessScope.team(team_member.pk, [team_member_2.pk]))

    def test_a_deactivated_or_anonymous_administrator_is_refused(self, team_admin, team_member):
        scope = AccessScope.for_user(team_admin.pk, team_member.pk)
        with pytest.raises(PermissionDeniedError):
            authorize_write(AnonymousUser(), scope)
        gone = TeamAdminFactory(is_active=False)
        with pytest.raises(PermissionDeniedError):
            authorize_write(gone, AccessScope.own(gone.pk))

    def test_super_administrators_are_unchanged(self, admin, team_member):
        authorize_write(admin, AccessScope.organization(admin.pk))
        authorize_write(admin, AccessScope.for_user(admin.pk, team_member.pk))
