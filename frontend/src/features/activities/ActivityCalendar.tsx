"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, CircleAlert, CircleCheck, ListTodo, Plus, Users } from "lucide-react";
import Link from "next/link";
import { type MouseEvent, useEffect, useMemo, useRef } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { Spinner } from "@/components/ui/Spinner";
import { describeError, isApiError } from "@/lib/api/errors";
import type { ActivityListItem } from "@/lib/api/types";
import { businessToday } from "@/lib/format";
import { useViewer } from "@/lib/viewer-context";
import { activityHref, type Workspace, workspaceApiSegment } from "@/lib/workspace";

import { activitiesApi, activityKeys, CALENDAR_MAX_PAGES, CALENDAR_PAGE_SIZE, type CalendarRange } from "./api";
import { typeLabel } from "./ActivityBits";
import {
  businessMinutesOf,
  byDay,
  type CalendarView,
  clockLabel,
  dayLabel,
  hourLabel,
  isSameMonth,
  layoutDay,
  MIN_BLOCK_MINUTES,
  monthWeeks,
  periodTitle,
  type Schedule,
  scheduledAt,
  scheduleFor,
  shiftAnchor,
  visibleDays,
  weekdayName,
} from "./calendar";
import { useClock } from "./clock";
import { useCalendarState } from "./list-state";

/** Entries a month cell lists before "+N more". */
const MAX_IN_CELL = 3;
/** One hour of the week and day views: h-12 (3rem = 48 px). */
const HOUR_PX = 48;
/** The hour the week and day views open scrolled to (its label just below the day headers). */
const FIRST_VISIBLE_HOUR = 8;
const LABEL_ROOM_PX = 12;
/** Shorter blocks have room for one line: the time and subject, as in a month cell. */
const TWO_LINE_MINUTES = 45;
/** Blocks this long have room for a third line: the owner's name. */
const THREE_LINE_MINUTES = 70;
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const VIEWS: { value: CalendarView; label: string }[] = [
  { value: "month", label: "Month" },
  { value: "week", label: "Week" },
  { value: "day", label: "Day" },
];

type EntryState = "completed" | "overdue" | "awaiting" | "current";

function entryState(item: ActivityListItem): EntryState {
  if (item.status === "completed") return "completed";
  if (item.is_overdue) return item.type === "meeting" ? "awaiting" : "overdue";
  return "current";
}

/** Colour reinforces what the words (read out) and the icon already say. */
const TONES: Record<EntryState | "meeting" | "task", string> = {
  meeting: "border-sky-200 bg-sky-50 text-sky-900 hover:bg-sky-100",
  task: "border-amber-200 bg-amber-50 text-amber-900 hover:bg-amber-100",
  overdue: "border-red-200 bg-red-50 text-red-800 hover:bg-red-100",
  awaiting: "border-orange-200 bg-orange-50 text-orange-900 hover:bg-orange-100",
  completed: "border-slate-200 bg-slate-50 text-slate-500 line-through hover:bg-slate-100",
  current: "",
};
const STATE_WORDS: Record<EntryState, string> = { completed: "completed", overdue: "overdue", awaiting: "awaiting outcome", current: "" };

function toneOf(item: ActivityListItem): string {
  const state = entryState(item);
  return state === "current" ? TONES[item.type === "meeting" ? "meeting" : "task"] : TONES[state];
}

function EntryIcon({ item }: { item: ActivityListItem }) {
  const state = entryState(item);
  const Icon = state === "completed" ? CircleCheck : state === "current" ? (item.type === "meeting" ? Users : ListTodo) : CircleAlert;
  return <Icon aria-hidden="true" className="mr-1 inline size-3 shrink-0 align-[-2px]" />;
}

function timeText(item: ActivityListItem): string {
  const start = businessMinutesOf(scheduledAt(item));
  if (item.type === "meeting" && item.ends_at) {
    const end = businessMinutesOf(item.ends_at);
    return `${clockLabel(start)} – ${clockLabel(end)}`;
  }
  return `Due ${clockLabel(start)}`;
}

/** What assistive technology reads before the time and subject: type, state, owner. */
function entryWords(item: ActivityListItem, showOwner: boolean): string {
  const state = STATE_WORDS[entryState(item)];
  return [typeLabel(item.type), state, showOwner ? `owner ${item.owner.full_name}` : ""].filter(Boolean).join(", ");
}

