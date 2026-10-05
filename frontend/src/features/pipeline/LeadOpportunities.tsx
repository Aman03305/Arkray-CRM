"use client";

import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { cursorOf } from "@/features/leads/api";
import { describeError } from "@/lib/api/errors";
import { formatPercent } from "@/lib/money";
import { newOpportunityHref, opportunityHref, type Workspace } from "@/lib/workspace";

import { pipelineApi, pipelineKeys } from "./api";
import { usePipelines } from "./hooks";
import { Amount, OutcomeBadge } from "./PipelineBits";

/**
 * A lead's opportunities in this workspace (newest first, 10 at a time). Won and lost
 * opportunities someone else closed before the lead was reassigned stay with them and are
 * not listed here (docs/pipeline.md#ownership).
 */
export function LeadOpportunities({ workspace, leadId, canCreate }: { workspace: Workspace; leadId: string; canCreate: boolean }) {
  const [cursor, setCursor] = useState<string | null>(null);
  const pipelines = usePipelines(workspace);
  const list = useQuery({
    queryKey: pipelineKeys.forLead(workspace, leadId, cursor),
    queryFn: () => pipelineApi.forLead(workspace, leadId, cursor),
  });
  const stageName = (stageId: string) =>
    pipelines.data?.results.flatMap((p) => p.stages).find((s) => s.id === stageId)?.name ?? "";
  const rows = list.data?.results;
  const more = Boolean(list.data?.next || list.data?.previous);

  return (
    <section aria-labelledby="lead-opportunities-heading" className="rounded-lg border border-slate-200 bg-white p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id="lead-opportunities-heading" className="text-sm font-semibold text-slate-900">
          Opportunities{rows && !more ? ` (${rows.length})` : ""}
        </h2>
        {canCreate ? (
          <Link
            href={newOpportunityHref(workspace, leadId)}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-sm font-medium text-brand-700 hover:bg-brand-50"
          >
            <Plus aria-hidden="true" className="size-4" />
            Opportunity
          </Link>
        ) : null}
      </div>
      {list.isError ? (
        <p className="text-sm text-red-700">Opportunities couldn&apos;t be loaded. {describeError(list.error).message}</p>
      ) : !rows ? (
        <Skeleton className="h-12 w-full" />
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-500">No opportunities yet.</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {rows.map((row) => (
            <li key={row.id} className="py-2.5 first:pt-0 last:pb-0">
              <Link href={opportunityHref(workspace, row.id)} className="text-sm font-medium text-brand-700 hover:underline">
                {row.title}
              </Link>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-600">
                <Amount value={row.value} className="font-medium text-slate-900" />
                {row.status === "open" ? (
                  <>
                    <span>{stageName(row.stage_id)}</span>
                    <span>{formatPercent(row.probability)}</span>
                  </>
                ) : (
                  <>
                    <OutcomeBadge status={row.status} />
                    {stageName(row.stage_id).toLowerCase() !== row.status ? <span>{stageName(row.stage_id)}</span> : null}
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {more ? (
        <nav aria-label="Opportunity pages" className="mt-3 flex justify-end gap-2">
          <Button variant="secondary" size="sm" disabled={!list.data?.previous} onClick={() => setCursor(cursorOf(list.data?.previous))}>
            Previous
          </Button>
          <Button variant="secondary" size="sm" disabled={!list.data?.next} onClick={() => setCursor(cursorOf(list.data?.next))}>
            Next
          </Button>
        </nav>
      ) : null}
    </section>
  );
}
