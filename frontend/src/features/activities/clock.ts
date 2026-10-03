"use client";

import { useSyncExternalStore } from "react";

import type { ActivityListItem } from "@/lib/api/types";

/** How often time-dependent actions are re-evaluated while a page shows them. */
const CLOCK_TICK_MS = 30_000;

let now = Date.now();
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;

function tick(): void {
  now = Date.now();
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    now = Date.now(); // React re-reads the snapshot after subscribing
    timer = setInterval(tick, CLOCK_TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

/** The current time (ms), re-read every 30 s while anything on the page uses it: one timer
 * however many rows ask. */
export function useClock(): number {
  return useSyncExternalStore(
    subscribe,
    () => now,
    () => now,
  );
}

/**
 * Whether to offer Complete: the server's `completable`, computed when the activity was
 * fetched, or a scheduled meeting that has started since (a page left open shows its
 * Complete button within 30 s of the start, without a reload). Presentation only: the
 * server decides when it is submitted.
 */
export function canComplete(
  activity: Pick<ActivityListItem, "type" | "status" | "starts_at" | "archived_at" | "completable">,
  clock: number,
): boolean {
  if (activity.completable) return true;
  return (
    activity.type === "meeting" &&
    activity.status === "scheduled" &&
    activity.archived_at === null &&
    activity.starts_at !== null &&
    Date.parse(activity.starts_at) <= clock
  );
}
