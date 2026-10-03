/**
 * Leads API client and query keys. Every call is scoped to a workspace
 * (/api/v1/workspaces/{me|all|userId}/leads...), and every cache key starts with that
 * workspace segment, so one user's leads can never be served from the cache under another
 * user's workspace.
 */
import { apiFetch } from "@/lib/api/client";
import type {
  AssigneePage,
  Lead,
  LeadCreateRequest,
  LeadDuplicateList,
  LeadOptions,
  LeadOrdering,
  LeadPage,
  LeadUpdateRequest,
  Rating,
} from "@/lib/api/types";
import { type Workspace, workspaceApiPath, workspaceApiSegment } from "@/lib/workspace";

export const SEARCH_MIN_LENGTH = 2;
export const SEARCH_MAX_LENGTH = 100;
export const PAGE_SIZE = 25;

export interface LeadFilters {
  q: string;
  status: string;
  source: string;
  rating: Rating | "";
  /** Owner id; organisation-wide workspace only. */
  owner: string;
  /** The chosen owner's name, shown while the picker's list doesn't include them (UI only). */
  ownerLabel: string;
  /** Inclusive business-day dates, "YYYY-MM-DD". */
  createdFrom: string;
  createdTo: string;
  archived: boolean;
  ordering: LeadOrdering;
}

export const NO_FILTERS: LeadFilters = {
  q: "",
  status: "",
  source: "",
  rating: "",
  owner: "",
  ownerLabel: "",
  createdFrom: "",
  createdTo: "",
  archived: false,
  ordering: "-created_at",
};

export const ORDERING_OPTIONS: readonly { value: LeadOrdering; label: string }[] = [
  { value: "-created_at", label: "Newest first" },
  { value: "created_at", label: "Oldest first" },
  { value: "name", label: "Name (A–Z)" },
  { value: "-updated_at", label: "Recently updated" },
  { value: "-last_contacted_at", label: "Recently contacted" },
  { value: "last_contacted_at", label: "Longest since contact" },
];

/** Narrowing filters in use (search, sort and the archive view don't count). */
export function activeFilterCount(filters: LeadFilters): number {
  return [filters.status, filters.source, filters.rating, filters.owner, filters.createdFrom, filters.createdTo].filter(
    Boolean,
  ).length;
}

export function effectiveSearch(q: string): string {
  const trimmed = q.trim();
  return trimmed.length >= SEARCH_MIN_LENGTH ? trimmed.slice(0, SEARCH_MAX_LENGTH) : "";
}

/** The cursor inside a `next`/`previous` link (the link's host is never used). */
export function cursorOf(link: string | null | undefined): string | null {
  if (!link) return null;
  try {
    return new URL(link, "https://arkray.invalid").searchParams.get("cursor");
  } catch {
    return null;
  }
}

export function listPath(workspace: Workspace, filters: LeadFilters, cursor: string | null, pageSize = PAGE_SIZE): string {
  const params = new URLSearchParams();
  const q = effectiveSearch(filters.q);
  if (q) params.set("q", q);
  if (filters.status) params.set("status", filters.status);
  if (filters.source) params.set("source", filters.source);
  if (filters.rating) params.set("rating", filters.rating);
  if (filters.owner && workspace.kind === "organization") params.set("owner", filters.owner);
  if (filters.createdFrom) params.set("created_from", filters.createdFrom);
  if (filters.createdTo) params.set("created_to", filters.createdTo);
  if (filters.archived) params.set("archived", "true");
  if (filters.ordering !== NO_FILTERS.ordering) params.set("ordering", filters.ordering);
  if (cursor) params.set("cursor", cursor);
  params.set("page_size", String(pageSize));
  return `${workspaceApiPath(workspace, "leads")}?${params.toString()}`;
}

export const leadKeys = {
  all: ["leads"] as const,
  lists: () => ["leads", "list"] as const,
  list: (workspace: Workspace, filters: LeadFilters, cursor: string | null) =>
    ["leads", "list", workspaceApiSegment(workspace), filters, cursor] as const,
  detail: (workspace: Workspace, id: string) => ["leads", "detail", workspaceApiSegment(workspace), id] as const,
  duplicates: (workspace: Workspace, email: string, phones: readonly string[], exclude: string | null) =>
    ["leads", "duplicates", workspaceApiSegment(workspace), email, phones, exclude] as const,
  options: ["lead-options"] as const,
  assignees: (q: string) => ["assignees", q] as const,
};

const lead = (workspace: Workspace, id: string, action = "") =>
  workspaceApiPath(workspace, `leads/${encodeURIComponent(id)}${action ? `/${action}` : ""}`);

export const leadsApi = {
  list: (workspace: Workspace, filters: LeadFilters, cursor: string | null) =>
    apiFetch<LeadPage>(listPath(workspace, filters, cursor)),
  get: (workspace: Workspace, id: string) => apiFetch<Lead>(lead(workspace, id)),
  create: (workspace: Workspace, body: LeadCreateRequest, idempotencyKey: string) =>
    apiFetch<Lead>(workspaceApiPath(workspace, "leads"), {
      method: "POST",
      body,
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  update: (workspace: Workspace, id: string, body: LeadUpdateRequest) =>
    apiFetch<Lead>(lead(workspace, id), { method: "PATCH", body }),
  changeStatus: (workspace: Workspace, id: string, status: string, version: number) =>
    apiFetch<Lead>(lead(workspace, id, "status"), { method: "POST", body: { status, version } }),
  assign: (workspace: Workspace, id: string, owner: string, version: number) =>
    apiFetch<Lead>(lead(workspace, id, "assign"), { method: "POST", body: { owner, version } }),
  archive: (workspace: Workspace, id: string, version: number) =>
    apiFetch<Lead>(lead(workspace, id, "archive"), { method: "POST", body: { version } }),
  restore: (workspace: Workspace, id: string, version: number) =>
    apiFetch<Lead>(lead(workspace, id, "restore"), { method: "POST", body: { version } }),
  duplicates: (workspace: Workspace, email: string, phones: readonly string[], exclude: string | null) => {
    const params = new URLSearchParams();
    if (email) params.set("email", email);
    for (const phone of phones) params.append("phone", phone);
    if (exclude) params.set("exclude", exclude);
    return apiFetch<LeadDuplicateList>(`${workspaceApiPath(workspace, "leads/duplicates")}?${params.toString()}`);
  },
  options: () => apiFetch<LeadOptions>("/api/v1/config/lead-options"),
  assignees: (q: string, cursor: string | null = null) => {
    const params = new URLSearchParams({ page_size: "100" });
    if (q) params.set("q", q);
    if (cursor) params.set("cursor", cursor);
    return apiFetch<AssigneePage>(`/api/v1/assignees?${params.toString()}`);
  },
};
