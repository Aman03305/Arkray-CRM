"use client";

import { CalendarClock, IndianRupee, ListTodo, type LucideIcon, Scale } from "lucide-react";
import Link from "next/link";
import { Fragment, type ReactNode, useEffect, useId, useRef } from "react";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PageHeader } from "@/components/ui/PageHeader";
import { PersonName } from "@/components/ui/PersonName";
import { Skeleton } from "@/components/ui/Skeleton";
import { CustomerName, When } from "@/features/activities/ActivityBits";
import { summaryFilters } from "@/features/activities/api";
import { presetActivityList } from "@/features/activities/list-state";
import { presetBoard } from "@/features/pipeline/hooks";
import { describeError, isApiError } from "@/lib/api/errors";
import type { Dashboard, DashboardActivity } from "@/lib/api/types";
import { businessToday, formatDateOnly } from "@/lib/format";
import { formatInr } from "@/lib/money";
import { activityHref, type Workspace, workspaceApiSegment, workspaceHref } from "@/lib/workspace";

import { useDashboard } from "./api";

/*
 * The dashboard of one workspace: a salesperson's own (/dashboard), one user's opened by an
 * administrator (/admin/users/{id}/dashboard) and, inside Admin Home, the organisation's.
 * Pipeline and activity figures only: there are no leads in the UI (ADR-0027; the API still
 * sends its lead figures, which are not shown).
 * Every figure comes from the server as it is shown (counts, and money as exact decimal
 * strings): nothing is computed here, and amounts never pass through a JavaScript number.
 */

const COUNT = new Intl.NumberFormat("en-IN");
const count = (n: number) => COUNT.format(n);
const counted = (n: number, one: string, many: string) => `${count(n)} ${n === 1 ? one : many}`;

/** A grouped number ("₹12,34,56,789.50") that may wrap only after a group's comma, never
 * inside a group (a break mid-group reads as a different amount). */
function Grouped({ text }: { text: string }) {
  const groups = text.split(",");
  return (
    <>
      {groups.map((group, i) => (
        <Fragment key={i}>
          {group}
          {i < groups.length - 1 ? (
            <>
              ,<wbr />
            </>
          ) : null}
        </Fragment>
      ))}
    </>
  );
}

/** Where a card or list footer leads: a page of this workspace, opened on the list that
 * shows exactly what the figure counts. The filters are preset in memory (never put in the
 * URL) when the link navigates in this tab; a link opened in a new tab shows the module's
 * own default list and leaves this tab's remembered filters alone. */
interface Target {
  href: string;
  preset?: () => void;
}

function targets(workspace: Workspace, today: string) {
  const segment = workspaceApiSegment(workspace);
  const activities = workspaceHref(workspace, "activities");
  const opens = summaryFilters(today);
  return {
    pipeline: { href: workspaceHref(workspace, "pipeline"), preset: () => presetBoard(segment) },
    meetingsToday: { href: activities, preset: () => presetActivityList(segment, opens.meetings_today) },
    upcomingMeetings: { href: activities, preset: () => presetActivityList(segment, opens.upcoming_meetings) },
    openTasks: { href: activities, preset: () => presetActivityList(segment, opens.open_tasks) },
  } satisfies Record<string, Target>;
}

type Targets = ReturnType<typeof targets>;

function TargetLink({ to, className, children }: { to: Target; className: string; children: ReactNode }) {
  // onNavigate, not onClick: it runs only for a navigation in this tab, so a Ctrl/Cmd-,
  // Shift- or middle-click (a new tab) doesn't overwrite this tab's remembered list (review).
  return (
    <Link href={to.href} onNavigate={to.preset} className={className}>
      {children}
    </Link>
  );
}

// --- figures ---------------------------------------------------------------------------------
function Figure({
  label,
  icon: Icon,
  value,
  unit,
  detail,
  to,
}: {
  label: string;
  icon: LucideIcon;
  value: string;
  unit?: string;
  detail: ReactNode;
  to: Target;
}) {
  return (
    <li className="min-w-0">
      <TargetLink
        to={to}
        className="group flex h-full flex-col rounded-lg border border-slate-200 bg-white p-4 shadow-sm transition-colors hover:border-brand-300 hover:bg-brand-50/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
      >
        {/* Block elements, so the link's accessible name reads "Tasks 9 open 2 due today ·
            1 overdue", not the words run together. */}
        <div className="flex items-center justify-between gap-2 text-sm font-medium text-slate-600">
          {label}
          <Icon aria-hidden="true" className="size-4 shrink-0 text-slate-500 group-hover:text-brand-600" />
        </div>
        <div className="mt-2 text-xl font-semibold tracking-tight text-slate-900 tabular-nums [overflow-wrap:anywhere] sm:text-2xl">
          <Grouped text={value} />
          {unit ? (
            <>
              {" "}
              <span className="text-sm font-normal tracking-normal text-slate-500">{unit}</span>
            </>
          ) : null}
        </div>
        {detail ? <div className="mt-1 text-sm text-slate-500">{detail}</div> : null}
      </TargetLink>
    </li>
  );
}

