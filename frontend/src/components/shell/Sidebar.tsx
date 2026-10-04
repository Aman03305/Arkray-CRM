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
  settingsNavItem,
  workspaceNavigation,
} from "@/lib/navigation";
import { hasCapability, initials } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { activeSection, isAskPath, workspaceFromPathname } from "@/lib/workspace";

const NAV_LINK = "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors";
// The current page is marked by more than colour: a bar at its left edge and a heavier weight.
const NAV_LINK_ACTIVE = "bg-slate-100 font-medium text-slate-900 shadow-[inset_3px_0_0_var(--color-brand-600)]";

function NavLink({ item, active, onNavigate }: { item: NavItem; active: boolean; onNavigate?: () => void }) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={`${NAV_LINK} ${active ? NAV_LINK_ACTIVE : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"}`}
    >
      <Icon aria-hidden="true" className={`size-4 ${active ? "text-brand-600" : "text-slate-500"}`} />
      {item.label}
    </Link>
  );
}

/** Whose CRM the four module links open, in a selected user's workspace (desktop and drawer). */
function WorkspaceLabel({ id, userId }: { id: string; userId: string }) {
  const subject = useWorkspaceSubject(userId);
  return (
    <p id={id} className="truncate px-3 pb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
      CRM for{" "}
      {subject.data ? (
        <span className="normal-case tracking-normal text-slate-700">{subject.data.full_name}</span>
      ) : subject.isError ? (
        <span className="normal-case tracking-normal">the selected user</span>
      ) : (
        <>
          <Skeleton className="h-3 w-24 align-middle" />
          <span className="sr-only">the selected user (loading)</span>
        </>
      )}
    </p>
  );
}

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const viewer = useViewer();
  // A URL naming no workspace (a malformed user id) shows "not found"; the links then lead
  // back to the viewer's own top-level pages, never into a guessed workspace. So does a
  // selected user's workspace the viewer may not open (a link shared by an administrator):
  // its pages are "not found" for them, and so were all five links (whole-software audit).
  const named = workspaceFromPathname(pathname, viewer);
  const workspace =
    named && !(named.kind === "user" && !hasCapability(viewer, "workspace.view_any"))
      ? named
      : workspaceFromPathname("/", viewer)!;
  const userId = selectedUserId(workspace);
  const labelId = useId();
  const section = activeSection(pathname);
  const adminItems = administrationNavigation(viewer);
  const signOut = useSignOut();

  return (
    <div className="flex h-full flex-col border-r border-slate-200 bg-white">
      <div className="flex h-14 items-center gap-2 border-b border-slate-200 px-5">
        <span aria-hidden="true" className="flex size-7 items-center justify-center rounded-md bg-brand-600 text-xs font-bold text-white">
          A
        </span>
        <span className="text-sm font-semibold tracking-tight">Arkray CRM</span>
      </div>

      <nav aria-label="Main" className="flex-1 space-y-6 overflow-y-auto px-3 py-4">
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
            <p className="px-3 pb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Administration</p>
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

      <div className="space-y-1 border-t border-slate-200 px-3 py-3">
        {signOut.isError ? (
          <p role="alert" className="px-3 text-xs text-red-600">
            Sign-out failed. Check your connection and try again.
          </p>
        ) : null}
        <NavLink item={settingsNavItem} active={pathname.startsWith(settingsNavItem.href)} onNavigate={onNavigate} />
        <div className="flex items-center gap-3 px-3 py-2" aria-label="Current user">
          {viewer ? (
            <>
              <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-full bg-slate-200 text-xs font-semibold text-slate-700">
                {initials(viewer.fullName)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-slate-900">{viewer.fullName}</span>
                <span className="block truncate text-xs text-slate-500">{viewer.email}</span>
              </span>
              <button
                type="button"
                onClick={() => signOut.mutate()}
                disabled={signOut.isPending || signOut.isSuccess}
                aria-label="Sign out"
                title={signOut.isError ? "Sign-out failed. Try again." : "Sign out"}
                className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
              >
                {signOut.isPending ? <Spinner /> : <LogOut aria-hidden="true" className="size-4" />}
              </button>
            </>
          ) : (
            <>
              <Skeleton className="size-8 rounded-full" />
              <Skeleton className="h-3 w-28" />
              <span className="sr-only">Loading user</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
