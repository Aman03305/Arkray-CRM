"use client";

import { X } from "lucide-react";
import { usePathname } from "next/navigation";
import { type ReactNode, useRef, useState } from "react";

import { Skeleton } from "@/components/ui/Skeleton";
import { useModalFocus } from "@/components/ui/useModalFocus";
import { PipelinesPanel } from "@/features/pipeline/PipelinesPanel";
import { navigationWorkspace, supportSessionPath } from "@/lib/navigation";
import { useViewer } from "@/lib/viewer-context";
import { activeSection } from "@/lib/workspace";

import { NavRail } from "./NavRail";
import { usePanelCollapsed } from "./panel-state";
import { Sidebar } from "./Sidebar";
import { SupportSessionBar } from "./SupportSessionBar";
import { Topbar } from "./Topbar";

/*
 * Bigin's layout: a dark header across the top; on desktop a narrow rail of modules on the
 * left and, on Pipeline pages, the pipelines panel next to it. Phones and tablets get the
 * header and a navigation drawer.
 *
 * During a support session a banner sits at the top of the content, and a page outside the
 * supported user's workspace is never rendered: it is replaced by theirs (SupportSessionBar).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const viewer = useViewer();
  const panelCollapsed = usePanelCollapsed();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const closeMobileNav = () => setMobileNavOpen(false);
  const drawer = useRef<HTMLElement>(null);
  const overlay = useRef<HTMLDivElement>(null);
  const session = viewer?.supportSession ?? null;
  // Mounted from the first support session on, so it also sees the session end.
  const [supportSeen, setSupportSeen] = useState(false);
  if (session && !supportSeen) setSupportSeen(true);
  const leaving = session ? supportSessionPath(pathname, session) !== null : false;

  // A modal drawer: focus moves in, stays in, the page behind is inert, Escape closes it
  // and focus returns to the menu button.
  useModalFocus(mobileNavOpen, drawer, closeMobileNav, overlay);

  const panelWorkspace = viewer && activeSection(pathname) === "pipeline" ? navigationWorkspace(pathname, viewer) : null;
  const contentInset = panelWorkspace ? (panelCollapsed ? "lg:pl-26" : "lg:pl-70") : "lg:pl-18";

  return (
    <div className="min-h-dvh">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-white focus:px-3 focus:py-2 focus:shadow"
      >
        Skip to content
      </a>

      <Topbar onMenuClick={() => setMobileNavOpen(true)} />

      {/* Desktop rail */}
      <aside className="fixed bottom-0 left-0 top-12 z-20 hidden w-18 lg:block">
        <NavRail />
      </aside>

      {/* Pipelines panel (Pipeline pages, desktop) */}
      {panelWorkspace ? (
        <aside className={`fixed bottom-0 left-18 top-12 z-20 hidden lg:block ${panelCollapsed ? "w-8" : "w-52"}`}>
          <PipelinesPanel workspace={panelWorkspace} />
        </aside>
      ) : null}

      {/* Mobile navigation drawer */}
      {mobileNavOpen ? (
        <div ref={overlay} className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <div className="absolute inset-0 bg-slate-900/40" onClick={closeMobileNav} aria-hidden="true" />
          <aside ref={drawer} tabIndex={-1} className="relative h-full w-64 focus:outline-none">
            <Sidebar onNavigate={closeMobileNav} />
            <button
              type="button"
              onClick={closeMobileNav}
              className="focus-on-shell absolute right-2 top-2 rounded-md p-2 text-shell-muted hover:bg-white/10 hover:text-white"
              aria-label="Close navigation"
            >
              <X aria-hidden="true" className="size-4" />
            </button>
          </aside>
        </div>
      ) : null}

      <div className={`flex min-h-dvh flex-col pt-12 ${contentInset}`}>
        {supportSeen ? <SupportSessionBar /> : null}
        <main id="main" className="min-w-0 flex-1 px-4 py-5 lg:px-6">
          {leaving ? (
            <div aria-busy="true">
              <Skeleton className="h-6 w-40" />
              <span className="sr-only">Loading</span>
            </div>
          ) : (
            children
          )}
        </main>
      </div>
    </div>
  );
}
