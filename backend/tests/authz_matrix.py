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
    "capability:<x>|<y>" — requires any of them: an organisation-wide capability and its
                       team counterpart (ADR-0035); the service then applies the caller's
                       reach (a team administrator's team, 404 outside it)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RouteRule:
    methods: frozenset[str]
    access: str


def _rule(access: str, *methods: str) -> RouteRule:
    return RouteRule(methods=frozenset(methods), access=access)


# ADR-0035: an organisation-wide capability or its team counterpart; the services apply the
# caller's reach (a team administrator's team: 404 outside it).
USERS_MANAGE = "capability:users.manage|users.manage_team"
ASSIGN_ANY = "capability:crm.assign_any|crm.assign_team"
SUPPORT = "capability:support.access|support.access_team"
MANAGE_ANY = "capability:crm.manage_any|crm.manage_team"
OPPORTUNITY = "api/v1/workspaces/<str:workspace>/opportunities/<uuid:opportunity_id>"
ACTIVITY = "api/v1/workspaces/<str:workspace>/activities/<uuid:activity_id>"
PIPELINE = "api/v1/workspaces/<str:workspace>/pipelines/<uuid:pipeline_id>"
ASK = "api/v1/workspaces/<str:workspace>/ask"
AI_QUERY = "capability:ai.query"
PRIVACY = "capability:privacy.manage"

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
    # The emailed link that lifts a sign-in lock (review of 73ff1e3, L1).
    "api/v1/auth/sign-in-unlock": _rule("public", "POST"),
    "api/v1/auth/sign-in-unlock/verify": _rule("public", "POST"),  # review round 1
    # --- two-step verification (ADR-0033): the second sign-in step is public (it needs the
    # pending challenge of a right password). Status is harmless account metadata; every
    # control endpoint is Super Admin-only and refused in a support session. -----------------
    "api/v1/auth/login/mfa": _rule("public", "POST"),
    "api/v1/auth/mfa": _rule("authenticated", "GET"),
    "api/v1/auth/mfa/setup": _rule("capability:users.manage", "POST"),
    "api/v1/auth/mfa/confirm": _rule("capability:users.manage", "POST"),
    "api/v1/auth/mfa/recovery-codes": _rule("capability:users.manage", "POST"),
    "api/v1/auth/mfa/disable": _rule("capability:users.manage", "POST"),
    "api/v1/auth/mfa/verify": _rule("capability:users.manage", "POST"),
    # --- user administration (Phase 1). Since R100 the services also refuse another
    # administrator's email, password and role (tests/security/test_admin_takeover.py). ----
    "api/v1/admin/users": _rule(USERS_MANAGE, "GET", "POST"),
    "api/v1/admin/users/<uuid:user_id>": _rule(USERS_MANAGE, "GET", "PATCH"),
    "api/v1/admin/users/<uuid:user_id>/change-email": _rule(USERS_MANAGE, "POST"),
    "api/v1/admin/users/<uuid:user_id>/deactivate": _rule(USERS_MANAGE, "POST"),
    "api/v1/admin/users/<uuid:user_id>/activate": _rule(USERS_MANAGE, "POST"),
    "api/v1/admin/users/<uuid:user_id>/resend-invitation": _rule(USERS_MANAGE, "POST"),
    # --- product enhancement phase: admin-set passwords, security events, support sessions
    # (refused during a support session where marked; identity.permissions) ----------------
    "api/v1/admin/users/<uuid:user_id>/set-password": _rule(USERS_MANAGE, "POST"),
    # ADR-0036: an emailed reset link (Users, and Admins for a super admin; never a super
    # admin), and deleting an account (hand over, deactivate, erase the identity).
    "api/v1/admin/users/<uuid:user_id>/reset-password": _rule(USERS_MANAGE, "POST"),
    "api/v1/admin/users/<uuid:user_id>/deletion-preview": _rule(USERS_MANAGE, "GET"),
    "api/v1/admin/users/<uuid:user_id>/delete": _rule(USERS_MANAGE, "POST"),
    # An ordinary user's second factor; never an administrator's (ADR-0033).
    "api/v1/admin/users/<uuid:user_id>/mfa/reset": _rule(USERS_MANAGE, "POST"),
    # Lifting the daily sign-in lock (review of 73ff1e3, L1).
    "api/v1/admin/users/<uuid:user_id>/clear-sign-in-lock": _rule(USERS_MANAGE, "POST"),
    "api/v1/admin/security-events": _rule("capability:audit.view", "GET"),
    "api/v1/admin/support-sessions": _rule(SUPPORT, "POST"),
    "api/v1/admin/support-sessions/current": _rule(SUPPORT, "DELETE"),
    # --- privacy remediation: data-subject exports and staff pseudonymisation (refused in a
    # support session; only the requesting administrator downloads, privacy.exports) --------
    "api/v1/admin/privacy/exports": _rule(PRIVACY, "GET", "POST"),
    "api/v1/admin/privacy/exports/<uuid:export_id>": _rule(PRIVACY, "GET"),
    "api/v1/admin/privacy/exports/<uuid:export_id>/download": _rule(PRIVACY, "GET"),
    "api/v1/admin/privacy/users/<uuid:user_id>/pseudonymise": _rule(PRIVACY, "POST"),
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
    # crm.manage_any. The owner follows the lead: chosen only for a new opportunity's new
    # customer record organisation-wide, changed only by "assign" (ADR-0027). ---------------
    "api/v1/workspaces/<str:workspace>/pipeline-board": _rule("workspace", "GET"),
    "api/v1/workspaces/<str:workspace>/pipeline-summary": _rule("workspace", "GET"),
    "api/v1/workspaces/<str:workspace>/opportunities": _rule("workspace", "GET", "POST"),
    f"{OPPORTUNITY}": _rule("workspace", "GET", "PATCH"),
    f"{OPPORTUNITY}/move": _rule("workspace", "POST"),
    f"{OPPORTUNITY}/assign": _rule(ASSIGN_ANY, "POST"),
    f"{OPPORTUNITY}/archive": _rule("workspace", "POST"),
    f"{OPPORTUNITY}/restore": _rule("workspace", "POST"),
    f"{OPPORTUNITY}/history": _rule("workspace", "GET"),
    # --- product enhancement phase: pipelines per workspace (personal ones configured by
    # their owner or an administrator managing that workspace; shared ones by
    # administrators organisation-wide, config.manage, checked by the services). -----------
    f"{OPPORTUNITY}/negotiated-prices": _rule("workspace", "GET", "POST"),
    # Correcting a won or lost deal's figures (review of 73ff1e3, M2): administrators only
    # (crm.manage_any, a recent second factor, never in a support session); whoever sees the
    # deal reads its corrections.
    f"{OPPORTUNITY}/correct": _rule(MANAGE_ANY, "POST"),
    f"{OPPORTUNITY}/corrections": _rule("workspace", "GET"),
    f"{OPPORTUNITY}/notes": _rule("workspace", "GET"),
    f"{ACTIVITY}/attachments": _rule("workspace", "POST"),
    "api/v1/workspaces/<str:workspace>/attachments/<uuid:attachment_id>": _rule(
        "workspace", "DELETE"
    ),
    "api/v1/workspaces/<str:workspace>/attachments/<uuid:attachment_id>/download": _rule(
        "workspace", "GET"
    ),
    "api/v1/workspaces/<str:workspace>/attachments/<uuid:attachment_id>/preview": _rule(
        "workspace", "GET"
    ),
    "api/v1/workspaces/<str:workspace>/pipelines": _rule("workspace", "GET", "POST"),
    f"{PIPELINE}": _rule("workspace", "GET", "PATCH"),
    f"{PIPELINE}/stages": _rule("workspace", "PUT"),
    f"{PIPELINE}/fields": _rule("workspace", "PUT"),
    # Deleting a removed field's values: whoever may configure the pipeline (the services).
    f"{PIPELINE}/fields/<uuid:field_id>/delete-values": _rule("workspace", "POST"),
    f"{PIPELINE}/archive": _rule("workspace", "POST"),
    f"{PIPELINE}/restore": _rule("workspace", "POST"),
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>/convert": _rule("workspace", "POST"),
    # Correcting a customer's details (privacy remediation): the lead's scope and
    # authorize_write, as for any lead write (pipeline.corrections).
    "api/v1/workspaces/<str:workspace>/leads/<uuid:lead_id>/correction": _rule("workspace", "POST"),
    "api/v1/config/pipelines": _rule("authenticated", "GET"),
    "api/v1/config/opportunity-options": _rule("authenticated", "GET"),  # ADR-0028
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
    # --- global search (Phase 7). Read-only: every kind of record is searched inside the
    # scope (scope.apply() before any word is matched); the query is never stored. ---------
    "api/v1/workspaces/<str:workspace>/search": _rule("workspace", "GET"),
    # --- Ask Arkray (Phase 8). The ai.query capability, then the workspace (404 when the
    # caller may not open it); questions and conversations are the caller's own in that
    # workspace (404 otherwise). The model only reaches data through scope-bound tools. ------
    f"{ASK}": _rule(AI_QUERY, "GET", "POST"),
    f"{ASK}/questions/<uuid:question_id>": _rule(AI_QUERY, "GET"),
    f"{ASK}/conversations": _rule(AI_QUERY, "GET"),
    f"{ASK}/conversations/<uuid:conversation_id>": _rule(AI_QUERY, "GET", "DELETE"),
}
