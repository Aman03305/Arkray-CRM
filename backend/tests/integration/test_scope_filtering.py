"""AccessScope filtering against a real table.

Uses the users table (owner column = the user's own id) because it is the only owner-bearing
table in Phase 0. From Phase 2 the cross-user suites exercise every CRM table this way.
"""

import pytest

from arkray.core.access import AccessScope
from arkray.identity.models import User

pytestmark = pytest.mark.django_db


class TestApply:
    """`apply()` against a real table (users filtered by their own id as the owner column)."""

    def test_self_scope_filters_out_other_owners(self, user_a, user_b):
        visible = AccessScope.own(user_a.pk).apply(User.objects.all(), owner_field="id")
        assert list(visible) == [user_a]

    def test_user_scope_shows_only_the_subject(self, admin, user_a, user_b):
        visible = AccessScope.for_user(admin.pk, user_b.pk).apply(
            User.objects.all(), owner_field="id"
        )
        assert list(visible) == [user_b]

    def test_organization_scope_is_unfiltered(self, admin, user_a, user_b):
        visible = AccessScope.organization(admin.pk).apply(User.objects.all(), owner_field="id")
        assert set(visible) == {admin, user_a, user_b}
