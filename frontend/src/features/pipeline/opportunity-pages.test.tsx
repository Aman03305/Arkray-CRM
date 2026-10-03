import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditOpportunityView, LeadView, NewOpportunityView, OpportunityView } from "@/features/workspace/views";
import { LEAD_ID, LEAD_OPTIONS, makeLead, makeLeadListItem, RAHUL_ID, adminViewer, salesViewer } from "@/test/fixtures";
import { makeCard, makeOpportunity, OPPORTUNITY_ID, PIPELINES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/pipeline/x", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const CONFIG = {
  "GET /api/v1/config/pipelines": { status: 200, body: PIPELINES },
  "GET /api/v1/config/lead-options": { status: 200, body: LEAD_OPTIONS },
};
const ME = `/api/v1/workspaces/me/opportunities/${OPPORTUNITY_ID}`;
const HISTORY = {
  [`GET ${ME}/history`]: {
    status: 200,
    body: {
      results: [
        {
          id: 2,
          from_stage_id: STAGES.new.id,
          to_stage_id: STAGES.proposal.id,
          from_stage_name: "New",
          to_stage_name: "Proposal",
          from_status: "open",
          to_status: "open",
          value: "1250000.00",
          probability: "50.00",
          lost_reason: "",
          actor: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true },
          occurred_at: "2026-09-25T06:00:00Z",
        },
        {
          id: 1,
          from_stage_id: null,
          to_stage_id: STAGES.new.id,
          from_stage_name: "",
          to_stage_name: "New",
          from_status: "",
          to_status: "open",
          value: "1250000.00",
          probability: "10.00",
          lost_reason: "",
          actor: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true },
          occurred_at: "2026-09-20T04:30:00Z",
        },
      ],
      next: null,
      previous: null,
    },
  },
};

beforeEach(() => {
  nav.push.mockReset();
});

