/**
 * ADR-0028: a lead's page, read-only, reached from the dashboard's new leads, search and its
 * opportunity. It reads the lead and its opportunities through the workspace on screen, and
 * every link stays in that workspace; another workspace's lead is "not found".
 */
import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeadView } from "@/features/workspace/views";
import type { Lead } from "@/lib/api/types";
import { adminViewer, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { makeCard, OPPORTUNITY_ID, PIPELINES, STAGES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/leads/x" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const LEAD_ID = "4d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a";
const RAHUL = { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true };

function makeLead(overrides: Partial<Lead> = {}): Lead {
  return {
    id: LEAD_ID,
    display_name: "ABC Diagnostics Mumbai",
    first_name: "ABC Diagnostics Mumbai",
    last_name: "",
    organization_name: "ABC Diagnostics",
    job_title: "",
    email: "lab@abc.example",
    phone: "9876543210",
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
    address_line_1: "Plot 4",
    address_line_2: "MIDC",
    city: "",
    state: "",
    postal_code: "",
    country: "",
    description: "",
    created_by: RAHUL,
    ...overrides,
  } as Lead;
}

const CARD = makeCard({ title: "ABC Diagnostics Mumbai — Adams 8380 V-lite", value: "850000.00", stage_id: STAGES.new.id, expected_close_date: "2026-12-15" });

function routes(workspace: string, overrides: Record<string, Parameters<typeof mockApi>[0][string]> = {}) {
  return mockApi({
    [`GET /api/v1/workspaces/${workspace}/leads/${LEAD_ID}`]: { status: 200, body: makeLead() },
    [`GET /api/v1/workspaces/${workspace}/opportunities`]: { status: 200, body: { results: [CARD], next: null, previous: null } },
    [`GET /api/v1/workspaces/${workspace}/pipelines`]: { status: 200, body: PIPELINES },
    ...overrides,
  });
}

beforeEach(() => {
  nav.pathname = `/leads/${LEAD_ID}`;
});

describe("a lead's page", () => {
  it("shows who the customer is, how to reach them and the opportunity they came with", async () => {
    const api = routes("me");
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { level: 1, name: "ABC Diagnostics Mumbai" })).toBeInTheDocument();
    expect(screen.getByText("Lead")).toBeInTheDocument();
    expect(screen.getByText("ABC Diagnostics")).toBeInTheDocument();
    const contact = screen.getByRole("region", { name: "Contact" });
    expect(within(contact).getByRole("link", { name: "9876543210" })).toHaveAttribute("href", "tel:9876543210");
    expect(within(contact).getByRole("link", { name: "lab@abc.example" })).toHaveAttribute("href", "mailto:lab@abc.example");
    expect(within(contact).getByText(/Plot 4/)).toHaveTextContent("Plot 4 MIDC");
    const deal = await screen.findByRole("link", { name: "ABC Diagnostics Mumbai — Adams 8380 V-lite" });
    expect(deal).toHaveAttribute("href", `/pipeline/${OPPORTUNITY_ID}`);
    const section = screen.getByRole("region", { name: "Opportunity" });
    await waitFor(() => expect(within(section).getByText("New")).toBeInTheDocument());
    expect(within(section).getByText("₹8,50,000")).toBeInTheDocument();
    // Read through this workspace only: the lead, and its opportunities by the lead's id.
    const [list] = api.callsTo("GET", "/api/v1/workspaces/me/opportunities");
    expect(list!.query.get("lead")).toBe(LEAD_ID);
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("href", "/dashboard");
    // Read-only but for a correction on the customer's request (correct-details.test.tsx):
    // nothing else on the page changes the lead.
    expect(screen.queryByRole("button", { name: /edit|archive|delete|convert/i })).not.toBeInTheDocument();
  });

  it("its opportunity opens from anywhere on the row, and Back to the Dashboard is 28 px to hit (WCAG 2.5.8)", async () => {
    routes("me");
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    const deal = await screen.findByRole("link", { name: "ABC Diagnostics Mumbai — Adams 8380 V-lite" });
    // The title alone was a 20 px line: its hit area (::after) now covers the row.
    expect(deal).toHaveClass("after:absolute", "after:inset-0");
    expect(deal.closest("li")).toHaveClass("relative");
    expect(within(deal.closest("li")!).getAllByRole("link")).toHaveLength(1);
    // 8 px of padding, given back as margin: taller to hit, nothing moves.
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveClass("py-1", "-mt-1", "mb-2");
  });

  it("in a user's workspace opened by an administrator, reads and links only inside it", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/leads/${LEAD_ID}`;
    const api = routes(RAHUL_ID);
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: adminViewer });
    const deal = await screen.findByRole("link", { name: "ABC Diagnostics Mumbai — Adams 8380 V-lite" });
    expect(deal).toHaveAttribute("href", `/admin/users/${RAHUL_ID}/pipeline/${OPPORTUNITY_ID}`);
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("href", `/admin/users/${RAHUL_ID}/dashboard`);
    expect(api.calls.every((call) => !call.path.startsWith("/api/v1/workspaces/") || call.path.startsWith(`/api/v1/workspaces/${RAHUL_ID}/`))).toBe(true);
  });

  it("is simply not found when the workspace may not see the lead (no hint it exists)", async () => {
    routes("me", { [`GET /api/v1/workspaces/me/leads/${LEAD_ID}`]: apiError(404, "not_found", "Not found.") });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.queryByText("ABC Diagnostics Mumbai")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Dashboard" })).toHaveAttribute("href", "/dashboard");
  });

  it("says when its opportunity is no longer in this workspace", async () => {
    routes("me", { "GET /api/v1/workspaces/me/opportunities": { status: 200, body: { results: [], next: null, previous: null } } });
    renderWithProviders(<LeadView leadId={LEAD_ID} />, { viewer: salesViewer });
    expect(await screen.findByText("No opportunity in this workspace.")).toBeInTheDocument();
  });
});
