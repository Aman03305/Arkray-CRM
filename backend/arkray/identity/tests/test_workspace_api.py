"""GET /api/v1/workspaces/{workspace}: whose CRM a page shows (Admin -> User Workspace)."""

import uuid

import pytest
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.identity.models import User, UserStatus
from arkray.identity.workspaces import AUDIT_ACTION_WORKSPACE_ACCESSED
from tests.factories import UserFactory
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db


def workspace(client, ref):
    return client.get(f"/api/v1/workspaces/{ref}")


def signed_in_user_ids():
    return {str(s.get_decoded().get(SESSION_KEY)) for s in Session.objects.all()}


class TestOwnWorkspace:
    def test_me_describes_the_callers_own_workspace(self, user_a_client, user_a):
        body = workspace(user_a_client, "me").json()
        assert body == {
            "kind": "self",
            "subject": {"id": str(user_a.pk), "full_name": "Rahul Sharma", "status": "active"},
        }
        assert not AuditEvent.objects.exists()

    def test_anonymous_is_401(self, api_client):
        assert workspace(api_client, "me").status_code == 401


class TestAdminOpensAUsersWorkspace:
    def test_describes_the_user_and_is_audited(self, admin_client, admin, user_a):
        body = workspace(admin_client, user_a.pk).json()
        assert body["kind"] == "user"
        assert body["subject"]["full_name"] == "Rahul Sharma"
        event = AuditEvent.objects.get(action=AUDIT_ACTION_WORKSPACE_ACCESSED)
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)

    def test_no_impersonation_the_admin_remains_themselves(self, admin_client, admin, user_a):
        workspace(admin_client, user_a.pk)
        assert admin_client.get("/api/v1/auth/me").json()["id"] == str(admin.pk)
        assert str(user_a.pk) not in signed_in_user_ids()  # no session was created for them

    def test_organisation_workspace(self, admin_client):
        body = workspace(admin_client, "all").json()
        assert body == {"kind": "organization", "subject": None}
        assert AuditEvent.objects.get().target_id == "all"

    def test_deactivated_users_remain_viewable(self, admin_client):
        departed = UserFactory(is_active=False)
        assert workspace(admin_client, departed.pk).json()["subject"]["status"] == "deactivated"


class TestSalesUserCannotReachOthers:
    def test_another_users_workspace_is_indistinguishable_from_a_missing_one(
        self, user_a_client, user_b
    ):
        other = workspace(user_a_client, user_b.pk)
        missing = workspace(user_a_client, uuid.uuid4())
        assert other.status_code == missing.status_code == 404
        assert without_request_id(other) == without_request_id(missing)
        assert not AuditEvent.objects.exists()

    def test_the_organisation_workspace_is_404(self, user_a_client):
        assert workspace(user_a_client, "all").status_code == 404

    @pytest.mark.parametrize("ref", ["admin", "..", "%2e%2e", "' OR 1=1 --"])
    def test_malformed_references_are_404(self, user_a_client, ref):
        assert workspace(user_a_client, ref).status_code == 404

    def test_a_deactivated_admins_session_opens_nothing(self, admin, user_a):
        client = signed_in(admin)
        User.objects.filter(pk=admin.pk).update(
            status=UserStatus.DEACTIVATED, is_active=False, deactivated_at=timezone.now()
        )
        assert workspace(client, user_a.pk).status_code == 401
        assert not AuditEvent.objects.exists()
