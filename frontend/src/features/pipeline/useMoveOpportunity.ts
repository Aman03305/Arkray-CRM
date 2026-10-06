"use client";

import { type MutateOptions, type QueryKey, useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { useSingleFlight } from "@/components/ui/useSingleFlight";

import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Board, Opportunity, Stage } from "@/lib/api/types";
import type { Workspace } from "@/lib/workspace";

import type { AgreedTerms } from "./AgreedTerms";
import { pipelineApi, pipelineKeys } from "./api";
import { syncAfterOpportunityWrite } from "./hooks";
import { moveCardInBoard } from "./transitions";

export interface MoveRequest {
  id: string;
  title: string;
  version: number;
  target: Stage;
  lostReason?: string;
  /** Required when the target is a negotiation stage: the agreed price and CPT. */
  terms?: AgreedTerms;
}

/** Why a move failed. `fields`: a refusal's field errors, for the inputs the dialog shows (the
 * agreed price and CPT: the server may refuse what the browser accepted, e.g. invisible
 * characters in the CPT, review). */
export interface MoveProblem {
  message: string;
  requestId: string | null;
  fields?: Record<string, string[]>;
}

/** What went wrong, in words that say what the user sees now. */
export function moveErrorMessage(error: unknown, request: Pick<MoveRequest, "title" | "target">): MoveProblem {
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
  return { message: `Couldn't move "${request.title}" to ${request.target.name}: ${message}`, requestId, fields: fieldErrors(error) };
}

/** What a move remembers to put the board back if the server refuses it. */
type MoveContext = { key?: QueryKey; snapshot?: Board };

/**
 * The one stage-transition call, for every path (drag and drop, the "Move to" menu,
 * dialogs). With `boardKey` the card moves on the board immediately; if the server
 * refuses, the board it was moved on is put back exactly as it was (the key is captured
 * when the move starts: filters may change meanwhile, review) and then refreshed, so the
 * board never shows a state the server doesn't have.
 */
export function useMoveOpportunity(workspace: Workspace, boardKey?: QueryKey) {
  const queryClient = useQueryClient();
  const mutation = useMutation<Opportunity, unknown, MoveRequest, MoveContext>({
    mutationFn: ({ id, target, version, lostReason, terms }) => pipelineApi.move(workspace, id, target.id, version, { lostReason, terms }),
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
  // One move per card at a time, guarded synchronously: a second activation before the
  // pending state renders (a double click, Enter then a click in the dialog) would carry the
  // same version and come back 409, "changed by someone else", about the user's own first
  // move. A refused duplicate is dropped: the first one is already on its way.
  const flight = useSingleFlight();
  const { mutateAsync } = mutation;
  const mutate = useCallback(
    (request: MoveRequest, options?: MutateOptions<Opportunity, unknown, MoveRequest, MoveContext>) => {
      flight(() => mutateAsync(request, options), request.id);
    },
    [flight, mutateAsync],
  );
  return { ...mutation, mutate };
}
