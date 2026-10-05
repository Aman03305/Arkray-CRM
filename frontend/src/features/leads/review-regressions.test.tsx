/**
 * Regressions from the ADR-0028 frontend review, each reproduced before its fix:
 * F1 (P2) a deal handed out of the workspace by "Change owner" left its lead's page cached;
 * F6 (P3) search hid an account name that was only a substring of the deal's derived name;
 * F9 (P3) with the pipelines unloaded, a new opportunity was sent into the default pipeline.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OpportunityDrawer } from "@/features/pipeline/OpportunityDrawer";
import { LeadView, OpportunityView } from "@/features/workspace/views";
import type { Lead } from "@/lib/api/types";
import { createQueryClient } from "@/lib/query-client";
import { adminViewer, LEAD_ID, OPPORTUNITY_OPTIONS_ROUTE, PRIYA_ID, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { makeCard, makeOpportunity, OPPORTUNITY_ID, PIPELINE_ROUTES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const RAHUL = { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true };
const PRIYA_REF = { id: PRIYA_ID, full_name: "Priya Patel", is_active: true };

function makeLead(): Lead {
  return {
    id: LEAD_ID,
    display_name: "Asha Mehta",
    first_name: "Asha",
    last_name: "Mehta",
    organization_name: "Apollo Diagnostics",
    job_title: "",
    email: "asha@apollo.example",
    phone: "+91 98765 43210",
    mobile: "",
    status: { key: "new", name: "New", category: "open" },
    source: null,
    rating: null,
    owner: RAHUL,
    last_contacted_at: null,
    archived_at: null,
    created_at: "2026-10-05T08:45:00Z",
    updated_at: "2026-10-05T08:45:00Z",
    version: 1,
    alternate_phone: "",
    address_line_1: "12 MG Road",
    address_line_2: "",
    city: "",
    state: "",
    postal_code: "",
    country: "",
    description: "",
    created_by: RAHUL,
  } as Lead;
}

beforeEach(() => {
  nav.push.mockReset();
  nav.replace.mockReset();
});

describe("F1: a deal handed out of the workspace takes its lead's page with it", () => {
  it("Back to the lead's page in the old workspace reads it afresh (not found), never from the cache", async () => {
    const W = `/api/v1/workspaces/${RAHUL_ID}`;
    let moved = false;
    const api = mockApi({
      ...PIPELINE_ROUTES,
      [`GET ${W}/leads/${LEAD_ID}`]: () => (moved ? apiError(404, "not_found", "Not found.") : { status: 200, body: makeLead() }),
      [`GET ${W}/opportunities`]: () => ({ status: 200, body: { results: moved ? [] : [makeCard()], next: null, previous: null } }),
      [`GET ${W}/opportunities/${OPPORTUNITY_ID}`]: { status: 200, body: makeOpportunity() },
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
      [`POST ${W}/opportunities/${OPPORTUNITY_ID}/assign`]: () => {
        moved = true;
        return { status: 200, body: makeOpportunity({ owner: PRIYA_REF, version: 3 }) };
      },
    });
    const client = createQueryClient(); // the production client: fresh for 30 s

    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    const leadPage = renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer, client });
    await screen.findByRole("link", { name: "Hospital Analyzer Project" });
    leadPage.unmount();

    nav.pathname = `/admin/users/${RAHUL_ID}/pipeline/${OPPORTUNITY_ID}`;
    const dealPage = renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: adminViewer, client });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "More actions for Hospital Analyzer Project" }));
    await user.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Change owner" }));
    const dialog = screen.getByRole("dialog", { name: "Change owner" });
    const owner = within(dialog).getByRole("combobox", { name: "New owner" });
    await within(owner).findByRole("option", { name: "Priya Patel (priya@example.test)" });
    await user.selectOptions(owner, PRIYA_ID);
    await user.click(within(dialog).getByRole("button", { name: "Change owner" }));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/admin/users/${RAHUL_ID}/pipeline`));
    dealPage.unmount();

    expect(client.getQueryData(["leads", "detail", RAHUL_ID, LEAD_ID])).toBeUndefined();
    const before = api.callsTo("GET", `${W}/leads/${LEAD_ID}`).length;
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer, client });
    expect(screen.queryByText("+91 98765 43210")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(api.callsTo("GET", `${W}/leads/${LEAD_ID}`).length).toBe(before + 1);
    expect(screen.queryByText("Asha Mehta")).not.toBeInTheDocument();
  });
});

describe("F6: search shows an account name that isn't what the deal is named after", () => {
  it("names the account 'Apollo' beside 'Apollo Hospitals — Adams 8180 V'", async () => {
    const { SearchLauncher } = await import("@/features/search/SearchLauncher");
    const EMPTY = { results: [], has_more: false };
    mockApi({
      "GET /api/v1/workspaces/me/search": {
        status: 200,
        body: {
          query: "apollo",
          terms: ["apollo"],
          leads: EMPTY,
          opportunities: {
            results: [
              {
                id: OPPORTUNITY_ID,
                title: "Apollo Hospitals — Adams 8180 V",
                status: "open",
                stage: { id: "s1", name: "Proposal" },
                account_name: "Apollo",
                customer_name: "Apollo Hospitals",
                lead: { id: LEAD_ID, display_name: "Apollo Hospitals", organization_name: "Apollo", restricted: false },
                owner: RAHUL,
              },
            ],
            has_more: false,
          },
          tasks: EMPTY,
          meetings: EMPTY,
          notes: EMPTY,
        },
      },
    });
    nav.pathname = "/dashboard";
    renderWithProviders(<SearchLauncher />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /search/i }));
    await user.type(screen.getByRole("combobox", { name: "Search leads, opportunities, tasks, meetings and notes" }), "apollo");
    const option = await screen.findByRole("option");
    // The customer the name is built from isn't repeated; the different account is shown.
    expect(option).toHaveAccessibleName("Opportunity: Apollo Hospitals — Adams 8180 V, Apollo, Proposal, Open");
  });
});

describe("F9: no new opportunity without its pipeline", () => {
  it("says the pipelines couldn't be loaded, offers a retry, and sends nothing", async () => {
    const api = mockApi({
      ...OPPORTUNITY_OPTIONS_ROUTE,
      "GET /api/v1/workspaces/me/pipelines": apiError(503, "service_unavailable", "Unavailable."),
    });
    nav.pathname = "/pipeline";
    renderWithProviders(<OpportunityDrawer workspace={{ kind: "self" }} opportunity={null} onClose={vi.fn()} onSaved={vi.fn()} />, {
      viewer: salesViewer,
    });
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    expect(await within(panel).findByText(/Pipelines couldn.t be loaded/)).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Try again" })).toBeInTheDocument();
    const user = userEvent.setup();
    await user.type(within(panel).getByLabelText("Customer name"), "XYZ Laboratory");
    await user.type(within(panel).getByLabelText("Installation price (₹)"), "100000");
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    expect(within(panel).getByRole("combobox", { name: "Pipeline" })).toHaveAccessibleDescription("Pipelines couldn't be loaded. Try again.");
    expect(api.callsTo("POST", "/api/v1/workspaces/me/opportunities")).toHaveLength(0);
  });
});
