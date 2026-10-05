import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NewOpportunityView, PipelineView } from "@/features/workspace/views";
import type { PipelineDto, PipelineList } from "@/lib/api/types";
import { adminViewer, INSTRUMENT_NAMES, OPPORTUNITY_OPTIONS_ROUTE, PRIYA_ID, salesViewer } from "@/test/fixtures";
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
  it("opens over the board, groups the fields, takes the customer's details and sends exact values", async () => {
    const api = mockApi({
      ...OPPORTUNITY_OPTIONS_ROUTE,
      [`GET ${PIPELINES_URL}`]: { status: 200, body: LIST },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([], { pipeline: { id: MINE_ID, key: "pmine", name: MINE.name } }) },
      // The server names the opportunity after its customer and instrument (ADR-0028).
      "POST /api/v1/workspaces/me/opportunities": { status: 201, body: makeOpportunity({ title: "Asha Mehta — Adams 8180 V" }) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Pipeline" }), MINE_ID);
    await user.click(screen.getAllByRole("button", { name: "New opportunity" })[0]!); // toolbar (the empty board offers it too)
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    // The board stays behind it; the panel's sections, in order (Additional: this pipeline has
    // custom fields).
    expect(within(panel).getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "Customer",
      "Instrument",
      "Timeline",
      "Pipeline",
      "Additional",
    ]);
    // Nobody names the opportunity, picks a lead, sets an own probability or describes it here.
    expect(within(panel).queryByLabelText(/Opportunity name/)).not.toBeInTheDocument();
    expect(within(panel).queryByRole("checkbox", { name: /Own probability/ })).not.toBeInTheDocument();
    expect(within(panel).queryByLabelText(/Description/)).not.toBeInTheDocument();
    // The board's pipeline is preselected, and its stages are offered by name.
    expect(within(panel).getByRole("combobox", { name: "Pipeline" })).toHaveValue(MINE_ID);
    expect(within(within(panel).getByRole("combobox", { name: "Stage" })).getAllByRole("option").map((o) => o.textContent)).toEqual(
      ["New", "Qualified", "Proposal", "Negotiation", "Won", "Lost"],
    );
    // The customer's details are typed here: they are the deal's customer, and its lead is made
    // from them (ADR-0028).
    await user.type(within(panel).getByLabelText("Account name"), "Apollo Diagnostics");
    await user.type(within(panel).getByLabelText("Customer name"), "Asha Mehta");
    await user.type(within(panel).getByLabelText("Contact (optional)"), "+91 98765 43210");
    await user.type(within(panel).getByLabelText("Email (optional)"), "asha@apollo.example");
    const instruments = await within(panel).findByRole("radiogroup", { name: "Instrument name (optional)" });
    expect(within(instruments).getAllByRole("radio").map((r) => r.textContent)).toEqual([...INSTRUMENT_NAMES]);
    await user.click(within(instruments).getByRole("radio", { name: "Adams 8180 V" }));
    await user.type(within(panel).getByLabelText("Installation price (₹)"), "12,50,000");
    await user.type(within(panel).getByLabelText("Work load (optional)"), "300 tests/day");
    await user.type(within(panel).getByLabelText("Expected CPT (optional)"), "45");
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
      value: "1250000",
      pipeline: MINE_ID,
      account_name: "Apollo Diagnostics",
      customer_name: "Asha Mehta",
      contact_phone: "+91 98765 43210",
      contact_email: "asha@apollo.example",
      instrument_name: "Adams 8180 V",
      work_load: "300 tests/day",
      expected_cpt: "45",
      custom_fields: { "f-tender": "GEM/2026/1", "f-segment": "o-lab" },
    });
    for (const absent of ["title", "lead", "owner", "probability", "description"]) expect(body).not.toHaveProperty(absent);
    expect(await screen.findByText("“Asha Mehta — Adams 8180 V” created.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "New opportunity" })).not.toBeInTheDocument();
  });

  it("in my own workspace: no lead to pick, no owner and no name; the customer or the account name is enough", async () => {
    const created = "POST /api/v1/workspaces/me/opportunities";
    const api = mockApi({
      ...OPPORTUNITY_OPTIONS_ROUTE,
      [`GET ${PIPELINES_URL}`]: { status: 200, body: { results: [PIPELINE] } },
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) },
      [created]: { status: 201, body: makeOpportunity({ title: "Analyser" }) },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "New opportunity" }))[0]!);
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    expect(within(panel).queryByLabelText(/lead/i)).not.toBeInTheDocument();
    expect(within(panel).queryByText(/Find a lead/i)).not.toBeInTheDocument();
    expect(within(panel).queryByRole("combobox", { name: /owner/i })).not.toBeInTheDocument();
    expect(within(panel).queryByLabelText(/Opportunity name/)).not.toBeInTheDocument();
    await user.type(within(panel).getByLabelText("Installation price (₹)"), "500000");
    await user.type(within(panel).getByLabelText("Account name"), "   "); // spaces are not a name
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    // Neither name given: one problem, on the customer name.
    expect(within(panel).getByLabelText("Customer name")).toHaveAttribute("aria-invalid", "true");
    expect(within(panel).getByLabelText("Customer name")).toHaveAccessibleDescription(/Enter the customer name or the account name\./);
    expect(within(panel).getByLabelText("Account name")).not.toHaveAttribute("aria-invalid", "true");
    expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")).toHaveLength(0);
    // The customer name alone is enough (the account name stays blank).
    await user.type(within(panel).getByLabelText("Customer name"), "Dr. Iyer");
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")).toHaveLength(1));
    const body = api.callsTo("POST", "/api/v1/workspaces/me/opportunities")[0]!.body as Record<string, unknown>;
    expect(body).toMatchObject({ value: "500000", customer_name: "Dr. Iyer" });
    expect(body).not.toHaveProperty("account_name"); // blank, so not sent
    expect(body).not.toHaveProperty("title");
    expect(body).not.toHaveProperty("lead");
    expect(body).not.toHaveProperty("owner"); // the workspace's user owns it
  });

  it("organisation-wide, an administrator chooses the owner, who is sent with the opportunity", async () => {
    const ALL = "/api/v1/workspaces/all";
    let posts = 0;
    const api = mockApi({
      ...OPPORTUNITY_OPTIONS_ROUTE,
      [`GET ${ALL}/pipelines`]: { status: 200, body: { results: [PIPELINE] } },
      [`GET ${ALL}/pipeline-board`]: { status: 200, body: makeBoard([]) },
      "GET /api/v1/assignees": {
        status: 200,
        body: { results: [{ id: PRIYA_ID, full_name: "Priya Patel", email: "priya@example.test" }], next: null, previous: null },
      },
      [`POST ${ALL}/opportunities`]: () =>
        ++posts === 1
          ? apiError(400, "validation_error", "Check the highlighted fields.", { owner: ["This user can't own opportunities."] })
          : { status: 201, body: makeOpportunity({ title: "Analyser" }) },
    });
    renderWithProviders(<PipelineView />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "New opportunity" }))[0]!);
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    const owner = within(panel).getByRole("combobox", { name: "Owner" });
    expect(within(owner).getByRole("option", { name: /Choose an owner|Loading users/ })).toBeInTheDocument();
    await user.type(within(panel).getByLabelText("Installation price (₹)"), "500000");
    await user.type(within(panel).getByLabelText("Account name"), "City Lab");
    await user.type(within(panel).getByLabelText("Customer name"), "Dr. Iyer");
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    // Required here: nobody else would own it.
    expect(owner).toHaveAttribute("aria-invalid", "true");
    expect(owner).toHaveAccessibleDescription(/Choose who owns this opportunity\./);
    expect(api.callsTo("POST", `${ALL}/opportunities`)).toHaveLength(0);
    await waitFor(() => expect(within(owner).getByRole("option", { name: "Priya Patel (priya@example.test)" })).toBeInTheDocument());
    await user.selectOptions(owner, PRIYA_ID);
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    // The server's reason for the owner is shown on the field.
    await waitFor(() => expect(owner).toHaveAccessibleDescription(/This user can't own opportunities\./));
    expect(owner).toHaveAttribute("aria-invalid", "true");
    expect(api.callsTo("POST", `${ALL}/opportunities`)[0]!.body).toMatchObject({ owner: PRIYA_ID, account_name: "City Lab", customer_name: "Dr. Iyer" });
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", `${ALL}/opportunities`)).toHaveLength(2));
    const body = api.callsTo("POST", `${ALL}/opportunities`)[1]!.body as Record<string, unknown>;
    expect(body.owner).toBe(PRIYA_ID);
    expect(body).not.toHaveProperty("lead");
    expect(body).not.toHaveProperty("title");
    expect(await screen.findByText("“Analyser” created.")).toBeInTheDocument();
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
    expect(screen.getByRole("heading", { level: 1, name: "Pipeline", hidden: true })).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Cancel" }));
    expect(nav.replace).toHaveBeenCalledWith("/pipeline");
  });
});
