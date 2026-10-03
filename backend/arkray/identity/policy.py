"""Role -> capability policy. The single place that knows what each role may do.

Application code checks *capabilities*, never role names. Adding a role (e.g. a sales
manager who sees their team) means adding one entry here plus, if needed, a new scope kind
in resolve_workspace — no endpoint changes. Unknown roles and inactive users get nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from .models import Role


class Capability(StrEnum):
    CRM_ACCESS_OWN = "crm.access_own"  # work in one's own CRM workspace
    CRM_VIEW_ALL = "crm.view_all"  # organisation-wide CRM reads (scope "all")
    WORKSPACE_VIEW_ANY = "workspace.view_any"  # open any user's CRM workspace (audited)
    CRM_ASSIGN_ANY = "crm.assign_any"  # assign / reassign records to any user
    # Create, edit and archive records in another user's workspace or organisation-wide
    # (audited). Viewing a workspace (WORKSPACE_VIEW_ANY) never implies changing it.
    CRM_MANAGE_ANY = "crm.manage_any"
    USERS_MANAGE = "users.manage"  # create, edit, (de)activate, invite users
    CONFIG_MANAGE = "config.manage"  # pipelines, stages, lead statuses and sources
    AUDIT_VIEW = "audit.view"
    AI_QUERY = "ai.query"  # use Ask Arkray (within the caller's own scope)


ROLE_CAPABILITIES: Mapping[str, frozenset[Capability]] = {
    Role.ADMIN: frozenset(
        {
            Capability.CRM_ACCESS_OWN,
            Capability.CRM_VIEW_ALL,
            Capability.WORKSPACE_VIEW_ANY,
            Capability.CRM_ASSIGN_ANY,
            Capability.CRM_MANAGE_ANY,
            Capability.USERS_MANAGE,
            Capability.CONFIG_MANAGE,
            Capability.AUDIT_VIEW,
            Capability.AI_QUERY,
        }
    ),
    Role.SALES_USER: frozenset({Capability.CRM_ACCESS_OWN, Capability.AI_QUERY}),
}


class _Actor(Protocol):
    @property
    def is_authenticated(self) -> bool: ...

    @property
    def is_active(self) -> bool: ...


def capabilities_for(actor: _Actor | None) -> frozenset[Capability]:
    if actor is None or not actor.is_authenticated or not actor.is_active:
        return frozenset()
    role = getattr(actor, "role", None)
    return ROLE_CAPABILITIES.get(role, frozenset()) if isinstance(role, str) else frozenset()


def has_capability(actor: _Actor | None, capability: Capability) -> bool:
    return capability in capabilities_for(actor)


def roles_with(capability: Capability) -> frozenset[str]:
    """Roles granting `capability`, for queries such as "who else can manage users"."""
    return frozenset(role for role, caps in ROLE_CAPABILITIES.items() if capability in caps)
