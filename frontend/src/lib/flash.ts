"use client";

import { useEffect, useState } from "react";

/**
 * A one-time notice carried across a client-side navigation ("Lead created" shown on the
 * page you land on). In memory only: a full page load (sign-out, reload) drops it, so it
 * can never be shown to the next person using the browser.
 */
let pending: string | null = null;

export function setFlash(message: string): void {
  pending = message;
}

/** The notice for this page (shown once), and a setter for the page's own notices. */
export function useFlash(): [string | null, (message: string | null) => void] {
  // Read without consuming (React may run initialisers twice), then consume after mount.
  const [notice, setNotice] = useState<string | null>(() => pending);
  useEffect(() => {
    pending = null;
  }, []);
  return [notice, setNotice];
}