function entryTitle(item: ActivityListItem, showOwner: boolean): string {
  return `${timeText(item)} · ${item.title}${showOwner ? ` · ${item.owner.full_name}` : ""}`;
}

/** A month cell's entry (and a short one in the week and day views): the time and subject
 * on one line, opening the activity. */
function EntryChip({ item, workspace, showOwner, className = "" }: { item: ActivityListItem; workspace: Workspace; showOwner: boolean; className?: string }) {
  const at = scheduledAt(item);
  return (
    <Link
      href={activityHref(workspace, item.id)}
      title={entryTitle(item, showOwner)}
      className={`block truncate rounded border px-1.5 py-0.5 text-xs ${toneOf(item)} ${className}`}
    >
      {/* Separating spaces sit outside the hidden text: trailing ones inside are dropped. */}
      <span className="sr-only">{entryWords(item, showOwner)}:</span> <EntryIcon item={item} />
      <time dateTime={at ?? undefined}>{clockLabel(businessMinutesOf(at))}</time> <span className="font-medium">{item.title}</span>
    </Link>
  );
}

/** A week or day view's block: the subject, then the time (and owner) as space allows; the
 * lines keep their height and the block cuts off what doesn't fit. */
function EntryBlock({
  item,
  workspace,
  showOwner,
  ownerLine,
}: {
  item: ActivityListItem;
  workspace: Workspace;
  showOwner: boolean;
  ownerLine: boolean;
}) {
  return (
    <Link
      href={activityHref(workspace, item.id)}
      title={entryTitle(item, showOwner)}
      className={`flex h-full flex-col overflow-hidden rounded border px-1.5 py-0.5 text-xs leading-4 ${toneOf(item)}`}
    >
      <span className="sr-only">{entryWords(item, showOwner)}:</span>{" "}
      <span className="shrink-0 truncate font-medium">
        <EntryIcon item={item} />
        {item.title}
        <span className="sr-only">,</span>
      </span>{" "}
      <time dateTime={scheduledAt(item) ?? undefined} className="shrink-0 truncate">
        {timeText(item)}
      </time>
      {/* Already read out with the type ("owner …") and in the tooltip. */}
      {showOwner && ownerLine ? (
        <span aria-hidden="true" className="shrink-0 truncate opacity-80">
          {item.owner.full_name}
        </span>
      ) : null}
    </Link>
  );
}

