/**
 * Sidebar model. Deliberately contains only the four CRM modules, Ask Arkray (when it is
 * on and the viewer may ask), Settings, and Users for viewers who can manage users. There are no Companies or Products modules.
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

import { canAsk, hasCapability, type Viewer } from "./viewer";
import { askHref, SECTION_LABELS, type Workspace, WORKSPACE_SECTIONS, type WorkspaceSection, workspaceHref } from "./workspace";

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
  return hasCapability(viewer, "users.manage")
    ? [{ key: "users", label: "Users", href: "/admin/users", icon: UsersRound }]
    : [];
}

export const settingsNavItem: NavItem = {
  key: "settings",
  label: "Settings",
  href: "/settings",
  icon: Settings,
};
