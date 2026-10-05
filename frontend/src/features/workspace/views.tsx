"use client";

import type { ReactNode } from "react";

import { NotFoundView } from "@/components/ui/NotFoundView";
import { Skeleton } from "@/components/ui/Skeleton";
import { ActivitiesListView } from "@/features/activities/ActivitiesListView";
import { ActivityDetailView } from "@/features/activities/ActivityDetailView";
import { AskView } from "@/features/ask/AskView";
import { WorkspaceDashboard } from "@/features/dashboard/DashboardView";
import { OpportunityDetailView } from "@/features/pipeline/OpportunityDetailView";
import { PipelineBoardView } from "@/features/pipeline/PipelineBoardView";
import { AdminHome } from "@/features/users/AdminHome";
import { useWorkspace } from "@/lib/use-workspace";
import { canAsk } from "@/lib/viewer";
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
 */

function ViewSkeleton() {
  return (
    <div aria-busy="true" className="space-y-3">
      <Skeleton className="h-6 w-40" />
      <Skeleton className="h-4 w-64" />
      <span className="sr-only">Loading</span>
    </div>
  );
}

function InWorkspace({ children }: { children: (workspace: Workspace, segment: string) => ReactNode }) {
  const workspace = useWorkspace();
  const viewer = useViewer();
  if (workspace === null) return <NotFoundView />;
  if (viewer === null) return <ViewSkeleton />;
  return children(workspace, workspaceApiSegment(workspace));
}

// The organisation-wide dashboard is the administrator's home (ADR-0010).
export function DashboardView() {
  return (
    <InWorkspace>
      {(workspace, segment) =>
        workspace.kind === "organization" ? (
          <AdminHome key="all" />
        ) : (
          <WorkspaceDashboard key={segment} workspace={workspace} />
        )
      }
    </InWorkspace>
  );
}

export function PipelineView() {
  return <InWorkspace>{(workspace, segment) => <PipelineBoardView key={segment} workspace={workspace} />}</InWorkspace>;
}

export function OpportunityView({ opportunityId }: { opportunityId: string }) {
  return (
    <InWorkspace>
      {(workspace, segment) => (
        <OpportunityDetailView key={`${segment}/${opportunityId}`} workspace={workspace} opportunityId={opportunityId} />
      )}
    </InWorkspace>
  );
}

/** New opportunity: the board, with the new-opportunity panel open over it. */
export function NewOpportunityView() {
  return (
    <InWorkspace>
      {(workspace, segment) => <PipelineBoardView key={`${segment}/new`} workspace={workspace} create />}
    </InWorkspace>
  );
}

/** Edit an opportunity: its page, with the edit panel open over it. */
export function EditOpportunityView({ opportunityId }: { opportunityId: string }) {
  return (
    <InWorkspace>
      {(workspace, segment) => (
        <OpportunityDetailView key={`${segment}/${opportunityId}/edit`} workspace={workspace} opportunityId={opportunityId} editOnOpen />
      )}
    </InWorkspace>
  );
}

export function ActivitiesView() {
  return <InWorkspace>{(workspace, segment) => <ActivitiesListView key={segment} workspace={workspace} />}</InWorkspace>;
}

export function ActivityView({ activityId }: { activityId: string }) {
  return (
    <InWorkspace>
      {(workspace, segment) => (
        <ActivityDetailView key={`${segment}/${activityId}`} workspace={workspace} activityId={activityId} />
      )}
    </InWorkspace>
  );
}

// Ask Arkray: only for viewers who may ask, in a CRM that has it on (the API refuses
// otherwise too). Keyed by workspace like every module view.
export function AskWorkspaceView() {
  const viewer = useViewer();
  return (
    <InWorkspace>
      {(workspace, segment) => (canAsk(viewer) ? <AskView key={segment} workspace={workspace} /> : <NotFoundView />)}
    </InWorkspace>
  );
}
