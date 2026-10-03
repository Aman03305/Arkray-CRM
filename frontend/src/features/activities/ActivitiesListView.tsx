"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarCheck, Plus, SlidersHorizontal } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { PageHeader } from "@/components/ui/PageHeader";
import { cursorOf } from "@/features/leads/api";
import { OwnerSelect } from "@/features/leads/OwnerSelect";
import { LeadPicker } from "@/features/pipeline/LeadPicker";
import { describeError, isApiError } from "@/lib/api/errors";
import type { ActivityListItem, ActivityOrdering } from "@/lib/api/types";
import { useFlash } from "@/lib/flash";
import { businessToday } from "@/lib/format";
import { useViewer } from "@/lib/viewer-context";
import { describeWorkspace, type Workspace, workspaceApiSegment } from "@/lib/workspace";

import {
  activeFilterCount,
  activitiesApi,
  type ActivityFilters,
  activityKeys,
  type ActivityTab,
  type LifecycleAction,
  NO_FILTERS,
  ORDERING_OPTIONS,
  STATUS_OPTIONS,
  TAB_DEFAULTS,
} from "./api";
import { ActivityFormDialog } from "./ActivityFormDialog";
import { ActivityTable } from "./ActivityTable";
import type { FormKind } from "./draft";
import { activityPermissions, useActivityAction } from "./hooks";
import { useActivityListState } from "./list-state";

const CONTROL = "h-9 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:outline-brand-600";
const TABS: { value: ActivityTab; label: string }[] = [
  { value: "all", label: "All" },
  { value: "task", label: "Tasks" },
  { value: "meeting", label: "Meetings" },
  { value: "note", label: "Notes" },
];
const CONFIRMED: ReadonlySet<LifecycleAction> = new Set(["cancel", "archive"]);

type Pending = { action: LifecycleAction; activity: ActivityListItem } | null;

function done(action: LifecycleAction, activity: ActivityListItem): string {
  const name = activity.type === "note" ? "The note" : activity.title;
  return { complete: `${name} completed.`, cancel: `${name} cancelled.`, reopen: `${name} reopened.`, archive: `${name} archived.`, restore: `${name} restored.` }[action];
}

/**
 * The Activities page, rendered unchanged in every workspace: a salesperson's own
 * (/activities), the organisation for administrators (/activities), and one user's
 * workspace opened by an administrator (/admin/users/{id}/activities). Only the API path
 * differs.
 */
