"""Ownership: who owns a new lead, reassignment, and the admin workspaces (own, one user's,
organisation-wide) running through the same endpoints."""

from __future__ import annotations

import pytest
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.identity.models import Role
from arkray.identity.policy import ROLE_CAPABILITIES, Capability
from arkray.leads.models import Lead
from arkray.leads.services import (
    OWNER_IS_SELF_ONLY,
    OWNER_IS_SUBJECT_ONLY,
    OWNER_NOT_ASSIGNABLE,
    OWNER_REQUIRED,
)
from tests.factories import AdminFactory, InvitedUserFactory, LeadFactory, UserFactory
from tests.helpers import signed_in

from .conftest import lead_url, leads_url

pytestmark = pytest.mark.django_db


def ids(response):
    return {row["id"] for row in response.json()["results"]}


def owner_error(response):
    assert response.status_code == 400, response.content
    return response.json()["error"]["details"]["owner"]


class TestCreateOwnership:
    def test_a_sales_user_owns_what_they_create(self, user_a_client, user_a):
        body = user_a_client.post(leads_url(), {"first_name": "R"}, format="json").json()
        assert body["owner"]["id"] == body["created_by"]["id"] == str(user_a.pk)

    def test_a_sales_user_cannot_create_for_someone_else(self, user_a_client, user_b):
        response = user_a_client.post(
            leads_url(), {"first_name": "R", "owner": str(user_b.pk)}, format="json"
        )
        assert owner_error(response) == [OWNER_IS_SELF_ONLY]
        assert not Lead.objects.exists()

    def test_naming_yourself_as_owner_is_fine(self, user_a_client, user_a):
        response = user_a_client.post(
            leads_url(), {"first_name": "R", "owner": str(user_a.pk)}, format="json"
        )
        assert response.status_code == 201

    def test_the_refusal_is_identical_whether_or_not_the_other_user_exists(
        self, user_a_client, user_b
    ):
        real = user_a_client.post(
            leads_url(), {"first_name": "R", "owner": str(user_b.pk)}, format="json"
        )
        ghost = user_a_client.post(
            leads_url(), {"first_name": "R", "owner": str(UserFactory.build().pk)}, format="json"
        )
        assert real.json()["error"]["details"] == ghost.json()["error"]["details"]

    def test_an_admin_creating_in_a_users_workspace_creates_it_for_that_user(
        self, admin_client, admin, user_a
    ):
        response = admin_client.post(leads_url(str(user_a.pk)), {"first_name": "R"}, format="json")
        assert response.status_code == 201
        body = response.json()
        assert body["owner"]["id"] == str(user_a.pk)
        assert body["created_by"]["id"] == str(admin.pk)  # provenance: the admin did it
        assert response["Location"].startswith(f"/api/v1/workspaces/{user_a.pk}/leads/")
        event = AuditEvent.objects.get(action="lead.created")
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)
        assert event.metadata["workspace"] == "user"

    def test_in_a_users_workspace_the_owner_is_that_user(self, admin_client, user_a, user_b):
        response = admin_client.post(
            leads_url(str(user_a.pk)), {"first_name": "R", "owner": str(user_b.pk)}, format="json"
        )
        assert owner_error(response) == [OWNER_IS_SUBJECT_ONLY]

    def test_leads_cannot_be_created_for_a_deactivated_user(self, admin_client):
        gone = UserFactory(is_active=False)
        response = admin_client.post(leads_url(str(gone.pk)), {"first_name": "R"}, format="json")
        assert owner_error(response) == [OWNER_NOT_ASSIGNABLE]

    def test_organisation_wide_creation_needs_an_explicit_owner(self, admin_client):
        response = admin_client.post(leads_url("all"), {"first_name": "R"}, format="json")
        assert owner_error(response) == [OWNER_REQUIRED]

    def test_organisation_wide_creation_for_an_active_user(self, admin_client, user_b):
        response = admin_client.post(
            leads_url("all"), {"first_name": "R", "owner": str(user_b.pk)}, format="json"
        )
        assert response.status_code == 201
        assert response.json()["owner"]["id"] == str(user_b.pk)
        assert response["Location"].startswith("/api/v1/workspaces/all/leads/")

    @pytest.mark.parametrize(
        "make_owner",
        [
            lambda: UserFactory(is_active=False),
            InvitedUserFactory,
            UserFactory.build,  # does not exist
        ],
    )
    def test_only_active_users_can_be_given_leads(self, admin_client, make_owner):
        response = admin_client.post(
            leads_url("all"), {"first_name": "R", "owner": str(make_owner().pk)}, format="json"
        )
        assert owner_error(response) == [OWNER_NOT_ASSIGNABLE]
        assert not Lead.objects.exists()

    def test_viewing_rights_alone_never_allow_writing_in_someone_elses_workspace(
        self, monkeypatch, admin_client, user_a
    ):
        """A future role that may *view* workspaces but not manage them (e.g. an auditor)."""
        monkeypatch.setitem(
            ROLE_CAPABILITIES,
            Role.ADMIN,
            ROLE_CAPABILITIES[Role.ADMIN] - {Capability.CRM_MANAGE_ANY},
        )
        lead = LeadFactory(owner=user_a)
        workspace = str(user_a.pk)
        assert admin_client.get(leads_url(workspace)).status_code == 200
        for method, url, body in [
            ("post", leads_url(workspace), {"first_name": "R"}),
            ("patch", lead_url(lead.pk, workspace), {"version": 1, "city": "Pune"}),
            ("post", lead_url(lead.pk, workspace, "status"), {"version": 1, "status": "contacted"}),
            ("post", lead_url(lead.pk, workspace, "archive"), {"version": 1}),
            ("post", leads_url("all"), {"first_name": "R", "owner": workspace}),
        ]:
            response = getattr(admin_client, method)(url, body, format="json")
            assert response.status_code == 403, (method, url)
        lead.refresh_from_db()
        assert (lead.version, Lead.objects.count()) == (1, 1)

    def test_managing_without_assigning_cannot_create_for_others(
        self, monkeypatch, admin_client, user_a
    ):
        monkeypatch.setitem(
            ROLE_CAPABILITIES,
            Role.ADMIN,
            ROLE_CAPABILITIES[Role.ADMIN] - {Capability.CRM_ASSIGN_ANY},
        )
        response = admin_client.post(leads_url(str(user_a.pk)), {"first_name": "R"}, format="json")
        assert response.status_code == 403


