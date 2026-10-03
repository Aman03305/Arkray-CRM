"use client";

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { WorkspaceBanner } from "@/components/shell/WorkspaceBanner";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { NotFoundView } from "@/components/ui/NotFoundView";
import { apiFetch } from "@/lib/api/client";
import { describeError, isApiError } from "@/lib/api/errors";
import type { WorkspaceDto } from "@/lib/api/types";

/**
 * An administrator viewing one user's CRM. Opening it calls GET /api/v1/workspaces/{id},
 * which authorises the access and writes the `workspace.accessed` audit event; the banner
 * then names whose CRM this is. The admin stays signed in as themselves throughout.
 */
export function UserWorkspaceFrame({ userId, children }: { userId: string; children: ReactNode }) {
  const workspace = useQuery({
    queryKey: ["workspace", userId],
    queryFn: () => apiFetch<WorkspaceDto>(`/api/v1/workspaces/${encodeURIComponent(userId)}`),
  });

  if (isApiError(workspace.error, 404)) return <NotFoundView />;
  if (workspace.isError) {
    const { message, requestId } = describeError(workspace.error);
    return (
      <Alert
        tone="error"
        title="This workspace couldn't be opened"
        requestId={requestId}
        action={
          <Button variant="secondary" size="sm" onClick={() => void workspace.refetch()} loading={workspace.isFetching}>
            Try again
          </Button>
        }
      >
        {message}
      </Alert>
    );
  }
  const subject = workspace.data?.subject;
  return (
    <>
      <WorkspaceBanner
        subjectName={subject?.full_name}
        note={subject?.status === "deactivated" ? "Deactivated user: history is kept for reference." : undefined}
      />
      {children}
    </>
  );
}
