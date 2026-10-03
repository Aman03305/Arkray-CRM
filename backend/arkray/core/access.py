"""Record-level access scoping.

An `AccessScope` states exactly whose CRM records a request may touch. It is produced by
`arkray.identity.workspaces.resolve_workspace()` from the authenticated actor and the
workspace in the URL — never from client-supplied owner IDs — and every CRM selector,
search query, dashboard aggregate and Ask Arkray tool filters through `AccessScope.apply()`.

Invariants (enforced at construction):
- SELF         -> owner_ids == {actor_id}
- USER         -> exactly one owner (another user's workspace, opened with permission)
- ORGANIZATION -> no owner filter at all; requires an explicit capability upstream

An empty owner set can never mean "everyone": organisation-wide access is a distinct kind,
so a bug that loses the owner list denies access instead of granting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

from django.db.models import Q

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet

M = TypeVar("M", bound="Model")


class ScopeKind(StrEnum):
    SELF = "self"
    USER = "user"
    ORGANIZATION = "organization"


@dataclass(frozen=True, slots=True)
class AccessScope:
    actor_id: UUID
    kind: ScopeKind
    owner_ids: frozenset[UUID]

    def __post_init__(self) -> None:
        # Freeze and type-check the owner set so it cannot change after validation.
        owners = frozenset(self.owner_ids)
        if not all(isinstance(owner, UUID) for owner in owners):
            raise ValueError("Owner ids must be UUIDs.")
        object.__setattr__(self, "owner_ids", owners)
        if not isinstance(self.actor_id, UUID):
            raise ValueError("The actor id must be a UUID.")
        if not isinstance(self.kind, ScopeKind):
            raise ValueError(f"Unknown scope kind: {self.kind}")

        if self.kind is ScopeKind.ORGANIZATION:
            if self.owner_ids:
                raise ValueError("An organisation-wide scope must not list owners.")
        elif self.kind is ScopeKind.SELF:
            if self.owner_ids != frozenset({self.actor_id}):
                raise ValueError("A SELF scope must contain exactly the actor.")
        elif len(self.owner_ids) != 1:
            raise ValueError("A USER scope must contain exactly one owner.")
        elif self.actor_id in self.owner_ids:
            raise ValueError("A USER scope is for another user's records; use SELF.")

    # --- constructors ----------------------------------------------------------------------
    @classmethod
    def own(cls, actor_id: UUID) -> AccessScope:
        return cls(actor_id=actor_id, kind=ScopeKind.SELF, owner_ids=frozenset({actor_id}))

    @classmethod
    def for_user(cls, actor_id: UUID, subject_user_id: UUID) -> AccessScope:
        if subject_user_id == actor_id:
            return cls.own(actor_id)
        return cls(actor_id=actor_id, kind=ScopeKind.USER, owner_ids=frozenset({subject_user_id}))

    @classmethod
    def organization(cls, actor_id: UUID) -> AccessScope:
        return cls(actor_id=actor_id, kind=ScopeKind.ORGANIZATION, owner_ids=frozenset())

    # --- queries ---------------------------------------------------------------------------
    @property
    def is_organization_wide(self) -> bool:
        return self.kind is ScopeKind.ORGANIZATION

    @property
    def subject_user_id(self) -> UUID | None:
        """The single workspace owner, or None for organisation-wide scopes."""
        if self.kind is ScopeKind.ORGANIZATION:
            return None
        (owner,) = self.owner_ids
        return owner

    @property
    def is_delegated(self) -> bool:
        """True when the actor is looking at data that is not their own."""
        return self.kind is not ScopeKind.SELF

    def permits_owner(self, owner_id: UUID) -> bool:
        return self.is_organization_wide or owner_id in self.owner_ids

    def apply(self, queryset: QuerySet[M], *, owner_field: str = "owner_id") -> QuerySet[M]:
        """Restrict a queryset to records this scope may see."""
        if self.is_organization_wide:
            return queryset
        return queryset.filter(**{f"{owner_field}__in": self.owner_ids})

    def condition(self, owner_field: str) -> Q:
        """The same restriction as a Q object, for records reached through a relation inside
        a larger condition (a timeline entry is visible if *its activity* is). An empty Q
        (no restriction) only for organisation-wide scopes, never for an empty owner set."""
        if self.is_organization_wide:
            return Q()
        return Q(**{f"{owner_field}__in": self.owner_ids})
