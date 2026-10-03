"use client";

import { useCallback, useState } from "react";

import { type LeadFilters, NO_FILTERS } from "./api";

/**
 * The Leads list's filters, search and page, remembered per workspace for this page load,
 * so opening a lead and coming back restores the list as it was.
 *
 * Deliberately in memory, not in the URL or browser storage: search terms are names,
 * emails and phone numbers, which must not end up in browser history, proxy logs or
 * storage that outlives a sign-out. Sign-out reloads the page, which clears this.
 * Keyed by workspace, so an admin's search in one user's workspace never carries over
 * to another's.
 */
interface ListState {
  filters: LeadFilters;
  cursor: string | null;
}

const remembered = new Map<string, ListState>();
const INITIAL: ListState = { filters: NO_FILTERS, cursor: null };

export function useLeadListState(workspaceKey: string) {
  const [state, setState] = useState<ListState>(() => remembered.get(workspaceKey) ?? INITIAL);

  const update = useCallback(
    (next: ListState) => {
      remembered.set(workspaceKey, next);
      setState(next);
    },
    [workspaceKey],
  );

  return {
    filters: state.filters,
    cursor: state.cursor,
    /** Change filters; always back to the first page. */
    setFilters: (patch: Partial<LeadFilters>) => update({ filters: { ...state.filters, ...patch }, cursor: null }),
    resetFilters: (keep: Partial<LeadFilters> = {}) => update({ filters: { ...NO_FILTERS, ...keep }, cursor: null }),
    setCursor: (cursor: string | null) => update({ filters: state.filters, cursor }),
  };
}

/**
 * Open this workspace's Leads list with these filters (the rest at their defaults) the next
 * time it is shown, replacing what was remembered: how a dashboard figure opens exactly the
 * list it counts ("New leads today": created today) without putting filters in the URL.
 */
export function presetLeadList(workspaceKey: string, filters: Partial<LeadFilters> = {}): void {
  remembered.set(workspaceKey, { filters: { ...NO_FILTERS, ...filters }, cursor: null });
}

/** Tests only. */
export function forgetLeadListState(): void {
  remembered.clear();
}
