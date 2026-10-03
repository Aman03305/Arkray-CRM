# 0004. Capability-based, deny-by-default authorization with owner scoping

Status: Accepted
Date: 2026-09-30

## Context
Sales users must see only their own records; admins see everything. More roles (for example,
sales managers) are expected. Authorization must be enforced server-side, including in search,
dashboards and AI.

## Decision
- Roles map to **capabilities** in one module (`identity.policy`); code checks capabilities,
  never role names. Unknown roles and inactive users get none.
- DRF's global default permission is `DenyAll`; each endpoint declares its permissions. An
  architecture test requires every `/api/` route to appear in the authorization matrix.
- Record access is expressed as an **`AccessScope`** (`SELF`, `USER`, `ORGANIZATION`) with
  constructor-enforced invariants; an empty owner set can never mean "all".
- Every CRM record has one `owner_id`. Every read path filters through `scope.apply()`.
  Records outside the scope return 404.
- v1 ownership coherence: related open records share their lead's owner, and reassignment
  moves them together.

## Consequences
- One mechanism covers the API, search, dashboard and Ask Arkray, so there is one place to get
  right and one to test.
- New roles need a mapping entry, plus a scope kind if they see other people's data; endpoints
  don't change.
- Record-level sharing ("share this lead with Priya") is not in v1; it would be a new scope
  source.

## Alternatives considered
- Django `Group`/`Permission` tables: model-level only, no record scoping, and
  DB-configurable roles that are harder to review.
- Per-object permission rows (django-guardian): storage and query cost the ownership model
  doesn't need.
- Checking roles inline in views: scattered logic that breaks when roles are added.
