"""Admin -> User Workspace resolution: the gate in front of every CRM endpoint."""

import uuid

import pytest
from django.core.cache import cache
from django.db import transaction

from arkray.audit.models import AuditEvent
from arkray.core.access import ScopeKind
from arkray.core.errors import NotFoundError, PermissionDeniedError
from arkray.identity.workspaces import (
    AUDIT_ACTION_WORKSPACE_ACCESSED,
    WORKSPACE_ORGANIZATION,
    WORKSPACE_SELF,
    resolve_workspace,
)
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def access_events():
    return AuditEvent.objects.filter(action=AUDIT_ACTION_WORKSPACE_ACCESSED)


class TestSalesUser:
    def test_me_resolves_to_own_records_only(self, user_a):
        scope = resolve_workspace(user_a, WORKSPACE_SELF)
        assert scope.kind is ScopeKind.SELF
        assert scope.owner_ids == {user_a.pk}

    def test_own_id_is_equivalent_to_me(self, user_a):
        assert resolve_workspace(user_a, str(user_a.pk)).kind is ScopeKind.SELF

    def test_cannot_open_another_users_workspace(self, user_a, user_b):
        with pytest.raises(NotFoundError):
            resolve_workspace(user_a, str(user_b.pk))

    def test_existing_and_missing_users_are_indistinguishable(self, user_a, user_b):
        """No user-ID enumeration: both cases produce the identical error."""
        errors = []
        for ref in (str(user_b.pk), str(uuid.uuid4())):
            with pytest.raises(NotFoundError) as caught:
                resolve_workspace(user_a, ref)
            errors.append((type(caught.value), caught.value.message))
        assert errors[0] == errors[1]

    def test_cannot_open_organization_scope(self, user_a):
        with pytest.raises(NotFoundError):
            resolve_workspace(user_a, WORKSPACE_ORGANIZATION)

    @pytest.mark.parametrize("ref", ["", "admin", "../all", "00000000", "' OR 1=1 --"])
    def test_malformed_references_are_not_found(self, user_a, ref):
        with pytest.raises(NotFoundError):
            resolve_workspace(user_a, ref)

    def test_deactivated_user_is_refused(self):
        with pytest.raises(PermissionDeniedError):
            resolve_workspace(UserFactory(is_active=False), WORKSPACE_SELF)

    def test_own_workspace_is_not_audited_as_delegated_access(self, user_a):
        resolve_workspace(user_a, WORKSPACE_SELF)
        assert not access_events().exists()


class TestAdmin:
    def test_opens_a_users_workspace_scoped_to_that_user(self, admin, user_a):
        scope = resolve_workspace(admin, str(user_a.pk))
        assert scope.kind is ScopeKind.USER
        assert scope.owner_ids == {user_a.pk}
        assert scope.actor_id == admin.pk  # the admin remains the actor: no impersonation

    def test_access_is_audited(self, admin, user_a):
        resolve_workspace(admin, str(user_a.pk))
        (event,) = access_events()
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)
        assert event.metadata == {"scope": "user"}

    def test_repeated_access_within_window_is_audited_once(
        self, admin, user_a, user_b, django_capture_on_commit_callbacks
    ):
        for _ in range(3):
            with django_capture_on_commit_callbacks(execute=True):
                resolve_workspace(admin, str(user_a.pk))
        with django_capture_on_commit_callbacks(execute=True):
            resolve_workspace(admin, str(user_b.pk))
        assert access_events().count() == 2

    def test_rolled_back_access_does_not_suppress_auditing(
        self, admin, user_a, django_capture_on_commit_callbacks
    ):
        """Regression: the window used to be consumed before the audit row committed."""

        def failing_request():
            with transaction.atomic():
                resolve_workspace(admin, str(user_a.pk))
                raise RuntimeError("view failed after resolving the workspace")

        with django_capture_on_commit_callbacks(execute=True), pytest.raises(RuntimeError):
            failing_request()
        assert not access_events().exists()
        with django_capture_on_commit_callbacks(execute=True):
            resolve_workspace(admin, str(user_a.pk))
        assert access_events().count() == 1

    def test_the_cache_plays_no_part_in_auditing(
        self, admin, user_a, monkeypatch, django_capture_on_commit_callbacks
    ):
        """Phase 9 review: the window was a cache key, so a write to Redis could hide an
        admin's access indefinitely. It is a database row now: Redis down, or a planted
        marker under the old key, changes nothing."""
        cache.set(f"audit:workspace-access:{admin.pk}:{user_a.pk}", 1, timeout=None)

        def unavailable(*args, **kwargs):
            raise ConnectionError("redis down")

        monkeypatch.setattr(cache, "get", unavailable)
        monkeypatch.setattr(cache, "set", unavailable)
        for _ in range(3):
            with django_capture_on_commit_callbacks(execute=True):
                resolve_workspace(admin, str(user_a.pk))
        assert access_events().count() == 1  # once per window, as always

    def test_organization_scope_is_audited(self, admin):
        scope = resolve_workspace(admin, WORKSPACE_ORGANIZATION)
        assert scope.kind is ScopeKind.ORGANIZATION
        assert access_events().get().target_id == WORKSPACE_ORGANIZATION

    @pytest.mark.parametrize(
        "spelling",
        ["urn:uuid:{}", "{{{}}}", "{}", " {}", "+{}"],
        ids=["urn", "braces", "no-hyphens", "leading-space", "plus"],
    )
    def test_only_canonical_uuids_are_workspace_references(self, admin, user_a, spelling):
        value = spelling.format(user_a.pk.hex if spelling == "{}" else user_a.pk)
        with pytest.raises(NotFoundError):
            resolve_workspace(admin, value)

    def test_canonical_uuid_is_case_insensitive(self, admin, user_a):
        assert resolve_workspace(admin, str(user_a.pk).upper()).owner_ids == {user_a.pk}

    def test_unknown_user_is_not_found(self, admin):
        with pytest.raises(NotFoundError):
            resolve_workspace(admin, str(uuid.uuid4()))

    def test_deactivated_users_history_remains_viewable(self, admin):
        departed = UserFactory(is_active=False)
        assert resolve_workspace(admin, str(departed.pk)).owner_ids == {departed.pk}

    def test_admin_me_is_their_own_records(self, admin):
        assert resolve_workspace(admin, WORKSPACE_SELF).kind is ScopeKind.SELF
