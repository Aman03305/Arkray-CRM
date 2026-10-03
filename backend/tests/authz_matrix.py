"""The authorization matrix: every API route and the access rule it implements.

Adding an endpoint without an entry here fails tests/architecture. The architecture tests
also check that each view declares exactly the permission its rule states and exposes
exactly the listed methods, and tests/security/test_authz_matrix.py exercises every
route as anonymous, sales user and admin.

Access rules:
    "public"         — no authentication (auth bootstrap only: CSRF, login, reset, invite)
    "authenticated"  — any active signed-in user, acting on their own account only
    "workspace"      — the {workspace} segment is resolved via resolve_workspace (404 when
                       the caller may not open it); any active signed-in user may ask
    "capability:<x>" — requires identity.policy.Capability <x>
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RouteRule:
    methods: frozenset[str]
    access: str


def _rule(access: str, *methods: str) -> RouteRule:
    return RouteRule(methods=frozenset(methods), access=access)


USERS_MANAGE = "capability:users.manage"
ASSIGN_ANY = "capability:crm.assign_any"
OPPORTUNITY = "api/v1/workspaces/<str:workspace>/opportunities/<uuid:opportunity_id>"
ACTIVITY = "api/v1/workspaces/<str:workspace>/activities/<uuid:activity_id>"

# Route pattern (as produced by the URL resolver) -> rule.
AUTHZ_MATRIX: dict[str, RouteRule] = {
    # --- authentication (Phase 1) ---------------------------------------------------------
    "api/v1/auth/csrf": _rule("public", "GET"),
    "api/v1/auth/login": _rule("public", "POST"),
    "api/v1/auth/logout": _rule("public", "POST"),
    "api/v1/auth/me": _rule("authenticated", "GET"),
    "api/v1/auth/password/change": _rule("authenticated", "POST"),
    "api/v1/auth/password-reset": _rule("public", "POST"),
    "api/v1/auth/password-reset/confirm": _rule("public", "POST"),
    "api/v1/auth/invitations/verify": _rule("public", "POST"),
    "api/v1/auth/invitations/accept": _rule("public", "POST"),
    # --- user administration (Phase 1) ----------------------------------------------------
    "api/v1/admin/users": _rule(USERS_MANAGE, "GET", "POST"),
    "api/v1/admin/users/<uuid:user_id>": _rule(USERS_MANAGE, "GET", "PATCH"),
    "api/v1/admin/users/<uuid:user_id>/change-email": _rule(USERS_MANAGE, "POST"),
    "api/v1/admin/users/<uuid:user_id>/deactivate": _rule(USERS_MANAGE, "POST"),
    "api/v1/admin/users/<uuid:user_id>/activate": _rule(USERS_MANAGE, "POST"),
    "api/v1/admin/users/<uuid:user_id>/resend-invitation": _rule(USERS_MANAGE, "POST"),
    # --- workspaces (Phase 1: describe; CRM resources nest below it from Phase 2) ------------
    "api/v1/workspaces/<str:workspace>": _rule("workspace", "GET"),
    # --- leads (Phase 2). Writes in delegated workspaces additionally need crm.manage_any
    # (identity.workspaces.authorize_write); the scope decides which leads exist (404). -----
    "api/v1/workspaces/<str:workspace>/leads": _rule("workspace", "GET", "POST"),
    "api/v1/workspaces/<str:workspace>/leads/duplicates": _rule("workspace", "GET"),
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>": _rule("workspace", "GET", "PATCH"),
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>/status": _rule("workspace", "POST"),
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>/assign": _rule(ASSIGN_ANY, "POST"),
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>/archive": _rule("workspace", "POST"),
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>/restore": _rule("workspace", "POST"),
    "api/v1/config/lead-options": _rule("authenticated", "GET"),
    "api/v1/assignees": _rule(ASSIGN_ANY, "GET"),
    # --- pipeline (Phase 3). Same workspace rules as leads: the scope decides which
    # opportunities (and which totals) exist; writes in delegated workspaces need
    # crm.manage_any. Nobody chooses an owner: it follows the lead. ------------------------
    "api/v1/workspaces/<str:workspace>/pipeline-board": _rule("workspace", "GET"),
    "api/v1/workspaces/<str:workspace>/pipeline-summary": _rule("workspace", "GET"),
    "api/v1/workspaces/<str:workspace>/opportunities": _rule("workspace", "GET", "POST"),
    f"{OPPORTUNITY}": _rule("workspace", "GET", "PATCH"),
    f"{OPPORTUNITY}/move": _rule("workspace", "POST"),
    f"{OPPORTUNITY}/archive": _rule("workspace", "POST"),
    f"{OPPORTUNITY}/restore": _rule("workspace", "POST"),
    f"{OPPORTUNITY}/history": _rule("workspace", "GET"),
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>/convert": _rule("workspace", "POST"),
    "api/v1/config/pipelines": _rule("authenticated", "GET"),
    # --- activities (Phase 4). Same workspace rules: the scope decides which activities,
    # timelines and counts exist; writes in delegated workspaces need crm.manage_any. Nobody
    # chooses an owner (current work follows the lead); lifecycle changes are actions. -------
    "api/v1/workspaces/<str:workspace>/activities": _rule("workspace", "GET", "POST"),
    "api/v1/workspaces/<str:workspace>/activity-summary": _rule("workspace", "GET"),
    f"{ACTIVITY}": _rule("workspace", "GET", "PATCH"),
    f"{ACTIVITY}/complete": _rule("workspace", "POST"),
    f"{ACTIVITY}/cancel": _rule("workspace", "POST"),
    f"{ACTIVITY}/reopen": _rule("workspace", "POST"),
    f"{ACTIVITY}/archive": _rule("workspace", "POST"),
    f"{ACTIVITY}/restore": _rule("workspace", "POST"),
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>/timeline": _rule("workspace", "GET"),
    f"{OPPORTUNITY}/timeline": _rule("workspace", "GET"),
    # --- dashboard (Phase 5). Read-only: the scope decides which records every figure and
    # list may include (aggregates are computed from scope.apply() first). -----------------
    "api/v1/workspaces/<str:workspace>/dashboard": _rule("workspace", "GET"),
}