describe("opportunity detail", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  });

  it("shows the exact figures, the lead, and the stage history", async () => {
    mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity({ probability: "62.50", probability_overridden: true, weighted_value: "781250.00" }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { level: 1, name: "Hospital Analyzer Project" })).toBeInTheDocument();
    const summary = screen.getByRole("region", { name: "Summary" });
    expect(within(summary).getByText("₹12,50,000")).toBeInTheDocument();
    expect(within(summary).getByText("62.5%")).toBeInTheDocument();
    expect(within(summary).getByText("(set manually)")).toBeInTheDocument();
    expect(within(summary).getByText("₹7,81,250")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Asha Mehta" })).toHaveAttribute("href", `/leads/${LEAD_ID}`);
    const history = screen.getByRole("region", { name: "Stage history" });
    expect(await within(history).findByText("Created in")).toBeInTheDocument();
    expect(within(history).getAllByText("Proposal").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute("href", `/pipeline/${OPPORTUNITY_ID}/edit`);
  });

  it("a closed opportunity whose lead has moved on shows the lead as restricted", async () => {
    mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity({ status: "won", stage: STAGES.won, lead: { id: null, restricted: true } }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByText("Lead in another workspace")).toBeInTheDocument();
    expect(screen.queryByText("Asha Mehta")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reopen" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark as won" })).not.toBeInTheDocument();
  });

  it("Mark as won uses the move operation with the version shown", async () => {
    const api = mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity() },
      [`POST ${ME}/move`]: { status: 200, body: makeOpportunity({ status: "won", stage: STAGES.won, probability: "100.00", version: 3 }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mark as won" }));
    const dialog = screen.getByRole("alertdialog", { name: "Mark as won?" });
    await user.click(within(dialog).getByRole("button", { name: "Mark as won" }));
    expect(await screen.findByText("Marked as won.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/move`)[0]!.body).toEqual({ stage: STAGES.won.id, version: 2 });
  });

  it("Move to stage lets keyboard users choose any allowed stage", async () => {
    const api = mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity() },
      [`POST ${ME}/move`]: { status: 200, body: makeOpportunity({ stage: STAGES.negotiation, version: 3 }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move to stage" }));
    const dialog = screen.getByRole("alertdialog", { name: "Move to stage" });
    expect(within(dialog).getAllByRole("radio").map((r) => r.closest("label")!.textContent)).toEqual([
      "New10%",
      "Qualified25%",
      "Negotiation75%",
      "Won100%",
      "Lost0%",
    ]);
    await user.click(within(dialog).getByRole("radio", { name: /Negotiation/ }));
    expect(within(dialog).getByText(/probability becomes 75%/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    expect(await screen.findByText("Moved to Negotiation.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/move`)[0]!.body).toEqual({ stage: STAGES.negotiation.id, version: 2 });
  });

  it("a guessed or someone else's opportunity looks like a missing page", async () => {
    mockApi({ ...CONFIG, [`GET ${ME}`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("archives with a confirmation that explains closed is not archived", async () => {
    const api = mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity() },
      [`POST ${ME}/archive`]: { status: 200, body: makeOpportunity({ archived_at: "2026-09-30T10:00:00Z", version: 3 }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    const dialog = screen.getByRole("alertdialog", { name: "Archive Hospital Analyzer Project?" });
    expect(within(dialog).getByText(/Closing it as won or lost is different/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Archive opportunity" }));
    expect(await screen.findByText("Opportunity archived.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/archive`)[0]!.body).toEqual({ version: 2 });
  });
});

describe("creating an opportunity", () => {
  beforeEach(() => {
    nav.pathname = "/pipeline/new";
  });

  it("for a lead from its page: exact amount string, stage, and an idempotency key reused for an identical retry", async () => {
    let attempts = 0;
    const api = mockApi({
      ...CONFIG,
      [`GET /api/v1/workspaces/me/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      "GET /api/v1/workspaces/me/leads": { status: 200, body: { results: [makeLeadListItem()], next: null, previous: null } },
      "POST /api/v1/workspaces/me/opportunities": () =>
        ++attempts === 1 ? apiError(503, "service_unavailable", "Try again.") : { status: 201, body: makeOpportunity() },
    });
    renderWithProviders(<NewOpportunityView leadId={LEAD_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    expect(await screen.findByRole("combobox", { name: "Lead" })).toHaveValue(LEAD_ID);
    await user.type(screen.getByLabelText("Title"), "Lab upgrade");
    await user.type(screen.getByLabelText("Value (₹)"), "12,50,000.50");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Stage" }), STAGES.negotiation.id);
    await user.click(screen.getByRole("button", { name: "Create opportunity" }));
    expect(await screen.findByText(/temporarily unavailable|Try again/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/pipeline/${OPPORTUNITY_ID}`));
    const [first, second] = api.callsTo("POST", "/api/v1/workspaces/me/opportunities");
    expect(first!.body).toEqual({ lead: LEAD_ID, title: "Lab upgrade", value: "1250000.50", stage: STAGES.negotiation.id });
    expect(first!.headers["Idempotency-Key"]).toBe(second!.headers["Idempotency-Key"]);
  });

  it("refuses amounts that aren't plain rupees before sending anything", async () => {
    const api = mockApi({
      ...CONFIG,
      "GET /api/v1/workspaces/me/leads": { status: 200, body: { results: [makeLeadListItem()], next: null, previous: null } },
    });
    renderWithProviders(<NewOpportunityView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await screen.findByRole("option", { name: /Asha Mehta/ });
    await user.selectOptions(screen.getByRole("combobox", { name: "Lead" }), LEAD_ID);
    await user.type(screen.getByLabelText("Title"), "x");
    await user.type(screen.getByLabelText("Value (₹)"), "1e6");
    await user.click(screen.getByRole("button", { name: "Create opportunity" }));
    expect(await screen.findByText(/Enter an amount in rupees/)).toBeInTheDocument();
    expect(screen.getByLabelText("Value (₹)")).toHaveFocus();
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("a manual probability is sent only when set, and a lead is required", async () => {
    const api = mockApi({
      ...CONFIG,
      "GET /api/v1/workspaces/me/leads": { status: 200, body: { results: [makeLeadListItem()], next: null, previous: null } },
      "POST /api/v1/workspaces/me/opportunities": { status: 201, body: makeOpportunity() },
    });
    renderWithProviders(<NewOpportunityView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Title"), "x");
    await user.type(screen.getByLabelText("Value (₹)"), "100");
    await user.click(screen.getByRole("button", { name: "Create opportunity" }));
    expect(await screen.findByText("Choose the lead this opportunity is for.")).toBeInTheDocument();
    await screen.findByRole("option", { name: /Asha Mehta/ });
    await user.selectOptions(screen.getByRole("combobox", { name: "Lead" }), LEAD_ID);
    await user.click(screen.getByRole("checkbox", { name: /Set the probability manually/ }));
    await user.type(screen.getByLabelText("Probability (%)"), "33.5");
    await user.click(screen.getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")[0]!.body).toEqual({
      lead: LEAD_ID,
      title: "x",
      value: "100",
      probability: "33.5",
    });
  });
});

describe("editing an opportunity", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}/edit`;
  });

  it("sends only the changed fields with the version, and resolves a conflict without losing work", async () => {
    let version = 2;
    const api = mockApi({
      ...CONFIG,
      [`GET ${ME}`]: () => ({ status: 200, body: makeOpportunity({ version, description: version === 2 ? "Two analysers for the central lab." : "Updated by someone else" }) }),
      [`PATCH ${ME}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 2
          ? apiError(409, "conflict", "Changed.")
          : { status: 200, body: makeOpportunity({ value: "1300000.00", version: 4 }) },
    });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const value = await screen.findByLabelText("Value (₹)");
    expect(value).toHaveValue("1250000");
    await user.clear(value);
    await user.type(value, "13,00,000");
    version = 3; // someone else saved meanwhile
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    const apply = await screen.findByRole("button", { name: "Apply my changes to the latest version" });
    expect(screen.getByText(/Nothing was overwritten/)).toBeInTheDocument();
    await user.click(apply);
    expect(screen.getByLabelText(/^Description/)).toHaveValue("Updated by someone else");
    expect(screen.getByLabelText("Value (₹)")).toHaveValue("13,00,000");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/pipeline/${OPPORTUNITY_ID}`));
    expect(api.callsTo("PATCH", ME).map((c) => c.body)).toEqual([
      { version: 2, value: "1300000" },
      { version: 3, value: "1300000" },
    ]);
  });

  it("a won opportunity's probability is shown as fixed", async () => {
    mockApi({ ...CONFIG, [`GET ${ME}`]: { status: 200, body: makeOpportunity({ status: "won", stage: STAGES.won, probability: "100.00" }) } });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByText(/fixed for won opportunities/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Set the probability manually/ })).not.toBeInTheDocument();
  });
});

describe("the lead page", () => {
  const LEAD = `/api/v1/workspaces/me/leads/${LEAD_ID}`;

  beforeEach(() => {
    nav.pathname = `/leads/${LEAD_ID}`;
  });

  it("lists the lead's opportunities and offers + Opportunity", async () => {
    const api = mockApi({
      ...CONFIG,
      [`GET ${LEAD}`]: { status: 200, body: makeLead() },
      "GET /api/v1/workspaces/me/opportunities": {
        status: 200,
        body: { results: [makeCard(), makeCard({ id: "22222222-2222-4222-8222-222222222222", title: "POCT Expansion", value: "400000.00", stage_id: STAGES.won.id, status: "won" })], next: null, previous: null },
      },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const section = await screen.findByRole("region", { name: /Opportunities/ });
    expect(await within(section).findByRole("link", { name: "Hospital Analyzer Project" })).toHaveAttribute("href", `/pipeline/${OPPORTUNITY_ID}`);
    expect(within(section).getByText("₹12,50,000")).toBeInTheDocument();
    expect(within(section).getByText("₹4,00,000")).toBeInTheDocument();
    expect(within(section).getByText("Won")).toBeInTheDocument();
    expect(within(section).getByRole("heading", { name: "Opportunities (2)" })).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Opportunity" })).toHaveAttribute("href", `/pipeline/new?lead=${LEAD_ID}`);
    expect(api.callsTo("GET", "/api/v1/workspaces/me/opportunities")[0]!.query.get("lead")).toBe(LEAD_ID);
  });

  it("converts the lead in one step and lands on the new opportunity", async () => {
    const api = mockApi({
      ...CONFIG,
      [`GET ${LEAD}`]: { status: 200, body: makeLead({ status: { key: "qualified", name: "Qualified", category: "qualified" } }) },
      "GET /api/v1/workspaces/me/opportunities": { status: 200, body: { results: [], next: null, previous: null } },
      [`POST ${LEAD}/convert`]: {
        status: 201,
        body: { lead: makeLead({ status: { key: "converted", name: "Converted", category: "converted" }, version: 4 }), opportunity: makeOpportunity() },
      },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Convert" }));
    const dialog = screen.getByRole("dialog", { name: "Convert Asha Mehta" });
    expect(within(dialog).getByText(/No company, account or contact records are created/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Opportunity title")).toHaveValue("Apollo Diagnostics");
    await user.type(within(dialog).getByLabelText("Value (₹)"), "12,50,000");
    await user.click(within(dialog).getByRole("button", { name: "Convert lead" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/pipeline/${OPPORTUNITY_ID}`));
    const call = api.callsTo("POST", `${LEAD}/convert`)[0]!;
    expect(call.body).toEqual({ version: 3, title: "Apollo Diagnostics", value: "1250000" });
    expect(call.headers["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("an already converted lead offers no Convert", async () => {
    mockApi({
      ...CONFIG,
      [`GET ${LEAD}`]: { status: 200, body: makeLead({ status: { key: "converted", name: "Converted", category: "converted" } }) },
      "GET /api/v1/workspaces/me/opportunities": { status: 200, body: { results: [makeCard()], next: null, previous: null } },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    await screen.findByRole("region", { name: /Opportunities/ });
    expect(screen.queryByRole("button", { name: "Convert" })).not.toBeInTheDocument();
  });

  it("in an administrator's view of Rahul's workspace, links stay in Rahul's workspace", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    mockApi({
      ...CONFIG,
      [`GET /api/v1/workspaces/${RAHUL_ID}/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
      [`GET /api/v1/workspaces/${RAHUL_ID}/opportunities`]: { status: 200, body: { results: [makeCard()], next: null, previous: null } },
    });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    const section = await screen.findByRole("region", { name: /Opportunities/ });
    expect(await within(section).findByRole("link", { name: "Hospital Analyzer Project" })).toHaveAttribute(
      "href",
      `/admin/users/${RAHUL_ID}/pipeline/${OPPORTUNITY_ID}`,
    );
    expect(within(section).getByRole("link", { name: "Opportunity" })).toHaveAttribute(
      "href",
      `/admin/users/${RAHUL_ID}/pipeline/new?lead=${LEAD_ID}`,
    );
  });
});
