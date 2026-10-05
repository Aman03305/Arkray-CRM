"use client";

import { useSyncExternalStore } from "react";

/*
 * Whether the pipelines panel is collapsed: a display preference only (no CRM data), kept
 * in this browser. Storage can be unavailable (private windows, blocked site data): the
 * panel then simply starts open.
 */
const KEY = "arkray.pipelines-panel";
const listeners = new Set<() => void>();
let collapsed: boolean | null = null;

function read(): boolean {
  if (collapsed === null) {
    try {
      collapsed = window.localStorage.getItem(KEY) === "collapsed";
    } catch {
      collapsed = false;
    }
  }
  return collapsed;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function usePanelCollapsed(): boolean {
  return useSyncExternalStore(subscribe, read, () => false);
}

export function setPanelCollapsed(value: boolean): void {
  collapsed = value;
  try {
    if (value) window.localStorage.setItem(KEY, "collapsed");
    else window.localStorage.removeItem(KEY);
  } catch {
    // kept for this page load only
  }
  for (const listener of listeners) listener();
}
