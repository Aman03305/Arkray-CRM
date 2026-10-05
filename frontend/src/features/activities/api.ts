/**
 * Activities API client and query keys. Every call is scoped to a workspace
 * (/api/v1/workspaces/{me|all|userId}/...), and every cache key starts with its root and
 * that workspace segment, so one user's activities, counts or timeline can never be served
 * from the cache under another user's workspace.
 */
import { cursorOf } from "@/lib/api/pagination";
import { apiFetch, apiUpload } from "@/lib/api/client";
import type {
  Activity,
  ActivityCreateRequest,
  ActivityListItem,
  ActivityOrdering,
  ActivityPage,
  ActivityStatus,
  ActivitySummary,
  ActivityType,
  ActivityUpdateRequest,
  Attachment,
  NotePage,
  TimelinePage,
} from "@/lib/api/types";
import { type Workspace, workspaceApiPath, workspaceApiSegment } from "@/lib/workspace";

export const PAGE_SIZE = 25;
export const TIMELINE_PAGE_SIZE = 20;
export const CURRENT_WORK_PAGE_SIZE = 10;
export const DEAL_NOTES_PAGE_SIZE = 20;
/** The calendar reads a range in pages of 100 (the API's largest), at most this many per type. */
export const CALENDAR_PAGE_SIZE = 100;
export const CALENDAR_MAX_PAGES = 5;

export type ActivityTab = "all" | ActivityType;

/**
 * The list's filters (allowlisted server-side; owner only organisation-wide). `status`
 * is a status key or a preset: "overdue", "current" (open tasks and scheduled meetings),
 * "upcoming" (current work from now on) and "not_cancelled" (the summary's "meetings
 * today" counts scheduled and completed ones), so each summary figure opens a list of
 * exactly the activities it counts.
 */
type StatusPreset = "overdue" | "current" | "upcoming" | "not_cancelled";

export interface ActivityFilters {
  tab: ActivityTab;
  status: ActivityStatus | StatusPreset | "";
  /** Inclusive business dates "YYYY-MM-DD" of the due time / start / creation. */
  dateFrom: string;
  dateTo: string;
  opportunity: string;
  /** The chosen opportunity's title, shown while the picker's list doesn't include it (UI only). */
  opportunityLabel: string;
  owner: string;
  ownerLabel: string;
  archived: boolean;
  ordering: ActivityOrdering;
}

export const NO_FILTERS: ActivityFilters = {
  tab: "all",
  status: "",
  dateFrom: "",
  dateTo: "",
  opportunity: "",
  opportunityLabel: "",
  owner: "",
  ownerLabel: "",
  archived: false,
  ordering: "-created_at",
};

/** Each tab's sensible starting point: open work soonest first, notes newest first. */
export const TAB_DEFAULTS: Record<ActivityTab, Pick<ActivityFilters, "status" | "ordering">> = {
  all: { status: "", ordering: "-created_at" },
  task: { status: "open", ordering: "scheduled" },
  meeting: { status: "scheduled", ordering: "scheduled" },
  note: { status: "", ordering: "-created_at" },
};

/**
 * The list each summary figure opens: exactly the activities it counts
 * (selectors.activity_summary; tested figure by figure in test_summary.py). Used by the
 * Activities page's shortcuts and the dashboard's cards. `today` is the business date.
 */
export function summaryFilters(today: string): Record<keyof ActivitySummary, Partial<ActivityFilters>> {
  return {
    open_tasks: { tab: "task", ...TAB_DEFAULTS.task },
    overdue_tasks: { tab: "task", status: "overdue", ordering: "scheduled" },
    tasks_due_today: { tab: "task", status: "open", ordering: "scheduled", dateFrom: today, dateTo: today },
    meetings_today: { tab: "meeting", status: "not_cancelled", ordering: "scheduled", dateFrom: today, dateTo: today },
    upcoming_meetings: { tab: "meeting", status: "upcoming", ordering: "scheduled" },
  };
}

export const STATUS_OPTIONS: Record<ActivityTab, readonly { value: ActivityFilters["status"]; label: string }[]> = {
  all: [
    { value: "", label: "Any status" },
    { value: "current", label: "Open and scheduled" },
    { value: "completed", label: "Completed" },
    { value: "cancelled", label: "Cancelled" },
  ],
  task: [
    { value: "open", label: "Open" },
    { value: "overdue", label: "Overdue" },
    { value: "completed", label: "Completed" },
    { value: "cancelled", label: "Cancelled" },
    { value: "", label: "Any status" },
  ],
  meeting: [
    { value: "scheduled", label: "Scheduled" },
    { value: "upcoming", label: "Upcoming" },
    { value: "not_cancelled", label: "Scheduled or completed" },
    { value: "completed", label: "Completed" },
    { value: "cancelled", label: "Cancelled" },
    { value: "", label: "Any status" },
  ],
  note: [],
};