export function ActivitiesListView({ workspace }: { workspace: Workspace }) {
  const viewer = useViewer();
  const segment = workspaceApiSegment(workspace);
  const list = useActivityListState(segment);
  const permissions = activityPermissions(viewer, workspace);
  const [form, setForm] = useState<FormKind | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [notice, setNotice] = useFlash();
  const [problem, setProblem] = useState<{ message: string; requestId: string | null } | null>(null);
  const noticeRegion = useRef<HTMLDivElement>(null);
  const focusNotice = useRef(false);
  useEffect(() => {
    if (focusNotice.current && (notice || problem)) {
      focusNotice.current = false;
      noticeRegion.current?.focus();
    }
  }, [notice, problem]);

  const activities = useQuery({
    queryKey: activityKeys.list(workspace, list.applied, list.cursor),
    queryFn: () => activitiesApi.list(workspace, list.applied, list.cursor),
    // While a new page or filter loads, keep showing the previous rows, but only from this
    // same workspace: never another user's activities under this user's banner.
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[2] === segment ? previous : undefined),
  });
  const summary = useQuery({ queryKey: activityKeys.summary(workspace), queryFn: () => activitiesApi.summary(workspace) });
  // Counts the server now refuses (or can't give) aren't left on screen.
  const counts = summary.isError ? undefined : summary.data;
  const action = useActivityAction(workspace);

  const run = (kind: LifecycleAction, activity: ActivityListItem) => {
    setNotice(null);
    setProblem(null);
    // Always the version the list shows now (after a 409 the list reloads).
    const current = activities.data?.results.find((a) => a.id === activity.id) ?? activity;
    action.mutate(
      { activity: current, action: kind },
      {
        onSuccess: () => {
          setPending(null);
          focusNotice.current = true;
          setNotice(done(kind, current));
        },
        onError: (error) => {
          setPending(null);
          focusNotice.current = true;
          setProblem(
            isApiError(error, 409)
              ? { message: "That activity was changed by someone else a moment ago. The list has been refreshed; try again.", requestId: null }
              : describeError(error),
          );
        },
      },
    );
  };
  const onRowAction = (kind: LifecycleAction, activity: ActivityListItem) => {
    if (CONFIRMED.has(kind)) {
      action.reset();
      setPending({ action: kind, activity });
    } else run(kind, activity);
  };

  if (isApiError(activities.error, 404)) return <NotFoundView />;

  const rows = activities.data?.results;
  const error = activities.isError ? describeError(activities.error) : null;
  const filtered = activeFilterCount(list.filters) > 0 || list.filters.status !== TAB_DEFAULTS[list.filters.tab].status;
  const today = businessToday();
  const shortcut = (label: string, count: number | undefined, patch: Partial<ActivityFilters>) => (
    <button
      key={label}
      type="button"
      onClick={() => list.setFilters({ ...NO_FILTERS, ...patch })}
      className="rounded-md border border-slate-200 bg-white px-3 py-2 text-left hover:border-brand-200 hover:bg-brand-50"
    >
      <span className="block text-lg font-semibold tabular-nums text-slate-900">{count ?? "–"}</span>
      <span className="block text-xs text-slate-600">{label}</span>
    </button>
  );

  return (
    <>
      <PageHeader
        title="Activities"
        subtitle={describeWorkspace(workspace)}
        actions={
          permissions.canWrite ? (
            <>
              <Button icon={<Plus aria-hidden="true" className="size-4" />} onClick={() => setForm("task")}>
                New task
              </Button>
              <Button variant="secondary" icon={<Plus aria-hidden="true" className="size-4" />} onClick={() => setForm("meeting")}>
                New meeting
              </Button>
            </>
          ) : null
        }
      />

      <nav aria-label="Shortcuts" className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        {/* Each opens a list of exactly what it counts (selectors.activity_summary). */}
        {shortcut("Open tasks", counts?.open_tasks, { tab: "task", ...TAB_DEFAULTS.task })}
        {shortcut("Overdue tasks", counts?.overdue_tasks, { tab: "task", status: "overdue", ordering: "scheduled" })}
        {shortcut("Tasks due today", counts?.tasks_due_today, { tab: "task", status: "open", ordering: "scheduled", dateFrom: today, dateTo: today })}
        {shortcut("Meetings today", counts?.meetings_today, { tab: "meeting", status: "not_cancelled", ordering: "scheduled", dateFrom: today, dateTo: today })}
        {shortcut("Upcoming meetings", counts?.upcoming_meetings, { tab: "meeting", status: "upcoming", ordering: "scheduled" })}
      </nav>

      <FiltersBar workspace={workspace} list={list} />

      <div ref={noticeRegion} tabIndex={-1} aria-live="polite" className="mb-4 space-y-2 empty:hidden focus:outline-none">
        {notice ? <Alert tone="success">{notice}</Alert> : null}
        {problem ? (
          <Alert tone="error" requestId={problem.requestId}>
            {problem.message}
          </Alert>
        ) : null}
      </div>

      {error ? (
        <Alert
          tone="error"
          title={isApiError(activities.error, 403) ? "You can't view these activities" : "Activities couldn't be loaded"}
          requestId={error.requestId}
          action={
            isApiError(activities.error, 403) ? null : isApiError(activities.error, 400) ? (
              <Button variant="secondary" size="sm" onClick={() => list.resetFilters()}>
                Start over
              </Button>
            ) : (
              <Button variant="secondary" size="sm" onClick={() => void activities.refetch()} loading={activities.isFetching}>
                Try again
              </Button>
            )
          }
        >
          {isApiError(activities.error, 400) && list.cursor ? "This page link is no longer valid." : error.message}
        </Alert>
      ) : rows && rows.length === 0 ? (
        list.cursor ? (
          <EmptyState
            icon={CalendarCheck}
            title="No more activities"
            action={
              <Button variant="secondary" onClick={() => list.setCursor(null)}>
                Back to the first page
              </Button>
            }
          />
        ) : filtered ? (
          <EmptyState
            icon={CalendarCheck}
            title="Nothing matches these filters"
            description="Try another status or date range, or clear the filters."
            action={
              <Button variant="secondary" onClick={() => list.resetFilters()}>
                Clear filters
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={CalendarCheck}
            title={list.filters.archived ? "No archived activities" : "No activities yet"}
            description={
              list.filters.archived
                ? "Activities you archive are kept here and can be restored."
                : workspace.kind === "user"
                  ? "This user has no tasks, meetings or notes here yet."
                  : "Tasks, meetings and notes about your leads will appear here. Add notes from a lead's page."
            }
          />
        )
      ) : (
        <>
          <ActivityTable
            activities={rows}
            loading={activities.isPending}
            workspace={workspace}
            showOwner={workspace.kind === "organization"}
            canWrite={permissions.canWrite}
            busyId={action.isPending ? (action.variables?.activity.id ?? null) : null}
            onAction={onRowAction}
          />
          <nav aria-label="Pagination" className="mt-4 flex items-center justify-end gap-2">
            <Button variant="secondary" size="sm" disabled={!activities.data?.previous || activities.isFetching} onClick={() => list.setCursor(cursorOf(activities.data?.previous))}>
              Previous
            </Button>
            <Button variant="secondary" size="sm" disabled={!activities.data?.next || activities.isFetching} onClick={() => list.setCursor(cursorOf(activities.data?.next))}>
              Next
            </Button>
          </nav>
        </>
      )}

      {form ? (
        <ActivityFormDialog
          workspace={workspace}
          kind={form}
          mode={{ kind: "create" }}
          onClose={() => setForm(null)}
          onSaved={(saved) => {
            setForm(null);
            focusNotice.current = true;
            setNotice(saved.type === "task" ? `Task "${saved.title}" created.` : `Meeting "${saved.title}" scheduled.`);
          }}
        />
      ) : null}
      <ConfirmDialog
        open={pending !== null}
        title={pending?.action === "archive" ? "Archive this activity?" : `Cancel ${pending?.activity.title ?? ""}?`}
        confirmLabel={pending?.action === "archive" ? "Archive" : `Cancel ${pending?.activity.type ?? ""}`}
        tone="danger"
        busy={action.isPending}
        error={action.isError ? describeError(action.error) : null}
        onConfirm={() => pending && run(pending.action, pending.activity)}
        onCancel={() => setPending(null)}
      >
        {pending?.action === "archive"
          ? "It will be hidden from lists, timelines and counts. Nothing is deleted, and it can be restored from Archived."
          : "It stays in the history as cancelled, with who cancelled it and when, and can be reopened."}
      </ConfirmDialog>
    </>
  );
}

