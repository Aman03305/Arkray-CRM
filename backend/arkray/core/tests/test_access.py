import uuid

import pytest
from django.db.models import Q

from arkray.core.access import AccessScope, ScopeKind


class TestInvariants:
    def test_self_scope_contains_only_the_actor(self):
        actor = uuid.uuid4()
        scope = AccessScope.own(actor)
        assert scope.kind is ScopeKind.SELF
        assert scope.owner_ids == {actor}
        assert scope.subject_user_id == actor
        assert not scope.is_delegated

    def test_self_scope_with_foreign_owner_is_rejected(self):
        with pytest.raises(ValueError, match="exactly the actor"):
            AccessScope(
                actor_id=uuid.uuid4(), kind=ScopeKind.SELF, owner_ids=frozenset({uuid.uuid4()})
            )

    def test_user_scope_requires_exactly_one_owner(self):
        with pytest.raises(ValueError, match="exactly one owner"):
            AccessScope(actor_id=uuid.uuid4(), kind=ScopeKind.USER, owner_ids=frozenset())
        with pytest.raises(ValueError, match="exactly one owner"):
            AccessScope(
                actor_id=uuid.uuid4(),
                kind=ScopeKind.USER,
                owner_ids=frozenset({uuid.uuid4(), uuid.uuid4()}),
            )

    def test_organization_scope_cannot_carry_owner_list(self):
        with pytest.raises(ValueError, match="must not list owners"):
            AccessScope(
                actor_id=uuid.uuid4(),
                kind=ScopeKind.ORGANIZATION,
                owner_ids=frozenset({uuid.uuid4()}),
            )

    def test_user_scope_cannot_name_the_actor(self):
        actor = uuid.uuid4()
        with pytest.raises(ValueError, match="use SELF"):
            AccessScope(actor_id=actor, kind=ScopeKind.USER, owner_ids=frozenset({actor}))

    def test_owner_set_is_frozen_even_if_a_mutable_set_is_passed(self):
        actor, subject = uuid.uuid4(), uuid.uuid4()
        owners = {subject}
        scope = AccessScope(actor_id=actor, kind=ScopeKind.USER, owner_ids=owners)
        owners.add(uuid.uuid4())
        assert scope.owner_ids == frozenset({subject})
        assert isinstance(scope.owner_ids, frozenset)

    def test_non_uuid_owners_are_rejected(self):
        with pytest.raises(ValueError, match="must be UUIDs"):
            AccessScope(actor_id=uuid.uuid4(), kind=ScopeKind.USER, owner_ids=frozenset({"x"}))

    def test_for_user_on_self_collapses_to_self_scope(self):
        actor = uuid.uuid4()
        assert AccessScope.for_user(actor, actor).kind is ScopeKind.SELF

    def test_delegated_scopes(self):
        actor, subject = uuid.uuid4(), uuid.uuid4()
        user_scope = AccessScope.for_user(actor, subject)
        assert user_scope.is_delegated
        assert user_scope.subject_user_id == subject
        org = AccessScope.organization(actor)
        assert org.is_delegated
        assert org.is_organization_wide
        assert org.subject_user_id is None

    def test_permits_owner(self):
        actor, other = uuid.uuid4(), uuid.uuid4()
        assert AccessScope.own(actor).permits_owner(actor)
        assert not AccessScope.own(actor).permits_owner(other)
        assert AccessScope.organization(actor).permits_owner(other)


def test_condition_restricts_through_a_relation_like_apply():
    actor, other = uuid.uuid4(), uuid.uuid4()
    assert AccessScope.own(actor).condition("activity__owner_id") == Q(
        activity__owner_id__in=frozenset({actor})
    )
    assert AccessScope.for_user(actor, other).condition("x") == Q(x__in=frozenset({other}))
    # Only an organisation-wide scope is unrestricted; there is no empty-owners shortcut.
    assert AccessScope.organization(actor).condition("x") == Q()
