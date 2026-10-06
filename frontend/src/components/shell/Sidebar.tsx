"use client";

import { LogOut } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useId } from "react";

import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { useSignOut } from "@/features/auth/useSignOut";
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

import { BrandMark } from "./BrandMark";

const NAV_LINK = "focus-on-shell flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors";
// The current page is marked by more than colour: a bar at its left edge and a heavier weight.
const NAV_LINK_ACTIVE = "bg-brand-600 font-semibold text-white shadow-[inset_3px_0_0_#ffffff]";

function NavLink({ item, active, onNavigate }: { item: NavItem; active: boolean; onNavigate?: () => void }) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={`${NAV_LINK} ${active ? NAV_LINK_ACTIVE : "text-shell-muted hover:bg-white/10 hover:text-white"}`}
    >
      <Icon aria-hidden="true" className="size-4" />
      {item.label}
    </Link>
  );
}

/** Whose CRM the module links open, in a selected user's workspace (desktop and drawer). */
function WorkspaceLabel({ id, userId }: { id: string; userId: string }) {
  const subject = useWorkspaceSubject(userId);
  return (
    <p id={id} className="truncate px-3 pb-1 text-xs font-medium uppercase tracking-wide text-shell-muted">
      CRM for{" "}
      {subject.data ? (
        <span className="normal-case tracking-normal text-white">{subject.data.full_name}</span>
      ) : subject.isError ? (
        <span className="normal-case tracking-normal">the selected user</span>
      ) : (
        <>
          <Skeleton className="h-3 w-24 bg-white/20! align-middle" />
          <span className="sr-only">the selected user (loading)</span>
        </>
      )}
    </p>
  );
}

/** The full navigation with labels: the drawer on phones and tablets. */
export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const viewer = useViewer();
  const workspace = navigationWorkspace(pathname, viewer);
  const userId = selectedUserId(workspace);
  const labelId = useId();
  const section = activeSection(pathname);
  const adminItems = administrationNavigation(viewer);
  const signOut = useSignOut();

  return (
    <div className="flex h-full flex-col bg-shell text-white">
      <div className="flex h-12 items-center gap-2 border-b border-white/10 px-5">
        <BrandMark />
        <span className="text-sm font-semibold tracking-tight">Arkray CRM</span>
      </div>

      <nav aria-label="Main" className="scroll-slim flex-1 space-y-6 overflow-y-auto px-3 py-4">
        <div>
          {userId ? <WorkspaceLabel id={labelId} userId={userId} /> : null}
          <ul className="space-y-1" aria-labelledby={userId ? labelId : undefined}>
            {workspaceNavigation(workspace).map((item) => (
              <li key={item.key}>
                <NavLink item={item} active={section === item.key} onNavigate={onNavigate} />
              </li>
            ))}
            {assistantNavigation(workspace, viewer).map((item) => (
              <li key={item.key}>
                <NavLink item={item} active={isAskPath(pathname)} onNavigate={onNavigate} />
              </li>
            ))}
          </ul>
        </div>

        {adminItems.length > 0 ? (
          <div>
            <p className="px-3 pb-1 text-xs font-medium uppercase tracking-wide text-shell-muted">Administration</p>
            <ul className="space-y-1">
              {adminItems.map((item) => (
                <li key={item.key}>
                  <NavLink
                    item={item}
                    active={section === null && !isAskPath(pathname) && pathname.startsWith(item.href)}
                    onNavigate={onNavigate}
                  />
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </nav>

      <div className="space-y-1 border-t border-white/10 px-3 py-3">
        {signOut.isError ? (
          <p role="alert" className="px-3 text-xs text-red-200">
            Sign-out failed. Check your connection and try again.
          </p>
        ) : null}
        {showsSettings(viewer) ? (
          <NavLink item={settingsNavItem} active={pathname.startsWith(settingsNavItem.href)} onNavigate={onNavigate} />
        ) : null}
        <div className="flex items-center gap-3 px-3 py-2" aria-label="Current user">
          {viewer ? (
            <>
              <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-full bg-avatar text-xs font-semibold text-shell">
                {initials(viewer.fullName)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-white">{viewer.fullName}</span>
                <span className="block truncate text-xs text-shell-muted">{viewer.email}</span>
              </span>
              <button
                type="button"
                onClick={() => signOut.mutate()}
                disabled={signOut.isPending || signOut.isSuccess}
                aria-label="Sign out"
                title={signOut.isError ? "Sign-out failed. Try again." : "Sign out"}
                className="focus-on-shell -m-0.5 rounded-md p-2 text-shell-muted hover:bg-white/10 hover:text-white disabled:opacity-50"
              >
                {signOut.isPending ? <Spinner /> : <LogOut aria-hidden="true" className="size-4" />}
              </button>
            </>
          ) : (
            <>
              <Skeleton className="size-8 rounded-full bg-white/20!" />
              <Skeleton className="h-3 w-28 bg-white/20!" />
              <span className="sr-only">Loading user</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
