"use client";

import type { ReactNode } from "react";

import { NotFoundView } from "@/components/ui/NotFoundView";
import { PageSkeleton } from "@/components/ui/PageSkeleton";
import { useWorkspace } from "@/lib/use-workspace";
import { useViewer } from "@/lib/viewer-context";
import { type Workspace, workspaceApiSegment } from "@/lib/workspace";

/*
 * One view per CRM module, rendered unchanged in every workspace (own, organisation, or a
 * user opened by an admin). Views read the workspace from the URL via useWorkspace() and
 * call /api/v1/workspaces/{segment}/...; they never branch on "am I an admin".
 *
 * Every view is keyed by its workspace segment (and record): moving from Rahul's workspace
 * to Priya's always starts from fresh component state, so nothing of Rahul's records,
 * filters or forms carries over (docs/admin-user-workspace.md#cache-isolation).
 *
 * Until the viewer has loaded, the workspace for top-level routes is unknown ("me" for a
 * sales user, "all" for an admin), so views render a skeleton rather than guess (and never
 * call an API with a guessed workspace). A URL that names no workspace (a malformed user id)
 * is "not found": it never falls back to anyone's records.
 *
 * Each module's views live in their own file next to this one, and pages import that file:
 * a page then loads only its own module's code (one shared file made every module page
 * load every module: 764 KB of JavaScript where Settings needs 323 KB).
 */

export function InWorkspace({ children }: { children: (workspace: Workspace, segment: string) => ReactNode }) {
  const workspace = useWorkspace();
  const viewer = useViewer();
  if (workspace === null) return <NotFoundView />;
  if (viewer === null) return <PageSkeleton />;
  return children(workspace, workspaceApiSegment(workspace));
}
