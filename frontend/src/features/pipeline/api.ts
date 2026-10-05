/**
 * Pipeline API client and query keys. Every call is scoped to a workspace
 * (/api/v1/workspaces/{me|all|userId}/...), and every cache key starts with the pipeline
 * root and that workspace segment, so one user's opportunities or totals can never be
 * served from the cache under another user's workspace.
 */
import { apiFetch } from "@/lib/api/client";
import type {
  Board,
  FieldInput,
  NegotiationPricePage,
  Opportunity,
  OpportunityCreateRequest,
  OpportunityMoveRequest,
  OpportunityOptions,
  OpportunityOrdering,
  OpportunityPage,
  OpportunityUpdateRequest,
  PipelineDto,
  PipelineList,
  StageHistoryPage,
  StageInput,
} from "@/lib/api/types";
import { type Workspace, workspaceApiPath, workspaceApiSegment } from "@/lib/workspace";

export const CARDS_PER_STAGE = 20;
export const PAGE_SIZE = 25;
/** How many recent open opportunities a picker lists before anything is typed. */
export const RECENT_OPEN_PAGE_SIZE = 20;

/** The board's filters (allowlisted server-side; owner only organisation-wide). */
export interface BoardFilters {
  pipeline: string;
  /** Inclusive business dates "YYYY-MM-DD". */
  closeFrom: string;
  closeTo: string;
  owner: string;
  ownerLabel: string;
}

export const NO_BOARD_FILTERS: BoardFilters = { pipeline: "", closeFrom: "", closeTo: "", owner: "", ownerLabel: "" };

export function activeBoardFilterCount(filters: BoardFilters): number {
  return [filters.closeFrom, filters.closeTo, filters.owner].filter(Boolean).length;
}

function filterParams(workspace: Workspace, filters: BoardFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.pipeline) params.set("pipeline", filters.pipeline);
  if (filters.closeFrom) params.set("expected_close_from", filters.closeFrom);
  if (filters.closeTo) params.set("expected_close_to", filters.closeTo);
  if (filters.owner && workspace.kind === "organization") params.set("owner", filters.owner);
  return params;
}

export function boardPath(workspace: Workspace, filters: BoardFilters, cardsPerStage = CARDS_PER_STAGE): string {
  const params = filterParams(workspace, filters);
  params.set("cards_per_stage", String(cardsPerStage));
  return `${workspaceApiPath(workspace, "pipeline-board")}?${params.toString()}`;
}

export interface StageListRequest {
  stage: string;
  ordering: OpportunityOrdering;
  cursor: string | null;
}

export function stageListPath(workspace: Workspace, filters: BoardFilters, request: StageListRequest): string {
  const params = filterParams(workspace, filters);
  params.set("stage", request.stage);
  params.set("ordering", request.ordering);
  params.set("page_size", String(PAGE_SIZE));
  if (request.cursor) params.set("cursor", request.cursor);
  return `${workspaceApiPath(workspace, "opportunities")}?${params.toString()}`;
}

/** What the opportunity form offers (the instruments): the same for everyone, and outside the
 * pipeline root, so CRM writes never refetch it. */
export const OPPORTUNITY_OPTIONS_KEY = ["opportunity-options"] as const;

export const pipelineKeys = {
  /** Everything workspace-scoped in the pipeline (invalidated after any CRM write). */
  all: ["pipeline"] as const,
  board: (workspace: Workspace, filters: BoardFilters) => ["pipeline", "board", workspaceApiSegment(workspace), filters] as const,
  boards: (workspace: Workspace) => ["pipeline", "board", workspaceApiSegment(workspace)] as const,
  stageList: (workspace: Workspace, filters: BoardFilters, request: StageListRequest) =>
    ["pipeline", "stage-list", workspaceApiSegment(workspace), filters, request] as const,
  stageLists: (workspace: Workspace) => ["pipeline", "stage-list", workspaceApiSegment(workspace)] as const,
  detail: (workspace: Workspace, id: string) => ["pipeline", "detail", workspaceApiSegment(workspace), id] as const,
  history: (workspace: Workspace, id: string, cursor: string | null) =>
    ["pipeline", "history", workspaceApiSegment(workspace), id, cursor] as const,
  /** Recent open opportunities, to choose one (a new task or meeting). */
  recentOpen: (workspace: Workspace) => ["pipeline", "recent-open", workspaceApiSegment(workspace)] as const,
  /** The pipelines this workspace may use (shared, its owner's own, and any holding its
   * deals): under the workspace root, so a user's pipelines never show in another's. */
  pipelines: (workspace: Workspace, archived = false) =>
    ["pipeline", "pipelines", workspaceApiSegment(workspace), archived ? "archived" : "active"] as const,
  pipeline: (workspace: Workspace, id: string) => ["pipeline", "pipeline", workspaceApiSegment(workspace), id] as const,
  negotiation: (workspace: Workspace, id: string) => ["pipeline", "negotiation", workspaceApiSegment(workspace), id] as const,
};

/** Extra details of a move (only for the targets that take them). */
export interface MoveDetails {
  lostReason?: string;
  /** Entering a negotiation stage: the negotiated price, an exact decimal string. */
  negotiatedPrice?: string;
}

