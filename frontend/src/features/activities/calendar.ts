/**
 * The Activities calendar's dates. Pure functions, unit-tested without rendering. Every day
 * and hour is the business time zone's (Asia/Kolkata, like every time on screen), never the
 * browser's: a business date is a "YYYY-MM-DD" string, stepped as a UTC calendar date so no
 * browser zone can move it. Weeks start on Monday.
 */
import type { ActivityListItem } from "@/lib/api/types";
import { toBusinessDateTimeInput } from "@/lib/format";

import { DEFAULT_DUE_TIME, type Draft, suggestedEnd } from "./draft";

export type CalendarView = "month" | "week" | "day";

/** A task's or meeting's block in the week and day views when it has no length of its own. */
export const MIN_BLOCK_MINUTES = 30;
const DAY_MINUTES = 24 * 60;
/** Where a meeting scheduled from a month cell starts (the day view has the hours). */
const MONTH_CELL_START = "09:00";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const;
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"] as const;

const pad = (n: number) => String(n).padStart(2, "0");

function parts(date: string): { y: number; m: number; d: number } {
  const [y, m, d] = date.split("-").map(Number) as [number, number, number];
  return { y, m, d };
}

function fromUtc(value: Date): string {
  return `${value.getUTCFullYear()}-${pad(value.getUTCMonth() + 1)}-${pad(value.getUTCDate())}`;
}

export function addDays(date: string, days: number): string {
  const { y, m, d } = parts(date);
  return fromUtc(new Date(Date.UTC(y, m - 1, d + days)));
}

/** The first of the month `months` away from `date`'s month. */
export function addMonths(date: string, months: number): string {
  const { y, m } = parts(date);
  return fromUtc(new Date(Date.UTC(y, m - 1 + months, 1)));
}

/** 0 for Monday ... 6 for Sunday. */
export function weekdayIndex(date: string): number {
  const { y, m, d } = parts(date);
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;
}

export function startOfWeek(date: string): string {
  return addDays(date, -weekdayIndex(date));
}

export function isSameMonth(a: string, b: string): boolean {
  return a.slice(0, 7) === b.slice(0, 7);
}

/** The month's weeks, Monday to Sunday, from the week of the 1st to the week of the last day. */
export function monthWeeks(anchor: string): string[][] {
  const first = `${anchor.slice(0, 7)}-01`;
  const last = addDays(addMonths(anchor, 1), -1);
  const weeks: string[][] = [];
  for (let start = startOfWeek(first); start <= last; start = addDays(start, 7)) {
    weeks.push(Array.from({ length: 7 }, (_, i) => addDays(start, i)));
  }
  return weeks;
}

/** Every day the view shows, in order. */
export function visibleDays(view: CalendarView, anchor: string): string[] {
  if (view === "month") return monthWeeks(anchor).flat();
  if (view === "week") return Array.from({ length: 7 }, (_, i) => addDays(startOfWeek(anchor), i));
  return [anchor];
}

/** The date the previous / next button moves to: a month, a week or a day away. */
export function shiftAnchor(view: CalendarView, anchor: string, step: 1 | -1): string {
  if (view === "month") return addMonths(anchor, step);
  return addDays(anchor, view === "week" ? 7 * step : step);
}

function monthName(date: string): string {
  return MONTHS[parts(date).m - 1]!;
}

const shortMonth = (date: string) => monthName(date).slice(0, 3);

export function weekdayName(date: string): string {
  return WEEKDAYS[weekdayIndex(date)]!;
}

/** "Thursday, 1 October 2026". */
export function dayLabel(date: string): string {
  const { y, d } = parts(date);
  return `${weekdayName(date)}, ${d} ${monthName(date)} ${y}`;
}

/** "October 2026", "5 – 11 Oct 2026", "28 Sep – 4 Oct 2026", "Monday, 5 October 2026". */
export function periodTitle(view: CalendarView, anchor: string): string {
  if (view === "month") return `${monthName(anchor)} ${parts(anchor).y}`;
  if (view === "day") return dayLabel(anchor);
  const start = startOfWeek(anchor);
  const end = addDays(start, 6);
  const a = parts(start);
  const b = parts(end);
  if (a.y !== b.y) return `${a.d} ${shortMonth(start)} ${a.y} – ${b.d} ${shortMonth(end)} ${b.y}`;
  if (a.m !== b.m) return `${a.d} ${shortMonth(start)} – ${b.d} ${shortMonth(end)} ${b.y}`;
  return `${a.d} – ${b.d} ${shortMonth(end)} ${b.y}`;
}

