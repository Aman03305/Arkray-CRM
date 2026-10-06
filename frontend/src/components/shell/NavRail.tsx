"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useId } from "react";

import { LinkPending } from "@/components/ui/LinkPending";
import { selectedUserId, useWorkspaceSubject } from "@/features/workspace/api";
import {
  administrationNavigation,
  assistantNavigation,
  type NavItem,
  navigationWorkspace,
  settingsNavItem,
  showsSettings,
  workspaceNavigation,
} from "@/lib/navigation";
import { initials } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { activeSection, isAskPath } from "@/lib/workspace";

function RailLink({ item, active }: { item: NavItem; active: boolean }) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      title={item.label}
      className={`focus-on-shell relative flex flex-col items-center gap-1 px-1 py-2.5 text-[11px] font-medium leading-tight transition-colors focus-visible:-outline-offset-2 ${
        active ? "bg-brand-600 font-semibold text-white" : "text-shell-muted hover:bg-white/10 hover:text-white"
      }`}
    >
      <Icon aria-hidden="true" className="size-5" />
      <span className="max-w-full truncate">{item.label}</span>
      <LinkPending className="inset-x-3 bottom-1 h-0.5 rounded-full bg-white" />
    </Link>
  );
}

/** Whose CRM the module links open, in a selected user's workspace. */
function RailWorkspace({ id, userId }: { id: string; userId: string }) {
  const subject = useWorkspaceSubject(userId);
  const name = subject.data?.full_name;
  return (
    <div id={id} title={name ? `CRM for ${name}` : undefined} className="flex flex-col items-center gap-1 border-b border-white/10 px-1 pb-2.5 pt-2 text-[10px] leading-tight text-shell-muted">
      <span aria-hidden="true" className="flex size-7 items-center justify-center rounded-full bg-amber-200 text-[11px] font-semibold text-amber-950">
        {name ? initials(name) : "…"}
      </span>
      <span className="max-w-full truncate">
        <span className="sr-only">CRM for</span>{" "}
        {name ? name.split(" ")[0] : subject.isError ? "Selected user" : "Loading…"}
      </span>
    </div>
  );
}

/**
 * The desktop navigation: a narrow dark rail of icons with their names under them, as in
 * Bigin. Phones and tablets use the drawer (Sidebar) instead.
 */
export function NavRail() {
  const pathname = usePathname();
  const viewer = useViewer();
  const workspace = navigationWorkspace(pathname, viewer);
  const userId = selectedUserId(workspace);
  const labelId = useId();
  const section = activeSection(pathname);
  const adminItems = administrationNavigation(viewer);

  return (
    <nav aria-label="Main" className="scroll-slim flex h-full flex-col overflow-y-auto bg-shell">
      {userId ? <RailWorkspace id={labelId} userId={userId} /> : null}
      <ul aria-labelledby={userId ? labelId : undefined}>
        {workspaceNavigation(workspace).map((item) => (
          <li key={item.key}>
            <RailLink item={item} active={section === item.key} />
          </li>
        ))}
        {assistantNavigation(workspace, viewer).map((item) => (
          <li key={item.key}>
            <RailLink item={item} active={isAskPath(pathname)} />
          </li>
        ))}
      </ul>
      {adminItems.length > 0 ? (
        <ul aria-label="Administration" className="mt-2 border-t border-white/10 pt-2">
          {adminItems.map((item) => (
            <li key={item.key}>
              <RailLink item={item} active={section === null && !isAskPath(pathname) && pathname.startsWith(item.href)} />
            </li>
          ))}
        </ul>
      ) : null}
      {showsSettings(viewer) ? (
        <div className="mt-auto border-t border-white/10">
          <RailLink item={settingsNavItem} active={pathname.startsWith(settingsNavItem.href)} />
        </div>
      ) : null}
    </nav>
  );
}
