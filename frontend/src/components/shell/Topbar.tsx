"use client";

import { Menu } from "lucide-react";

import { SearchLauncher } from "@/features/search/SearchLauncher";

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

      {/* Global search of this page's workspace (/workspaces/{workspace}/search). */}
      <SearchLauncher />
    </header>
  );
}