class TestReassign:
    def assign(self, client, lead, owner, workspace="all", version=1):
        return client.post(
            lead_url(lead.pk, workspace, "assign"),
            {"owner": str(owner.pk), "version": version},
            format="json",
        )

    def test_an_admin_reassigns_organisation_wide(self, admin_client, admin, user_a, user_b):
        lead = LeadFactory(owner=user_a)
        response = self.assign(admin_client, lead, user_b)
        assert response.status_code == 200
        assert (response.json()["owner"]["id"], response.json()["version"]) == (str(user_b.pk), 2)
        lead.refresh_from_db()
        assert (lead.owner_id, lead.created_by_id) == (user_b.pk, user_a.pk)  # history kept
        event = AuditEvent.objects.get(action="lead.reassigned")
        assert event.actor_id == admin.pk
        assert event.subject_user_id == user_a.pk
        assert event.metadata == {
            "workspace": "organization",
            "from_owner_id": str(user_a.pk),
            "to_owner_id": str(user_b.pk),
        }

    def test_reassigning_from_a_users_workspace_moves_it_out_of_that_workspace(
        self, admin_client, user_a, user_b
    ):
        lead = LeadFactory(owner=user_a)
        response = self.assign(admin_client, lead, user_b, workspace=str(user_a.pk))
        assert response.status_code == 200
        assert response.json()["owner"]["id"] == str(user_b.pk)
        assert ids(admin_client.get(leads_url(str(user_a.pk)))) == set()
        assert ids(admin_client.get(leads_url(str(user_b.pk)))) == {str(lead.pk)}
        assert admin_client.get(lead_url(lead.pk, str(user_a.pk))).status_code == 404

    def test_the_new_owner_sees_it_and_the_previous_owner_no_longer_does(
        self, admin_client, user_a, user_b, user_a_client, user_b_client
    ):
        lead = LeadFactory(owner=user_a)
        self.assign(admin_client, lead, user_b)
        assert user_a_client.get(lead_url(lead.pk)).status_code == 404
        assert ids(user_a_client.get(leads_url())) == set()
        assert user_b_client.get(lead_url(lead.pk)).status_code == 200

    def test_sales_users_cannot_reassign_even_their_own_leads(self, user_a_client, user_a, user_b):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.post(
            lead_url(lead.pk, "me", "assign"),
            {"owner": str(user_b.pk), "version": 1},
            format="json",
        )
        assert response.status_code == 403
        lead.refresh_from_db()
        assert lead.owner_id == user_a.pk

    def test_reassigning_to_the_current_owner_is_a_no_op(self, admin_client, user_a):
        lead = LeadFactory(owner=user_a, version=3)
        response = self.assign(admin_client, lead, user_a, version=1)
        assert (response.status_code, response.json()["version"]) == (200, 3)
        assert not AuditEvent.objects.filter(action="lead.reassigned").exists()

    def test_a_stale_version_conflicts(self, admin_client, user_a, user_b):
        lead = LeadFactory(owner=user_a, version=2)
        assert self.assign(admin_client, lead, user_b, version=1).status_code == 409

    @pytest.mark.parametrize(
        "make_owner", [lambda: UserFactory(is_active=False), InvitedUserFactory, UserFactory.build]
    )
    def test_only_active_users_can_receive_leads(self, admin_client, user_a, make_owner):
        lead = LeadFactory(owner=user_a)
        response = self.assign(admin_client, lead, make_owner())
        assert owner_error(response) == [OWNER_NOT_ASSIGNABLE]
        lead.refresh_from_db()
        assert (lead.owner_id, lead.version) == (user_a.pk, 1)

    def test_admins_can_receive_leads_too(self, admin_client, admin, user_a):
        lead = LeadFactory(owner=user_a)
        assert self.assign(admin_client, lead, admin).status_code == 200

    def test_archived_leads_are_restored_before_reassignment(self, admin_client, user_a, user_b):
        lead = LeadFactory(owner=user_a, archived_at=timezone.now())
        assert self.assign(admin_client, lead, user_b).status_code == 422

    def test_an_unknown_lead_is_404(self, admin_client, user_b):
        ghost = LeadFactory.build(owner=user_b)
        assert self.assign(admin_client, ghost, user_b).status_code == 404


