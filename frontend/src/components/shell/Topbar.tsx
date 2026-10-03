"use client";

import { Menu, Search } from "lucide-react";

export function Topbar({ onMenuClick }: { onMenuClick: () => void }) {
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-slate-200 bg-white/95 px-4 backdrop-blur lg:px-8">
      <button
        type="button"
        onClick={onMenuClick}
        className="rounded-md p-2 text-slate-500 hover:bg-slate-100 lg:hidden"
        aria-label="Open navigation"
      >
        <Menu aria-hidden="true" className="size-5" />
      </button>

      {/* Global search is wired to /workspaces/{workspace}/search in Phase 7. */}
      <div className="relative max-w-md flex-1">
        <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-400" />
        <input
          type="search"
          disabled
          placeholder="Search leads, opportunities, activities"
          aria-label="Search"
          className="h-9 w-full rounded-md border border-slate-200 bg-slate-50 pl-9 pr-3 text-sm placeholder:text-slate-400 disabled:cursor-not-allowed"
        />
      </div>
    </header>
  );
}
