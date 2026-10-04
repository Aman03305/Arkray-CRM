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
 *
 * Every path below /admin/users/{segment}/ is a user workspace. If the segment is not a user
 * id, the path names no workspace at all (null): it never falls back to the viewer's own or
 * the organisation's records, which would put other data under the selected user's banner.
 */
import { hasCapability, type Viewer } from "./viewer";

export const WORKSPACE_SECTIONS = ["dashboard", "pipeline", "leads", "activities"] as const;
export type WorkspaceSection = (typeof WORKSPACE_SECTIONS)[number];

export const SECTION_LABELS: Record<WorkspaceSection, string> = {
  dashboard: "Dashboard",
  pipeline: "Pipeline",
  leads: "Leads",
  activities: "Activities",
};

export type Workspace =
  | { kind: "self" }
  | { kind: "organization" }
  | { kind: "user"; userId: string };

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ADMIN_USER_PATH = /^\/admin\/users\/([^/]+)(?:\/|$)/;

export function isUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
}

/**
 * The selected user's id in a /admin/users/{segment}/... path: undefined when the path is not
 * below a user, null when the segment is not a user id. The segment is percent-decoded
 * exactly once, as Next.js decodes the route's `userId` param, so "%66..." and "f..." name
 * the same user here and in the layout (and never "this user's banner, the organisation's
 * data").
 */
export function userIdFromPathname(pathname: string): string | null | undefined {
  const match = ADMIN_USER_PATH.exec(pathname);
  if (!match) return undefined;
  let segment: string;
  try {
    segment = decodeURIComponent(match[1]!);
  } catch {
    return null; // malformed percent-encoding
  }
  return isUuid(segment) ? segment.toLowerCase() : null;
}

export function workspaceFromPathname(pathname: string, viewer: Viewer | null): Workspace | null {
  const userId = userIdFromPathname(pathname);
  if (userId === null) return null;
  if (userId !== undefined) return { kind: "user", userId };
  return hasCapability(viewer, "crm.view_all") ? { kind: "organization" } : { kind: "self" };
}

/**
 * The canonical spelling of a user-workspace path (lower-case id, no percent-encoding in the
 * id), or null when `pathname` already is canonical or is not a user-workspace path.
 */
export function canonicalUserPath(pathname: string): string | null {
  const match = ADMIN_USER_PATH.exec(pathname);
  const userId = userIdFromPathname(pathname);
  if (!match || !userId || match[1] === userId) return null;
  return `/admin/users/${userId}${pathname.slice(`/admin/users/${match[1]}`.length)}`;
}

export function workspaceHref(workspace: Workspace, section: WorkspaceSection): string {
  return workspace.kind === "user"
    ? `/admin/users/${workspace.userId}/${section}`
    : `/${section}`;
}

/** The way back to a module of this workspace, e.g. from a lead that isn't found. */
export function sectionBack(workspace: Workspace, section: WorkspaceSection): { href: string; label: string } {
  return { href: workspaceHref(workspace, section), label: `Back to ${SECTION_LABELS[section]}` };
}

/** A user's CRM workspace, opened by an administrator (their Dashboard unless stated). */
export function userWorkspaceHref(userId: string, section: WorkspaceSection = "dashboard"): string {
  return workspaceHref({ kind: "user", userId: userId.toLowerCase() }, section);
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
  const raw = pathname.replace(/^\/admin\/users\/[^/]+/, "").split("/")[1] ?? "";
  let segment: string;
  try {
    segment = decodeURIComponent(raw); // as Next.js decodes the [section] param
  } catch {
    return null;
  }
  return (WORKSPACE_SECTIONS as readonly string[]).includes(segment) ? (segment as WorkspaceSection) : null;
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

/** Ask Arkray in this workspace: /ask, or /admin/users/{userId}/ask. */
export function askHref(workspace: Workspace): string {
  return workspace.kind === "user" ? `/admin/users/${workspace.userId}/ask` : "/ask";
}

/** Is `pathname` Ask Arkray (in any workspace)? */
export function isAskPath(pathname: string): boolean {
  return /^(?:\/admin\/users\/[^/]+)?\/ask\/?$/.test(pathname);
}

/** An activity's page in this workspace, e.g. /activities/{id} or /admin/users/{userId}/activities/{id}. */
export function activityHref(workspace: Workspace, activityId: string): string {
  return `${workspaceHref(workspace, "activities")}/${encodeURIComponent(activityId)}`;
}
