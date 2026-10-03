"use client";

import { X } from "lucide-react";
import { type ReactNode, useRef, useState } from "react";

import { useModalFocus } from "@/components/ui/useModalFocus";

import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";

export function AppShell({ children }: { children: ReactNode }) {
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const closeMobileNav = () => setMobileNavOpen(false);
  const drawer = useRef<HTMLElement>(null);
  const overlay = useRef<HTMLDivElement>(null);

  // A modal drawer: focus moves in, stays in, the page behind is inert, Escape closes it
  // and focus returns to the menu button.
  useModalFocus(mobileNavOpen, drawer, closeMobileNav, overlay);

  return (
    <div className="min-h-dvh">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-white focus:px-3 focus:py-2 focus:shadow"
      >
        Skip to content
      </a>

      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 hidden w-60 lg:block">
        <Sidebar />
      </aside>

      {/* Mobile sidebar */}
      {mobileNavOpen ? (
        <div ref={overlay} className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <div className="absolute inset-0 bg-slate-900/30" onClick={closeMobileNav} aria-hidden="true" />
          <aside ref={drawer} tabIndex={-1} className="relative h-full w-64 focus:outline-none">
            <Sidebar onNavigate={closeMobileNav} />
            <button
              type="button"
              onClick={closeMobileNav}
              className="absolute right-2 top-3 rounded-md p-2 text-slate-500 hover:bg-slate-100"
              aria-label="Close navigation"
            >
              <X aria-hidden="true" className="size-4" />
            </button>
          </aside>
        </div>
      ) : null}

      <div className="flex min-h-dvh flex-col lg:pl-60">
        <Topbar onMenuClick={() => setMobileNavOpen(true)} />
        <main id="main" className="flex-1 px-4 py-6 lg:px-8">
          {children}
        </main>
      </div>
    </div>
  );
}
