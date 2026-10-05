"use client";

import { type QueryKey, useMutation, useQueryClient } from "@tanstack/react-query";

import { describeError, isApiError } from "@/lib/api/errors";
import type { Board, Opportunity, Stage } from "@/lib/api/types";
import type { Workspace } from "@/lib/workspace";

import { pipelineApi, pipelineKeys } from "./api";
import { syncAfterOpportunityWrite } from "./hooks";
import { moveCardInBoard } from "./transitions";

export interface MoveRequest {
  id: string;
  title: string;
  version: number;
  target: Stage;
  lostReason?: string;
  /** Required when the target is a negotiation stage (exact decimal string). */
  negotiatedPrice?: string;
}

/** What went wrong, in words that say what the user sees now. */
export function moveErrorMessage(error: unknown, request: Pick<MoveRequest, "title" | "target">): { message: string; requestId: string | null } {
  if (isApiError(error, 409)) {
    return {
      message: `"${request.title}" was changed by someone else a moment ago, so it wasn't moved. The pipeline shows the latest version now; please try again.`,
      requestId: null,
    };
  }
  if (isApiError(error, 404)) {
    return { message: `"${request.title}" is no longer in this workspace (it may have been reassigned or archived).`, requestId: null };
  }
  const { message, requestId } = describeError(error);
  if (isApiError(error, 0)) {
    return { message: `Couldn't move "${request.title}": ${message} It is back where it was.`, requestId };
  }
  return { message: `Couldn't move "${request.title}" to ${request.target.name}: ${message}`, requestId };
}

/**
 * The one stage-transition call, for every path (drag and drop, the "Move to" menu,
 * dialogs). With `boardKey` the card moves on the board immediately; if the server
 * refuses, the board it was moved on is put back exactly as it was (the key is captured
 * when the move starts: filters may change meanwhile, review) and then refreshed, so the
 * board never shows a state the server doesn't have.
 */
export function useMoveOpportunity(workspace: Workspace, boardKey?: QueryKey) {
  const queryClient = useQueryClient();
  return useMutation<Opportunity, unknown, MoveRequest, { key?: QueryKey; snapshot?: Board }>({
    mutationFn: ({ id, target, version, lostReason, negotiatedPrice }) =>
      pipelineApi.move(workspace, id, target.id, version, { lostReason, negotiatedPrice }),
    onMutate: async ({ id, target }) => {
      if (!boardKey) return {};
      await queryClient.cancelQueries({ queryKey: boardKey });
      const snapshot = queryClient.getQueryData<Board>(boardKey);
      if (snapshot) queryClient.setQueryData<Board>(boardKey, moveCardInBoard(snapshot, id, target));
      return { key: boardKey, snapshot };
    },
    onError: (_error, _request, context) => {
      if (context?.key && context.snapshot) queryClient.setQueryData(context.key, context.snapshot);
    },
    onSuccess: (opportunity) => syncAfterOpportunityWrite(queryClient, workspace, opportunity),
    // Success or not, reload what the server has (counts, totals, order, versions).
    onSettled: () => void queryClient.invalidateQueries({ queryKey: pipelineKeys.all }),
  });
}
