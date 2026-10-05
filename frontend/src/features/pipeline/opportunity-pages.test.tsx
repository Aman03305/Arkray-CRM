import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditOpportunityView, NewOpportunityView, OpportunityView } from "@/features/workspace/views";
import { useFlash } from "@/lib/flash";
import { businessToday } from "@/lib/format";
import { adminViewer, LEAD_ID, OPPORTUNITY_OPTIONS_ROUTE, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { makeBoard, makeOpportunity, OPPORTUNITY_ID, PIPELINE_ID, PIPELINE_ROUTES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/pipeline/x", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const CONFIG = PIPELINE_ROUTES;
/** What the new-opportunity panel reads besides the pipelines: the instruments, and the
 * advisory duplicate check (nothing found). */
const PANEL = {
  ...OPPORTUNITY_OPTIONS_ROUTE,
  "GET /api/v1/workspaces/me/leads/duplicates": { status: 200, body: { results: [] } },
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
/** The customer's details as typed in the new-opportunity panel (they are the customer, ADR-0027;
 * the server makes the opportunity's lead from them, ADR-0028). */
const CUSTOMER = {
  account_name: "Apollo Diagnostics",
  customer_name: "Asha Mehta",
  contact_phone: "+91 98765 43210",
  contact_email: "asha@apollo.example",
  address: "Mumbai",
};
const BOARD = {
  "GET /api/v1/workspaces/me/pipeline-board": { status: 200, body: makeBoard() },
};
const ASSIGNEES = {
  "GET /api/v1/assignees": {
    status: 200,
    body: {
      results: [
        { id: PRIYA_ID, full_name: "Priya Patel", email: "priya@example.test" },
        { id: RAHUL_ID, full_name: "Rahul Sharma", email: "rahul@example.test" },
      ],
      next: null,
      previous: null,
    },
  },
};
const PRIYA_REF = { id: PRIYA_ID, full_name: "Priya Patel", is_active: true };

/** Fill the panel's required customer fields (and the optional ones when asked). */
async function typeCustomer(user: ReturnType<typeof userEvent.setup>, drawer: HTMLElement, all = false) {
  await user.type(within(drawer).getByLabelText("Account name"), CUSTOMER.account_name);
  await user.type(within(drawer).getByLabelText("Customer name"), CUSTOMER.customer_name);
  if (!all) return;
  await user.type(within(drawer).getByLabelText("Contact (optional)"), CUSTOMER.contact_phone);
  await user.type(within(drawer).getByLabelText("Email (optional)"), CUSTOMER.contact_email);
  await user.type(within(drawer).getByLabelText("Address (optional)"), CUSTOMER.address);
}

/** Shows the one-time notice for the current page, as the page navigated to would. */
function Notice() {
  const [notice] = useFlash();
  return <p>{notice ?? "no notice"}</p>;
}

beforeEach(() => {
  nav.push.mockReset();
  nav.replace.mockReset();
});

describe("opportunity detail", () => {
  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  });

  it("shows the exact figures, the customer and its lead, the owner, and (in History) the stage history", async () => {
    mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: {
        status: 200,
        body: makeOpportunity({ probability: "62.50", probability_overridden: true, weighted_value: "781250.00", expected_cpt: "₹45 per test" }),
      },
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
    // The deal's own customer name is text; its lead (made with it) is one click away (ADR-0028).
    expect(within(customer).getAllByText("Asha Mehta").map((name) => name.closest("a"))).toEqual([null, expect.any(HTMLAnchorElement)]);
    expect(within(customer).getByText("Lead")).toBeInTheDocument();
    expect(within(customer).getByRole("link", { name: "Asha Mehta" })).toHaveAttribute("href", `/leads/${LEAD_ID}`);
    expect(within(customer).getByRole("link", { name: "asha@apollo.example" })).toHaveAttribute("href", "mailto:asha@apollo.example");
    expect(within(customer).getByRole("link", { name: "+91 98765 43210" })).toHaveAttribute("href", expect.stringMatching(/^tel:/));
    const instrument = screen.getByRole("region", { name: "Instrument" });
    expect(within(instrument).getByText("HbA1c analyser")).toBeInTheDocument();
    expect(within(instrument).getByText("Expected CPT")).toBeInTheDocument();
    expect(within(instrument).getByText("₹45 per test")).toBeInTheDocument();
    // No Leads module (ADR-0027): no Lead section, and the only lead link is its own lead page.
    expect(screen.queryByRole("region", { name: "Lead" })).not.toBeInTheDocument();
    expect([...document.querySelectorAll('a[href*="/leads"]')].map((link) => link.getAttribute("href"))).toEqual([`/leads/${LEAD_ID}`]);
    // The Record section names the owner.
    const record = screen.getByRole("region", { name: "Record" });
    expect(within(record).getByText("Owner")).toBeInTheDocument();
    expect(within(record).getAllByText("Rahul Sharma").length).toBeGreaterThan(0);
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

  it("a closed opportunity whose customer has moved on keeps its own details, offers no new work and only Reopen", async () => {
    mockApi({
      ...CONFIG,
      ...HISTORY,
      [`GET ${ME}`]: { status: 200, body: makeOpportunity({ status: "won", stage: STAGES.won, lead: { id: null, restricted: true } }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const customer = await screen.findByRole("region", { name: "Customer" });
    expect(within(customer).getByText("Apollo Diagnostics")).toBeInTheDocument(); // the deal's own record of it
    // Its lead is in another workspace now: said so, never linked.
    expect(within(customer).getByText("In another workspace")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Asha Mehta" })).not.toBeInTheDocument();
    expect(document.querySelector('a[href*="/leads"]')).toBeNull();
    expect(screen.queryByRole("region", { name: "Open work" })).not.toBeInTheDocument(); // new work follows the customer
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
    // Reading is allowed: its lead opens in Rahul's workspace.
    expect(within(screen.getByRole("region", { name: "Customer" })).getByRole("link", { name: "Asha Mehta" })).toHaveAttribute(
      "href",
      `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`,
    );
  });
});

describe("changing an opportunity's owner", () => {
  const ALL = `/api/v1/workspaces/all/opportunities/${OPPORTUNITY_ID}`;
  const RAHULS = `/api/v1/workspaces/${RAHUL_ID}/opportunities/${OPPORTUNITY_ID}`;
  const MORE = "More actions for Hospital Analyzer Project";

  beforeEach(() => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
  });

  async function openMenu(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: MORE }));
    return screen.getByRole("menu");
  }

  it("an administrator changes the owner of an open deal: the assign operation with the version shown", async () => {
    const api = mockApi({
      ...CONFIG,
      ...ASSIGNEES,
      [`GET ${ALL}`]: { status: 200, body: makeOpportunity() },
      [`POST ${ALL}/assign`]: { status: 200, body: makeOpportunity({ owner: PRIYA_REF, version: 3 }) },
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(within(await openMenu(user)).getByRole("menuitem", { name: "Change owner" }));
    const dialog = screen.getByRole("dialog", { name: "Change owner" });
    const owner = within(dialog).getByRole("combobox", { name: "New owner" });
    await within(owner).findByRole("option", { name: "Priya Patel (priya@example.test)" });
    await user.selectOptions(owner, PRIYA_ID);
    await user.click(within(dialog).getByRole("button", { name: "Change owner" }));
    // Organisation-wide the deal stays on screen, now with its new owner.
    expect(await screen.findByText("“Hospital Analyzer Project” now belongs to Priya Patel.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Change owner" })).not.toBeInTheDocument();
    expect(api.callsTo("POST", `${ALL}/assign`).map((c) => c.body)).toEqual([{ owner: PRIYA_ID, version: 2 }]);
    expect(within(screen.getByRole("region", { name: "Record" })).getByText("Priya Patel")).toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("is not offered to a salesperson", async () => {
    mockApi({ ...CONFIG, [`GET ${ME}`]: { status: 200, body: makeOpportunity() } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const user = userEvent.setup();
    const menu = await openMenu(user);
    expect(within(menu).getByRole("menuitem", { name: "Delete (archive)" })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Change owner" })).not.toBeInTheDocument();
  });

  it("is not offered for a won or lost deal (it keeps the owner who closed it)", async () => {
    for (const closed of [
      makeOpportunity({ status: "won", stage: STAGES.won, probability: "100.00" }),
      makeOpportunity({ status: "lost", stage: STAGES.lost, probability: "0.00" }),
    ]) {
      mockApi({ ...CONFIG, [`GET ${ALL}`]: { status: 200, body: closed } });
      const view = renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
      const user = userEvent.setup();
      const menu = await openMenu(user);
      expect(within(menu).getByRole("menuitem", { name: "Delete (archive)" })).toBeInTheDocument();
      expect(within(menu).queryByRole("menuitem", { name: "Change owner" })).not.toBeInTheDocument();
      view.unmount();
    }
  });

  it("is not offered for an archived deal", async () => {
    mockApi({ ...CONFIG, [`GET ${ALL}`]: { status: 200, body: makeOpportunity({ archived_at: "2026-09-30T10:00:00Z" }) } });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    const menu = await openMenu(user);
    expect(within(menu).getByRole("menuitem", { name: "Restore" })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Change owner" })).not.toBeInTheDocument();
  });

  it("handed out of the selected user's workspace, it goes to that workspace's Pipeline with the notice", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline/${OPPORTUNITY_ID}`;
    const api = mockApi({
      ...CONFIG,
      ...ASSIGNEES,
      [`GET ${RAHULS}`]: { status: 200, body: makeOpportunity() },
      [`POST ${RAHULS}/assign`]: { status: 200, body: makeOpportunity({ owner: PRIYA_REF, version: 3 }) },
    });
    const page = renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(within(await openMenu(user)).getByRole("menuitem", { name: "Change owner" }));
    const dialog = screen.getByRole("dialog", { name: "Change owner" });
    const owner = within(dialog).getByRole("combobox", { name: "New owner" });
    await within(owner).findByRole("option", { name: "Priya Patel (priya@example.test)" });
    await user.selectOptions(owner, PRIYA_ID);
    await user.click(within(dialog).getByRole("button", { name: "Change owner" }));
    const pipeline = `/admin/users/${RAHUL_ID}/pipeline`;
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(pipeline));
    expect(api.callsTo("POST", `${RAHULS}/assign`)[0]!.body).toEqual({ owner: PRIYA_ID, version: 2 });
    // The notice travels with the navigation: Rahul's Pipeline shows it.
    page.unmount();
    nav.pathname = pipeline;
    renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("“Hospital Analyzer Project” now belongs to Priya Patel.")).toBeInTheDocument();
  });

  it("a deal changed meanwhile (409) says so in the dialog", async () => {
    mockApi({
      ...CONFIG,
      ...ASSIGNEES,
      [`GET ${ALL}`]: { status: 200, body: makeOpportunity() },
      [`POST ${ALL}/assign`]: apiError(409, "conflict", "Changed."),
    });
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer });
    const user = userEvent.setup();
    await user.click(within(await openMenu(user)).getByRole("menuitem", { name: "Change owner" }));
    const dialog = screen.getByRole("dialog", { name: "Change owner" });
    const owner = within(dialog).getByRole("combobox", { name: "New owner" });
    await within(owner).findByRole("option", { name: "Priya Patel (priya@example.test)" });
    await user.selectOptions(owner, PRIYA_ID);
    await user.click(within(dialog).getByRole("button", { name: "Change owner" }));
    expect(await within(dialog).findByText(/It changed a moment ago/)).toBeInTheDocument();
    expect(screen.queryByText(/now belongs to/)).not.toBeInTheDocument();
  });
});

describe("creating an opportunity", () => {
  beforeEach(() => {
    nav.pathname = "/pipeline/new";
  });

  it("a panel over the board: the customer's details as typed, exact amounts, and an idempotency key reused for an identical retry", async () => {
    let attempts = 0;
    const api = mockApi({
      ...CONFIG,
      ...BOARD,
      ...PANEL,
      "POST /api/v1/workspaces/me/opportunities": () =>
        ++attempts === 1 ? apiError(503, "service_unavailable", "Try again.") : { status: 201, body: makeOpportunity() },
    });
    renderWithProviders(<NewOpportunityView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "New opportunity" });
    // No lead to pick and no name to type (ADR-0028): the customer's details are the customer,
    // and the server names the opportunity and makes its lead from them.
    expect(within(drawer).queryByRole("combobox", { name: /lead/i })).not.toBeInTheDocument();
    expect(within(drawer).queryByLabelText(/Opportunity name/)).not.toBeInTheDocument();
    expect(within(drawer).getByLabelText("Opportunity date")).toHaveValue(businessToday());
    expect(within(drawer).getByLabelText("Contact (optional)")).toHaveAttribute("type", "tel");
    // The pipeline is offered even when there is only one; its stages are offered by name.
    await waitFor(() => expect(within(drawer).getByRole("combobox", { name: "Pipeline" })).toHaveValue(PIPELINE_ID));
    expect(within(within(drawer).getByRole("combobox", { name: "Stage" })).getAllByRole("option").map((o) => o.textContent)).toEqual(
      ["New", "Qualified", "Proposal", "Negotiation", "Won", "Lost"],
    );
    await typeCustomer(user, drawer, true);
    await user.click(await within(drawer).findByRole("radio", { name: "Adams 8380 V-lite" }));
    await user.type(within(drawer).getByLabelText("Installation price (₹)"), "12,50,000.50");
    await user.type(within(drawer).getByLabelText("Expected CPT (optional)"), "45");
    expect(within(drawer).queryByLabelText("Negotiated price (₹)")).not.toBeInTheDocument();
    await user.selectOptions(within(drawer).getByRole("combobox", { name: "Stage" }), STAGES.negotiation.id);
    await user.type(within(drawer).getByLabelText("Negotiated price (₹)"), "11,00,000");
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    expect(await within(drawer).findByText(/temporarily unavailable|Try again/i)).toBeInTheDocument();
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/pipeline/${OPPORTUNITY_ID}`));
    const [first, second] = api.callsTo("POST", "/api/v1/workspaces/me/opportunities");
    expect(first!.body).toEqual({
      value: "1250000.50",
      opportunity_date: businessToday(),
      ...CUSTOMER,
      instrument_name: "Adams 8380 V-lite",
      expected_cpt: "45",
      pipeline: PIPELINE_ID,
      stage: STAGES.negotiation.id,
      negotiated_price: "1100000",
    });
    expect(first!.headers["Idempotency-Key"]).toBe(second!.headers["Idempotency-Key"]);
  });

  it("refuses amounts that aren't plain rupees before sending anything", async () => {
    const api = mockApi({ ...CONFIG, ...BOARD, ...PANEL });
    renderWithProviders(<NewOpportunityView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "New opportunity" });
    await typeCustomer(user, drawer);
    await user.type(within(drawer).getByLabelText("Installation price (₹)"), "1e6");
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    expect(await within(drawer).findByText(/Enter an amount in rupees/)).toBeInTheDocument();
    expect(within(drawer).getByLabelText("Installation price (₹)")).toHaveFocus();
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("the customer or the account name is required; a new opportunity takes its stage's probability", async () => {
    const api = mockApi({
      ...CONFIG,
      ...BOARD,
      ...PANEL,
      "POST /api/v1/workspaces/me/opportunities": { status: 201, body: makeOpportunity() },
    });
    renderWithProviders(<NewOpportunityView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "New opportunity" });
    // No own probability (and no description) when creating: those are set when editing.
    expect(within(drawer).queryByRole("checkbox", { name: /Own probability/ })).not.toBeInTheDocument();
    expect(within(drawer).queryByLabelText("Probability (%)")).not.toBeInTheDocument();
    expect(within(drawer).queryByLabelText(/Description/)).not.toBeInTheDocument();
    await user.type(within(drawer).getByLabelText("Installation price (₹)"), "100");
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    expect(await within(drawer).findByText("Enter the customer name or the account name.")).toBeInTheDocument();
    expect(within(drawer).getByLabelText("Customer name")).toHaveAccessibleDescription("Enter the customer name or the account name.");
    expect(within(drawer).getByLabelText("Account name")).not.toHaveAttribute("aria-invalid", "true");
    expect(within(drawer).getByLabelText("Customer name")).toHaveFocus(); // the (only) problem
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
    // The account name alone is enough.
    await user.type(within(drawer).getByLabelText("Account name"), CUSTOMER.account_name);
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")[0]!.body).toEqual({
      value: "100",
      opportunity_date: businessToday(),
      account_name: CUSTOMER.account_name,
      pipeline: PIPELINE_ID,
    });
  });

  it("in a selected user's workspace, a reason about the owner is shown in the panel (there is no Owner field)", async () => {
    const message = "This user's account isn't active, so nothing new can be added to their workspace.";
    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline/new`;
    const api = mockApi({
      ...CONFIG,
      ...OPPORTUNITY_OPTIONS_ROUTE,
      [`GET /api/v1/workspaces/${RAHUL_ID}/pipeline-board`]: { status: 200, body: makeBoard() },
      [`POST /api/v1/workspaces/${RAHUL_ID}/opportunities`]: apiError(400, "invalid_input", "Check the details and try again.", { owner: [message] }),
    });
    renderWithProviders(<NewOpportunityView />, { viewer: adminViewer });
    const user = userEvent.setup();
    const drawer = await screen.findByRole("dialog", { name: "New opportunity" });
    expect(within(drawer).queryByRole("combobox", { name: /owner/i })).not.toBeInTheDocument(); // Rahul owns it
    await user.type(within(drawer).getByLabelText("Installation price (₹)"), "100");
    await typeCustomer(user, drawer);
    await user.click(within(drawer).getByRole("button", { name: "Create opportunity" }));
    expect(await within(drawer).findByRole("alert")).toHaveTextContent(message);
    expect(api.callsTo("POST", `/api/v1/workspaces/${RAHUL_ID}/opportunities`)[0]!.body).not.toHaveProperty("owner");
    expect(screen.getByRole("dialog", { name: "New opportunity" })).toBeInTheDocument(); // the typing is kept
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("the board's New opportunity button opens the same panel, and Cancel leaves nothing behind", async () => {
    nav.pathname = "/pipeline";
    const { PipelineView } = await import("@/features/workspace/views");
    const api = mockApi({ ...CONFIG, ...BOARD });
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
    // The panel names the deal it edits (its name isn't a field).
    expect(drawer).toHaveAccessibleDescription("Hospital Analyzer Project");
    expect(within(drawer).queryByLabelText(/Opportunity name/)).not.toBeInTheDocument();
    for (const group of ["Customer", "Instrument", "Timeline", "Pipeline", "Additional"]) {
      expect(within(drawer).getByRole("region", { name: group })).toBeInTheDocument();
    }
    // Its pipeline and stage are shown, not chosen, here.
    const pipeline = within(drawer).getByRole("region", { name: "Pipeline" });
    expect(pipeline).toHaveTextContent("Sales Pipeline · Proposal");
    expect(within(pipeline).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(drawer).getByLabelText("Account name")).toHaveValue("Apollo Diagnostics");
    expect(within(drawer).getByLabelText(/^Work load/)).toHaveValue("300 tests/day");
    expect(within(drawer).getByLabelText(/^Description/)).toHaveValue("Two analysers for the central lab.");
    await user.click(within(drawer).getByRole("button", { name: "Save changes" }));
    expect(screen.queryByRole("dialog", { name: "Edit opportunity" })).not.toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "PATCH")).toBe(false);
    expect(screen.getByRole("heading", { level: 1, name: "Hospital Analyzer Project" })).toBeInTheDocument();
  });

  it("a won opportunity's probability can't be changed: the panel shows its stage, with no own probability", async () => {
    mockApi({ ...CONFIG, ...HISTORY, [`GET ${ME}`]: { status: 200, body: makeOpportunity({ status: "won", stage: STAGES.won, probability: "100.00" }) } });
    renderWithProviders(<EditOpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: salesViewer });
    const drawer = await screen.findByRole("dialog", { name: "Edit opportunity" });
    expect(within(drawer).getByRole("region", { name: "Pipeline" })).toHaveTextContent("Sales Pipeline · Won");
    expect(within(drawer).queryByRole("checkbox", { name: /Own probability/ })).not.toBeInTheDocument();
    expect(within(drawer).queryByLabelText("Probability (%)")).not.toBeInTheDocument();
  });
});