function Labelled({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-slate-600">
        {label}
      </label>
      {children}
    </div>
  );
}

/** The tab, status and sort up front; dates, lead, owner (organisation-wide) and the
 * archived view under "More filters". Only filters the API accepts exist here. */
function FiltersBar({ workspace, list }: { workspace: Workspace; list: ReturnType<typeof useActivityListState> }) {
  const ids = { status: useId(), sort: useId(), from: useId(), to: useId(), panel: useId(), range: useId() };
  const { filters } = list;
  const count = activeFilterCount(filters);
  const [expanded, setExpanded] = useState(count > 0 || filters.archived);
  const statuses = STATUS_OPTIONS[filters.tab];

  return (
    <div className="mb-4 space-y-3">
      <div role="group" aria-label="Show" className="inline-flex flex-wrap rounded-md border border-slate-300 bg-white p-0.5 text-sm">
        {TABS.map((tab) => (
          <button
            key={tab.value}
            type="button"
            aria-pressed={filters.tab === tab.value}
            onClick={() => list.setTab(tab.value)}
            className={`rounded px-3 py-1 ${filters.tab === tab.value ? "bg-slate-900 font-medium text-white" : "text-slate-600 hover:bg-slate-50"}`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        {statuses.length ? (
          <div>
            <label htmlFor={ids.status} className="sr-only">
              Filter by status
            </label>
            <select
              id={ids.status}
              value={filters.status}
              onChange={(e) => list.setFilters({ status: e.target.value as ActivityFilters["status"] })}
              className={CONTROL}
            >
              {statuses.map((s) => (
                <option key={s.label} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        <div>
          <label htmlFor={ids.sort} className="sr-only">
            Sort by
          </label>
          <select id={ids.sort} value={filters.ordering} onChange={(e) => list.setFilters({ ordering: e.target.value as ActivityOrdering })} className={CONTROL}>
            {ORDERING_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <Button
          variant="secondary"
          icon={<SlidersHorizontal aria-hidden="true" className="size-4" />}
          aria-expanded={expanded}
          aria-controls={ids.panel}
          onClick={() => setExpanded((v) => !v)}
        >
          More filters{count > 0 ? ` (${count})` : ""}
        </Button>
        {count > 0 ? (
          <Button variant="ghost" onClick={() => list.resetFilters()}>
            Clear
          </Button>
        ) : null}
      </div>
      <div id={ids.panel} hidden={!expanded} className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Labelled id={ids.from} label="Due / start / written from">
            <input
              id={ids.from}
              type="date"
              value={filters.dateFrom}
              onChange={(e) => list.setFilters({ dateFrom: e.target.value })}
              aria-describedby={list.rangeInvalid ? ids.range : undefined}
              aria-invalid={list.rangeInvalid || undefined}
              className={`${CONTROL} w-full`}
            />
          </Labelled>
          <Labelled id={ids.to} label="to">
            <input
              id={ids.to}
              type="date"
              value={filters.dateTo}
              onChange={(e) => list.setFilters({ dateTo: e.target.value })}
              aria-describedby={list.rangeInvalid ? ids.range : undefined}
              aria-invalid={list.rangeInvalid || undefined}
              className={`${CONTROL} w-full`}
            />
          </Labelled>
          <div className="sm:col-span-2">
            <LeadPicker
              workspace={workspace}
              value={filters.lead}
              valueLabel={filters.leadLabel}
              onChange={(lead, leadLabel) => list.setFilters({ lead, leadLabel, opportunity: "" })}
            />
          </div>
          {workspace.kind === "organization" ? (
            <div className="sm:col-span-2">
              <OwnerSelect
                label="Owner"
                placeholder="Anyone"
                value={filters.owner}
                valueLabel={filters.ownerLabel}
                onChange={(owner, ownerLabel) => list.setFilters({ owner, ownerLabel })}
              />
            </div>
          ) : null}
          <label className="flex items-center gap-2 text-sm text-slate-700 sm:col-span-2">
            <input
              type="checkbox"
              checked={filters.archived}
              onChange={(e) => list.setFilters({ archived: e.target.checked })}
              className="size-4 accent-brand-600"
            />
            Show archived activities only
          </label>
        </div>
        {list.rangeInvalid ? (
          <p id={ids.range} role="alert" className="mt-2 text-xs text-red-600">
            The end date must be on or after the start date. The list shows the last valid range.
          </p>
        ) : null}
      </div>
    </div>
  );
}