export const ORDERING_OPTIONS: readonly { value: ActivityOrdering; label: string }[] = [
  { value: "-created_at", label: "Newest first" },
  { value: "created_at", label: "Oldest first" },
  { value: "scheduled", label: "Due / start, soonest first" },
  { value: "-scheduled", label: "Due / start, latest first" },
];

/** Narrowing filters in use (the tab, sort and archive view don't count). */
export function activeFilterCount(filters: ActivityFilters): number {
  return [filters.dateFrom, filters.dateTo, filters.opportunity, filters.owner].filter(Boolean).length;
}

export function invalidRange(filters: Pick<ActivityFilters, "dateFrom" | "dateTo">): boolean {
  return Boolean(filters.dateFrom && filters.dateTo && filters.dateFrom > filters.dateTo);
}

export function listParams(workspace: Workspace, filters: ActivityFilters, cursor: string | null, pageSize = PAGE_SIZE): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.tab !== "all") params.set("type", filters.tab);
  if (filters.status === "overdue") params.set("overdue", "true");
  else if (filters.status === "current") params.set("current", "true");
  else if (filters.status === "upcoming") params.set("upcoming", "true");
  else if (filters.status === "not_cancelled") params.set("cancelled", "false");
  else if (filters.status) params.set("status", filters.status);
  if (filters.dateFrom) params.set("date_from", filters.dateFrom);
  if (filters.dateTo) params.set("date_to", filters.dateTo);
  if (filters.opportunity) params.set("opportunity", filters.opportunity);
  if (filters.owner && workspace.kind === "organization") params.set("owner", filters.owner);
  if (filters.archived) params.set("archived", "true");
  if (filters.ordering !== NO_FILTERS.ordering) params.set("ordering", filters.ordering);
  if (cursor) params.set("cursor", cursor);
  params.set("page_size", String(pageSize));
  return params;
}

/** An opportunity's current work: open tasks and scheduled meetings, soonest first. */
export interface CurrentWorkTarget {
  opportunity: string;
}

function currentWorkPath(workspace: Workspace, target: CurrentWorkTarget): string {
  const params = new URLSearchParams({ current: "true", ordering: "scheduled", page_size: String(CURRENT_WORK_PAGE_SIZE) });
  params.set("opportunity", target.opportunity);
  return `${workspaceApiPath(workspace, "activities")}?${params.toString()}`;
}

/**
 * The calendar's range: inclusive business dates of the days on screen, and, organisation-wide
 * only, one owner ("My calendar"; empty for everyone).
 */
export interface CalendarRange {
  from: string;
  to: string;
  owner: string;
}

export interface CalendarData {
  /** Tasks (by due time) and meetings (by start) in the range, cancelled and archived left out. */
  items: ActivityListItem[];
  /** A type had more than CALENDAR_MAX_PAGES pages in the range: only its first ones are here. */
  truncated: boolean;
}

/**
 * One type's entries in the range: the list API with the filters the list itself sends
 * (same scope, same authorisation, the per-type schedule indexes), soonest first, following
 * `next` with exactly the same parameters (cursors are bound to them).
 */
async function calendarEntries(workspace: Workspace, type: "task" | "meeting", range: CalendarRange) {
  const filters: ActivityFilters = {
    ...NO_FILTERS,
    tab: type,
    status: "not_cancelled",
    ordering: "scheduled",
    dateFrom: range.from,
    dateTo: range.to,
    owner: range.owner,
  };
  const items: ActivityListItem[] = [];
  let cursor: string | null = null;
  for (let read = 0; read < CALENDAR_MAX_PAGES; read += 1) {
    const params = listParams(workspace, filters, cursor, CALENDAR_PAGE_SIZE);
    const page: ActivityPage = await apiFetch<ActivityPage>(`${workspaceApiPath(workspace, "activities")}?${params.toString()}`);
    items.push(...page.results);
    cursor = cursorOf(page.next);
    if (!cursor) return { items, truncated: false };
  }
  return { items, truncated: true };
}

export type TimelineSubject = { kind: "opportunity"; id: string };

function timelinePath(workspace: Workspace, subject: TimelineSubject, cursor: string | null): string {
  const params = new URLSearchParams({ page_size: String(TIMELINE_PAGE_SIZE) });
  if (cursor) params.set("cursor", cursor);
  return `${workspaceApiPath(workspace, `opportunities/${encodeURIComponent(subject.id)}/timeline`)}?${params.toString()}`;
}

