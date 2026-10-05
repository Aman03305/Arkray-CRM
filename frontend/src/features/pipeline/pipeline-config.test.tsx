import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NewOpportunityView, PipelineView } from "@/features/workspace/views";
import type { PipelineDto, PipelineList } from "@/lib/api/types";
import { LEAD_ID, makeLead, makeLeadListItem, salesViewer } from "@/test/fixtures";
import { makeBoard, makeOpportunity, PIPELINE, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

import { forgetBoardState } from "./hooks";

const nav = vi.hoisted(() => ({ pathname: "/pipeline", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const PIPELINES_URL = "/api/v1/workspaces/me/pipelines";
const BOARD = "/api/v1/workspaces/me/pipeline-board";
const MINE_ID = "0e0f5a61-2222-4a2b-8c3d-000000000002";

const MINE: PipelineDto = {
  ...PIPELINE,
  id: MINE_ID,
  key: "pmine",
  name: "Government Tender",
  owner: { id: "u1", full_name: "Priya Patel", is_active: true },
  is_default: false,
  version: 4,
  can_manage: true,
  custom_fields: [
    { id: "f-tender", name: "Tender number", type: "text", required: true, options: [], position: 0 },
    {
      id: "f-segment",
      name: "Segment",
      type: "single_select",
      required: false,
      options: [
        { id: "o-hosp", label: "Hospital" },
        { id: "o-lab", label: "Lab" },
      ],
      position: 1,
    },
  ],
};
const LIST: PipelineList = { results: [PIPELINE, MINE] };

beforeEach(() => {
  nav.pathname = "/pipeline";
  nav.push.mockReset();
  nav.replace.mockReset();
  forgetBoardState();
});

describe("choosing and configuring pipelines", () => {
  it("switches the board between the workspace's pipelines and offers settings only where allowed", async () => {
    const api = mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: LIST },
      [`GET ${BOARD}`]: (call) => ({
        status: 200,
        body: makeBoard([], { pipeline: call.query.get("pipeline") === MINE_ID ? { id: MINE_ID, key: "pmine", name: MINE.name } : { id: PIPELINE.id, key: "sales", name: PIPELINE.name } }),
      }),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const picker = await screen.findByRole("combobox", { name: "Pipeline" });
    await waitFor(() => expect(picker).toHaveValue(PIPELINE.id));
    // The shared default pipeline is configured by administrators: no settings here.
    expect(screen.queryByRole("button", { name: "Pipeline settings" })).not.toBeInTheDocument();
    await user.selectOptions(picker, MINE_ID);
    await waitFor(() => expect(api.calls.some((c) => c.path === BOARD && c.query.get("pipeline") === MINE_ID)).toBe(true));
    expect(await screen.findByRole("button", { name: "Pipeline settings" })).toBeInTheDocument();
  });

  it("creates a pipeline from an editable template, with stages reordered by buttons", async () => {
    const created: PipelineDto = { ...MINE, id: "new-id", name: "Diagnostics Sales", custom_fields: [] };
    const api = mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: { results: [PIPELINE] } },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) },
      [`POST ${PIPELINES_URL}`]: { status: 201, body: created },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New pipeline" }));
    const panel = screen.getByRole("dialog", { name: "New pipeline" });
    expect(within(panel).getByLabelText("Name")).toHaveFocus();
    await user.type(within(panel).getByLabelText("Name"), "Diagnostics Sales");
    // Rename "Proposal" to "Demo", move it above "Qualified", remove "Lost".
    const proposal = within(panel).getByLabelText("Stage 3 name");
    await user.clear(proposal);
    await user.type(proposal, "Demo");
    await user.click(within(panel).getByRole("button", { name: "Move Demo up" }));
    expect(within(panel).getByRole("button", { name: "Move Demo up" })).toHaveFocus(); // keyboard keeps its place
    await user.click(within(panel).getByRole("button", { name: "Remove Lost" }));
    await user.click(within(panel).getByRole("button", { name: "Create pipeline" }));
    await waitFor(() => expect(api.callsTo("POST", PIPELINES_URL)).toHaveLength(1));
    expect(api.callsTo("POST", PIPELINES_URL)[0]!.body).toEqual({
      name: "Diagnostics Sales",
      stages: [
        { name: "New", type: "open", probability: "10" },
        { name: "Demo", type: "open", probability: "50" },
        { name: "Qualified", type: "open", probability: "25" },
        { name: "Negotiation", type: "negotiation", probability: "75" },
        { name: "Won", type: "won" },
      ],
    });
    expect(await screen.findByText("Pipeline “Diagnostics Sales” created.")).toBeInTheDocument();
  });

  it("shows the server's reasons next to the stages and never loses the edit", async () => {
    mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: LIST },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([], { pipeline: { id: MINE_ID, key: "pmine", name: MINE.name } }) },
      [`PUT ${PIPELINES_URL}/${MINE_ID}/stages`]: apiError(422, "business_rule_violation", "“Proposal” has 2 opportunities. Move them to another stage before removing it."),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Pipeline" }), MINE_ID);
    await user.click(await screen.findByRole("button", { name: "Pipeline settings" }));
    const panel = screen.getByRole("dialog", { name: "Pipeline settings" });
    await user.click(within(panel).getByRole("button", { name: "Remove Proposal" }));
    await user.click(within(panel).getByRole("button", { name: "Save" }));
    expect(await within(panel).findByText(/has 2 opportunities/)).toBeInTheDocument();
    expect(within(panel).queryByLabelText("Proposal type")).not.toBeInTheDocument(); // still removed in the form
  });
});

