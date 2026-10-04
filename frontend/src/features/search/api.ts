/**
 * Global search API client and query: GET /api/v1/workspaces/{me|all|userId}/search?q=...
 *
 * The cache key starts with the workspace segment, then the query as sent, so one
 * workspace's results can never be served (or shown) under another workspace: an answer
 * that arrives after a switch fills its own workspace's entry, which the new screen doesn't
 * observe. Results are kept in memory only, briefly (no browser storage, no server cache),
 * and a sign-out reloads the page, dropping them.
 */
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api/client";
import type { SearchResults } from "@/lib/api/types";
import { type Workspace, workspaceApiPath, workspaceApiSegment } from "@/lib/workspace";

export const searchKeys = {
  all: ["search"] as const,
  results: (workspace: Workspace, query: string) => ["search", workspaceApiSegment(workspace), query] as const,
};

export const searchApi = {
  search: (workspace: Workspace, query: string, signal?: AbortSignal) =>
    apiFetch<SearchResults>(`${workspaceApiPath(workspace, "search")}?${new URLSearchParams({ q: query })}`, { signal }),
};

/** "Rahul" and "Rahul Sha" are the same search being typed; "Apollo" and "Zeta" aren't. */
function continues(previous: unknown, query: string | null): boolean {
  if (typeof previous !== "string" || !previous || !query) return false;
  const [a, b] = [previous.toLowerCase(), query.toLowerCase()];
  return a.startsWith(b) || b.startsWith(a);
}

/**
 * The results for `query` in `workspace` (`query` null: nothing to search). While a longer
 * or shorter version of the same query loads, the previous results stay on screen, dimmed
 * and marked as updating (`isPlaceholderData`; the dialog never opens one on Enter), but
 * only the same workspace's: never another's, even if a component forgot to remount on a
 * switch (the search dialog does remount), and never an unrelated query's. Requests a
 * newer query supersedes are cancelled.
 */
export function useGlobalSearch(workspace: Workspace, query: string | null) {
  const segment = workspaceApiSegment(workspace);
  return useQuery({
    queryKey: searchKeys.results(workspace, query ?? ""),
    queryFn: ({ signal }) => searchApi.search(workspace, query!, signal),
    enabled: query !== null,
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === segment && continues(previousQuery.queryKey[2], query) ? previous : undefined,
    staleTime: 15_000,
    gcTime: 60_000,
  });
}
