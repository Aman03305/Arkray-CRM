# 0010. Next.js with URL-derived workspaces and shared module views

Status: Accepted
Date: 2026-09-30

## Context
Admins must navigate a selected user's Dashboard, Pipeline, Leads and Activities, with the
context persisting across navigation and without duplicating the frontend. Admin Home should be
user-management oriented (org KPIs + users table).

## Decision
- Next.js 16 App Router, TypeScript strict and Tailwind v4, with client-side data fetching
  against the same-origin API. Node 24 LTS.
- **The URL is the source of truth.** `/admin/users/{id}/<section>` is that user's workspace;
  top-level `/<section>` is the viewer's own workspace, or the organisation for viewers with
  `crm.view_all`. `workspaceFromPathname()` is a pure, tested function.
- Module views are written once and rendered in both route trees. They get their API path from
  the workspace (`/api/v1/workspaces/{me|all|id}/...`). The sidebar derives its links from the
  same function, so navigation stays inside the selected user's workspace.
- The admin's top-level Dashboard (organisation workspace) renders Admin Home: org KPIs plus
  the Users table.
- The UI shows or hides by capability; the API enforces.

## Consequences
- Context survives reloads and shared links, and there is no client state to desynchronise.
- One implementation per module view, so fixes apply to users and admins alike.
- Views must never branch on "is admin"; if they branch at all, it is on workspace kind.

## Alternatives considered
- A React context holding the "selected user": lost on reload, and the sidebar is rendered
  outside the nested layouts.
- Duplicated admin pages: double maintenance and divergent behaviour.
- A Vite SPA: viable, but Next.js was requested and provides routing, layouts and a production
  server out of the box.
