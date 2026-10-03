/**
 * Workspaces: whose CRM data a page shows.
 *
 * The URL is the single source of truth, so the selected-user context survives navigation,
 * reloads and shared links without any client state:
 *
 *   /dashboard, /leads, ...                 -> own workspace ("me"), or the whole
 *                                              organisation ("all") for viewers with crm.view_all
 *   /admin/users/{userId}/dashboard, ...    -> that user's workspace (admin, audited server-side)
 *
 * The same page components render in every workspace; only the API path differs
 * (/api/v1/workspaces/{me|all|userId}/...). The backend resolves and authorises the segment.
 */
import { hasCapability, type Viewer } from "./viewer";

export const WORKSPACE_SECTIONS = ["dashboard", "pipeline", "leads", "activities"] as const;
export type WorkspaceSection = (typeof WORKSPACE_SECTIONS)[number];

export type Workspace =
  | { kind: "self" }
  | { kind: "organization" }
  | { kind: "user"; userId: string };

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ADMIN_USER_PATH = /^\/admin\/users\/([^/]+)(?:\/|$)/;

export function isUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
}

export function workspaceFromPathname(pathname: string, viewer: Viewer | null): Workspace {
  const match = ADMIN_USER_PATH.exec(pathname);
  if (match && isUuid(match[1]!)) {
    return { kind: "user", userId: match[1]!.toLowerCase() };
  }
  return hasCapability(viewer, "crm.view_all") ? { kind: "organization" } : { kind: "self" };
}

export function workspaceHref(workspace: Workspace, section: WorkspaceSection): string {
  return workspace.kind === "user"
    ? `/admin/users/${workspace.userId}/${section}`
    : `/${section}`;
}

/** A lead's page in this workspace, e.g. /leads/{id} or /admin/users/{userId}/leads/{id}. */
export function leadHref(workspace: Workspace, leadId: string, action?: "edit"): string {
  return `${workspaceHref(workspace, "leads")}/${encodeURIComponent(leadId)}${action ? `/${action}` : ""}`;
}

export function newLeadHref(workspace: Workspace): string {
  return `${workspaceHref(workspace, "leads")}/new`;
}

export function workspaceApiSegment(workspace: Workspace): string {
  switch (workspace.kind) {
    case "self":
      return "me";
    case "organization":
      return "all";
    case "user":
      return workspace.userId;
  }
}

export function workspaceApiPath(workspace: Workspace, resource: string): string {
  return `/api/v1/workspaces/${workspaceApiSegment(workspace)}/${resource.replace(/^\/+/, "")}`;
}

export function activeSection(pathname: string): WorkspaceSection | null {
  const segment = pathname.replace(/^\/admin\/users\/[^/]+/, "").split("/")[1];
  return (WORKSPACE_SECTIONS as readonly string[]).includes(segment ?? "")
    ? (segment as WorkspaceSection)
    : null;
}

export function describeWorkspace(workspace: Workspace): string {
  switch (workspace.kind) {
    case "self":
      return "Your records";
    case "organization":
      return "All users' records";
    case "user":
      return "Selected user's records";
  }
}

/** An opportunity's page in this workspace, e.g. /pipeline/{id} or /admin/users/{userId}/pipeline/{id}. */
export function opportunityHref(workspace: Workspace, opportunityId: string, action?: "edit"): string {
  return `${workspaceHref(workspace, "pipeline")}/${encodeURIComponent(opportunityId)}${action ? `/${action}` : ""}`;
}

/** The new-opportunity form, optionally for one lead (a UUID, never a name, in the URL). */
export function newOpportunityHref(workspace: Workspace, leadId?: string): string {
  const base = `${workspaceHref(workspace, "pipeline")}/new`;
  return leadId ? `${base}?lead=${encodeURIComponent(leadId)}` : base;
}

/** An activity's page in this workspace, e.g. /activities/{id} or /admin/users/{userId}/activities/{id}. */
export function activityHref(workspace: Workspace, activityId: string): string {
  return `${workspaceHref(workspace, "activities")}/${encodeURIComponent(activityId)}`;
}