function Figures({ dashboard, go, updating }: { dashboard: Dashboard; go: Targets; updating: boolean }) {
  const { pipeline, activities } = dashboard;
  const heading = useId();
  return (
    <section aria-labelledby={heading} aria-busy={updating || undefined}>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id={heading} className="sr-only">
          Key figures
        </h2>
        <p className="text-xs text-slate-500">
          {updating ? <span className="mr-2 font-medium text-slate-700">Updating…</span> : null}
          <time dateTime={dashboard.business_date}>{formatDateOnly(dashboard.business_date)}</time>
        </p>
      </div>
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Figure
          label="Pipeline value"
          icon={IndianRupee}
          value={formatInr(pipeline.pipeline_value)}
          detail={`${counted(pipeline.open_count, "open opportunity", "open opportunities")} · all pipelines`}
          to={go.pipeline}
        />
        <Figure
          label="Weighted pipeline"
          icon={Scale}
          value={formatInr(pipeline.weighted_pipeline)}
          detail="All pipelines"
          to={go.pipeline}
        />
        <Figure
          label="Meetings"
          icon={CalendarClock}
          value={count(activities.meetings_today)}
          unit="today"
          detail={`${count(activities.upcoming_meetings)} upcoming`}
          to={go.meetingsToday}
        />
        <Figure
          label="Tasks"
          icon={ListTodo}
          value={count(activities.open_tasks)}
          unit="open"
          detail={
            <>
              {count(activities.tasks_due_today)} due today ·{" "}
              <span className={activities.overdue_tasks > 0 ? "font-medium text-red-700" : undefined}>
                {count(activities.overdue_tasks)} overdue
              </span>
            </>
          }
          to={go.openTasks}
        />
      </ul>
    </section>
  );
}

// --- supporting lists ------------------------------------------------------------------------
function Panel({ title, footer, children }: { title: string; footer: ReactNode; children: ReactNode }) {
  const heading = useId();
  return (
    <section aria-labelledby={heading} className="flex min-w-0 flex-col rounded-lg border border-slate-200 bg-white">
      <h2 id={heading} className="border-b border-slate-100 px-4 py-3 text-sm font-semibold text-slate-900">
        {title}
      </h2>
      <div className="flex-1 px-4">{children}</div>
      <div className="border-t border-slate-100 px-4 py-2.5 text-sm">{footer}</div>
    </section>
  );
}

// Underlined: a link inside a sentence must not rely on colour alone (WCAG 1.4.1; audit).
const FOOTER_LINK = "font-medium text-brand-700 underline underline-offset-2 hover:text-brand-800";
const NONE = "py-6 text-center text-sm text-slate-500";
// Names wrap rather than being cut off: the lists are short, and a truncated row could hide
// whose work it is, or that they are deactivated (review).
const WRAP = "[overflow-wrap:anywhere]";

function ActivityRows({
  rows,
  workspace,
  kind,
}: {
  rows: readonly DashboardActivity[];
  workspace: Workspace;
  kind: "meeting" | "task";
}) {
  const withOwner = workspace.kind === "organization";
  return (
    <ul className="divide-y divide-slate-100">
      {rows.map((activity) => (
        <li key={activity.id} className="py-3">
          <div className="flex items-start justify-between gap-3">
            <Link
              href={activityHref(workspace, activity.id)}
              className={`min-w-0 text-sm font-medium text-slate-900 hover:text-brand-700 hover:underline ${WRAP}`}
            >
              {activity.title}
            </Link>
            {activity.is_overdue ? <Badge tone="red">Overdue</Badge> : null}
          </div>
          <p className={`mt-0.5 text-xs text-slate-500 ${WRAP}`}>
            {kind === "meeting" ? (
              <When iso={activity.starts_at} />
            ) : (
              <>
                {activity.due_at ? "Due " : null}
                <When iso={activity.due_at} empty="No due date" />
              </>
            )}
            {" · "}
            <CustomerName lead={activity.lead} />
            {withOwner ? (
              <>
                {" · "}
                <PersonName person={activity.owner} />
              </>
            ) : null}
          </p>
        </li>
      ))}
    </ul>
  );
}

function UpcomingMeetings({ dashboard, workspace, go }: { dashboard: Dashboard; workspace: Workspace; go: Targets }) {
  const total = dashboard.activities.upcoming_meetings;
  return (
    <Panel
      title="Upcoming meetings"
      footer={
        <TargetLink to={go.upcomingMeetings} className={FOOTER_LINK}>
          {total > dashboard.upcoming_meetings.length
            ? `View all ${counted(total, "upcoming meeting", "upcoming meetings")}`
            : "View upcoming meetings"}
        </TargetLink>
      }
    >
      {dashboard.upcoming_meetings.length === 0 ? (
        <p className={NONE}>No upcoming meetings.</p>
      ) : (
        <ActivityRows rows={dashboard.upcoming_meetings} workspace={workspace} kind="meeting" />
      )}
    </Panel>
  );
}

