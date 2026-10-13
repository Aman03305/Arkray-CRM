"""Teams (ADR-0035): a team administrator (role team_admin) and the sales users they manage.

A sales user's `manager` names their team administrator, or is NULL (managed by the super
administrators only). Who manages whom is changed only by identity.services, under the
user-administration lock; every question here also asks for the *member's* role, and the
callers ask for the *actor's* team capability, so a reference left behind by a role change
grants nothing.

Reach, the one rule every team check applies:
- a super administrator (the organisation-wide capabilities) reaches everyone;
- a team administrator reaches themselves and their team's sales users, whatever the
  member's status (a deactivated member's history stays viewable, like everyone's);
- nobody else reaches anyone else.

Changing a team takes effect at the member's next request, like every other permission:
a write already authorised in flight completes (docs/authorization.md#teams-adr-0035).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol
from uuid import UUID

from django.db import connection
from django.db.models import Q

from arkray.core.access import AccessScope

from .models import Role, User


class _Actor(Protocol):
    @property
    def pk(self) -> UUID: ...


def reach_condition(admin_id: UUID) -> Q:
    """The users a team administrator reaches, as a filter: themselves and their team's sales
    users, any status. Which capability the caller needs for it is the caller's question
    (users.manage_team, support.access_team, crm.assign_team...)."""
    return Q(pk=admin_id) | Q(manager_id=admin_id, role=Role.SALES_USER)


def in_reach(admin_id: UUID, user: User) -> bool:
    """`reach_condition` for a row the caller read: under its row lock (and the
    user-administration lock) wherever a decision depends on it, so a team change committed
    a moment ago is seen and one that waits sees the decision's result."""
    return user.pk == admin_id or (user.manager_id == admin_id and user.role == Role.SALES_USER)


def team_member_ids(admin_id: UUID) -> frozenset[UUID]:
    """The sales users `admin_id` manages, any status."""
    return frozenset(
        User.objects.filter(manager_id=admin_id, role=Role.SALES_USER).values_list("pk", flat=True)
    )


def team_scope(actor: _Actor) -> AccessScope:
    """The team administrator's "all" workspace: their own records and their team's."""
    return AccessScope.team(actor.pk, team_member_ids(actor.pk))


def is_team_member(admin_id: UUID, user_id: UUID) -> bool:
    return User.objects.filter(pk=user_id, manager_id=admin_id, role=Role.SALES_USER).exists()


def all_in_team(admin_id: UUID, user_ids: Collection[UUID]) -> bool:
    """True when every id is the administrator themselves or one of their team's sales users
    (one query)."""
    others = set(user_ids) - {admin_id}
    if not others:
        return True
    members = User.objects.filter(pk__in=others, manager_id=admin_id, role=Role.SALES_USER).count()
    return members == len(others)


def lock_team_member(admin_id: UUID, user_id: UUID) -> bool:
    """True if `user_id` is the administrator or one of their team's sales users, holding a
    share lock on the user's row until the caller's transaction ends: a concurrent team
    change (which updates the row) waits for the caller to commit, or the caller sees its
    result. Like identity.selectors.lock_assignable_user, which assignment also takes."""
    if user_id == admin_id:
        return True
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM identity_user WHERE id = %s AND manager_id = %s AND role = %s FOR SHARE",
            [user_id, admin_id, Role.SALES_USER.value],
        )
        return cursor.fetchone() is not None
