"use client";

import { useCallback, useState } from "react";

import { type ActivityFilters, type ActivityTab, invalidRange, NO_FILTERS, TAB_DEFAULTS } from "./api";

/**
 * The Activities list's tab, filters and page, remembered per workspace for this page load,
 * so opening an activity and coming back restores the list as it was.
 *
 * In memory only, never in the URL or browser storage (like the Leads list): a lead filter
 * shows a lead's name, which must not end up in history or storage that outlives a
 * sign-out. Keyed by workspace, so an admin's filters in Rahul's activities never carry
 * over into Priya's. An inverted date range is shown as a problem and never sent: the list
 * keeps the last valid filters (`applied`).
 */
interface ListState {
  filters: ActivityFilters;
  applied: ActivityFilters;
  cursor: string | null;
}

const remembered = new Map<string, ListState>();
const INITIAL: ListState = { filters: NO_FILTERS, applied: NO_FILTERS, cursor: null };

export function useActivityListState(workspaceKey: string) {
  const [state, setState] = useState<ListState>(() => remembered.get(workspaceKey) ?? INITIAL);
  const update = useCallback(
    (next: ListState) => {
      remembered.set(workspaceKey, next);
      setState(next);
    },
    [workspaceKey],
  );
  const setFilters = (filters: ActivityFilters) =>
    update({ filters, applied: invalidRange(filters) ? state.applied : filters, cursor: null });
  return {
    filters: state.filters,
    applied: state.applied,
    rangeInvalid: invalidRange(state.filters),
    cursor: state.cursor,
    /** Change filters; always back to the first page. */
    setFilters: (patch: Partial<ActivityFilters>) => setFilters({ ...state.filters, ...patch }),
    /** A tab starts from its own sensible status and sort; other filters stay. */
    setTab: (tab: ActivityTab) => setFilters({ ...state.filters, tab, ...TAB_DEFAULTS[tab] }),
    resetFilters: () => setFilters({ ...NO_FILTERS, tab: state.filters.tab, ...TAB_DEFAULTS[state.filters.tab], archived: state.filters.archived }),
    setCursor: (cursor: string | null) => update({ ...state, cursor }),
  };
}

/** Tests only. */
export function forgetActivityListState(): void {
  remembered.clear();
}