function TasksNeedingAttention({ dashboard, workspace, go }: { dashboard: Dashboard; workspace: Workspace; go: Targets }) {
  const total = dashboard.activities.open_tasks;
  return (
    <Panel
      title="Tasks requiring attention"
      footer={
        <TargetLink to={go.openTasks} className={FOOTER_LINK}>
          {total > dashboard.next_tasks.length ? `View all ${counted(total, "open task", "open tasks")}` : "View open tasks"}
        </TargetLink>
      }
    >
      {dashboard.next_tasks.length === 0 ? (
        <p className={NONE}>No open tasks.</p>
      ) : (
        <>
          <ActivityRows rows={dashboard.next_tasks} workspace={workspace} kind="task" />
        </>
      )}
    </Panel>
  );
}

// --- states ----------------------------------------------------------------------------------
function DashboardSkeleton() {
  return (
    <div aria-busy="true" aria-hidden="true">
      <Skeleton className="mb-3 h-4 w-28" />
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <li key={i} className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
            <Skeleton className="h-4 w-24" />
            <div>
              <Skeleton className="h-7 w-28" />
            </div>
            <div>
              <Skeleton className="h-4 w-36" />
            </div>
          </li>
        ))}
      </ul>
      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        {Array.from({ length: 2 }, (_, i) => (
          <div key={i} className="space-y-4 rounded-lg border border-slate-200 bg-white p-4">
            <Skeleton className="h-4 w-32" />
            {Array.from({ length: 3 }, (_, j) => (
              <div key={j}>
                <Skeleton className="h-4 w-full" />
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Refresh once when the business day changes while the page stays open, so "today" and the
 * lists it opens don't stay on yesterday after midnight (India time). */
function useNewBusinessDay(businessDate: string | undefined, refresh: () => void) {
  const latest = useRef(refresh);
  const tried = useRef<string | null>(null);
  useEffect(() => {
    latest.current = refresh;
  });
  useEffect(() => {
    if (!businessDate) return;
    const timer = window.setInterval(() => {
      const today = businessToday();
      if (today > businessDate && tried.current !== today) {
        tried.current = today;
        latest.current();
      }
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [businessDate]);
}

/**
 * A dashboard page: its header, then the figures and lists of one workspace, a skeleton
 * while they load, or what went wrong. Nothing but this workspace's current answer is ever
 * shown: no zeros while loading, and an error (a failed refresh included) replaces the
 * figures rather than leaving old ones up. A workspace the caller may not open is the
 * standard not-found page alone.
 */
export function DashboardContent({ workspace, header }: { workspace: Workspace; header: ReactNode }) {
  const dashboard = useDashboard(workspace);
  useNewBusinessDay(dashboard.data?.business_date, () => void dashboard.refetch());

  if (isApiError(dashboard.error, 404)) return <NotFoundView />;

  const updating = dashboard.isFetching && !dashboard.isPending && !dashboard.isError;
  // One live region that stays in place (a region inserted with its text already in it is
  // often not announced).
  const status = dashboard.isPending && !dashboard.isError ? "Loading the dashboard" : updating ? "Updating the dashboard" : "";
  let body: ReactNode;
  if (dashboard.isError) {
    const denied = isApiError(dashboard.error, 403);
    const { message, requestId } = describeError(dashboard.error);
    body = (
      <Alert
        tone="error"
        title={denied ? "You can't view this dashboard" : "The dashboard couldn't be loaded"}
        requestId={requestId}
        action={
          denied ? null : (
            <Button variant="secondary" size="sm" onClick={() => void dashboard.refetch()} loading={dashboard.isFetching}>
              Try again
            </Button>
          )
        }
      >
        {message}
      </Alert>
    );
  } else if (dashboard.isPending) {
    body = <DashboardSkeleton />;
  } else {
    const data = dashboard.data;
    const go = targets(workspace, data.business_date);
    body = (
      <div className="space-y-6">
        <Figures dashboard={data} go={go} updating={updating} />
        <div className="grid gap-4 lg:grid-cols-2">
          <UpcomingMeetings dashboard={data} workspace={workspace} go={go} />
          <TasksNeedingAttention dashboard={data} workspace={workspace} go={go} />
        </div>
      </div>
    );
  }
  return (
    <>
      {header}
      <p role="status" className="sr-only">
        {status}
      </p>
      {body}
    </>
  );
}

/** A person's dashboard: their own, or one user's opened by an administrator. */
export function WorkspaceDashboard({ workspace }: { workspace: Workspace }) {
  return <DashboardContent workspace={workspace} header={<PageHeader title="Dashboard" />} />;
}
