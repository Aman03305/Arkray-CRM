"use client";

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState, useSyncExternalStore } from "react";

import type { Board, Opportunity, OpportunityCard, OpportunityPage, PipelineDto, Stage } from "@/lib/api/types";
import { hasCapability, type Viewer } from "@/lib/viewer";
import type { Workspace } from "@/lib/workspace";

import { type BoardFilters, NO_BOARD_FILTERS, pipelineApi, pipelineKeys } from "./api";
import { moveCardInBoard } from "./transitions";

/** Pipelines and their stages: configuration, cached for the session. */
export function usePipelines() {
  return useQuery({ queryKey: pipelineKeys.pipelines, queryFn: pipelineApi.pipelines, staleTime: 10 * 60_000 });
}

export function defaultPipeline(pipelines: readonly PipelineDto[] | undefined): PipelineDto | undefined {
  return pipelines?.find((p) => p.is_default) ?? pipelines?.find((p) => p.is_active);
}

export function activeStages(pipeline: PipelineDto | undefined): Stage[] {
  return (pipeline?.stages ?? []).filter((s) => s.is_active);
}

export function firstOpenStage(pipeline: PipelineDto | undefined): Stage | undefined {
  return activeStages(pipeline).find((s) => s.category === "open");
}

export interface PipelinePermissions {
  /** Create, edit, move, archive and restore opportunities in this workspace. */
  canWrite: boolean;
}

/**
 * What the UI offers in this workspace (mirroring identity.workspaces.authorize_write on
 * the server). Nobody picks an opportunity's owner (it follows the lead), so creating needs
 * no assigning rights. Presentation only: the API decides.
 */
export function pipelinePermissions(viewer: Viewer | null, workspace: Workspace): PipelinePermissions {
  return { canWrite: hasCapability(viewer, workspace.kind === "self" ? "crm.access_own" : "crm.manage_any") };
}

/** The card as the server now has it (version included), from its full representation. */
function asCard(opportunity: Opportunity, previous: OpportunityCard): OpportunityCard {
  return {
    ...previous,
    title: opportunity.title,
    lead: opportunity.lead,
    owner: opportunity.owner,
    stage_id: opportunity.stage.id,
    status: opportunity.status,
    value: opportunity.value,
    probability: opportunity.probability,
    probability_overridden: opportunity.probability_overridden,
    weighted_value: opportunity.weighted_value,
    expected_close_date: opportunity.expected_close_date,
    closed_at: opportunity.closed_at,
    archived_at: opportunity.archived_at,
    version: opportunity.version,
    updated_at: opportunity.updated_at,
  };
}

/**
 * Put a changed opportunity into every cached board and stage list of this workspace at
 * once (its new version above all), before they reload: a move made before the reload
 * lands must not send the old version (review, and the live walkthrough: a card moved
 * on its own page and then dragged on the board got a misleading "changed by someone
 * else"). An archived opportunity leaves them. Money totals are left to the reload.
 */
export function patchCachedCards(queryClient: QueryClient, workspace: Workspace, opportunity: Opportunity): void {
  for (const [key, board] of queryClient.getQueriesData<Board>({ queryKey: pipelineKeys.boards(workspace) })) {
    const current = board?.columns.flatMap((c) => c.cards).find((c) => c.id === opportunity.id);
    if (!board || !current) continue;
    const moved = current.stage_id === opportunity.stage.id ? board : moveCardInBoard(board, opportunity.id, opportunity.stage);
    queryClient.setQueryData<Board>(key, {
      ...moved,
      columns: moved.columns.map((column) => ({
        ...column,
        count: opportunity.archived_at && column.cards.some((c) => c.id === opportunity.id) ? Math.max(0, column.count - 1) : column.count,
        cards: opportunity.archived_at
          ? column.cards.filter((c) => c.id !== opportunity.id)
          : column.cards.map((card) => (card.id === opportunity.id ? asCard(opportunity, card) : card)),
      })),
    });
  }
  for (const [key, page] of queryClient.getQueriesData<OpportunityPage>({ queryKey: pipelineKeys.stageLists(workspace) })) {
    if (!page?.results.some((c) => c.id === opportunity.id)) continue;
    const stage = (key[4] as { stage?: string } | undefined)?.stage;
    const leaves = opportunity.archived_at !== null || (stage !== undefined && stage !== opportunity.stage.id);
    queryClient.setQueryData<OpportunityPage>(key, {
      ...page,
      results: leaves
        ? page.results.filter((c) => c.id !== opportunity.id)
        : page.results.map((c) => (c.id === opportunity.id ? asCard(opportunity, c) : c)),
    });
  }
}

