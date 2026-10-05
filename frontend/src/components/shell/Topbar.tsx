"use client";

import { ChevronDown, LogOut, Menu, Plus, SquareKanban } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { useSignOut } from "@/features/auth/useSignOut";
import { pipelinePermissions } from "@/features/pipeline/hooks";
import { SearchLauncher } from "@/features/search/SearchLauncher";
import { receivesNewWork, selectedUserId, useWorkspaceSubject } from "@/features/workspace/api";
import { navigationWorkspace, settingsNavItem, showsSettings } from "@/lib/navigation";
import { initials } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import { newOpportunityHref, userWorkspaceHref } from "@/lib/workspace";

import { BrandMark } from "./BrandMark";
import { HEADER_MENU_ITEM, HeaderMenu } from "./HeaderMenu";

/**
 * The header across the top of every page, as in Bigin: the product, search, a quick
 * "create" button and the account menu. On phones it also opens the navigation drawer.
 */
export function Topbar({ onMenuClick }: { onMenuClick: () => void }) {
  return (
    <header className="fixed inset-x-0 top-0 z-30 flex h-12 items-center gap-2 bg-shell px-2 text-white sm:gap-3 lg:px-0 lg:pr-4">
      <button
        type="button"
        onClick={onMenuClick}
        className="focus-on-shell rounded-md p-2 text-shell-muted hover:bg-white/10 hover:text-white lg:hidden"
        aria-label="Open navigation"
      >
        <Menu aria-hidden="true" className="size-5" />
      </button>

      <HomeLink />

      {/* Global search of this page's workspace (/workspaces/{workspace}/search). */}
      <SearchLauncher />

      <div className="ml-auto flex shrink-0 items-center gap-2">
        <QuickCreate />
        <AccountMenu />
      </div>
    </header>
  );
}

/** The product name, leading home (during a support session: the supported user's Dashboard). */
function HomeLink() {
  const session = useViewer()?.supportSession;
  return (
    <Link
      href={session ? userWorkspaceHref(session.target.id) : "/dashboard"}
      aria-label="Arkray CRM"
      className="focus-on-shell flex shrink-0 items-center gap-2 rounded-md lg:w-18 lg:justify-center xl:w-auto xl:pl-5 xl:pr-3"
    >
      <BrandMark />
      <span aria-hidden="true" className="hidden text-[15px] font-semibold tracking-tight sm:inline lg:hidden xl:inline">
        Arkray CRM
      </span>
    </Link>
  );
}

/** "+": the new-record forms this viewer may open in this page's workspace. */
function QuickCreate() {
  const pathname = usePathname();
  const viewer = useViewer();
  const workspace = navigationWorkspace(pathname, viewer);
  const subject = useWorkspaceSubject(selectedUserId(workspace));
  if (!viewer || !receivesNewWork(subject.data)) return null;
  const items = [
    pipelinePermissions(viewer, workspace).canWrite
      ? { key: "opportunity", label: "New opportunity", href: newOpportunityHref(workspace), icon: SquareKanban }
      : null,
  ].filter((item) => item !== null);
  if (items.length === 0) return null;
  return (
    <HeaderMenu
      label="Create"
      trigger={<Plus aria-hidden="true" className="size-5" />}
      triggerClassName="focus-on-shell flex size-8 items-center justify-center rounded-md bg-brand-600 text-white hover:bg-brand-700"
    >
      {(close) => (
        <>
          <p className="px-3.5 pb-1 pt-1 text-xs font-medium uppercase tracking-wide text-slate-500">Create</p>
          <ul>
            {items.map((item) => {
              const Icon = item.icon;
              return (
                <li key={item.key}>
                  <Link href={item.href} onClick={close} className={HEADER_MENU_ITEM}>
                    <Icon aria-hidden="true" className="size-4 text-brand-600" />
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </HeaderMenu>
  );
}

/** The signed-in user: their name, Settings and Sign out. */
function AccountMenu() {
  const viewer = useViewer();
  const signOut = useSignOut();
  if (!viewer) {
    return (
      <span className="flex items-center">
        <Skeleton className="size-8 rounded-full bg-white/20!" />
        <span className="sr-only">Loading user</span>
      </span>
    );
  }
  const SettingsIcon = settingsNavItem.icon;
  return (
    <HeaderMenu
      label={`Account: ${viewer.fullName}`}
      trigger={
        <>
          <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-full bg-avatar text-xs font-semibold text-shell">
            {initials(viewer.fullName)}
          </span>
          <ChevronDown aria-hidden="true" className="hidden size-3.5 text-shell-muted sm:block" />
        </>
      }
      triggerClassName="focus-on-shell flex items-center gap-1 rounded-full p-0.5 hover:bg-white/10"
    >
      {(close) => (
        <>
          <div className="flex items-center gap-3 border-b border-slate-100 px-3.5 pb-3 pt-2">
            <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-full bg-avatar text-sm font-semibold text-shell">
              {initials(viewer.fullName)}
            </span>
            <span className="min-w-0">
              <span className="block truncate text-sm font-semibold text-slate-900">{viewer.fullName}</span>
              <span className="block truncate text-xs text-slate-500">{viewer.email}</span>
            </span>
          </div>
          <div className="pt-1">
            {showsSettings(viewer) ? (
              <Link href={settingsNavItem.href} onClick={close} className={HEADER_MENU_ITEM}>
                <SettingsIcon aria-hidden="true" className="size-4 text-slate-500" />
                {settingsNavItem.label}
              </Link>
            ) : null}
            <button
              type="button"
              onClick={() => signOut.mutate()}
              disabled={signOut.isPending || signOut.isSuccess}
              className={HEADER_MENU_ITEM}
            >
              {signOut.isPending ? <Spinner /> : <LogOut aria-hidden="true" className="size-4 text-slate-500" />}
              Sign out
            </button>
            {signOut.isError ? (
              <p role="alert" className="px-3.5 pb-1 text-xs text-red-700">
                Sign-out failed. Check your connection and try again.
              </p>
            ) : null}
          </div>
        </>
      )}
    </HeaderMenu>
  );
}
