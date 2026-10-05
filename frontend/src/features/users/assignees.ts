/**
 * Who can own CRM records: active users who work in a CRM workspace
 * (GET /api/v1/assignees, crm.assign_any). Not workspace-scoped: the list is the same
 * everywhere.
 */
import { apiFetch } from "@/lib/api/client";
import type { AssigneePage } from "@/lib/api/types";

export const ASSIGNEE_SEARCH_MIN_LENGTH = 2;

export const assigneeKeys = {
  list: (q: string) => ["assignees", q] as const,
};

export const assigneesApi = {
  list: (q: string, cursor: string | null = null) => {
    const params = new URLSearchParams({ page_size: "100" });
    if (q) params.set("q", q);
    if (cursor) params.set("cursor", cursor);
    return apiFetch<AssigneePage>(`/api/v1/assignees?${params.toString()}`);
  },
};
