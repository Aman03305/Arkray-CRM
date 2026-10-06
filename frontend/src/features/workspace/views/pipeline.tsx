"use client";

import { OpportunityDetailView } from "@/features/pipeline/OpportunityDetailView";
import { PipelineBoardView } from "@/features/pipeline/PipelineBoardView";

import { InWorkspace } from "./InWorkspace";

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
