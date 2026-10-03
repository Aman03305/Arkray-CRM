/**
 * Sidebar model. Deliberately contains only the four CRM modules plus Settings, and Users
 * for viewers who can manage users. There are no Companies or Products modules.
 */
import {
  CalendarCheck,
  Contact,
  LayoutDashboard,
  type LucideIcon,
  Settings,
  SquareKanban,
  UsersRound,
} from "lucide-react";

import { hasCapability, type Viewer } from "./viewer";
import { type Workspace, type WorkspaceSection, workspaceHref } from "./workspace";

export interface NavItem {
  key: string;
  label: string;
  href: string;
  icon: LucideIcon;
}

const SECTION_META: Record<WorkspaceSection, { label: string; icon: LucideIcon }> = {
  dashboard: { label: "Dashboard", icon: LayoutDashboard },
  pipeline: { label: "Pipeline", icon: SquareKanban },
  leads: { label: "Leads", icon: Contact },
  activities: { label: "Activities", icon: CalendarCheck },
};

export function workspaceNavigation(workspace: Workspace): NavItem[] {
  return (Object.keys(SECTION_META) as WorkspaceSection[]).map((section) => ({
    key: section,
    label: SECTION_META[section].label,
    href: workspaceHref(workspace, section),
    icon: SECTION_META[section].icon,
  }));
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