export const activityKeys = {
  /** Everything workspace-scoped about activities (lists, details, counts, current work). */
  all: ["activities"] as const,
  lists: (workspace: Workspace) => ["activities", "list", workspaceApiSegment(workspace)] as const,
  list: (workspace: Workspace, filters: ActivityFilters, cursor: string | null) =>
    ["activities", "list", workspaceApiSegment(workspace), filters, cursor] as const,
  detail: (workspace: Workspace, id: string) => ["activities", "detail", workspaceApiSegment(workspace), id] as const,
  summary: (workspace: Workspace) => ["activities", "summary", workspaceApiSegment(workspace)] as const,
  /** Under "activities", so any activity write marks every cached range stale. */
  calendar: (workspace: Workspace, range: CalendarRange) => ["activities", "calendar", workspaceApiSegment(workspace), range] as const,
  /** Every opportunity's current work cached for this workspace. */
  currentWork: (workspace: Workspace) => ["activities", "current", workspaceApiSegment(workspace)] as const,
  current: (workspace: Workspace, target: CurrentWorkTarget) =>
    ["activities", "current", workspaceApiSegment(workspace), target.opportunity] as const,
  /** A deal's Notes (whole texts and files); under "activities", so any activity write marks it stale. */
  dealNotes: (workspace: Workspace, opportunityId: string) =>
    ["activities", "deal-notes", workspaceApiSegment(workspace), opportunityId] as const,
};

export const timelineKeys = {
  /** Every timeline (marked stale after any opportunity or activity write). */
  all: ["timeline"] as const,
  subject: (workspace: Workspace, subject: TimelineSubject) =>
    ["timeline", subject.kind, workspaceApiSegment(workspace), subject.id] as const,
};

const activity = (workspace: Workspace, id: string, action = "") =>
  workspaceApiPath(workspace, `activities/${encodeURIComponent(id)}${action ? `/${action}` : ""}`);

const attachmentPath = (workspace: Workspace, id: string, action = "") =>
  workspaceApiPath(workspace, `attachments/${encodeURIComponent(id)}${action ? `/${action}` : ""}`);

export type LifecycleAction = "complete" | "cancel" | "reopen" | "archive" | "restore";

export const activitiesApi = {
  list: (workspace: Workspace, filters: ActivityFilters, cursor: string | null) =>
    apiFetch<ActivityPage>(`${workspaceApiPath(workspace, "activities")}?${listParams(workspace, filters, cursor).toString()}`),
  current: (workspace: Workspace, target: CurrentWorkTarget) => apiFetch<ActivityPage>(currentWorkPath(workspace, target)),
  get: (workspace: Workspace, id: string) => apiFetch<Activity>(activity(workspace, id)),
  summary: (workspace: Workspace) => apiFetch<ActivitySummary>(workspaceApiPath(workspace, "activity-summary")),
  /** The tasks and meetings of the days the calendar shows (both types read in parallel). */
  calendar: async (workspace: Workspace, range: CalendarRange): Promise<CalendarData> => {
    const [tasks, meetings] = await Promise.all([calendarEntries(workspace, "task", range), calendarEntries(workspace, "meeting", range)]);
    return { items: [...tasks.items, ...meetings.items], truncated: tasks.truncated || meetings.truncated };
  },
  create: (workspace: Workspace, body: ActivityCreateRequest, idempotencyKey: string) =>
    apiFetch<Activity>(workspaceApiPath(workspace, "activities"), {
      method: "POST",
      body,
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  update: (workspace: Workspace, id: string, body: ActivityUpdateRequest) =>
    apiFetch<Activity>(activity(workspace, id), { method: "PATCH", body }),
  /** Complete, cancel, reopen, archive or restore: explicit operations, never a PATCH. */
  act: (workspace: Workspace, id: string, action: LifecycleAction, version: number) =>
    apiFetch<Activity>(activity(workspace, id, action), { method: "POST", body: { version } }),
  timeline: (workspace: Workspace, subject: TimelineSubject, cursor: string | null) =>
    apiFetch<TimelinePage>(timelinePath(workspace, subject, cursor)),
  /** An opportunity's notes, newest first, with their files and whether each may be changed. */
  dealNotes: (workspace: Workspace, opportunityId: string, cursor: string | null) => {
    const params = new URLSearchParams({ page_size: String(DEAL_NOTES_PAGE_SIZE) });
    if (cursor) params.set("cursor", cursor);
    return apiFetch<NotePage>(
      `${workspaceApiPath(workspace, `opportunities/${encodeURIComponent(opportunityId)}/notes`)}?${params.toString()}`,
    );
  },
  /** One file, as the raw body; its name travels in X-Filename. */
  upload: (workspace: Workspace, noteId: string, file: File) =>
    apiUpload<Attachment>(activity(workspace, noteId, "attachments"), file, file.name),
  removeFile: (workspace: Workspace, attachmentId: string) =>
    apiFetch<void>(attachmentPath(workspace, attachmentId), { method: "DELETE" }),
};

/** Plain same-origin GETs with the session cookie: an <a download> and an <img> load them. */
export const fileUrls = {
  download: (workspace: Workspace, attachmentId: string) => attachmentPath(workspace, attachmentId, "download"),
  preview: (workspace: Workspace, attachmentId: string) => attachmentPath(workspace, attachmentId, "preview"),
};