function Segmented<T extends string | boolean>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-md border border-slate-300 bg-white p-0.5 text-sm">
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className={`rounded px-3 py-1 ${value === option.value ? "bg-slate-900 font-medium text-white" : "text-slate-600 hover:bg-slate-50"}`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** Whether a click landed on the cell or column itself, not on an entry or a button in it. */
function onBackground(event: MouseEvent<HTMLElement>): boolean {
  return !(event.target as HTMLElement).closest("a, button");
}

/** Phones have no room for entries in a month cell: tapping a day opens it instead. */
function wideScreen(): boolean {
  return typeof window.matchMedia !== "function" || window.matchMedia("(min-width: 40rem)").matches;
}

function entryCount(count: number): string {
  return count === 1 ? "1 activity" : `${count} activities`;
}

interface GridProps {
  workspace: Workspace;
  today: string;
  entries: Map<string, ActivityListItem[]>;
  canWrite: boolean;
  showOwner: boolean;
  onSchedule: (schedule: Schedule) => void;
  onOpenDay: (day: string) => void;
}

function MonthGrid({ anchor, ...grid }: GridProps & { anchor: string }) {
  const weeks = monthWeeks(anchor);
  return (
    // Positioned, so screen-reader-only text inside can't widen the page (Phase 7 walkthrough).
    <div className="relative overflow-x-auto">
      <table className="w-full table-fixed border-collapse text-sm">
        <caption className="sr-only">{periodTitle("month", anchor)}</caption>
        <thead>
          <tr>
            {weeks[0]!.map((day) => (
              <th key={day} scope="col" className="border-b border-slate-200 px-2 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                <abbr title={weekdayName(day)} className="no-underline">
                  {weekdayName(day).slice(0, 3)}
                </abbr>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {weeks.map((week) => (
            <tr key={week[0]}>
              {week.map((day) => (
                <MonthCell key={day} day={day} inMonth={isSameMonth(day, anchor)} items={grid.entries.get(day) ?? []} {...grid} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MonthCell({
  day,
  inMonth,
  items,
  workspace,
  today,
  canWrite,
  showOwner,
  onSchedule,
  onOpenDay,
}: GridProps & { day: string; inMonth: boolean; items: ActivityListItem[] }) {
  const label = dayLabel(day);
  const isToday = day === today;
  const shown = items.length > MAX_IN_CELL ? items.slice(0, MAX_IN_CELL - 1) : items;
  const more = items.length - shown.length;
  return (
    <td
      onClick={(event) => {
        if (!onBackground(event)) return;
        if (!wideScreen()) onOpenDay(day);
        else if (canWrite) onSchedule(scheduleFor(day));
      }}
      className={`group relative h-20 border-b border-r border-slate-100 p-1 align-top last:border-r-0 sm:h-28 ${inMonth ? "bg-white" : "bg-slate-50"} ${
        canWrite ? "sm:cursor-pointer" : ""
      }`}
    >
      <div className="flex items-center justify-between gap-1">
        <button
          type="button"
          onClick={() => onOpenDay(day)}
          aria-label={`${label}${items.length ? `, ${entryCount(items.length)}` : ""}. Open the day`}
          aria-current={isToday ? "date" : undefined}
          className={`inline-flex size-7 shrink-0 items-center justify-center rounded-full text-xs tabular-nums ${
            isToday ? "bg-brand-600 font-semibold text-white" : inMonth ? "text-slate-800 hover:bg-slate-100" : "text-slate-400 hover:bg-slate-100"
          }`}
        >
          {Number(day.slice(8))}
        </button>
        {canWrite ? (
          <button
            type="button"
            onClick={() => onSchedule(scheduleFor(day))}
            aria-label={`Schedule on ${label}`}
            className="rounded p-1 text-slate-500 opacity-0 hover:bg-slate-100 hover:text-slate-800 focus-visible:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
          >
            <Plus aria-hidden="true" className="size-3.5" />
          </button>
        ) : null}
      </div>
      {items.length ? (
        // Phones: a dot per entry (up to three); the day's button says how many.
        <span aria-hidden="true" className="mt-1 flex gap-0.5 px-1 sm:hidden">
          {items.slice(0, MAX_IN_CELL).map((item) => (
            <span key={item.id} className={`size-1.5 rounded-full ${item.type === "meeting" ? "bg-sky-500" : "bg-amber-500"}`} />
          ))}
        </span>
      ) : null}
      <ul className="mt-0.5 hidden space-y-0.5 sm:block">
        {shown.map((item) => (
          <li key={item.id}>
            <EntryChip item={item} workspace={workspace} showOwner={showOwner} />
          </li>
        ))}
        {more > 0 ? (
          <li>
            <button
              type="button"
              onClick={() => onOpenDay(day)}
              aria-label={`${more} more on ${label}`}
              className="rounded px-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900"
            >
              +{more} more
            </button>
          </li>
        ) : null}
      </ul>
    </td>
  );
}

function TimeGrid({ days, clock, ...grid }: GridProps & { days: string[]; clock: number }) {
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scroller.current) scroller.current.scrollTop = FIRST_VISIBLE_HOUR * HOUR_PX - LABEL_ROOM_PX;
  }, []);
  const single = days.length === 1;
  const columns = single ? "grid-cols-[3.5rem_minmax(0,1fr)]" : "grid-cols-[3.5rem_repeat(7,minmax(0,1fr))]";
  return (
    <div ref={scroller} className="relative max-h-[36rem] overflow-auto">
      <div className={single ? "" : "min-w-[42rem]"}>
        <div className={`sticky top-0 z-10 grid border-b border-slate-200 bg-white ${columns}`}>
          <div />
          {days.map((day) => (
            <div key={day} className="flex items-center justify-between gap-1 border-l border-slate-100 px-2 py-1.5">
              <button
                type="button"
                onClick={() => grid.onOpenDay(day)}
                disabled={single}
                aria-label={single ? dayLabel(day) : `${dayLabel(day)}. Open the day`}
                aria-current={day === grid.today ? "date" : undefined}
                className="flex items-baseline gap-1.5 rounded px-1 text-left enabled:hover:bg-slate-100"
              >
                <span className="text-xs font-medium uppercase tracking-wide text-slate-500">{weekdayName(day).slice(0, 3)}</span>
                <span
                  className={`inline-flex size-7 items-center justify-center rounded-full text-sm tabular-nums ${
                    day === grid.today ? "bg-brand-600 font-semibold text-white" : "text-slate-900"
                  }`}
                >
                  {Number(day.slice(8))}
                </span>
              </button>
              {grid.canWrite ? (
                <button
                  type="button"
                  onClick={() => grid.onSchedule(scheduleFor(day))}
                  aria-label={`Schedule on ${dayLabel(day)}`}
                  className="rounded p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-800"
                >
                  <Plus aria-hidden="true" className="size-3.5" />
                </button>
              ) : null}
            </div>
          ))}
        </div>
        <div className={`grid ${columns}`}>
          <div aria-hidden="true">
            {HOURS.map((hour) => (
              <div key={hour} className="h-12 pr-2 text-right text-[11px] leading-none text-slate-500">
                <span className={hour === 0 ? "invisible" : "relative -top-1.5"}>{hourLabel(hour)}</span>
              </div>
            ))}
          </div>
          {days.map((day) => (
            <DayColumn key={day} day={day} items={grid.entries.get(day) ?? []} clock={clock} {...grid} />
          ))}
        </div>
      </div>
    </div>
  );
}

function DayColumn({
  day,
  items,
  clock,
  workspace,
  today,
  canWrite,
  showOwner,
  onSchedule,
}: GridProps & { day: string; items: ActivityListItem[]; clock: number }) {
  const placed = useMemo(() => layoutDay(items), [items]);
  const isToday = day === today;
  return (
    <section
      aria-label={`${dayLabel(day)}${items.length ? `, ${entryCount(items.length)}` : ""}`}
      onClick={(event) => {
        if (!canWrite || !onBackground(event)) return;
        // The half hour clicked (the column is the whole day, 48 px an hour).
        const offset = event.clientY - event.currentTarget.getBoundingClientRect().top;
        const minutes = Math.floor((offset / HOUR_PX) * 2) * 30;
        onSchedule(scheduleFor(day, Math.min(Math.max(minutes, 0), 24 * 60 - MIN_BLOCK_MINUTES)));
      }}
      className={`relative border-l border-slate-100 ${isToday ? "bg-brand-50/40" : ""} ${canWrite ? "cursor-pointer" : ""}`}
    >
      {HOURS.map((hour) => (
        <div key={hour} aria-hidden="true" className="h-12 border-t border-slate-100" />
      ))}
      {isToday ? (
        // Positions are set on the client only (React sets them through the DOM; the CSP
        // refuses style attributes in server-rendered HTML).
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 z-[1] h-0.5 bg-red-500"
          style={{ top: (businessMinutesOf(new Date(clock).toISOString()) / 60) * HOUR_PX }}
        />
      ) : null}
      <ol className="pointer-events-none absolute inset-0">
        {placed.map((block) => (
          <li
            key={block.item.id}
            className="pointer-events-auto absolute px-0.5 py-px"
            style={{
              top: (block.start / 60) * HOUR_PX,
              height: (block.length / 60) * HOUR_PX,
              left: `${(block.lane / block.lanes) * 100}%`,
              width: `${100 / block.lanes}%`,
            }}
          >
            {block.length < TWO_LINE_MINUTES ? (
              <EntryChip item={block.item} workspace={workspace} showOwner={showOwner} className="h-full" />
            ) : (
              <EntryBlock item={block.item} workspace={workspace} showOwner={showOwner} ownerLine={block.length >= THREE_LINE_MINUTES} />
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * The Activities calendar: tasks (at their due time) and meetings (start to end) by month,
 * week or day, in India time like every time on screen. Cancelled and archived ones are left
 * out; completed ones stay, struck through. Organisation-wide, "My calendar" narrows it to
 * the viewer's own. Choosing a day (or, in the week and day views, a time) opens the form
 * for a meeting or a task then; an entry opens its activity page.
 *
 * It reads the activities list API (the same scope, filters and authorisation as the list),
 * at most CALENDAR_MAX_PAGES pages per type; beyond that it says the range is cut short.
 */
export function ActivityCalendar({ workspace, canWrite, onSchedule }: { workspace: Workspace; canWrite: boolean; onSchedule: (schedule: Schedule) => void }) {
  const viewer = useViewer();
  const segment = workspaceApiSegment(workspace);
  const calendar = useCalendarState(segment);
  const clock = useClock();
  const today = businessToday(new Date(clock));
  const organisation = workspace.kind === "organization";
  const days = visibleDays(calendar.view, calendar.anchor);
  const range: CalendarRange = {
    from: days[0]!,
    to: days[days.length - 1]!,
    owner: organisation && calendar.mine ? (viewer?.id ?? "") : "",
  };
  const query = useQuery({
    queryKey: activityKeys.calendar(workspace, range),
    queryFn: () => activitiesApi.calendar(workspace, range),
    // While another period loads, keep the entries shown, but only this workspace's and the
    // same people's (never everyone's under "My calendar").
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[2] === segment && (previousQuery.queryKey[3] as CalendarRange).owner === range.owner ? previous : undefined,
  });
  const entries = useMemo(() => byDay(query.data?.items ?? []), [query.data]);

  if (isApiError(query.error, 404)) return <NotFoundView />;

  const error = query.isError ? describeError(query.error) : null;
  const openDay = (day: string) => calendar.update({ view: "day", anchor: day });
  const grid: GridProps = { workspace, today, entries, canWrite, showOwner: organisation && !calendar.mine, onSchedule, onOpenDay: openDay };
  const unit = calendar.view;

  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 p-3">
        <Button variant="secondary" size="sm" onClick={() => calendar.update({ anchor: today })}>
          Today
        </Button>
        <div className="flex items-center">
          <button
            type="button"
            aria-label={`Previous ${unit}`}
            onClick={() => calendar.update({ anchor: shiftAnchor(calendar.view, calendar.anchor, -1) })}
            className="rounded-full p-1.5 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          >
            <ChevronLeft aria-hidden="true" className="size-4" />
          </button>
          <button
            type="button"
            aria-label={`Next ${unit}`}
            onClick={() => calendar.update({ anchor: shiftAnchor(calendar.view, calendar.anchor, 1) })}
            className="rounded-full p-1.5 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          >
            <ChevronRight aria-hidden="true" className="size-4" />
          </button>
        </div>
        <h2 aria-live="polite" className="text-base font-semibold text-slate-900">
          {periodTitle(calendar.view, calendar.anchor)}
        </h2>
        {query.isFetching ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-slate-500">
            <Spinner className="size-3.5" />
            <span className="sr-only">Loading activities</span>
          </span>
        ) : null}
        <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
          {organisation && viewer ? (
            <Segmented
              label="Whose activities"
              value={calendar.mine}
              onChange={(mine) => calendar.update({ mine })}
              options={[
                { value: true, label: "My calendar" },
                { value: false, label: "Everyone" },
              ]}
            />
          ) : null}
          <Segmented label="Calendar view" value={calendar.view} onChange={(view) => calendar.update({ view })} options={VIEWS} />
        </div>
      </div>

      {error || query.data?.truncated ? (
        <div className="space-y-2 border-b border-slate-200 p-3">
          {error ? (
            <Alert
              tone="error"
              title={isApiError(query.error, 403) ? "You can't view these activities" : "The calendar couldn't be loaded"}
              requestId={error.requestId}
              action={
                isApiError(query.error, 403) ? null : (
                  <Button variant="secondary" size="sm" onClick={() => void query.refetch()} loading={query.isFetching}>
                    Try again
                  </Button>
                )
              }
            >
              {error.message}
            </Alert>
          ) : null}
          {query.data?.truncated ? (
            <Alert tone="info" title="Not everything fits">
              {`This ${unit} has more than ${CALENDAR_MAX_PAGES * CALENDAR_PAGE_SIZE} tasks or meetings; only the first ${CALENDAR_MAX_PAGES * CALENDAR_PAGE_SIZE} of each are shown. `}
              {organisation && !calendar.mine ? "Choose My calendar, or a week or a day, to see them all." : "Choose a week or a day to see them all."}
            </Alert>
          ) : null}
        </div>
      ) : null}

      <div aria-busy={query.isPending || undefined}>
        {calendar.view === "month" ? (
          <MonthGrid anchor={calendar.anchor} {...grid} />
        ) : (
          <TimeGrid key={calendar.view} days={days} clock={clock} {...grid} />
        )}
      </div>
    </div>
  );
}