/** "7:00 pm", "12:30 am" (minutes after the business midnight). */
export function clockLabel(minutes: number): string {
  const h = Math.floor(minutes / 60) % 24;
  const m = minutes % 60;
  return `${h % 12 === 0 ? 12 : h % 12}:${pad(m)} ${h < 12 ? "am" : "pm"}`;
}

/** "7 pm": an hour row's label. */
export function hourLabel(hour: number): string {
  return `${hour % 12 === 0 ? 12 : hour % 12} ${hour < 12 ? "am" : "pm"}`;
}

/** When a calendar entry happens: a task's due time, a meeting's start. */
export function scheduledAt(item: Pick<ActivityListItem, "type" | "due_at" | "starts_at">): string | null {
  return item.type === "meeting" ? item.starts_at : item.due_at;
}

/** The business date of an instant ("" when there is none). */
export function businessDateOf(iso: string | null): string {
  return toBusinessDateTimeInput(iso).slice(0, 10);
}

/** Minutes after the business midnight of an instant's own business day. */
export function businessMinutesOf(iso: string | null): number {
  const wallClock = toBusinessDateTimeInput(iso);
  if (!wallClock) return 0;
  return Number(wallClock.slice(11, 13)) * 60 + Number(wallClock.slice(14, 16));
}

/** Entries by their business date, each day's in time order (then by subject). */
export function byDay(items: readonly ActivityListItem[]): Map<string, ActivityListItem[]> {
  const days = new Map<string, ActivityListItem[]>();
  const sorted = [...items].sort(
    (a, b) => Date.parse(scheduledAt(a) ?? "") - Date.parse(scheduledAt(b) ?? "") || a.title.localeCompare(b.title) || a.id.localeCompare(b.id),
  );
  for (const item of sorted) {
    const day = businessDateOf(scheduledAt(item));
    if (!day) continue;
    const list = days.get(day);
    if (list) list.push(item);
    else days.set(day, [item]);
  }
  return days;
}

export interface Placed {
  item: ActivityListItem;
  /** Minutes after midnight where the block starts, and how many minutes it covers. */
  start: number;
  length: number;
  /** Side-by-side position among the entries it overlaps: lane `lane` of `lanes`. */
  lane: number;
  lanes: number;
}

/**
 * One day's entries as blocks on the hour grid: a meeting from its start to its end (cut at
 * midnight when it runs into the next day), a task as a short block at its due time.
 * Entries that overlap share the width, each in its own lane, as calendars do.
 */
export function layoutDay(items: readonly ActivityListItem[]): Placed[] {
  const blocks = items
    .map((item) => {
      const start = businessMinutesOf(scheduledAt(item));
      let length = MIN_BLOCK_MINUTES;
      if (item.type === "meeting" && item.starts_at && item.ends_at) {
        const minutes = Math.round((Date.parse(item.ends_at) - Date.parse(item.starts_at)) / 60_000);
        if (minutes > 0) length = minutes;
      }
      length = Math.max(MIN_BLOCK_MINUTES, Math.min(length, DAY_MINUTES - start));
      return { item, start, length, lane: 0, lanes: 1 };
    })
    .sort((a, b) => a.start - b.start || b.length - a.length);

  const placed: Placed[] = [];
  let cluster: Placed[] = [];
  let laneEnds: number[] = [];
  let clusterEnd = -1;
  const closeCluster = () => {
    for (const block of cluster) block.lanes = laneEnds.length;
    cluster = [];
    laneEnds = [];
  };
  for (const block of blocks) {
    if (block.start >= clusterEnd) closeCluster();
    let lane = laneEnds.findIndex((end) => end <= block.start);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(0);
    }
    laneEnds[lane] = block.start + block.length;
    block.lane = lane;
    cluster.push(block);
    placed.push(block);
    clusterEnd = Math.max(clusterEnd, block.start + block.length);
  }
  closeCluster();
  return placed;
}

export type Schedule = Pick<Draft, "dueDate" | "dueTime" | "startsAt" | "endsAt">;

/**
 * What the form starts with when a day or a time is chosen on the calendar: a task due then
 * (at the end of the working day for a whole day) or a meeting starting then (at 9:00 for a
 * whole day) and lasting 30 minutes.
 */
export function scheduleFor(date: string, minutes: number | null = null): Schedule {
  if (minutes === null) {
    const startsAt = `${date}T${MONTH_CELL_START}`;
    return { dueDate: date, dueTime: DEFAULT_DUE_TIME, startsAt, endsAt: suggestedEnd(startsAt) };
  }
  const time = `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}`;
  const startsAt = `${date}T${time}`;
  return { dueDate: date, dueTime: time, startsAt, endsAt: suggestedEnd(startsAt) };
}
