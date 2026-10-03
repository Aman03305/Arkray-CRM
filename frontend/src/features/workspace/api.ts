/**
 * Whose workspace this is: GET /api/v1/workspaces/{userId} (id, name, status, nothing more).
 * The banner, the sidebar and the lead form share this one query (one request per user
 * workspace, which the API authorises and audits); its key carries the user id, so one
 * user's name can never be shown in another user's workspace.
 */
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api/client";
import type { WorkspaceDto } from "@/lib/api/types";
import { isUuid, type Workspace } from "@/lib/workspace";

export type WorkspaceSubject = NonNullable<WorkspaceDto["subject"]>;

export const workspaceKeys = {
  subject: (userId: string) => ["workspace", userId] as const,
};

export function useWorkspaceSubject(userId: string | null) {
  return useQuery({
    queryKey: workspaceKeys.subject(userId ?? ""),
    queryFn: async ({ signal }) => {
      const workspace = await apiFetch<WorkspaceDto>(`/api/v1/workspaces/${encodeURIComponent(userId!)}`, { signal });
      // Never trust a mismatched answer to name this workspace.
      if (workspace.subject?.id.toLowerCase() !== userId) throw new Error("The workspace answer names another user.");
      return workspace.subject;
    },
    enabled: userId !== null && isUuid(userId),
  });
}

/** The selected user of a workspace, or null in one's own or the organisation's. */
export function selectedUserId(workspace: Workspace | null): string | null {
  return workspace?.kind === "user" ? workspace.userId : null;
}

/** Can new work (leads, opportunities, activities) be given to this workspace's user? */
export function receivesNewWork(subject: WorkspaceSubject | undefined): boolean {
  return subject === undefined || subject.status === "active";
}
