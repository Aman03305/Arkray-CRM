"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

/**
 * A one-time notice carried across a client-side navigation ("Lead created" shown on the
 * page you land on). In memory only: a full page load (sign-out, reload) drops it, so it
 * can never be shown to the next person using the browser.
 *
 * A notice is bound to the page it was sent to. Only that page shows it; whichever page
 * mounts first takes it off the queue, so a navigation that never landed (Back pressed
 * during the round trip) can't leave "Rahul's lead was converted" for Priya's workspace
 * (Phase 6 review).
 */
let pending: { message: string; path: string } | null = null;

export function setFlash(message: string, path: string): void {
  pending = { message, path };
}

/** The notice for this page (shown once), and a setter for the page's own notices. */
export function useFlash(): [string | null, (message: string | null) => void] {
  const pathname = usePathname();
  // Read without consuming (React may run initialisers twice), then consume after mount.
  const [notice, setNotice] = useState<string | null>(() =>
    pending !== null && pending.path === pathname ? pending.message : null,
  );
  useEffect(() => {
    pending = null;
  }, []);
  return [notice, setNotice];
}
