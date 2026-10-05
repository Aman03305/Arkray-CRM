/**
 * Sidebar model. Deliberately contains only the four CRM modules, Ask Arkray (when it is
 * on and the viewer may ask), Settings, and Users for viewers who can manage users. There are no Companies or Products modules.
 *
 * During a support session the administrator works only in that one user's CRM: every link
 * leads into it, Users and Settings are not offered, and any other page is replaced by the
 * same module of that user's workspace (supportSessionPath).
 */
import {
  CalendarCheck,
  MessageSquareText,
  Contact,
  LayoutDashboard,
  type LucideIcon,
  Settings,
  SquareKanban,
  UsersRound,
} from "lucide-react";

import { canAsk, hasCapability, type SupportSession, type Viewer } from "./viewer";
import {
  activeSection,
  askHref,
  isAskPath,
  SECTION_LABELS,
  type Workspace,
  workspaceFromPathname,
  WORKSPACE_SECTIONS,
  type WorkspaceSection,
  workspaceHref,
  userIdFromPathname,
} from "./workspace";

/**
 * The workspace the shell's links lead into. A URL naming no workspace (a malformed user
 * id) shows "not found"; the links then lead back to the viewer's own top-level pages,
 * never into a guessed workspace. So does a selected user's workspace the viewer may not
 * open (a link shared by an administrator): its pages are "not found" for them, and so were
 * all five links (whole-software audit).
 */
export function navigationWorkspace(pathname: string, viewer: Viewer | null): Workspace {
  const session = viewer?.supportSession;
  if (session) return { kind: "user", userId: session.target.id };
  const named = workspaceFromPathname(pathname, viewer);
  return named && !(named.kind === "user" && !hasCapability(viewer, "workspace.view_any"))
    ? named
    : workspaceFromPathname("/", viewer)!;
}

export interface NavItem {
  key: string;
  label: string;
  href: string;
  icon: LucideIcon;
}

const SECTION_ICONS: Record<WorkspaceSection, LucideIcon> = {
  dashboard: LayoutDashboard,
  pipeline: SquareKanban,
  leads: Contact,
  activities: CalendarCheck,
};

/** The four modules, every link inside `workspace` (a selected user's stay under their URL). */
export function workspaceNavigation(workspace: Workspace): NavItem[] {
  return WORKSPACE_SECTIONS.map((section) => ({
    key: section,
    label: SECTION_LABELS[section],
    href: workspaceHref(workspace, section),
    icon: SECTION_ICONS[section],
  }));
}

/** Ask Arkray, inside `workspace`: only when the CRM has it on and the viewer may ask. */
export function assistantNavigation(workspace: Workspace, viewer: Viewer | null): NavItem[] {
  return canAsk(viewer) ? [{ key: "ask", label: "Ask Arkray", href: askHref(workspace), icon: MessageSquareText }] : [];
}

export function administrationNavigation(viewer: Viewer | null): NavItem[] {
  return hasCapability(viewer, "users.manage") && !viewer?.supportSession
    ? [{ key: "users", label: "Users", href: "/admin/users", icon: UsersRound }]
    : [];
}

export const settingsNavItem: NavItem = {
  key: "settings",
  label: "Settings",
  href: "/settings",
  icon: Settings,
};

/** Settings (the viewer's own account), except during a support session. */
export function showsSettings(viewer: Viewer | null): boolean {
  return !viewer?.supportSession;
}

/**
 * Where a page goes during a support session: null when it is already in the supported
 * user's workspace; otherwise the same module of that workspace (/leads/... -> their Leads,
 * another user's Pipeline -> theirs), or their Dashboard for anything else (Users, Settings,
 * the organisation dashboard). Records of other workspaces are never carried across.
 */
export function supportSessionPath(pathname: string, session: SupportSession): string | null {
  const userId = userIdFromPathname(pathname);
  if (userId === session.target.id) return null;
  const workspace: Workspace = { kind: "user", userId: session.target.id };
  if (isAskPath(pathname)) return askHref(workspace);
  return workspaceHref(workspace, activeSection(pathname) ?? "dashboard");
}
