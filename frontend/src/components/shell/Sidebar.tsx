"use client";

import { LogOut } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { useSignOut } from "@/features/auth/useSignOut";
import {
  administrationNavigation,
  type NavItem,
  settingsNavItem,
  workspaceNavigation,
} from "@/lib/navigation";
import { initials } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { activeSection, workspaceFromPathname } from "@/lib/workspace";

function NavLink({ item, active, onNavigate }: { item: NavItem; active: boolean; onNavigate?: () => void }) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={`flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors ${
        active
          ? "bg-slate-100 font-medium text-slate-900"
          : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
      }`}
    >
      <Icon aria-hidden="true" className={`size-4 ${active ? "text-brand-600" : "text-slate-400"}`} />
      {item.label}
    </Link>
  );
}

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const viewer = useViewer();
  const workspace = workspaceFromPathname(pathname, viewer);
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
        <ul className="space-y-1">
          {workspaceNavigation(workspace).map((item) => (
            <li key={item.key}>
              <NavLink item={item} active={section === item.key} onNavigate={onNavigate} />
            </li>
          ))}
        </ul>

        {adminItems.length > 0 ? (
          <div>
            <p className="px-3 pb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Administration</p>
            <ul className="space-y-1">
              {adminItems.map((item) => (
                <li key={item.key}>
                  <NavLink item={item} active={pathname.startsWith(item.href)} onNavigate={onNavigate} />
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
                className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
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
