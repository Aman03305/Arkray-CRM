/**
 * Activities API client and query keys. Every call is scoped to a workspace
 * (/api/v1/workspaces/{me|all|userId}/...), and every cache key starts with its root and
 * that workspace segment, so one user's activities, counts or timeline can never be served
 * from the cache under another user's workspace.
 */
import { apiFetch } from "@/lib/api/client";
import type {
  Activity,
  ActivityCreateRequest,
  ActivityOrdering,
  ActivityPage,
  ActivityStatus,
  ActivitySummary,
  ActivityType,
  ActivityUpdateRequest,
  TimelinePage,
} from "@/lib/api/types";
import { type Workspace, workspaceApiPath, workspaceApiSegment } from "@/lib/workspace";

export const PAGE_SIZE = 25;
export const TIMELINE_PAGE_SIZE = 20;
export const CURRENT_WORK_PAGE_SIZE = 10;

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
  lead: string;
  leadLabel: string;
  opportunity: string;
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
  lead: "",
  leadLabel: "",
  opportunity: "",
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
  return [filters.dateFrom, filters.dateTo, filters.lead, filters.opportunity, filters.owner].filter(Boolean).length;
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
  if (filters.lead) params.set("lead", filters.lead);
  if (filters.opportunity) params.set("opportunity", filters.opportunity);
  if (filters.owner && workspace.kind === "organization") params.set("owner", filters.owner);
  if (filters.archived) params.set("archived", "true");
  if (filters.ordering !== NO_FILTERS.ordering) params.set("ordering", filters.ordering);
  if (cursor) params.set("cursor", cursor);
  params.set("page_size", String(pageSize));
  return params;
}

/** A lead's or an opportunity's current work: open tasks and scheduled meetings, soonest first. */
export interface CurrentWorkTarget {
  lead?: string;
  opportunity?: string;
}

function currentWorkPath(workspace: Workspace, target: CurrentWorkTarget): string {
  const params = new URLSearchParams({ current: "true", ordering: "scheduled", page_size: String(CURRENT_WORK_PAGE_SIZE) });
  if (target.lead) params.set("lead", target.lead);
  if (target.opportunity) params.set("opportunity", target.opportunity);
  return `${workspaceApiPath(workspace, "activities")}?${params.toString()}`;
}

export type TimelineSubject = { kind: "lead" | "opportunity"; id: string };

function timelinePath(workspace: Workspace, subject: TimelineSubject, cursor: string | null): string {
  const resource = subject.kind === "lead" ? "leads" : "opportunities";
  const params = new URLSearchParams({ page_size: String(TIMELINE_PAGE_SIZE) });
  if (cursor) params.set("cursor", cursor);
  return `${workspaceApiPath(workspace, `${resource}/${encodeURIComponent(subject.id)}/timeline`)}?${params.toString()}`;
}

export const activityKeys = {
  /** Everything workspace-scoped about activities (lists, details, counts, current work). */
  all: ["activities"] as const,
  lists: (workspace: Workspace) => ["activities", "list", workspaceApiSegment(workspace)] as const,
  list: (workspace: Workspace, filters: ActivityFilters, cursor: string | null) =>
    ["activities", "list", workspaceApiSegment(workspace), filters, cursor] as const,
  detail: (workspace: Workspace, id: string) => ["activities", "detail", workspaceApiSegment(workspace), id] as const,
  summary: (workspace: Workspace) => ["activities", "summary", workspaceApiSegment(workspace)] as const,
  /** Every lead's and opportunity's current work cached for this workspace. */
  currentWork: (workspace: Workspace) => ["activities", "current", workspaceApiSegment(workspace)] as const,
  current: (workspace: Workspace, target: CurrentWorkTarget) =>
    ["activities", "current", workspaceApiSegment(workspace), target.lead ?? "", target.opportunity ?? ""] as const,
};

export const timelineKeys = {
  /** Every timeline (marked stale after any lead, opportunity or activity write). */
  all: ["timeline"] as const,
  subject: (workspace: Workspace, subject: TimelineSubject) =>
    ["timeline", subject.kind, workspaceApiSegment(workspace), subject.id] as const,
};

const activity = (workspace: Workspace, id: string, action = "") =>
  workspaceApiPath(workspace, `activities/${encodeURIComponent(id)}${action ? `/${action}` : ""}`);

export type LifecycleAction = "complete" | "cancel" | "reopen" | "archive" | "restore";

export const activitiesApi = {
  list: (workspace: Workspace, filters: ActivityFilters, cursor: string | null) =>
    apiFetch<ActivityPage>(`${workspaceApiPath(workspace, "activities")}?${listParams(workspace, filters, cursor).toString()}`),
  current: (workspace: Workspace, target: CurrentWorkTarget) => apiFetch<ActivityPage>(currentWorkPath(workspace, target)),
  get: (workspace: Workspace, id: string) => apiFetch<Activity>(activity(workspace, id)),
  summary: (workspace: Workspace) => apiFetch<ActivitySummary>(workspaceApiPath(workspace, "activity-summary")),
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
};
