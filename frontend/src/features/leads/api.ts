/**
 * The lead (the canonical customer record) as the UI reads it: one lead, its opportunities,
 * and the advisory duplicate check. There is no Leads module (ADR-0027): a lead is made with
 * its opportunity and shown read-only (ADR-0028). Every call is scoped to a workspace and
 * every cache key starts with "leads" and that workspace segment, so one user's lead is never
 * served under another user's workspace.
 */
import { apiFetch } from "@/lib/api/client";
import type { Lead, LeadDuplicateList, OpportunityPage } from "@/lib/api/types";
import { type Workspace, workspaceApiPath, workspaceApiSegment } from "@/lib/workspace";

/** How many of a lead's opportunities its page lists at once (usually it has one). */
export const LEAD_OPPORTUNITIES_PAGE_SIZE = 10;

export const leadKeys = {
  all: ["leads"] as const,
  detail: (workspace: Workspace, id: string) => ["leads", "detail", workspaceApiSegment(workspace), id] as const,
  opportunities: (workspace: Workspace, id: string, cursor: string | null) =>
    ["leads", "opportunities", workspaceApiSegment(workspace), id, cursor] as const,
  duplicates: (workspace: Workspace, email: string, phones: readonly string[]) =>
    ["leads", "duplicates", workspaceApiSegment(workspace), email, phones] as const,
};

export const leadsApi = {
  get: (workspace: Workspace, id: string) => apiFetch<Lead>(workspaceApiPath(workspace, `leads/${encodeURIComponent(id)}`)),
  /** The lead's opportunities in this workspace, newest first. */
  opportunities: (workspace: Workspace, id: string, cursor: string | null) => {
    const params = new URLSearchParams({ lead: id, page_size: String(LEAD_OPPORTUNITIES_PAGE_SIZE) });
    if (cursor) params.set("cursor", cursor);
    return apiFetch<OpportunityPage>(`${workspaceApiPath(workspace, "opportunities")}?${params.toString()}`);
  },
  /** Leads of this workspace with the same email or phone (never outside the workspace). */
  duplicates: (workspace: Workspace, email: string, phones: readonly string[]) => {
    const params = new URLSearchParams();
    if (email) params.set("email", email);
    for (const phone of phones) params.append("phone", phone);
    return apiFetch<LeadDuplicateList>(`${workspaceApiPath(workspace, "leads/duplicates")}?${params.toString()}`);
  },
};
