import type { Board, BoardColumn, Opportunity, OpportunityCard, PipelineDto, PipelineList, Stage } from "@/lib/api/types";

import { LEAD_ID, PRIYA_ID, RAHUL_ID } from "./fixtures";

export const PIPELINE_ID = "0e0f5a61-1111-4a2b-8c3d-000000000001";
export const OPPORTUNITY_ID = "7c6b5a49-3827-4e1f-9d0c-b1a2c3d4e5f6";
export const OTHER_OPPORTUNITY_ID = "7c6b5a49-3827-4e1f-9d0c-b1a2c3d4e5f7";

const stage = (
  n: number,
  key: string,
  name: string,
  probability: string,
  category: Stage["category"],
  negotiation = false,
): Stage => ({
  id: `5ea9e000-0000-4000-8000-00000000000${n}`,
  key,
  name,
  position: n * 10,
  probability,
  category,
  type: negotiation ? "negotiation" : category,
  is_negotiation: negotiation,
  is_active: true,
});

export const STAGES = {
  new: stage(1, "new", "New", "10.00", "open"),
  qualified: stage(2, "qualified", "Qualified", "25.00", "open"),
  proposal: stage(3, "proposal", "Proposal", "50.00", "open"),
  negotiation: stage(4, "negotiation", "Negotiation", "75.00", "open", true),
  won: stage(5, "won", "Won", "100.00", "won"),
  lost: stage(6, "lost", "Lost", "0.00", "lost"),
};
export const STAGE_LIST: Stage[] = Object.values(STAGES);

export const PIPELINE: PipelineDto = {
  id: PIPELINE_ID,
  key: "sales",
  name: "Sales Pipeline",
  owner: null,
  is_default: true,
  is_active: true,
  version: 1,
  can_manage: false,
  stages: STAGE_LIST,
  custom_fields: [],
};

export const PIPELINES: PipelineList = { results: [PIPELINE] };

/** The pipelines of the workspaces tests use (own, organisation, Rahul's, Priya's). */
export const PIPELINE_ROUTES = Object.fromEntries(
  ["me", "all", RAHUL_ID, PRIYA_ID].map((workspace) => [`GET /api/v1/workspaces/${workspace}/pipelines`, { status: 200, body: PIPELINES }]),
);

export function makeCard(overrides: Partial<OpportunityCard> = {}): OpportunityCard {
  const base: OpportunityCard = {
    id: OPPORTUNITY_ID,
    title: "Hospital Analyzer Project",
    lead: { id: LEAD_ID, display_name: "Asha Mehta", organization_name: "Apollo Diagnostics", restricted: false },
    owner: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true },
    account_name: "Apollo Diagnostics",
    stage_id: STAGES.proposal.id,
    status: "open",
    value: "1250000.00",
    probability: "50.00",
    probability_overridden: false,
    weighted_value: "625000.00",
    expected_close_date: "2026-12-15",
    negotiated_price: null,
    closed_at: null,
    archived_at: null,
    version: 2,
    created_at: "2026-09-20T04:30:00Z",
    updated_at: "2026-09-21T04:30:00Z",
  };
  return { ...base, ...overrides };
}

export function makeOpportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  const card = Object.fromEntries(Object.entries(makeCard()).filter(([key]) => key !== "stage_id")) as Omit<OpportunityCard, "stage_id">;
  const base: Opportunity = {
    ...card,
    pipeline: { id: PIPELINE_ID, key: "sales", name: "Sales Pipeline" },
    stage: STAGES.proposal,
    opportunity_date: "2026-09-20",
    customer_name: "Asha Mehta",
    contact_phone: "+91 98765 43210",
    contact_email: "asha@apollo.example",
    address: "12 MG Road, Bengaluru",
    instrument_name: "HbA1c analyser",
    work_load: "300 tests/day",
    custom_fields: {},
    negotiated_at: null,
    description: "Two analysers for the central lab.",
    lost_reason: "",
    created_by: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true },
  };
  return { ...base, ...overrides };
}

export function column(stageKey: keyof typeof STAGES, cards: OpportunityCard[] = [], overrides: Partial<BoardColumn> = {}): BoardColumn {
  const s = STAGES[stageKey];
  return {
    stage: s,
    count: cards.length,
    total_value: cards.length ? cards[0]!.value : "0.00",
    weighted_value: cards.length ? cards[0]!.weighted_value : "0.00",
    ordering: s.category === "open" ? "expected_close" : "-closed_at",
    cards,
    next: null,
    ...overrides,
  };
}

/** A board whose columns hold `cards` by their stage_id. */
export function makeBoard(cards: OpportunityCard[] = [makeCard()], overrides: Partial<Board> = {}): Board {
  const open = cards.filter((c) => c.status === "open");
  return {
    pipeline: { id: PIPELINE_ID, key: "sales", name: "Sales Pipeline" },
    currency: "INR",
    totals: {
      pipeline_value: open.length ? "1250000.00" : "0.00",
      weighted_pipeline: open.length ? "625000.00" : "0.00",
      open_count: open.length,
    },
    columns: (Object.keys(STAGES) as (keyof typeof STAGES)[]).map((key) =>
      column(
        key,
        cards.filter((c) => c.stage_id === STAGES[key].id),
      ),
    ),
    ...overrides,
  };
}
