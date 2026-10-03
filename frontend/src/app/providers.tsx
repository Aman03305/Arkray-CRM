"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";
import { flushSync } from "react-dom";

import { AUTH_CHANNEL, LEAVING_EVENT, reloadPage } from "@/lib/browser";
import { createQueryClient, isPublicPath } from "@/lib/query-client";

/** App-wide providers, plus the guards that stop data outliving a sign-in (lib/browser). */
export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(createQueryClient);
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    // Set once this tab starts leaving by itself (sign-in, sign-out, session end).
    let leavingNow = false;
    // Synchronously unmount everything before the browser snapshots the page.
    const onLeaving = () => {
      leavingNow = true;
      flushSync(() => setLeaving(true));
    };
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) reloadPage(); // restored from the back/forward cache
    };
    window.addEventListener(LEAVING_EVENT, onLeaving);
    window.addEventListener("pageshow", onPageShow);

    let channel: BroadcastChannel | null = null;
    try {
      channel = new BroadcastChannel(AUTH_CHANNEL);
      channel.onmessage = () => {
        // This tab's own announcement reaches this listener too (every other channel object
        // gets the message, even in the same page). A tab already on its way out must not
        // reload the page it is leaving: that cancelled sign-out's navigation and landed on
        // /login?next=... without the "signed out" message (Phase 2 live walkthrough).
        if (leavingNow) return;
        // Someone signed in or out in another tab: nothing shown here may be current.
        if (!isPublicPath(window.location.pathname)) {
          flushSync(() => setLeaving(true));
          reloadPage();
        }
      };
    } catch {
      channel = null;
    }

    return () => {
      window.removeEventListener(LEAVING_EVENT, onLeaving);
      window.removeEventListener("pageshow", onPageShow);
      channel?.close();
    };
  }, []);

  if (leaving) return null;
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
