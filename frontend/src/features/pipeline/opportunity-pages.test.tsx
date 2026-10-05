import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditOpportunityView, LeadView, NewOpportunityView, OpportunityView } from "@/features/workspace/views";
import { businessToday } from "@/lib/format";
import { LEAD_ID, LEAD_OPTIONS, makeLead, makeLeadListItem, RAHUL_ID, adminViewer, salesViewer } from "@/test/fixtures";
import { makeBoard, makeCard, makeOpportunity, OPPORTUNITY_ID, PIPELINE_ID, PIPELINE_ROUTES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/pipeline/x", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const CONFIG = {
  ...PIPELINE_ROUTES,
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
/** What a create sends for the fixture lead's details (prefilled from the lead). */
const FROM_LEAD = {
  account_name: "Apollo Diagnostics",
  customer_name: "Asha Mehta",
  contact_phone: "+91 98765 43210",
  contact_email: "asha@apollo.example",
  address: "Mumbai",
};
const LEADS = {
  [`GET /api/v1/workspaces/me/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
  "GET /api/v1/workspaces/me/leads": { status: 200, body: { results: [makeLeadListItem()], next: null, previous: null } },
  "GET /api/v1/workspaces/me/pipeline-board": { status: 200, body: makeBoard() },
};

beforeEach(() => {
  nav.push.mockReset();
  nav.replace.mockReset();
});

describe("opportunity detail", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  });

  it("shows the exact figures, the customer, the lead, and (in History) the stage history", async () => {
    mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity({ probability: "62.50", probability_overridden: true, weighted_value: "781250.00" }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { level: 1, name: "Hospital Analyzer Project" })).toBeInTheDocument();
    const deal = screen.getByRole("region", { name: "Deal" });
    expect(within(deal).getByText("Installation price")).toBeInTheDocument();
    expect(within(deal).getByText("₹12,50,000")).toBeInTheDocument();
    expect(within(deal).getByText("62.5%")).toBeInTheDocument();
    expect(within(deal).getByText("(own)")).toBeInTheDocument();
    expect(within(deal).getByText("₹7,81,250")).toBeInTheDocument();
    const customer = screen.getByRole("region", { name: "Customer" });
    expect(within(customer).getByRole("link", { name: "asha@apollo.example" })).toHaveAttribute("href", "mailto:asha@apollo.example");
    expect(within(screen.getByRole("region", { name: "Instrument" })).getByText("HbA1c analyser")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Asha Mehta" })).toHaveAttribute("href", `/leads/${LEAD_ID}`);
    expect(screen.queryByRole("region", { name: "Stage history" })).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "History" }));
    expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
    const history = screen.getByRole("region", { name: "Stage history" });
    expect(await within(history).findByText("Created in")).toBeInTheDocument();
    expect(within(history).getAllByText("Proposal").length).toBeGreaterThan(0);
  });

  it("the tabs follow the arrow keys", async () => {
    mockApi({ ...CONFIG, ...HISTORY, [`GET ${ME}`]: { status: 200, body: makeOpportunity() } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const overview = await screen.findByRole("tab", { name: "Overview" });
    overview.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Notes" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Notes" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "History" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(overview).toHaveFocus();
  });

  it("a closed opportunity whose lead has moved on shows the lead as restricted", async () => {
    mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity({ status: "won", stage: STAGES.won, lead: { id: null, restricted: true } }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByText("Lead in another workspace")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Asha Mehta" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reopen" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Won" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Lost" })).not.toBeInTheDocument();
  });

  it("Won uses the move operation with the version shown", async () => {
    const api = mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity() },
      [`POST ${ME}/move`]: { status: 200, body: makeOpportunity({ status: "won", stage: STAGES.won, probability: "100.00", version: 3 }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Won" }));
    const dialog = screen.getByRole("alertdialog", { name: "Mark as won?" });
    await user.click(within(dialog).getByRole("button", { name: "Mark as won" }));
    expect(await screen.findByText("Marked as won.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/move`)[0]!.body).toEqual({ stage: STAGES.won.id, version: 2 });
  });

  it("Move lets keyboard users choose any allowed stage, and a negotiation stage asks for the price", async () => {
    const api = mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity() },
      [`POST ${ME}/move`]: { status: 200, body: makeOpportunity({ stage: STAGES.negotiation, negotiated_price: "1100000.00", version: 3 }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Move" }));
    const dialog = screen.getByRole("alertdialog", { name: "Move to stage" });
    expect(within(dialog).getAllByRole("radio").map((r) => r.closest("label")!.textContent)).toEqual([
      "New10%",
      "Qualified25%",
      "Negotiation75%",
      "Won100%",
      "Lost0%",
    ]);
    await user.click(within(dialog).getByRole("radio", { name: /Qualified/ }));
    expect(within(dialog).getByText(/probability becomes 25%/)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Negotiated price (₹)")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("radio", { name: /Negotiation/ }));
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    expect(await within(dialog).findByText("Enter the negotiated price.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/move`)).toHaveLength(0);
    await user.type(within(dialog).getByLabelText("Negotiated price (₹)"), "11,00,000");
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    expect(await screen.findByText("Moved to Negotiation.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/move`)[0]!.body).toEqual({ stage: STAGES.negotiation.id, version: 2, negotiated_price: "1100000" });
  });

  it("in negotiation, Update price appends a negotiated price with the version shown", async () => {
    const negotiating = makeOpportunity({ stage: STAGES.negotiation, negotiated_price: "1100000.00", negotiated_at: "2026-09-29T06:00:00Z" });
    const api = mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: negotiating },
      [`POST ${ME}/negotiated-prices`]: { status: 201, body: { ...negotiating, negotiated_price: "1050000.00", version: 3 } },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const deal = await screen.findByRole("region", { name: "Deal" });
    expect(within(deal).getByText("₹11,00,000")).toBeInTheDocument();
    await user.click(within(deal).getByRole("button", { name: "Update price" }));
    const dialog = screen.getByRole("dialog", { name: "Update negotiated price" });
    await user.type(within(dialog).getByLabelText("Negotiated price (₹)"), "10,50,000");
    await user.click(within(dialog).getByRole("button", { name: "Save price" }));
    expect(await screen.findByText("Negotiated price recorded.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/negotiated-prices`)[0]!.body).toEqual({ version: 2, price: "1050000" });
  });

  it("a guessed or someone else's opportunity looks like a missing page", async () => {
    mockApi({ ...CONFIG, [`GET ${ME}`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("Delete (archive) asks first, archives with the version, and can be undone with Restore", async () => {
    let archived = false;
    const api = mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: () => ({ status: 200, body: archived ? makeOpportunity({ archived_at: "2026-09-30T10:00:00Z", version: 3 }) : makeOpportunity() }),
      [`POST ${ME}/archive`]: () => {
        archived = true;
        return { status: 200, body: makeOpportunity({ archived_at: "2026-09-30T10:00:00Z", version: 3 }) };
      },
      [`POST ${ME}/restore`]: { status: 200, body: makeOpportunity({ version: 4 }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "More actions for Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete (archive)" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete this opportunity?" });
    expect(within(dialog).getByText(/history is kept and it can be restored/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Opportunity deleted (archived). You can restore it.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/archive`)[0]!.body).toEqual({ version: 2 });
    expect(await screen.findByText("Archived")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "More actions for Hospital Analyzer Project" }));
    await user.click(screen.getByRole("menuitem", { name: "Restore" }));
    await user.click(within(screen.getByRole("alertdialog", { name: "Restore this opportunity?" })).getByRole("button", { name: "Restore" }));
    expect(await screen.findByText("Opportunity restored.")).toBeInTheDocument();
    expect(api.callsTo("POST", `${ME}/restore`)[0]!.body).toEqual({ version: 3 });
  });

  it("someone who can only read sees no actions", async () => {
    mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET /api/v1/workspaces/${RAHUL_ID}/opportunities/${OPPORTUNITY_ID}`]: { status: 200, body: makeOpportunity() },
    });
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline/${OPPORTUNITY_ID}`;
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, {
      viewer: { ...adminViewer, capabilities: adminViewer.capabilities.filter((c) => c !== "crm.manage_any") },
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Hospital Analyzer Project" })).toBeInTheDocument();
    for (const name of ["Won", "Lost", "Move", "Edit", "More actions for Hospital Analyzer Project"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });
});

describe("creating an opportunity", () => {
  beforeEach(() => {
    nav.pathname = "/pipeline/new";
  });

  it("for a lead from its page: a panel over the board, the lead's details prefilled, exact amounts, and an idempotency key reused for an identical retry", async () => {
    let attempts = 0;
    const api = mockApi({
      ...CONFIG,
      ...LEADS,
      "POST /api/v1/workspaces/me/opportunities": () =>
        ++attempts === 1 ? apiError(503, "service_unavailable", "Try again.") : { status: 201, body: makeOpportunity() },
    });
    renderWithProviders(<NewOpportunityView leadId={LEAD_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "New opportunity" });
    expect(await within(drawer).findByRole("combobox", { name: "Lead" })).toHaveValue(LEAD_ID);
    await waitFor(() => expect(within(drawer).getByLabelText("Account name")).toHaveValue("Apollo Diagnostics"));
    expect(within(drawer).getByLabelText("Customer name")).toHaveValue("Asha Mehta");
    expect(within(drawer).getByLabelText("Opportunity date")).toHaveValue(businessToday());
    await user.type(within(drawer).getByLabelText("Opportunity name"), "Lab upgrade");
    await user.type(within(drawer).getByLabelText("Installation price (₹)"), "12,50,000.50");
    expect(within(drawer).queryByLabelText("Negotiated price (₹)")).not.toBeInTheDocument();
    await user.selectOptions(within(drawer).getByRole("combobox", { name: "Stage" }), STAGES.negotiation.id);
    await user.type(within(drawer).getByLabelText("Negotiated price (₹)"), "11,00,000");
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    expect(await within(drawer).findByText(/temporarily unavailable|Try again/i)).toBeInTheDocument();
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/pipeline/${OPPORTUNITY_ID}`));
    const [first, second] = api.callsTo("POST", "/api/v1/workspaces/me/opportunities");
    expect(first!.body).toEqual({
      lead: LEAD_ID,
      title: "Lab upgrade",
      value: "1250000.50",
      opportunity_date: businessToday(),
      ...FROM_LEAD,
      pipeline: PIPELINE_ID,
      stage: STAGES.negotiation.id,
      negotiated_price: "1100000",
    });
    expect(first!.headers["Idempotency-Key"]).toBe(second!.headers["Idempotency-Key"]);
  });

  it("refuses amounts that aren't plain rupees before sending anything", async () => {
    const api = mockApi({ ...CONFIG, ...LEADS });
    renderWithProviders(<NewOpportunityView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "New opportunity" });
    await within(drawer).findByRole("option", { name: /Asha Mehta/ });
    await user.selectOptions(within(drawer).getByRole("combobox", { name: "Lead" }), LEAD_ID);
    await waitFor(() => expect(within(drawer).getByLabelText("Account name")).toHaveValue("Apollo Diagnostics"));
    await user.type(within(drawer).getByLabelText("Opportunity name"), "x");
    await user.type(within(drawer).getByLabelText("Installation price (₹)"), "1e6");
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    expect(await within(drawer).findByText(/Enter an amount in rupees/)).toBeInTheDocument();
    expect(within(drawer).getByLabelText("Installation price (₹)")).toHaveFocus();
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("a lead, an account and a customer are required; a manual probability is sent only when set", async () => {
    const api = mockApi({
      ...CONFIG,
      ...LEADS,
      "POST /api/v1/workspaces/me/opportunities": { status: 201, body: makeOpportunity() },
    });
    renderWithProviders(<NewOpportunityView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "New opportunity" });
    await user.type(within(drawer).getByLabelText("Opportunity name"), "x");
    await user.type(within(drawer).getByLabelText("Installation price (₹)"), "100");
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    expect(await within(drawer).findByText("Choose the lead this opportunity is for.")).toBeInTheDocument();
    expect(within(drawer).getByLabelText("Account name")).toHaveAccessibleDescription(/./);
    expect(within(drawer).getByLabelText("Customer name")).toHaveAccessibleDescription(/./);
    await within(drawer).findByRole("option", { name: /Asha Mehta/ });
    await user.selectOptions(within(drawer).getByRole("combobox", { name: "Lead" }), LEAD_ID);
    await waitFor(() => expect(within(drawer).getByLabelText("Account name")).toHaveValue("Apollo Diagnostics"));
    await user.click(within(drawer).getByRole("checkbox", { name: /Own probability/ }));
    await user.type(within(drawer).getByLabelText("Probability (%)"), "33.5");
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")[0]!.body).toEqual({
      lead: LEAD_ID,
      title: "x",
      value: "100",
      opportunity_date: businessToday(),
      ...FROM_LEAD,
      pipeline: PIPELINE_ID,
      probability: "33.5",
    });
  });

  it("the board's New opportunity button opens the same panel, and Cancel leaves nothing behind", async () => {
    nav.pathname = "/pipeline";
    const { PipelineView } = await import("@/features/workspace/views");
    const api = mockApi({ ...CONFIG, ...LEADS });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New opportunity" }));
    const drawer = screen.getByRole("dialog", { name: "New opportunity" });
    await user.click(within(drawer).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "New opportunity" })).not.toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
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
      ...HISTORY,
      [`GET ${ME}`]: () => ({ status: 200, body: makeOpportunity({ version, description: version === 2 ? "Two analysers for the central lab." : "Updated by someone else" }) }),
      [`PATCH ${ME}`]: (call: RecordedCall) =>
        (call.body as { version: number }).version === 2
          ? apiError(409, "conflict", "Changed.")
          : { status: 200, body: makeOpportunity({ value: "1300000.00", version: 4 }) },
    });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "Edit opportunity" });
    const value = within(drawer).getByLabelText("Installation price (₹)");
    expect(value).toHaveValue("1250000");
    await user.clear(value);
    await user.type(value, "13,00,000");
    version = 3; // someone else saved meanwhile
    await user.click(within(drawer).getByRole("button", { name: "Save changes" }));
    const apply = await within(drawer).findByRole("button", { name: "Keep my changes" });
    expect(within(drawer).getByText(/Nothing was saved or overwritten/)).toBeInTheDocument();
    await user.click(apply);
    expect(within(drawer).getByLabelText(/^Description/)).toHaveValue("Updated by someone else");
    expect(within(drawer).getByLabelText("Installation price (₹)")).toHaveValue("13,00,000");
    await user.click(within(drawer).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/pipeline/${OPPORTUNITY_ID}`));
    expect(api.callsTo("PATCH", ME).map((c) => c.body)).toEqual([
      { version: 2, value: "1300000" },
      { version: 3, value: "1300000" },
    ]);
  });

  it("Edit on the deal page opens the panel over the deal; closing it unchanged sends nothing", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
    const api = mockApi({ ...CONFIG, ...HISTORY, [`GET ${ME}`]: { status: 200, body: makeOpportunity() } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const drawer = screen.getByRole("dialog", { name: "Edit opportunity" });
    for (const group of ["Basic", "Customer", "Instrument", "Closing", "Additional"]) {
      expect(within(drawer).getByRole("region", { name: group })).toBeInTheDocument();
    }
    expect(within(drawer).getByLabelText("Account name")).toHaveValue("Apollo Diagnostics");
    expect(within(drawer).getByLabelText(/^Work load/)).toHaveValue("300 tests/day");
    await user.click(within(drawer).getByRole("button", { name: "Save changes" }));
    expect(screen.queryByRole("dialog", { name: "Edit opportunity" })).not.toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "PATCH")).toBe(false);
    expect(screen.getByRole("heading", { level: 1, name: "Hospital Analyzer Project" })).toBeInTheDocument();
  });

  it("a won opportunity's probability is shown as fixed", async () => {
    mockApi({ ...CONFIG, ...HISTORY, [`GET ${ME}`]: { status: 200, body: makeOpportunity({ status: "won", stage: STAGES.won, probability: "100.00" }) } });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const drawer = await screen.findByRole("dialog", { name: "Edit opportunity" });
    expect(within(drawer).getByText("Probability 100%")).toBeInTheDocument();
    expect(within(drawer).queryByRole("checkbox", { name: /Own probability/ })).not.toBeInTheDocument();
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