class TestAdminWorkspaces:
    @pytest.fixture
    def world(self, user_a, user_b, admin):
        return {
            "a1": LeadFactory(owner=user_a),
            "a2": LeadFactory(owner=user_a),
            "b1": LeadFactory(owner=user_b),
            "admin": LeadFactory(owner=admin),
        }

    def test_a_selected_users_workspace_shows_only_their_leads(self, admin_client, user_a, world):
        assert ids(admin_client.get(leads_url(str(user_a.pk)))) == {
            str(world["a1"].pk),
            str(world["a2"].pk),
        }

    def test_switching_users_switches_the_data(self, admin_client, user_a, user_b, world):
        assert str(world["b1"].pk) not in ids(admin_client.get(leads_url(str(user_a.pk))))
        assert ids(admin_client.get(leads_url(str(user_b.pk)))) == {str(world["b1"].pk)}

    def test_the_organisation_workspace_shows_everyone_and_filters_by_owner(
        self, admin_client, user_b, world
    ):
        assert ids(admin_client.get(leads_url("all"))) == {str(lead.pk) for lead in world.values()}
        narrowed = admin_client.get(leads_url("all"), {"owner": str(user_b.pk)})
        assert ids(narrowed) == {str(world["b1"].pk)}

    def test_the_owner_filter_can_only_narrow(self, admin_client, user_a, user_b, world):
        response = admin_client.get(leads_url(str(user_a.pk)), {"owner": str(user_b.pk)})
        assert response.status_code == 400

    def test_admin_in_their_own_workspace_sees_only_their_own(self, admin_client, world):
        assert ids(admin_client.get(leads_url("me"))) == {str(world["admin"].pk)}

    def test_an_admin_edit_in_a_users_workspace_is_attributed_to_the_admin(
        self, admin_client, admin, user_a, world
    ):
        lead = world["a1"]
        response = admin_client.patch(
            lead_url(lead.pk, str(user_a.pk)), {"version": 1, "city": "Pune"}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["created_by"]["id"] == str(user_a.pk)  # unchanged
        event = AuditEvent.objects.get(action="lead.updated")
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)
        assert event.metadata == {"workspace": "user", "fields": ["city"]}

    def test_opening_a_users_leads_is_audited_as_workspace_access(
        self, admin_client, admin, user_a, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            admin_client.get(leads_url(str(user_a.pk)))
        admin_client.get(leads_url(str(user_a.pk)))
        events = AuditEvent.objects.filter(action="workspace.accessed", actor_id=admin.pk)
        assert events.count() == 1  # once per window
        assert events.get().subject_user_id == user_a.pk

    def test_a_deactivated_users_leads_stay_viewable(self, admin_client):
        gone = UserFactory(is_active=False)
        lead = LeadFactory(owner=gone)
        assert ids(admin_client.get(leads_url(str(gone.pk)))) == {str(lead.pk)}
        owner = admin_client.get(lead_url(lead.pk, "all")).json()["owner"]
        assert owner["is_active"] is False


class TestAssignees:
    def test_lists_active_crm_users_by_name(self, admin_client, admin):
        rahul = UserFactory(first_name="Rahul", last_name="Sharma")
        anita_b = AdminFactory(first_name="Anita", last_name="Bose")
        UserFactory(first_name="Gone", is_active=False)
        InvitedUserFactory(first_name="Pending")
        body = admin_client.get("/api/v1/assignees").json()
        names = [row["full_name"] for row in body["results"]]
        assert names == ["Anita Admin", "Anita Bose", "Rahul Sharma"]
        assert {row["id"] for row in body["results"]} == {
            str(admin.pk),
            str(rahul.pk),
            str(anita_b.pk),
        }
        assert set(body["results"][0]) == {"id", "full_name", "email"}

    def test_search_and_pages(self, admin_client):
        for i in range(5):
            UserFactory(first_name=f"Sales{i}", last_name="Person")
        assert len(admin_client.get("/api/v1/assignees", {"q": "sales3"}).json()["results"]) == 1
        page = admin_client.get("/api/v1/assignees", {"page_size": 2}).json()
        assert len(page["results"]) == 2
        assert page["next"]
        assert len(admin_client.get(page["next"]).json()["results"]) == 2

    def test_sales_users_cannot_list_assignees(self, user_a_client):
        assert user_a_client.get("/api/v1/assignees").status_code == 403

    def test_unknown_parameters_are_refused(self, admin_client):
        assert admin_client.get("/api/v1/assignees", {"role": "admin"}).status_code == 400


def test_a_second_admin_sees_leads_created_by_the_first(admin, user_a):
    other_admin = AdminFactory()
    lead = LeadFactory(owner=user_a, created_by=admin)
    assert signed_in(other_admin).get(lead_url(lead.pk, "all")).status_code == 200
