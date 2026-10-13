"""Role -> capability policy. The single place that knows what each role may do.

Application code checks *capabilities*, never role names. Adding a role (e.g. a sales
manager who sees their team) means adding one entry here plus, if needed, a new scope kind
in resolve_workspace — no endpoint changes. Unknown roles and inactive users get nothing.

Teams (ADR-0035): the super administrator (role "admin") holds the organisation-wide
capabilities; a team administrator (role "team_admin") holds their `*_team` counterparts,
which reach only the actor and the sales users they manage (identity.teams). The team
capabilities are distinct strings on purpose: a check that knows only the organisation-wide
capability denies a team administrator, so anything not built for teams fails closed.
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
    # Start a time-limited, audited support session in one user's CRM (never with their
    # password; docs/admin-user-workspace.md#support-sessions).
    SUPPORT_ACCESS = "support.access"
    # Privacy requests: export a person's data, pseudonymise a former staff member
    # (docs/privacy.md; audited, never inside a support session).
    PRIVACY_MANAGE = "privacy.manage"
    # --- a team administrator's counterparts (ADR-0035): the same powers, over the actor and
    # the sales users they manage only (identity.teams) -----------------------------------------
    CRM_VIEW_TEAM = "crm.view_team"  # the "all" workspace is the team (scope TEAM)
    WORKSPACE_VIEW_TEAM = "workspace.view_team"  # open a team member's workspace (audited)
    CRM_ASSIGN_TEAM = "crm.assign_team"  # assign / reassign records within the team
    CRM_MANAGE_TEAM = "crm.manage_team"  # write in a team member's workspace or the team's
    USERS_MANAGE_TEAM = "users.manage_team"  # create and manage the team's sales users
    SUPPORT_ACCESS_TEAM = "support.access_team"  # support sessions with team members


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
            Capability.SUPPORT_ACCESS,
            Capability.PRIVACY_MANAGE,
        }
    ),
    # No configuration, audit or privacy capability: those are organisation-wide and stay
    # with the super administrators (ADR-0035).
    Role.TEAM_ADMIN: frozenset(
        {
            Capability.CRM_ACCESS_OWN,
            Capability.CRM_VIEW_TEAM,
            Capability.WORKSPACE_VIEW_TEAM,
            Capability.CRM_ASSIGN_TEAM,
            Capability.CRM_MANAGE_TEAM,
            Capability.USERS_MANAGE_TEAM,
            Capability.AI_QUERY,
            Capability.SUPPORT_ACCESS_TEAM,
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


def has_any_capability(actor: _Actor | None, *capabilities: Capability) -> bool:
    held = capabilities_for(actor)
    return any(capability in held for capability in capabilities)


def roles_with(capability: Capability) -> frozenset[str]:
    """Roles granting `capability`, for queries such as "who else can manage users"."""
    return frozenset(role for role, caps in ROLE_CAPABILITIES.items() if capability in caps)


def administrator_roles() -> frozenset[str]:
    """Roles that manage user accounts, organisation-wide or a team's: the administrators
    whose credentials only they change (ADR-0030), who are always invited, and for whom a
    second factor is required (ADR-0033). The last-administrator guard counts only the
    organisation-wide ones (`roles_with(USERS_MANAGE)`)."""
    return roles_with(Capability.USERS_MANAGE) | roles_with(Capability.USERS_MANAGE_TEAM)
