"use client";

import { LeadDetailView } from "@/features/leads/LeadDetailView";

import { InWorkspace } from "./InWorkspace";

/** A lead (read-only), reached from the dashboard, search and its opportunity (ADR-0028). */
export function LeadView({ leadId }: { leadId: string }) {
  return (
    <InWorkspace>
      {(workspace, segment) => <LeadDetailView key={`${segment}/${leadId}`} workspace={workspace} leadId={leadId} />}
    </InWorkspace>
  );
}
