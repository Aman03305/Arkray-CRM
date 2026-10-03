"use client";

import { CalendarCheck, LayoutDashboard } from "lucide-react";

import { Skeleton } from "@/components/ui/Skeleton";
import { LeadDetailView } from "@/features/leads/LeadDetailView";
import { LeadFormView } from "@/features/leads/LeadFormView";
import { LeadsListView } from "@/features/leads/LeadsListView";
import { OpportunityDetailView } from "@/features/pipeline/OpportunityDetailView";
import { OpportunityFormView } from "@/features/pipeline/OpportunityFormView";
import { PipelineBoardView } from "@/features/pipeline/PipelineBoardView";
import { AdminHome } from "@/features/users/AdminHome";
import { useWorkspace } from "@/lib/use-workspace";
import { useViewer } from "@/lib/viewer-context";
import { workspaceApiSegment } from "@/lib/workspace";

import { ModulePlaceholder } from "./ModulePlaceholder";

/*
 * One view per CRM module, rendered unchanged in every workspace (own, organisation, or a
 * user opened by an admin). Views read the workspace from the URL via useWorkspace() and
 * call /api/v1/workspaces/{segment}/...; they never branch on "am I an admin".
 * Each phase replaces a placeholder with the real view in its own feature folder.
 *
 * Until the viewer has loaded, the workspace for top-level routes is unknown ("me" for a
 * sales user, "all" for an admin), so views render a skeleton rather than guess (and never
 * call an API with a guessed workspace).
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
export function DashboardView() {
  // The organisation-wide dashboard is the administrator's home (ADR-0010).
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  if (workspace.kind === "organization") return <AdminHome />;
  return <ModulePlaceholder title="Dashboard" icon={LayoutDashboard} />;
}

// Pipeline views are keyed by workspace too: Rahul's board, filters and cards never carry
// over into Priya's (docs/pipeline.md#frontend).
export function PipelineView() {
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  return <PipelineBoardView key={workspaceApiSegment(workspace)} workspace={workspace} />;
}

// Lead views are keyed by workspace: moving from one user's workspace to another's always
// starts from fresh component state, so nothing of the previous user's leads carries over.
export function LeadsView() {
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  return <LeadsListView key={workspaceApiSegment(workspace)} workspace={workspace} />;
}

export function NewLeadView() {
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  return <LeadFormView key={workspaceApiSegment(workspace)} workspace={workspace} mode={{ kind: "create" }} />;
}

export function LeadView({ leadId }: { leadId: string }) {
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  return <LeadDetailView key={`${workspaceApiSegment(workspace)}/${leadId}`} workspace={workspace} leadId={leadId} />;
}

export function EditLeadView({ leadId }: { leadId: string }) {
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  return (
    <LeadFormView key={`${workspaceApiSegment(workspace)}/${leadId}`} workspace={workspace} mode={{ kind: "edit", leadId }} />
  );
}

export function OpportunityView({ opportunityId }: { opportunityId: string }) {
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  return (
    <OpportunityDetailView key={`${workspaceApiSegment(workspace)}/${opportunityId}`} workspace={workspace} opportunityId={opportunityId} />
  );
}

export function NewOpportunityView({ leadId }: { leadId?: string }) {
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  return <OpportunityFormView key={`${workspaceApiSegment(workspace)}/${leadId ?? ""}`} workspace={workspace} mode={{ kind: "create", leadId }} />;
}

export function EditOpportunityView({ opportunityId }: { opportunityId: string }) {
  const workspace = useWorkspace();
  if (useViewer() === null) return <ViewSkeleton />;
  return (
    <OpportunityFormView
      key={`${workspaceApiSegment(workspace)}/${opportunityId}`}
      workspace={workspace}
      mode={{ kind: "edit", opportunityId }}
    />
  );
}

export function ActivitiesView() {
  if (useViewer() === null) return <ViewSkeleton />;
  return <ModulePlaceholder title="Activities" icon={CalendarCheck} />;
}