describe("the new-opportunity panel", () => {
  it("opens over the board, groups the fields, fills the customer from the lead and sends exact values", async () => {
    const api = mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: LIST },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([], { pipeline: { id: MINE_ID, key: "pmine", name: MINE.name } }) },
      "GET /api/v1/workspaces/me/leads": { status: 200, body: { results: [makeLeadListItem()], next: null, previous: null } },
      [`GET /api/v1/workspaces/me/leads/${LEAD_ID}`]: {
        status: 200,
        body: makeLead({ organization_name: "Apollo Diagnostics", email: "asha@apollo.example", mobile: "+91 98765 43210" }),
      },
      "POST /api/v1/workspaces/me/opportunities": { status: 201, body: makeOpportunity({ title: "HbA1c analyser" }) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Pipeline" }), MINE_ID);
    await user.click(screen.getAllByRole("button", { name: "New opportunity" })[0]!); // toolbar (the empty board offers it too)
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    // The board stays behind it; the panel's sections:
    for (const group of ["Basic", "Customer", "Instrument", "Closing", "Additional"]) {
      expect(within(panel).getByRole("heading", { name: group })).toBeInTheDocument();
    }
    await user.selectOptions(await within(panel).findByLabelText("Lead"), LEAD_ID);
    await waitFor(() => expect(within(panel).getByLabelText("Account name")).toHaveValue("Apollo Diagnostics"));
    expect(within(panel).getByLabelText("Email (optional)")).toHaveValue("asha@apollo.example");
    await user.type(within(panel).getByLabelText("Opportunity name"), "HbA1c analyser");
    await user.type(within(panel).getByLabelText("Installation price (₹)"), "12,50,000");
    await user.type(within(panel).getByLabelText("Work load (optional)"), "300 tests/day");
    // The pipeline's custom fields, required ones checked here too.
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    expect(within(panel).getByLabelText("Tender number")).toHaveAttribute("aria-invalid", "true");
    expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")).toHaveLength(0);
    await user.type(within(panel).getByLabelText("Tender number"), "GEM/2026/1");
    await user.selectOptions(within(panel).getByLabelText("Segment (optional)"), "o-lab");
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")).toHaveLength(1));
    const body = api.callsTo("POST", "/api/v1/workspaces/me/opportunities")[0]!.body as Record<string, unknown>;
    expect(body).toMatchObject({
      lead: LEAD_ID,
      title: "HbA1c analyser",
      value: "1250000",
      pipeline: MINE_ID,
      account_name: "Apollo Diagnostics",
      contact_email: "asha@apollo.example",
      work_load: "300 tests/day",
      custom_fields: { "f-tender": "GEM/2026/1", "f-segment": "o-lab" },
    });
    expect(await screen.findByText("“HbA1c analyser” created.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "New opportunity" })).not.toBeInTheDocument();
  });

  it("a negotiation stage at creation asks for the negotiated price", async () => {
    mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: { results: [PIPELINE] } },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "New opportunity" }))[0]!);
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    expect(within(panel).queryByLabelText("Negotiated price (₹)")).not.toBeInTheDocument();
    await user.selectOptions(within(panel).getByLabelText("Stage"), STAGES.negotiation.id);
    expect(within(panel).getByLabelText("Negotiated price (₹)")).toBeInTheDocument();
  });

  it("the /new route opens the panel over the board and closing returns to the board", async () => {
    mockApi({
      [`GET ${PIPELINES_URL}`]: { status: 200, body: { results: [PIPELINE] } },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) },
    });
    nav.pathname = "/pipeline/new";
    renderWithProviders(<NewOpportunityView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const panel = await screen.findByRole("dialog", { name: "New opportunity" });
    expect(screen.getByRole("heading", { name: "Pipeline", hidden: true })).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Cancel" }));
    expect(nav.replace).toHaveBeenCalledWith("/pipeline");
  });
});