const pipelinePath = (workspace: Workspace, id: string, action = "") =>
  workspaceApiPath(workspace, `pipelines/${encodeURIComponent(id)}${action ? `/${action}` : ""}`);

const opportunity = (workspace: Workspace, id: string, action = "") =>
  workspaceApiPath(workspace, `opportunities/${encodeURIComponent(id)}${action ? `/${action}` : ""}`);

export const pipelineApi = {
  options: () => apiFetch<OpportunityOptions>("/api/v1/config/opportunity-options"),
  pipelines: (workspace: Workspace, archived = false) =>
    apiFetch<PipelineList>(`${workspaceApiPath(workspace, "pipelines")}${archived ? "?archived=true" : ""}`),
  pipeline: (workspace: Workspace, id: string) => apiFetch<PipelineDto>(pipelinePath(workspace, id)),
  /** One request, fields included: a refused field leaves no pipeline behind. */
  createPipeline: (workspace: Workspace, name: string, stages: StageInput[], fields: FieldInput[] = []) =>
    apiFetch<PipelineDto>(workspaceApiPath(workspace, "pipelines"), {
      method: "POST",
      body: { name, stages, ...(fields.length ? { custom_fields: fields } : {}) },
    }),
  renamePipeline: (workspace: Workspace, id: string, version: number, name: string) =>
    apiFetch<PipelineDto>(pipelinePath(workspace, id), { method: "PATCH", body: { version, name } }),
  replaceStages: (workspace: Workspace, id: string, version: number, stages: StageInput[]) =>
    apiFetch<PipelineDto>(pipelinePath(workspace, id, "stages"), { method: "PUT", body: { version, stages } }),
  replaceFields: (workspace: Workspace, id: string, version: number, fields: FieldInput[]) =>
    apiFetch<PipelineDto>(pipelinePath(workspace, id, "fields"), { method: "PUT", body: { version, custom_fields: fields } }),
  archivePipeline: (workspace: Workspace, id: string, version: number) =>
    apiFetch<PipelineDto>(pipelinePath(workspace, id, "archive"), { method: "POST", body: { version } }),
  restorePipeline: (workspace: Workspace, id: string, version: number) =>
    apiFetch<PipelineDto>(pipelinePath(workspace, id, "restore"), { method: "POST", body: { version } }),
  board: (workspace: Workspace, filters: BoardFilters, cardsPerStage?: number) =>
    apiFetch<Board>(boardPath(workspace, filters, cardsPerStage)),
  stageList: (workspace: Workspace, filters: BoardFilters, request: StageListRequest) =>
    apiFetch<OpportunityPage>(stageListPath(workspace, filters, request)),
  recentOpen: (workspace: Workspace) =>
    apiFetch<OpportunityPage>(`${workspaceApiPath(workspace, "opportunities")}?status=open&page_size=${RECENT_OPEN_PAGE_SIZE}`),
  get: (workspace: Workspace, id: string) => apiFetch<Opportunity>(opportunity(workspace, id)),
  history: (workspace: Workspace, id: string, cursor: string | null) => {
    const params = new URLSearchParams({ page_size: "50" });
    if (cursor) params.set("cursor", cursor);
    return apiFetch<StageHistoryPage>(`${opportunity(workspace, id, "history")}?${params.toString()}`);
  },
  create: (workspace: Workspace, body: OpportunityCreateRequest, idempotencyKey: string) =>
    apiFetch<Opportunity>(workspaceApiPath(workspace, "opportunities"), {
      method: "POST",
      body,
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  update: (workspace: Workspace, id: string, body: OpportunityUpdateRequest) =>
    apiFetch<Opportunity>(opportunity(workspace, id), { method: "PATCH", body }),
  /** THE stage transition (drag and drop, "Move to stage", won, lost, reopen). */
  move: (workspace: Workspace, id: string, stage: string, version: number, details: MoveDetails = {}) => {
    const body: OpportunityMoveRequest = {
      stage,
      version,
      ...(details.lostReason ? { lost_reason: details.lostReason } : {}),
      ...(details.negotiatedPrice ? { negotiated_price: details.negotiatedPrice } : {}),
    };
    return apiFetch<Opportunity>(opportunity(workspace, id, "move"), { method: "POST", body });
  },
  negotiatedPrices: (workspace: Workspace, id: string) =>
    apiFetch<NegotiationPricePage>(`${opportunity(workspace, id, "negotiated-prices")}?page_size=50`),
  recordNegotiatedPrice: (workspace: Workspace, id: string, version: number, price: string) =>
    apiFetch<Opportunity>(opportunity(workspace, id, "negotiated-prices"), { method: "POST", body: { version, price } }),
  archive: (workspace: Workspace, id: string, version: number) =>
    apiFetch<Opportunity>(opportunity(workspace, id, "archive"), { method: "POST", body: { version } }),
  restore: (workspace: Workspace, id: string, version: number) =>
    apiFetch<Opportunity>(opportunity(workspace, id, "restore"), { method: "POST", body: { version } }),
  /** Change an open opportunity's owner: its customer, with its other open deals and
   * current work, moves to them (crm.assign_any). */
  assign: (workspace: Workspace, id: string, owner: string, version: number) =>
    apiFetch<Opportunity>(opportunity(workspace, id, "assign"), { method: "POST", body: { owner, version } }),
};