/**
 * After any opportunity write: cache the returned opportunity for this workspace, patch it
 * into cached boards and stage lists, and mark every pipeline query stale (boards, totals,
 * stage lists, a lead's opportunities, history), so no page shows it as it was. (Writes
 * that change a lead, such as a conversion, sync the lead's queries themselves.)
 */
export function syncAfterOpportunityWrite(queryClient: QueryClient, workspace: Workspace, opportunity?: Opportunity): void {
  const key = opportunity ? pipelineKeys.detail(workspace, opportunity.id) : null;
  if (opportunity && key) {
    queryClient.setQueryData(key, opportunity);
    patchCachedCards(queryClient, workspace, opportunity);
  }
  void queryClient.invalidateQueries({
    queryKey: pipelineKeys.all,
    predicate: (query) => key === null || JSON.stringify(query.queryKey) !== JSON.stringify(key),
  });
}

export function useOpportunityWriteSync(workspace: Workspace) {
  const queryClient = useQueryClient();
  return useCallback(
    (opportunity?: Opportunity) => syncAfterOpportunityWrite(queryClient, workspace, opportunity),
    [queryClient, workspace],
  );
}

// --- the board's filters and stage view, remembered per workspace for this page load -------
// In memory only (like the Leads list): an admin's filters in Rahul's pipeline never carry
// over to Priya's, and nothing lands in history or storage that outlives a sign-out.
interface BoardState {
  /** What the filter controls show. */
  filters: BoardFilters;
  /** The last valid filters: what is actually requested (an inverted date range is shown
   * as a problem next to the dates and never sent, so the board stays as it was). */
  applied: BoardFilters;
  /** The stage shown as a list (phones always; desktop after "View all"), or null. */
  stage: string | null;
}

const remembered = new Map<string, BoardState>();
const INITIAL: BoardState = { filters: NO_BOARD_FILTERS, applied: NO_BOARD_FILTERS, stage: null };

export function invalidRange(filters: BoardFilters): boolean {
  return Boolean(filters.closeFrom && filters.closeTo && filters.closeFrom > filters.closeTo);
}

export function useBoardState(workspaceKey: string) {
  const [state, setState] = useState<BoardState>(() => remembered.get(workspaceKey) ?? INITIAL);
  const update = useCallback(
    (next: BoardState) => {
      remembered.set(workspaceKey, next);
      setState(next);
    },
    [workspaceKey],
  );
  const setFilters = (filters: BoardFilters) =>
    update({ ...state, filters, applied: invalidRange(filters) ? state.applied : filters });
  return {
    filters: state.filters,
    applied: state.applied,
    rangeInvalid: invalidRange(state.filters),
    stage: state.stage,
    setFilters: (patch: Partial<BoardFilters>) => setFilters({ ...state.filters, ...patch }),
    resetFilters: () => setFilters({ ...NO_BOARD_FILTERS, pipeline: state.filters.pipeline }),
    setStage: (stage: string | null) => update({ ...state, stage }),
  };
}

/** Tests only. */
export function forgetBoardState(): void {
  remembered.clear();
}

// --- layout ----------------------------------------------------------------------------------
const WIDE = "(min-width: 1024px)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => undefined;
  const query = window.matchMedia(WIDE);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

/** Wide enough for the board's columns (the sidebar layout breakpoint); phones and small
 * tablets get stage tabs with one stage's list instead of six squeezed columns. */
export function useWideLayout(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia(WIDE).matches : true),
    () => true,
  );
}
