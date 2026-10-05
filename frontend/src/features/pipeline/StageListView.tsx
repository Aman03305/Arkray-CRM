"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { cursorOf } from "@/lib/api/pagination";
import { describeError } from "@/lib/api/errors";
import type { BoardColumn, OpportunityCard, Stage } from "@/lib/api/types";
import { type Workspace, workspaceApiSegment } from "@/lib/workspace";

import { type BoardFilters, pipelineApi, pipelineKeys } from "./api";
import { OpportunityCardView } from "./OpportunityCardView";

interface StageListViewProps {
  workspace: Workspace;
  filters: BoardFilters;
  column: BoardColumn;
  stages: readonly Stage[];
  showOwner: boolean;
  canWrite: boolean;
  movingId: string | null;
  onMove: (card: OpportunityCard, target: Stage) => void;
  today: string;
}

/**
 * Every opportunity of one stage, 25 at a time, in the board's order for that stage: the
 * phone layout (one stage at a time) and "View all" for a crowded column. Keyset pages, so
 * a stage with thousands of opportunities never loads them all.
 */
export function StageListView({ workspace, filters, column, stages, showOwner, canWrite, movingId, onMove, today }: StageListViewProps) {
  const [cursor, setCursor] = useState<string | null>(null);
  const request = { stage: column.stage.id, ordering: column.ordering, cursor };
  const segment = workspaceApiSegment(workspace);
  const page = useQuery({
    queryKey: pipelineKeys.stageList(workspace, filters, request),
    queryFn: () => pipelineApi.stageList(workspace, filters, request),
    // Keep the previous page on screen while the next loads, but only of this workspace
    // and this stage: never one user's (or stage's) cards under another's heading.
    placeholderData: (previous, query) => {
      const previousRequest = query?.queryKey[4] as { stage?: string } | undefined;
      return query?.queryKey[2] === segment && previousRequest?.stage === column.stage.id ? previous : undefined;
    },
  });

  if (page.isError && !page.data) {
    const { message, requestId } = describeError(page.error);
    return (
      <Alert
        tone="error"
        title={`${column.stage.name} couldn't be loaded`}
        requestId={requestId}
        action={
          <Button variant="secondary" size="sm" onClick={() => void page.refetch()} loading={page.isFetching}>
            Try again
          </Button>
        }
      >
        {message}
      </Alert>
    );
  }
  const cards = page.data?.results;
  return (
    <div aria-busy={page.isFetching || undefined}>
      {!cards ? (
        <div className="space-y-2">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <span className="sr-only">Loading {column.stage.name}</span>
        </div>
      ) : cards.length === 0 ? (
        <p className="rounded-md border border-dashed border-slate-300 bg-white px-4 py-8 text-center text-sm text-slate-500">
          {cursor ? "No more opportunities in this stage." : `No opportunities in ${column.stage.name}.`}
        </p>
      ) : (
        <ul className="space-y-2" aria-label={`${column.stage.name} opportunities`}>
          {cards.map((card) => (
            <li key={card.id}>
              <OpportunityCardView
                card={card}
                workspace={workspace}
                stages={stages}
                showOwner={showOwner}
                canWrite={canWrite}
                moving={movingId === card.id}
                onMove={onMove}
                today={today}
              />
            </li>
          ))}
        </ul>
      )}
      <nav aria-label={`${column.stage.name} pages`} className="mt-3 flex items-center justify-end gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={!page.data?.previous || page.isFetching}
          onClick={() => setCursor(cursorOf(page.data?.previous))}
        >
          Previous
        </Button>
        <Button variant="secondary" size="sm" disabled={!page.data?.next || page.isFetching} onClick={() => setCursor(cursorOf(page.data?.next))}>
          Next
        </Button>
      </nav>
    </div>
  );
}
