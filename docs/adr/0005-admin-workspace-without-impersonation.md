# 0005. Admin user workspace as scoped, audited access, not impersonation

Status: Accepted
Date: 2026-09-30

## Context
Clicking a user's name must open that user's CRM (`/admin/users/{id}/dashboard`) with
Dashboard, Pipeline, Leads and Activities, and a persistent context banner. Admins must never
need the user's password or take over their session, and every access must be
permission-checked and auditable.

## Decision
- All CRM endpoints live under `/api/v1/workspaces/{workspace}/`, where `{workspace}` is
  `me`, `all` or a user UUID.
- `identity.workspaces.resolve_workspace(actor, workspace)` returns an `AccessScope`. A UUID
  requires `workspace.view_any`. Without it, or if the user doesn't exist, the result is 404
  (enumeration-safe). Deactivated users remain viewable.
- The admin remains the authenticated actor. Writes record the admin as actor and
  `created_by`, and the viewed user as `subject_user_id`. Records created in Rahul's workspace
  are owned by Rahul.
- Delegated viewing writes `workspace.accessed` at least once per actor/workspace per 15 min,
  and audits every access if the de-duplication cache is down. Writes are always audited.
- The frontend reuses the same module views; the workspace is derived from the URL
  ([ADR-0010](0010-frontend-workspace-routing.md)).

## Consequences
- No session juggling and no "log in as" surface to abuse.
- The audit trail answers "who looked at Rahul's pipeline, and when".
- Every CRM route carries a workspace segment. URLs are slightly longer, and the explicitness
  is intended.

## Alternatives considered
- **Impersonation / "login as"**: actor identity is lost or confused, sessions are mixed, and
  it is dangerous.
- An `?owner=` query parameter on flat routes: easy to forget and easy to misuse; the path
  segment forces scope resolution on every CRM route.
- Separate admin endpoints duplicating every CRM endpoint: double the code and double the
  authorization surface.
