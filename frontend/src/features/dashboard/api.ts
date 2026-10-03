/**
 * Dashboard API client and query. One request per page load
 * (/api/v1/workspaces/{me|all|userId}/dashboard); its cache key starts with that workspace
 * segment, so one user's figures can never be served under another user's workspace.
 */
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api/client";
import type { Dashboard } from "@/lib/api/types";
import { type Workspace, workspaceApiPath, workspaceApiSegment } from "@/lib/workspace";

export const dashboardKeys = {
  workspace: (workspace: Workspace) => ["dashboard", workspaceApiSegment(workspace)] as const,
};

export const dashboardApi = {
  get: (workspace: Workspace, signal?: AbortSignal) =>
    apiFetch<Dashboard>(workspaceApiPath(workspace, "dashboard"), { signal }),
};

/**
 * The figures are never served from a cache: each visit to the dashboard (Back included)
 * reads them afresh, and nothing is kept once the page is left (`gcTime: 0`). So a change
 * committed anywhere shows on the next visit, and no other workspace's figures can be on
 * screen while new ones load. Returning to the browser tab refreshes them; meanwhile the
 * figures on screen are marked as updating, and a refresh that fails replaces them with the
 * error. Offline, the request is still made (`networkMode: "always"`), so a failure is
 * explained instead of the skeleton waiting for the network forever.
 */
export function useDashboard(workspace: Workspace) {
  return useQuery({
    queryKey: dashboardKeys.workspace(workspace),
    queryFn: ({ signal }) => dashboardApi.get(workspace, signal),
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    networkMode: "always",
  });
}
