"use client";

import { useCallback, useState } from "react";

import { businessToday } from "@/lib/format";

import { type ActivityFilters, type ActivityTab, invalidRange, NO_FILTERS, TAB_DEFAULTS } from "./api";
import type { CalendarView } from "./calendar";

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
export type PageView = "list" | "calendar";

interface ListState {
  /** The list or the calendar; any filter change shows the list (filters are the list's). */
  view: PageView;
  filters: ActivityFilters;
  applied: ActivityFilters;
  cursor: string | null;
}

const remembered = new Map<string, ListState>();
const INITIAL: ListState = { view: "list", filters: NO_FILTERS, applied: NO_FILTERS, cursor: null };

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
    update({ view: "list", filters, applied: invalidRange(filters) ? state.applied : filters, cursor: null });
  return {
    view: state.view,
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
    /** The calendar keeps the list's filters and page for when the list is shown again. */
    showCalendar: () => update({ ...state, view: "calendar" }),
  };
}

/**
 * Open this workspace's Activities list with these filters (the rest at their defaults) the
 * next time it is shown, replacing what was remembered: how a dashboard figure opens
 * exactly the list it counts (the same presets as the page's own shortcuts).
 */
export function presetActivityList(workspaceKey: string, filters: Partial<ActivityFilters> = {}): void {
  const preset = { ...NO_FILTERS, ...filters };
  remembered.set(workspaceKey, { view: "list", filters: preset, applied: preset, cursor: null });
}

/**
 * The calendar's view, the day it is on and (organisation-wide) whose activities it shows,
 * remembered per workspace for this page load like the list's filters. It opens on this
 * month, everyone's.
 */
interface CalendarState {
  view: CalendarView;
  /** A business date inside the period shown. */
  anchor: string;
  /** Organisation-wide only: the viewer's own activities ("My calendar"). */
  mine: boolean;
}

const rememberedCalendars = new Map<string, CalendarState>();

export function useCalendarState(workspaceKey: string) {
  const [state, setState] = useState<CalendarState>(
    () => rememberedCalendars.get(workspaceKey) ?? { view: "month", anchor: businessToday(), mine: false },
  );
  const update = (patch: Partial<CalendarState>) => {
    const next = { ...state, ...patch };
    rememberedCalendars.set(workspaceKey, next);
    setState(next);
  };
  return { ...state, update };
}

/** Tests only. */
export function forgetActivityListState(): void {
  remembered.clear();
  rememberedCalendars.clear();
}
